from __future__ import annotations

import json
from pathlib import Path

from runtime.common.config import load_flat_yaml
from runtime.gpt_sovits.config import GPTSoVITSConfig
from runtime.gpt_sovits.types import ReferenceAudio


def resolve_reference_audio(config: GPTSoVITSConfig, emotion: str) -> ReferenceAudio:
    emotion_map = load_flat_yaml(config.emotion_map)
    prompt_texts = load_flat_yaml(config.reference_texts)
    requested = emotion.strip().lower() or "neutral"
    filename = emotion_map.get(requested) or emotion_map.get("neutral")
    if not filename:
        raise RuntimeError(f"No reference audio configured for emotion '{requested}'")

    path = (config.reference_dir / str(filename)).resolve()
    if not path.exists():
        raise RuntimeError(f"Reference audio missing for emotion '{requested}': {path}")
    return ReferenceAudio(
        emotion=requested if requested in emotion_map else "neutral",
        path=path,
        prompt_text=_load_prompt_text(path, requested, prompt_texts),
    )


def validate_model_manifest(manifest_path: str | Path, project_root: str | Path | None = None) -> list[Path]:
    root = Path(project_root) if project_root is not None else Path.cwd()
    manifest = (root / manifest_path).resolve()
    if not manifest.exists():
        raise RuntimeError(f"GPT-SoVITS model manifest not found: {manifest}")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    required_files = data.get("required_files", [])
    if not isinstance(required_files, list) or not required_files:
        raise RuntimeError(f"GPT-SoVITS manifest has no required_files: {manifest}")
    missing = [(root / str(path)).resolve() for path in required_files if not (root / str(path)).exists()]
    if missing:
        detail = "; ".join(str(path) for path in missing)
        raise RuntimeError(f"GPT-SoVITS required file(s) missing: {detail}")
    return [(root / str(path)).resolve() for path in required_files]


def _load_prompt_text(reference_path: Path, emotion: str, prompt_texts: dict[str, object]) -> str:
    sidecar = reference_path.with_suffix(".txt")
    if sidecar.exists():
        text = sidecar.read_text(encoding="utf-8").strip()
        if text:
            return text
    return str(prompt_texts.get(emotion) or prompt_texts.get("neutral") or "")
