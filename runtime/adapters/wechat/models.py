from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


MessageKind = Literal["text", "voice", "unknown"]


@dataclass(frozen=True)
class ILinkCredentials:
    bot_token: str
    bot_user_id: str
    base_url: str
    account_id: str = "main"

    def validate(self) -> None:
        if not self.bot_token.strip():
            raise ValueError("ilink bot_token must not be empty")
        if not self.bot_user_id.strip():
            raise ValueError("ilink bot_user_id must not be empty")
        if not self.base_url.strip():
            raise ValueError("ilink base_url must not be empty")


@dataclass(frozen=True)
class ILinkTextMessage:
    message_id: str
    seq: str
    from_user_id: str
    session_id: str
    context_token: str
    text: str
    create_time_ms: int
    get_updates_buf: str = ""
    message_type: str = "USER"
    account_id: str = "main"
    raw_type: str = "TEXT"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def conversation_key(self) -> str:
        return f"wechat_ilink:{self.account_id}:{self.from_user_id}"

    @property
    def fingerprint(self) -> str:
        return "|".join(
            [
                self.message_id,
                str(self.seq),
                self.from_user_id,
                str(self.create_time_ms),
                self.message_type,
                self.get_updates_buf,
            ]
        )

    def to_log_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["context_token"] = _short_secret(self.context_token)
        data["from_user_id"] = _short_user_id(self.from_user_id)
        data["session_id"] = _short_user_id(self.session_id)
        return data


@dataclass(frozen=True)
class ParsedUpdateBatch:
    messages: tuple[ILinkTextMessage, ...]
    cursor: str
    raw_count: int


@dataclass(frozen=True)
class SendResult:
    ok: bool
    message_id: str = ""
    error: str = ""
    retryable: bool = False
    status_code: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HealthStatus:
    state: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state == "ok"


def _short_secret(value: str, keep: int = 6) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "***"
    return f"{value[:keep]}..."


def _short_user_id(value: str) -> str:
    if "@" not in value:
        return _short_secret(value, keep=4)
    prefix, suffix = value.split("@", 1)
    return f"{_short_secret(prefix, keep=4)}@{suffix}"
