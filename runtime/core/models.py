from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4


def new_request_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"


@dataclass(frozen=True)
class UnifiedMessage:
    request_id: str
    channel: str
    session_id: str
    sender_id: str
    sender_name: str
    message_type: str
    text: str | None = None
    audio_path: str | None = None
    character_id: str = "elysia"
    metadata: dict[str, object] = field(default_factory=dict)

    @classmethod
    def local_audio(
        cls,
        audio_path: str | Path,
        *,
        session_id: str = "local:desktop:default",
        sender_id: str = "local-user",
        sender_name: str = "Local User",
        character_id: str = "elysia",
        request_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> "UnifiedMessage":
        return cls(
            request_id=request_id or new_request_id(),
            channel="local",
            session_id=session_id,
            sender_id=sender_id,
            sender_name=sender_name,
            message_type="voice",
            audio_path=str(audio_path),
            character_id=character_id,
            metadata=dict(metadata or {}),
        )

    @classmethod
    def local_text(
        cls,
        text: str,
        *,
        session_id: str = "local:desktop:default",
        sender_id: str = "local-user",
        sender_name: str = "Local User",
        character_id: str = "elysia",
        request_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> "UnifiedMessage":
        return cls(
            request_id=request_id or new_request_id(),
            channel="local",
            session_id=session_id,
            sender_id=sender_id,
            sender_name=sender_name,
            message_type="text",
            text=text,
            character_id=character_id,
            metadata=dict(metadata or {}),
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CoreTiming:
    input_audio_ms: int = 0
    asr_ms: int | None = None
    asr_inference_ms: int | None = None
    llm_first_token_ms: int | None = None
    llm_total_ms: int | None = None
    tts_ms: int | None = None
    tts_backend_ms: int | None = None
    total_ms: int | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class AlysiaReply:
    request_id: str
    session_id: str
    channel: str
    ok: bool
    text: str
    message_type: str
    audio_path: str
    state: str
    states: tuple[str, ...]
    error_code: str
    error: str
    transcription: dict[str, object] | None
    reply: dict[str, object] | None
    timings: CoreTiming
    metadata: dict[str, object] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["timings"] = self.timings.to_dict()
        return data
