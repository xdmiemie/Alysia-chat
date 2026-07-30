from __future__ import annotations

from typing import Any

from runtime.adapters.wechat.models import ILinkTextMessage, ParsedUpdateBatch


TEXT_TYPES = {"1", "TEXT", "text", "Text"}
USER_MESSAGE_TYPES = {"1", "USER", "user", "User"}


class ILinkEventParser:
    def __init__(self, *, account_id: str = "main", bot_user_id: str = "") -> None:
        self.account_id = account_id
        self.bot_user_id = bot_user_id

    def parse_update_batch(self, payload: dict[str, Any]) -> ParsedUpdateBatch:
        data = payload.get("data", {})
        if not isinstance(data, dict):
            data = {}
        cursor = str(
            payload.get(
                "get_updates_buf",
                payload.get("next_get_updates_buf", payload.get("cursor", "")),
            )
            or ""
        )
        updates_value = payload.get(
            "msgs",
            payload.get(
                "update_list",
                payload.get("updates", data.get("msgs", data.get("update_list", []))),
            ),
        )
        updates = _as_list(updates_value)
        messages = [message for update in updates for message in self.parse_update(update, cursor)]
        return ParsedUpdateBatch(messages=tuple(messages), cursor=cursor, raw_count=len(updates))

    def parse_update(self, update: dict[str, Any], cursor: str = "") -> list[ILinkTextMessage]:
        message_type = str(update.get("message_type", update.get("type", "")) or "")
        if message_type and message_type not in USER_MESSAGE_TYPES:
            return []

        from_user_id = str(update.get("from_user_id", update.get("sender_id", "")) or "")
        if self.bot_user_id and from_user_id == self.bot_user_id:
            return []

        message_id = str(update.get("message_id", update.get("id", "")) or "")
        seq = str(update.get("seq", update.get("sequence", "")) or "")
        session_id = str(update.get("session_id", update.get("conversation_id", from_user_id)) or from_user_id)
        context_token = str(update.get("context_token", update.get("contextToken", "")) or "")
        create_time_ms = _int_or_zero(update.get("create_time_ms", update.get("create_time", 0)))
        item_list = _as_list(update.get("item_list", update.get("items", [])))

        messages: list[ILinkTextMessage] = []
        for item in item_list:
            item_type = str(item.get("type", item.get("item_type", "")) or "")
            if item_type not in TEXT_TYPES:
                continue
            text = _extract_text(item)
            if not text.strip():
                continue
            messages.append(
                ILinkTextMessage(
                    message_id=message_id,
                    seq=seq,
                    from_user_id=from_user_id,
                    session_id=session_id,
                    context_token=context_token,
                    text=text,
                    create_time_ms=create_time_ms,
                    get_updates_buf=cursor,
                    message_type="USER",
                    account_id=self.account_id,
                    raw_type=item_type,
                    metadata={
                        "ilink_message_id": message_id,
                        "ilink_seq": seq,
                    },
                )
            )
        return messages


def _extract_text(item: dict[str, Any]) -> str:
    text_item = item.get("text_item", item.get("text", item))
    if isinstance(text_item, dict):
        return str(text_item.get("text", text_item.get("content", "")) or "")
    return str(text_item or "")


def _as_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
