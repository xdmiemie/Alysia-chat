from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime.common.config import _parse_scalar


@dataclass(frozen=True)
class AccountConfig:
    account_key: str = "role_account_main"
    dedicated_account: bool = True
    expected_display_name: str = "set-after-validation"


@dataclass(frozen=True)
class ClientConfig:
    executable_names: tuple[str, ...] = ("Weixin.exe", "WeChat.exe", "WeChatAppEx.exe", "wechat_starter_v1.0.0.exe")
    launch_if_missing: bool = False
    require_logged_in: bool = True
    require_unlocked_desktop: bool = True
    verified_version: str = "set-after-validation"
    selector_profile: str = "set-after-calibration"
    pywechat_root: Path = Path("third_party/pywechat")


@dataclass(frozen=True)
class MonitorConfig:
    mode: str = "pinned_contacts"
    poll_interval_ms: int = 500
    buffer_seconds: float = 1.5
    max_visible_sessions: int = 20
    ignore_group_chats: bool = True
    ignore_system_messages: bool = True


@dataclass(frozen=True)
class ReplyConfig:
    default_mode: str = "text"
    min_reply_interval_seconds: float = 3.0
    fallback_to_text: bool = True
    max_text_chars: int = 1000


@dataclass(frozen=True)
class UIConfig:
    operation_timeout_seconds: float = 15.0
    verify_chat_title_before_send: bool = True
    verify_own_message_after_send: bool = True
    allow_coordinate_fallback: bool = False
    restore_window_after_operation: bool = True
    max_send_retries: int = 2


@dataclass(frozen=True)
class ContactsConfig:
    default_policy: str = "deny"
    allow_contact_keys: tuple[str, ...] = ()
    allow_display_names: tuple[str, ...] = ()
    block_contact_keys: tuple[str, ...] = ()

    def is_allowed(self, contact_key: str, display_name: str = "") -> bool:
        if contact_key in self.block_contact_keys:
            return False
        if contact_key in self.allow_contact_keys:
            return True
        if contact_key in self.allow_display_names:
            return True
        if display_name and display_name in self.allow_display_names:
            return True
        return self.default_policy == "allow"


@dataclass(frozen=True)
class WeChatDesktopConfig:
    enabled: bool = False
    provider: str = "pyweixin"
    log_path: Path = Path("storage/logs/wechat_desktop/text.jsonl")
    dedup_path: Path = Path("storage/wechat_desktop/state/dedup.jsonl")
    account: AccountConfig = AccountConfig()
    client: ClientConfig = ClientConfig()
    monitor: MonitorConfig = MonitorConfig()
    reply: ReplyConfig = ReplyConfig()
    ui: UIConfig = UIConfig()
    contacts: ContactsConfig = ContactsConfig()


def load_wechat_desktop_config(path: str | Path = "configs/runtime/wechat.yaml") -> WeChatDesktopConfig:
    raw = load_nested_yaml(Path(path))
    account = raw.get("account", {})
    client = raw.get("client", {})
    monitor = raw.get("monitor", {})
    reply = raw.get("reply", {})
    ui = raw.get("ui", {})
    contacts = raw.get("contacts", {})

    return WeChatDesktopConfig(
        enabled=bool(raw.get("enabled", WeChatDesktopConfig.enabled)),
        provider=str(raw.get("provider", WeChatDesktopConfig.provider)),
        log_path=Path(str(raw.get("log_path", WeChatDesktopConfig.log_path))),
        dedup_path=Path(str(raw.get("dedup_path", WeChatDesktopConfig.dedup_path))),
        account=AccountConfig(
            account_key=str(account.get("account_key", AccountConfig.account_key)),
            dedicated_account=bool(account.get("dedicated_account", AccountConfig.dedicated_account)),
            expected_display_name=str(account.get("expected_display_name", AccountConfig.expected_display_name)),
        ),
        client=ClientConfig(
            executable_names=_as_string_tuple(client.get("executable_names", ClientConfig.executable_names)),
            launch_if_missing=bool(client.get("launch_if_missing", ClientConfig.launch_if_missing)),
            require_logged_in=bool(client.get("require_logged_in", ClientConfig.require_logged_in)),
            require_unlocked_desktop=bool(
                client.get("require_unlocked_desktop", ClientConfig.require_unlocked_desktop)
            ),
            verified_version=str(client.get("verified_version", ClientConfig.verified_version)),
            selector_profile=str(client.get("selector_profile", ClientConfig.selector_profile)),
            pywechat_root=Path(str(client.get("pywechat_root", ClientConfig.pywechat_root))),
        ),
        monitor=MonitorConfig(
            mode=str(monitor.get("mode", MonitorConfig.mode)),
            poll_interval_ms=int(monitor.get("poll_interval_ms", MonitorConfig.poll_interval_ms)),
            buffer_seconds=float(monitor.get("buffer_seconds", MonitorConfig.buffer_seconds)),
            max_visible_sessions=int(monitor.get("max_visible_sessions", MonitorConfig.max_visible_sessions)),
            ignore_group_chats=bool(monitor.get("ignore_group_chats", MonitorConfig.ignore_group_chats)),
            ignore_system_messages=bool(monitor.get("ignore_system_messages", MonitorConfig.ignore_system_messages)),
        ),
        reply=ReplyConfig(
            default_mode=str(reply.get("default_mode", ReplyConfig.default_mode)),
            min_reply_interval_seconds=float(
                reply.get("min_reply_interval_seconds", ReplyConfig.min_reply_interval_seconds)
            ),
            fallback_to_text=bool(reply.get("fallback_to_text", ReplyConfig.fallback_to_text)),
            max_text_chars=int(reply.get("max_text_chars", ReplyConfig.max_text_chars)),
        ),
        ui=UIConfig(
            operation_timeout_seconds=float(
                ui.get("operation_timeout_seconds", UIConfig.operation_timeout_seconds)
            ),
            verify_chat_title_before_send=bool(
                ui.get("verify_chat_title_before_send", UIConfig.verify_chat_title_before_send)
            ),
            verify_own_message_after_send=bool(
                ui.get("verify_own_message_after_send", UIConfig.verify_own_message_after_send)
            ),
            allow_coordinate_fallback=bool(ui.get("allow_coordinate_fallback", UIConfig.allow_coordinate_fallback)),
            restore_window_after_operation=bool(
                ui.get("restore_window_after_operation", UIConfig.restore_window_after_operation)
            ),
            max_send_retries=int(ui.get("max_send_retries", UIConfig.max_send_retries)),
        ),
        contacts=ContactsConfig(
            default_policy=str(contacts.get("default_policy", ContactsConfig.default_policy)),
            allow_contact_keys=_as_string_tuple(contacts.get("allow_contact_keys", ())),
            allow_display_names=_as_string_tuple(contacts.get("allow_display_names", ())),
            block_contact_keys=_as_string_tuple(contacts.get("block_contact_keys", ())),
        ),
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
        container: Any = {}
        if not isinstance(parent, dict):
            raise ValueError(f"YAML mapping under non-mapping parent: {raw_line}")
        parent[key] = container
        stack.append((indent, container))
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
    if isinstance(value, tuple):
        return tuple(str(item) for item in value)
    return (str(value),)
