"""Isolated candidate/active voice preview on a temporary local API port."""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import wave
from dataclasses import replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.gpt_sovits import model_registry
from runtime.gpt_sovits.backend import GPTSoVITSHttpBackend
from runtime.gpt_sovits.config import GPTSoVITSConfig
from runtime.gpt_sovits.types import ReferenceAudio

FIXED_TEXTS = (
    "你好呀，今天过得怎么样？",
    "辛苦啦，先休息一下吧。",
    "真的？那也太让人开心了吧！",
    "别担心，我会陪着你的。",
)


def synthesize_fixed_set(model: model_registry.GPTSoVITSModelEntry, *, emotion: str,
                         output_dir: Path, texts: tuple[str, ...] = FIXED_TEXTS) -> list[dict[str, object]]:
    import yaml
    from scripts.gpt_sovits_models import _infer_config_yaml

    output_dir.mkdir(parents=True, exist_ok=True)
    raw = yaml.safe_load(_infer_config_yaml(model))
    for key, value in raw["custom"].items():
        if key.endswith("_path"):
            raw["custom"][key] = str((PROJECT_ROOT / value).resolve())
    config_path = output_dir / "preview_infer.yaml"
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    reference = _reference(model, emotion)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    upstream = PROJECT_ROOT / "third_party" / "GPT-SoVITS"
    compat = PROJECT_ROOT / "runtime" / "compat"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(compat), str(upstream), str(upstream / "tools"))),
           "PYTHONIOENCODING": "utf-8"}
    ffmpeg = PROJECT_ROOT / "tools" / "ffmpeg" / "bin"
    if ffmpeg.exists():
        env["PATH"] = str(ffmpeg) + os.pathsep + env.get("PATH", "")
    with (output_dir / "preview_server.log").open("wb") as server_log:
        process = subprocess.Popen([sys.executable, "-u", "api_v2.py", "-a", "127.0.0.1", "-p", str(port),
                                    "-c", str(config_path)], cwd=upstream, env=env,
                                   stdout=server_log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("试听服务启动失败；查看 preview_server.log")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                        break
                except OSError:
                    time.sleep(0.5)
            else:
                raise TimeoutError("试听服务启动超时")
            backend = GPTSoVITSHttpBackend(replace(GPTSoVITSConfig(),
                                                   upstream_url=f"http://127.0.0.1:{port}/tts"))
            results: list[dict[str, object]] = []
            for index, text in enumerate(texts, start=1):
                audio, elapsed_ms = backend.synthesize(text, reference, 1.0, "wav")
                target = output_dir / f"{index:02d}.wav"
                target.write_bytes(audio)
                with wave.open(str(target), "rb") as handle:
                    if handle.getnframes() <= 0:
                        raise RuntimeError(f"试听音频为空：{text}")
                result = {"text": text, "audio_path": str(target), "elapsed_ms": elapsed_ms,
                          "model_id": model.model_id, "emotion": reference.emotion}
                results.append(result)
                print("[voice_preview] " + json.dumps(result, ensure_ascii=False), flush=True)
            return results
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _reference(model: model_registry.GPTSoVITSModelEntry, emotion: str) -> ReferenceAudio:
    files = sorted(model.reference_files)
    chosen = next((path for path in files if path.stem.lower().startswith(emotion.lower())), None)
    chosen = chosen or next((path for path in files if path.stem.lower().startswith("neutral")), None)
    chosen = chosen or (files[0] if files else None)
    if chosen is None:
        raise RuntimeError("候选缺少参考音频")
    sidecar = chosen.with_suffix(".txt")
    if not sidecar.exists() or not sidecar.read_text(encoding="utf-8-sig").strip():
        raise RuntimeError(f"参考音频缺少文本：{chosen}")
    return ReferenceAudio(emotion=emotion, path=chosen,
                          prompt_text=sidecar.read_text(encoding="utf-8-sig").strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--character", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--emotion", default="neutral")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--text", default="")
    args = parser.parse_args(argv)
    model = model_registry.find_model(args.character, args.model)
    texts = (args.text,) if args.text else FIXED_TEXTS
    synthesize_fixed_set(model, emotion=args.emotion, output_dir=Path(args.output_dir), texts=texts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
