"""Run one persisted, whole-song Feiyu job inside the supervisor's GPU lease."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from services.media_api.songs import SongStore
from services.media_api.song_audio import SongAudioConfig, SongAudioPipeline, inspect_audio
from services.media_api.song_source import NetEaseSource


async def run(store: SongStore, job_id: str, *, local_source: Path | None = None):
    job = store.get(job_id)
    directory = store.output(job_id).parent
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "source.audio"
    song = job["song"]
    if local_source is not None:
        # CLI-only self-authored acceptance fixture; never available to Agent/API.
        shutil.copyfile(local_source, source)
    else:
        await asyncio.to_thread(NetEaseSource.from_environment().download, song["song_id"], source)
    ffmpeg = str(ROOT / "tools/ffmpeg/bin/ffmpeg.exe")
    ffprobe = str(ROOT / "tools/ffmpeg/bin/ffprobe.exe")
    info = await asyncio.to_thread(inspect_audio, source, ffprobe)
    if abs(info["seconds"] - song["duration_seconds"]) > 2:
        raise ValueError("source_is_not_full_song")
    config = SongAudioConfig(ffmpeg=ffmpeg, ffprobe=ffprobe,
        separator_executable=str(ROOT / "envs/song/Scripts/audio-separator.exe"),
        separator_model_dir=str(ROOT / "model_store/cache/audio_separator"),
        rvc_python=str(ROOT / "envs/song/Scripts/python.exe"),
        rvc_bundle=str(ROOT / "model_store/registry/rvc/feiyu/v1"), max_duration_seconds=600)
    pipeline = SongAudioPipeline(config)
    if job["payload"]["mode"] == "cover":
        clip = directory / "whole.wav"
        await pipeline.clip(source, clip, 0, math.ceil(info["seconds"]))
        vocals, instrumental = await pipeline.separate(clip, directory / "stems")
        converted = await pipeline.convert(vocals, directory / "converted")
        result = await pipeline.mix(converted, instrumental, store.output(job_id))
        if abs(result["seconds"] - info["seconds"]) > 2:
            raise ValueError("cover_duration_mismatch")
        rvc = json.loads((directory / "converted/cover_result.json").read_text(encoding="utf-8"))
    else:
        shutil.copyfile(source, store.output(job_id))
        rvc = None
    # QQ transport was locally verified for WAV; whole-song files are 24 kHz mono.
    from services.media_api.song_audio import _run
    transport = directory / "transport.wav"
    await _run([ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(store.output(job_id)),
                "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(transport)], 60)
    transport.replace(store.output(job_id))
    result = inspect_audio(store.output(job_id), ffprobe)
    (directory / "receipt.json").write_text(json.dumps({"full_song": True, "input": info,
        "output": result, "rvc": rvc},
        ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    parser.add_argument("--state-dir", type=Path, default=ROOT / "storage/qq_companion")
    parser.add_argument("--local-source", type=Path)
    args = parser.parse_args()
    store = SongStore(args.state_dir)
    try:
        asyncio.run(run(store, args.job, local_source=args.local_source))
    except Exception as exc:
        # Detailed diagnostics stay in the local worker log, never in chat.
        print(f"Cover failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
