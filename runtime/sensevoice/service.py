from __future__ import annotations

from pathlib import Path

from runtime.audio.audio_format import inspect_wav
from runtime.sensevoice.backend import FunASRSenseVoiceBackend, SenseVoiceBackend, resolve_model_path
from runtime.sensevoice.config import SenseVoiceConfig, load_sensevoice_config
from runtime.sensevoice.text import parse_sensevoice_text
from runtime.sensevoice.types import TranscriptionResult


class SenseVoiceRecognizer:
    def __init__(self, config: SenseVoiceConfig, backend: SenseVoiceBackend) -> None:
        self.config = config
        self.backend = backend

    @classmethod
    def from_config(cls, config_path: str | Path = "configs/runtime/sensevoice.yaml") -> "SenseVoiceRecognizer":
        config = load_sensevoice_config(config_path)
        model_file = resolve_model_path(config.model_manifest)
        backend = FunASRSenseVoiceBackend(model_file, device=config.device)
        return cls(config=config, backend=backend)

    def transcribe(self, audio_path: str | Path) -> TranscriptionResult:
        metrics = inspect_wav(audio_path)
        duration_ms = int(metrics.duration_seconds * 1000)
        if duration_ms < self.config.min_duration_ms:
            return TranscriptionResult(
                text="",
                language="unknown",
                emotion="unknown",
                event="too_short",
                duration_ms=duration_ms,
                inference_ms=0,
                skipped=True,
                reason=f"audio shorter than {self.config.min_duration_ms} ms",
            )

        if metrics.peak_normalized <= self.config.silence_peak_threshold:
            return TranscriptionResult(
                text="",
                language="unknown",
                emotion="unknown",
                event="nospeech",
                duration_ms=duration_ms,
                inference_ms=0,
                skipped=True,
                reason="audio peak below silence threshold",
            )

        raw = self.backend.transcribe(audio_path)
        parsed = parse_sensevoice_text(raw.text)
        skipped = parsed.text == ""
        return TranscriptionResult(
            text=parsed.text,
            language=parsed.language,
            emotion=parsed.emotion,
            event=parsed.event,
            duration_ms=duration_ms,
            inference_ms=raw.inference_ms,
            skipped=skipped,
            reason="empty transcription" if skipped else "",
        )
