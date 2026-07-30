from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime.common.config import _parse_scalar


@dataclass(frozen=True)
class ILinkConfig:
    credentials_path: Path = Path("storage/secrets/ilink/account.json")
    cursor_path: Path = Path("storage/wechat/state/get_updates_buf.txt")
    bot_agent: str = "Alysia-chat/0.1.0"
    channel_version: str = "2.1.7"
    request_timeout_seconds: float = 15.0
    long_poll_extra_timeout_seconds: float = 10.0
    reconnect_seconds: float = 5.0
    max_reconnect_seconds: float = 60.0


@dataclass(frozen=True)
class ReplyConfig:
    private_chat_enabled: bool = True
    buffer_seconds: float = 1.5
    min_reply_interval_seconds: float = 3.0
    default_reply_mode: str = "text"
    fallback_to_text: bool = True
    max_text_length: int = 4096


@dataclass(frozen=True)
class ContactsConfig:
    default_policy: str = "deny"
    allow_user_ids: tuple[str, ...] = ()
    block_user_ids: tuple[str, ...] = ()

    def is_allowed(self, user_id: str) -> bool:
        if user_id in self.block_user_ids:
            return False
        if user_id in self.allow_user_ids:
            return True
        return self.default_policy == "allow"


@dataclass(frozen=True)
class WeChatConfig:
    enabled: bool = False
    provider: str = "ilink"
    debug_raw_events: bool = False
    ilink: ILinkConfig = ILinkConfig()
    reply: ReplyConfig = ReplyConfig()
    contacts: ContactsConfig = ContactsConfig()
    log_path: Path = Path("storage/logs/wechat/ilink.jsonl")


def load_wechat_config(path: str | Path = "configs/runtime/wechat.yaml") -> WeChatConfig:
    raw = load_nested_yaml(Path(path))
    ilink = raw.get("ilink", {})
    reply = raw.get("reply", {})
    contacts = raw.get("contacts", {})
    return WeChatConfig(
        enabled=bool(raw.get("enabled", WeChatConfig.enabled)),
        provider=str(raw.get("provider", WeChatConfig.provider)),
        debug_raw_events=bool(raw.get("debug_raw_events", WeChatConfig.debug_raw_events)),
        ilink=ILinkConfig(
            credentials_path=Path(str(ilink.get("credentials_path", ILinkConfig.credentials_path))),
            cursor_path=Path(str(ilink.get("cursor_path", ILinkConfig.cursor_path))),
            bot_agent=str(ilink.get("bot_agent", ILinkConfig.bot_agent)),
            channel_version=str(ilink.get("channel_version", ILinkConfig.channel_version)),
            request_timeout_seconds=float(
                ilink.get("request_timeout_seconds", ILinkConfig.request_timeout_seconds)
            ),
            long_poll_extra_timeout_seconds=float(
                ilink.get(
                    "long_poll_extra_timeout_seconds",
                    ILinkConfig.long_poll_extra_timeout_seconds,
                )
            ),
            reconnect_seconds=float(ilink.get("reconnect_seconds", ILinkConfig.reconnect_seconds)),
            max_reconnect_seconds=float(
                ilink.get("max_reconnect_seconds", ILinkConfig.max_reconnect_seconds)
            ),
        ),
        reply=ReplyConfig(
            private_chat_enabled=bool(reply.get("private_chat_enabled", ReplyConfig.private_chat_enabled)),
            buffer_seconds=float(reply.get("buffer_seconds", ReplyConfig.buffer_seconds)),
            min_reply_interval_seconds=float(
                reply.get("min_reply_interval_seconds", ReplyConfig.min_reply_interval_seconds)
            ),
            default_reply_mode=str(reply.get("default_reply_mode", ReplyConfig.default_reply_mode)),
            fallback_to_text=bool(reply.get("fallback_to_text", ReplyConfig.fallback_to_text)),
            max_text_length=int(reply.get("max_text_length", ReplyConfig.max_text_length)),
        ),
        contacts=ContactsConfig(
            default_policy=str(contacts.get("default_policy", ContactsConfig.default_policy)),
            allow_user_ids=_as_string_tuple(contacts.get("allow_user_ids", ())),
            block_user_ids=_as_string_tuple(contacts.get("block_user_ids", ())),
        ),
        log_path=Path(str(raw.get("log_path", WeChatConfig.log_path))),
    )


def load_nested_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]
    lines = path.read_text(encoding="utf-8-sig").splitlines()

    for index, raw_line in enumerate(lines):
        line_without_comment = raw_line.split("#", 1)[0].rstrip()
        if not line_without_comment.strip():
            continue
        indent = len(line_without_comment) - len(line_without_comment.lstrip(" "))
        stripped = line_without_comment.strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()

        parent = stack[-1][1]
        if stripped.startswith("- "):
            if not isinstance(parent, list):
                raise ValueError(f"YAML list item without list parent: {raw_line}")
            parent.append(_parse_scalar(stripped[2:]))
            continue

        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value:
            if not isinstance(parent, dict):
                raise ValueError(f"YAML mapping under non-mapping parent: {raw_line}")
            parent[key] = _parse_scalar(value)
            continue

        next_container: Any = {}
        if not isinstance(parent, dict):
            raise ValueError(f"YAML mapping under non-mapping parent: {raw_line}")
        parent[key] = next_container
        stack.append((indent, next_container))

        next_line = _peek_next_content(lines, index, indent)
        if next_line and next_line.strip().startswith("- "):
            parent[key] = []
            stack[-1] = (indent, parent[key])

    return root


def _peek_next_content(lines: list[str], index: int, current_indent: int) -> str:
    for next_line in lines[index + 1 :]:
        if not next_line.strip() or next_line.strip().startswith("#"):
            continue
        next_indent = len(next_line) - len(next_line.lstrip(" "))
        if next_indent > current_indent:
            return next_line
        return ""
    return ""


def _as_string_tuple(value: Any) -> tuple[str, ...]:
    if value in (None, "", "[]"):
        return ()
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return (str(value),)
