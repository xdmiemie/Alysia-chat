from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class RawTranscription:
    text: str
    inference_ms: int


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    language: str
    emotion: str
    event: str
    duration_ms: int
    inference_ms: int
    skipped: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
