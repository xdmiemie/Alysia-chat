from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Literal


Direction = Literal["incoming", "outgoing", "unknown"]
MessageType = Literal["text", "voice", "image", "file", "system", "unknown"]


@dataclass(frozen=True)
class DesktopMessage:
    account_key: str
    contact_key: str
    contact_display_name: str
    chat_title: str
    direction: Direction
    message_type: MessageType
    text: str
    runtime_id: str = ""
    control_path: str = ""
    relative_order: int = 0
    time_label: str = ""
    raw_fingerprint: str = ""
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def session_id(self) -> str:
        return f"wechat_desktop:{self.account_key}:{self.contact_key}"

    @property
    def fingerprint(self) -> str:
        if self.raw_fingerprint:
            return self.raw_fingerprint
        return stable_message_fingerprint(
            account_key=self.account_key,
            contact_key=self.contact_key,
            direction=self.direction,
            message_type=self.message_type,
            text=self.text,
            time_label=self.time_label,
            runtime_id=self.runtime_id,
            control_path=self.control_path,
            relative_order=self.relative_order,
        )

    @property
    def voice_duration_ms(self) -> int:
        for key in ("voice_duration_ms", "duration_ms", "audio_duration_ms"):
            value = self.metadata.get(key)
            try:
                duration_ms = int(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if duration_ms > 0:
                return duration_ms
        return _parse_duration_ms(self.text)

    @property
    def is_bound_to_ui(self) -> bool:
        return bool(self.raw_fingerprint or self.runtime_id or self.control_path)

    def to_log_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["contact_key"] = redact_contact(self.contact_key)
        data["contact_display_name"] = redact_contact(self.contact_display_name)
        data["chat_title"] = redact_contact(self.chat_title)
        data["text"] = redact_text(self.text)
        metadata = dict(data.get("metadata") or {})
        for key in ("raw_text", "screenshot", "image", "ocr_text"):
            metadata.pop(key, None)
        data["metadata"] = metadata
        return data


@dataclass(frozen=True)
class BufferedDesktopBatch:
    account_key: str
    contact_key: str
    contact_display_name: str
    chat_title: str
    messages: tuple[DesktopMessage, ...]

    @property
    def session_id(self) -> str:
        return f"wechat_desktop:{self.account_key}:{self.contact_key}"

    @property
    def text(self) -> str:
        return "\n".join(message.text for message in self.messages if message.text.strip()).strip()

    @property
    def fingerprints(self) -> tuple[str, ...]:
        return tuple(message.fingerprint for message in self.messages)


@dataclass(frozen=True)
class SendResult:
    ok: bool
    request_id: str
    contact_key: str
    error: str = ""
    retryable: bool = False
    attempts: int = 1
    verified: bool = False
    metadata: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["contact_key"] = redact_contact(self.contact_key)
        return data


@dataclass(frozen=True)
class HealthStatus:
    state: str
    details: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.state == "ok"


def stable_message_fingerprint(
    *,
    account_key: str,
    contact_key: str,
    direction: str,
    message_type: str,
    text: str,
    time_label: str,
    runtime_id: str,
    control_path: str,
    relative_order: int,
) -> str:
    normalized = " ".join(text.split())
    parts = [
        account_key,
        contact_key,
        direction,
        message_type,
        normalized,
        time_label,
        runtime_id,
        control_path,
        str(relative_order),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def redact_contact(value: str) -> str:
    if not value:
        return ""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{value[:2]}...#{digest}"


def redact_text(value: str, max_chars: int = 24) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) <= max_chars:
        return cleaned
    return f"{cleaned[:max_chars]}..."


def _parse_duration_ms(value: str) -> int:
    cleaned = value.strip().lower().replace(" ", "")
    if not cleaned:
        return 0
    digits = ""
    for char in cleaned:
        if char.isdigit() or char == ".":
            digits += char
        elif digits:
            break
    if not digits:
        return 0
    try:
        number = float(digits)
    except ValueError:
        return 0
    if "ms" in cleaned or "毫秒" in cleaned:
        return int(number)
    return int(number * 1000)
