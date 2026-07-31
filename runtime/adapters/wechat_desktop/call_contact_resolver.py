from __future__ import annotations

import re
from typing import Protocol

from runtime.adapters.wechat_desktop.call_models import CallContact, CallWindowSnapshot


class CallContactResolverClient(Protocol):
    def resolve_call_contact(self, call: CallWindowSnapshot) -> CallContact | str | dict[str, object] | None:
        ...


class CallContactResolver:
    def __init__(self, *, account_key: str, client: object | None = None) -> None:
        self.account_key = account_key
        self.client = client

    def resolve(self, call: CallWindowSnapshot) -> CallContact | None:
        custom = self._resolve_with_client(call)
        if custom is not None:
            return custom
        display = _clean_contact_text(call.contact_display_text or call.title)
        if not display:
            return None
        return CallContact(
            account_key=self.account_key,
            contact_key=display,
            display_name=display,
            source="call_window_text",
        )

    def _resolve_with_client(self, call: CallWindowSnapshot) -> CallContact | None:
        resolve = getattr(self.client, "resolve_call_contact", None)
        if not callable(resolve):
            return None
        value = resolve(call)
        if isinstance(value, CallContact):
            return value
        if isinstance(value, str) and value.strip():
            cleaned = value.strip()
            return CallContact(self.account_key, cleaned, cleaned, source="client")
        if isinstance(value, dict):
            contact_key = str(value.get("contact_key") or value.get("key") or "").strip()
            display_name = str(value.get("display_name") or value.get("name") or contact_key).strip()
            if contact_key:
                return CallContact(
                    account_key=str(value.get("account_key") or self.account_key),
                    contact_key=contact_key,
                    display_name=display_name,
                    source=str(value.get("source") or "client"),
                )
        return None


def _clean_contact_text(value: str) -> str:
    cleaned = " ".join(value.replace("\r", "\n").split())
    if not cleaned:
        return ""
    noise = (
        "邀请你进行语音通话",
        "邀请你进行视频通话",
        "语音通话",
        "视频通话",
        "微信",
        "WeChat",
        "Weixin",
        "Incoming voice call",
        "Incoming video call",
        "Voice call",
        "Video call",
        "Answer",
        "Decline",
        "Reject",
    )
    for marker in noise:
        cleaned = cleaned.replace(marker, " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:：")
    return cleaned
