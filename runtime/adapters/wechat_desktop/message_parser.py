from __future__ import annotations

from runtime.adapters.wechat_desktop.models import DesktopMessage
from runtime.adapters.wechat_desktop.ui_snapshot import ChatMessageSnapshot, ChatSessionSnapshot


INCOMING_VALUES = {"incoming", "received", "friend", "other", "left", "\u5bf9\u65b9"}
OUTGOING_VALUES = {"outgoing", "sent", "self", "me", "right", "\u672c\u8d26\u53f7", "\u672c\u4eba"}
TEXT_VALUES = {"text", "txt", "\u6587\u5b57", "\u6587\u672c", "1"}
VOICE_VALUES = {"voice", "audio", "\u8bed\u97f3"}
IMAGE_VALUES = {"image", "\u56fe\u7247"}
FILE_VALUES = {"file", "\u6587\u4ef6"}
SYSTEM_VALUES = {"system", "\u7cfb\u7edf"}


class MessageParser:
    def __init__(self, *, account_key: str) -> None:
        self.account_key = account_key

    def parse(self, session: ChatSessionSnapshot, snapshot: ChatMessageSnapshot) -> DesktopMessage:
        direction = normalize_direction(snapshot.direction)
        message_type = normalize_message_type(snapshot.message_type)
        contact_key = session.contact_key or session.display_name or session.title
        return DesktopMessage(
            account_key=self.account_key,
            contact_key=contact_key,
            contact_display_name=session.display_name or session.title or contact_key,
            chat_title=session.title or session.display_name or contact_key,
            direction=direction,
            message_type=message_type,
            text=snapshot.text,
            runtime_id=snapshot.runtime_id,
            control_path=snapshot.control_path,
            relative_order=snapshot.relative_order,
            time_label=snapshot.time_label,
            raw_fingerprint=str(snapshot.metadata.get("fingerprint", "")),
            metadata={
                **snapshot.metadata,
                "session_last_text": session.last_text,
            },
        )


def normalize_direction(value: str) -> str:
    lowered = str(value).strip().lower()
    if lowered in INCOMING_VALUES:
        return "incoming"
    if lowered in OUTGOING_VALUES:
        return "outgoing"
    return "unknown"


def normalize_message_type(value: str) -> str:
    lowered = str(value).strip().lower()
    if lowered in TEXT_VALUES:
        return "text"
    if lowered in VOICE_VALUES:
        return "voice"
    if lowered in IMAGE_VALUES:
        return "image"
    if lowered in FILE_VALUES:
        return "file"
    if lowered in SYSTEM_VALUES:
        return "system"
    return "unknown"
