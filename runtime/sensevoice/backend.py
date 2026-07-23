from __future__ import annotations

import json
import contextlib
import sys
import time
from pathlib import Path
from typing import Protocol

from runtime.sensevoice.types import RawTranscription


class SenseVoiceBackend(Protocol):
    def transcribe(self, audio_path: str | Path) -> RawTranscription:
        ...


def resolve_model_path(manifest_path: str | Path, project_root: str | Path | None = None) -> Path:
    root = Path(project_root) if project_root is not None else Path.cwd()
    manifest = (root / manifest_path).resolve()
    if not manifest.exists():
        raise RuntimeError(f"SenseVoice model manifest not found: {manifest}")

    data = json.loads(manifest.read_text(encoding="utf-8"))
    required_files = data.get("required_files", [])
    if not required_files:
        raise RuntimeError(f"SenseVoice manifest has no required_files: {manifest}")

    model_file = (root / str(required_files[0])).resolve()
    if not model_file.exists():
        raise RuntimeError(
            "SenseVoice model file is missing: "
            f"{model_file}. Prepare the model under model_store/registry first."
        )
    return model_file


class FunASRSenseVoiceBackend:
    def __init__(self, model_file: str | Path, device: str = "cpu") -> None:
        try:
            from funasr import AutoModel
        except ImportError as exc:
            raise RuntimeError(
                "Missing SenseVoice dependency 'funasr'. Install the stage 2 model "
                "requirements before starting sensevoice-service. "
                f"Original import error: {exc}"
            ) from exc

        self.model_file = Path(model_file)
        self.device = device
        model_dir = self.model_file.parent
        with contextlib.redirect_stdout(sys.stderr):
            self._model = AutoModel(
                model=str(model_dir),
                trust_remote_code=True,
                device=device,
                disable_update=True,
            )

    def transcribe(self, audio_path: str | Path) -> RawTranscription:
        started = time.perf_counter()
        with contextlib.redirect_stdout(sys.stderr):
            output = self._model.generate(input=str(audio_path), language="auto", use_itn=True)
        inference_ms = int((time.perf_counter() - started) * 1000)
        return RawTranscription(text=_extract_text(output), inference_ms=inference_ms)


def _extract_text(output: object) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        value = output.get("text", "")
        return str(value)
    if isinstance(output, list) and output:
        return _extract_text(output[0])
    return ""
