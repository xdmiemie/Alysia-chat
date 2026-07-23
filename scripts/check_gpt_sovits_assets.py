from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.gpt_sovits.config import load_gpt_sovits_config
from runtime.gpt_sovits.references import resolve_reference_audio, validate_model_manifest
from runtime.audio.audio_format import inspect_wav


def main() -> int:
    config = load_gpt_sovits_config()
    ok = True
    try:
        files = validate_model_manifest(config.model_manifest, project_root=PROJECT_ROOT)
        for path in files:
            print(f"[OK] {path}")
    except RuntimeError as exc:
        ok = False
        print(f"[MISSING] {exc}")

    for emotion in ["neutral", "happy", "gentle", "sad", "surprised", "serious"]:
        try:
            reference = resolve_reference_audio(config, emotion)
            metrics = inspect_wav(reference.path)
            prompt_status = "prompt OK" if reference.prompt_text else "prompt missing"
            print(
                f"[OK] {emotion} reference: {reference.path} "
                f"({metrics.duration_seconds:.2f}s, {metrics.sample_rate} Hz, {prompt_status})"
            )
            if not reference.prompt_text:
                ok = False
        except RuntimeError as exc:
            ok = False
            print(f"[MISSING] {emotion} reference: {exc}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
