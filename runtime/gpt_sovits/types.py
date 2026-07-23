from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SpeechRequest:
    text: str
    character_id: str = "elysia"
    emotion: str = "neutral"
    speed: float = 1.0
    format: str = "wav"


@dataclass(frozen=True)
class SpeechResult:
    audio_bytes: bytes
    format: str
    sample_rate: int | None
    reference_audio: Path
    normalized_text: str
    backend_ms: int


@dataclass(frozen=True)
class ReferenceAudio:
    emotion: str
    path: Path
    prompt_text: str = ""
