from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.adapters.wechat_desktop.models import BufferedDesktopBatch, DesktopMessage


@dataclass
class _PendingBatch:
    account_key: str
    contact_key: str
    contact_display_name: str
    chat_title: str
    messages: list[DesktopMessage] = field(default_factory=list)
    updated_at: float = field(default_factory=time.monotonic)

    def to_batch(self) -> BufferedDesktopBatch:
        return BufferedDesktopBatch(
            account_key=self.account_key,
            contact_key=self.contact_key,
            contact_display_name=self.contact_display_name,
            chat_title=self.chat_title,
            messages=tuple(self.messages),
        )


class MessageBuffer:
    def __init__(self, *, buffer_seconds: float, max_text_chars: int) -> None:
        self.buffer_seconds = max(0.0, float(buffer_seconds))
        self.max_text_chars = max_text_chars
        self._batches: dict[tuple[str, str, str], _PendingBatch] = {}

    def add(self, message: DesktopMessage, now: float | None = None) -> list[BufferedDesktopBatch]:
        current = time.monotonic() if now is None else now
        if self.buffer_seconds <= 0 or message.message_type == "voice":
            return [
                _PendingBatch(
                    account_key=message.account_key,
                    contact_key=message.contact_key,
                    contact_display_name=message.contact_display_name,
                    chat_title=message.chat_title,
                    messages=[_truncate_message(message, self.max_text_chars)],
                    updated_at=current,
                ).to_batch()
            ]
        key = (message.account_key, message.contact_key, message.message_type)
        batch = self._batches.get(key)
        if batch is None:
            batch = _PendingBatch(
                account_key=message.account_key,
                contact_key=message.contact_key,
                contact_display_name=message.contact_display_name,
                chat_title=message.chat_title,
            )
            self._batches[key] = batch
        batch.messages.append(_truncate_message(message, self.max_text_chars))
        batch.updated_at = current
        return self.flush_expired(now=current)

    def flush_expired(self, now: float | None = None) -> list[BufferedDesktopBatch]:
        current = time.monotonic() if now is None else now
        ready_keys = [
            key for key, batch in self._batches.items() if current - batch.updated_at >= self.buffer_seconds
        ]
        return [self._batches.pop(key).to_batch() for key in ready_keys]

    def flush_all(self) -> list[BufferedDesktopBatch]:
        batches = [batch.to_batch() for batch in self._batches.values()]
        self._batches.clear()
        return batches


def _truncate_message(message: DesktopMessage, max_chars: int) -> DesktopMessage:
    if max_chars <= 0 or len(message.text) <= max_chars:
        return message
    return DesktopMessage(
        account_key=message.account_key,
        contact_key=message.contact_key,
        contact_display_name=message.contact_display_name,
        chat_title=message.chat_title,
        direction=message.direction,
        message_type=message.message_type,
        text=message.text[:max_chars],
        runtime_id=message.runtime_id,
        control_path=message.control_path,
        relative_order=message.relative_order,
        time_label=message.time_label,
        raw_fingerprint=message.fingerprint,
        metadata={**message.metadata, "truncated": True},
    )
