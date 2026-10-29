"""Local whole-song normalization, stem separation, Feiyu RVC, and mix stages."""

from __future__ import annotations

import asyncio
import json
import math
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from scripts.qq_companion.install_feiyu_rvc import INDEX_SHA, MODEL_SHA, sha256


@dataclass(frozen=True)
class SongAudioConfig:
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    separator_executable: str = "audio-separator"
    separator_model: str = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
    separator_model_dir: str = "model_store/cache/audio_separator"
    separator_sha256: str = "5b84f37e8d444c8cb30c79d77f613a41c05868ff9c9ac6c7049c00aefae115aa"
    rvc_python: str = sys.executable
    rvc_bundle: str = "model_store/registry/rvc/feiyu/v1"
    max_duration_seconds: int = 600


def inspect_audio(path: Path, ffprobe: str, *, max_seconds: float = 600,
                  minimum_seconds: float = 0.5) -> dict:
    if not path.is_file() or path.stat().st_size < 1024:
        raise ValueError("audio file is missing or empty")
    result = subprocess.run([ffprobe, "-v", "error", "-show_entries",
                             "stream=codec_type,sample_rate,channels:format=duration",
                             "-of", "json", str(path)], capture_output=True, text=True,
                            timeout=20, check=True)
    data = json.loads(result.stdout)
    seconds = float(data["format"]["duration"])
    streams = [item for item in data.get("streams", []) if item.get("codec_type") == "audio"]
    if not streams or not math.isfinite(seconds) or not minimum_seconds <= seconds <= max_seconds:
        raise ValueError("invalid audio duration or stream")
    return {"seconds": seconds, "sample_rate": int(streams[0]["sample_rate"]),
            "channels": int(streams[0]["channels"]), "bytes": path.stat().st_size,
            "sha256": sha256(path)}


async def _run(argv: list[str], timeout: float, *, log_path: Path | None = None) -> None:
    log_file = log_path.open("wb") if log_path else None
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=log_file or asyncio.subprocess.PIPE,
            stderr=subprocess.STDOUT if log_file else asyncio.subprocess.PIPE,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
        try:
            if log_file:
                await asyncio.wait_for(process.wait(), timeout)
                stderr = b""
            else:
                _, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            if os.name == "nt":
                try:
                    await asyncio.to_thread(subprocess.run,
                                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                            capture_output=True, timeout=10, check=False)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if process.returncode is None:
                process.kill()
            await process.communicate()
            raise
    finally:
        if log_file:
            log_file.close()
    if process.returncode:
        details = (log_path.read_bytes()[-1200:] if log_path else stderr[-1200:]).decode(errors="replace")
        raise RuntimeError(f"audio process exited {process.returncode}: {details[-300:]}")


class SongAudioPipeline:
    def __init__(self, config: SongAudioConfig) -> None:
        self.config = config

    async def clip(self, source: Path, target: Path, start: int, duration: int) -> dict:
        info = await asyncio.to_thread(inspect_audio, source, self.config.ffprobe)
        if not 0 <= start < info["seconds"] or not 5 <= duration <= self.config.max_duration_seconds:
            raise ValueError("invalid clip range")
        if start + duration > info["seconds"] + 1:
            raise ValueError("clip exceeds song length")
        await _run([self.config.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-ss", str(start), "-i", str(source), "-t", str(duration), "-vn",
                    "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(target)], 90)
        return await asyncio.to_thread(inspect_audio, target, self.config.ffprobe,
                                       max_seconds=duration + 2)

    async def separate(self, clip: Path, directory: Path) -> tuple[Path, Path]:
        model = Path(self.config.separator_model_dir) / self.config.separator_model
        if not model.is_file() or sha256(model) != self.config.separator_sha256:
            raise ValueError("separator model checksum mismatch or missing")
        directory.mkdir(parents=True, exist_ok=True)
        await _run([self.config.separator_executable, str(clip),
                    "--model_filename", self.config.separator_model,
                    "--model_file_dir", self.config.separator_model_dir,
                    "--output_dir", str(directory), "--output_format", "WAV"], 900)
        outputs = list(directory.glob("*.wav"))
        vocals = next((p for p in outputs if "vocal" in p.stem.lower() and
                       "instrumental" not in p.stem.lower()), None)
        instrumental = next((p for p in outputs if "instrumental" in p.stem.lower() or
                             "no vocals" in p.stem.lower()), None)
        if vocals is None or instrumental is None or vocals == instrumental:
            raise ValueError("separator did not produce both stems")
        a, b = await asyncio.gather(
            asyncio.to_thread(inspect_audio, vocals, self.config.ffprobe,
                              max_seconds=self.config.max_duration_seconds + 2),
            asyncio.to_thread(inspect_audio, instrumental, self.config.ffprobe,
                              max_seconds=self.config.max_duration_seconds + 2))
        if abs(a["seconds"] - b["seconds"]) > 0.5:
            raise ValueError("separated stems are misaligned")
        return vocals, instrumental

    async def convert(self, vocals: Path, output_dir: Path) -> Path:
        bundle = Path(self.config.rvc_bundle).resolve(strict=True)
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        if (manifest.get("id") != "FeiyuVoice-RVC-v1" or
                sha256(bundle / "FeiyuVoice-RVC-v1.pth") != MODEL_SHA or
                sha256(bundle / "FeiyuVoice-RVC-v1.index") != INDEX_SHA):
            raise ValueError("FeiyuVoice RVC model integrity failure")
        if not (bundle / "runtime.local.json").is_file():
            raise RuntimeError("FeiyuVoice Applio runtime is not configured")
        worker = Path(__file__).resolve().parents[2] / "scripts/qq_companion/feiyu_rvc_worker.py"
        await _run([self.config.rvc_python, "-B", str(worker),
                    "--runner", str(bundle / "run_rvc.py"),
                    "--input", str(vocals.resolve(strict=True)),
                    "--output-dir", str(output_dir.resolve())],
                   max(180, self.config.max_duration_seconds * 4),
                   log_path=output_dir.with_suffix(".log"))
        result = output_dir / "rvc_native.wav"
        await asyncio.to_thread(inspect_audio, result, self.config.ffprobe,
                                max_seconds=self.config.max_duration_seconds + 2)
        receipt = json.loads((output_dir / "cover_result.json").read_text(encoding="utf-8"))
        if (receipt.get("status") != "success" or receipt.get("model_sha256") != MODEL_SHA or
                receipt.get("index_sha256") != INDEX_SHA):
            raise ValueError("RVC receipt did not verify selected model")
        return result

    async def mix(self, vocals: Path, instrumental: Path, target: Path) -> dict:
        await _run([self.config.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-i", str(vocals), "-i", str(instrumental), "-filter_complex",
                    "[0:a]volume=1.0[v];[1:a]volume=0.8[i];[v][i]amix=inputs=2:duration=shortest:normalize=0,alimiter=limit=0.95:level=false[out]",
                    "-map", "[out]", "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le",
                    str(target)], 90)
        return await asyncio.to_thread(inspect_audio, target, self.config.ffprobe,
                                       max_seconds=self.config.max_duration_seconds + 2)

