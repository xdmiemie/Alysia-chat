from __future__ import annotations

from pathlib import Path

from runtime.gpt_sovits.backend import GPTSoVITSHttpBackend, SpeechBackend
from runtime.gpt_sovits.config import GPTSoVITSConfig, load_gpt_sovits_config
from runtime.gpt_sovits.references import resolve_reference_audio, validate_model_manifest
from runtime.gpt_sovits.text import split_tts_sentences
from runtime.gpt_sovits.types import SpeechRequest, SpeechResult


class GPTSoVITSSynthesizer:
    def __init__(self, config: GPTSoVITSConfig, backend: SpeechBackend) -> None:
        self.config = config
        self.backend = backend

    @classmethod
    def from_config(
        cls,
        config_path: str | Path | GPTSoVITSConfig = "configs/runtime/gpt_sovits.yaml",
    ) -> "GPTSoVITSSynthesizer":
        config = config_path if isinstance(config_path, GPTSoVITSConfig) else load_gpt_sovits_config(config_path)
        validate_model_manifest(config.model_manifest)
        return cls(config=config, backend=GPTSoVITSHttpBackend(config))

    def synthesize(self, request: SpeechRequest) -> SpeechResult:
        if request.character_id != self.config.character_id:
            raise ValueError(f"Unsupported character_id: {request.character_id}")
        if request.format.lower() != "wav":
            raise ValueError("Stage 4 currently supports only wav output")

        sentences = split_tts_sentences(request.text, max_chars=self.config.max_sentence_chars)
        if not sentences:
            raise ValueError("text produced no speakable sentences")

        normalized_text = "\n".join(sentences)
        reference = resolve_reference_audio(self.config, request.emotion)
        speed = max(0.75, min(1.25, float(request.speed)))
        audio, backend_ms = self.backend.synthesize(
            text=normalized_text,
            reference=reference,
            speed=speed,
            audio_format=request.format.lower(),
        )
        return SpeechResult(
            audio_bytes=audio,
            format=request.format.lower(),
            sample_rate=None,
            reference_audio=reference.path,
            normalized_text=normalized_text,
            backend_ms=backend_ms,
        )
