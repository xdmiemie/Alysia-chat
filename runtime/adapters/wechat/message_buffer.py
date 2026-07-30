from __future__ import annotations

import time
from dataclasses import dataclass, field

from runtime.adapters.wechat.models import ILinkTextMessage


@dataclass
class BufferedMessageBatch:
    account_id: str
    from_user_id: str
    messages: list[ILinkTextMessage] = field(default_factory=list)
    updated_at: float = field(default_factory=time.monotonic)

    @property
    def key(self) -> tuple[str, str]:
        return (self.account_id, self.from_user_id)

    @property
    def latest(self) -> ILinkTextMessage:
        return self.messages[-1]

    @property
    def text(self) -> str:
        return "\n".join(message.text for message in self.messages if message.text.strip()).strip()


class MessageBuffer:
    def __init__(self, buffer_seconds: float) -> None:
        self.buffer_seconds = max(0.0, float(buffer_seconds))
        self._batches: dict[tuple[str, str], BufferedMessageBatch] = {}

    def add(self, message: ILinkTextMessage, now: float | None = None) -> list[BufferedMessageBatch]:
        current = time.monotonic() if now is None else now
        if self.buffer_seconds <= 0:
            return [
                BufferedMessageBatch(
                    account_id=message.account_id,
                    from_user_id=message.from_user_id,
                    messages=[message],
                    updated_at=current,
                )
            ]

        key = (message.account_id, message.from_user_id)
        batch = self._batches.get(key)
        if batch is None:
            batch = BufferedMessageBatch(
                account_id=message.account_id,
                from_user_id=message.from_user_id,
            )
            self._batches[key] = batch
        batch.messages.append(message)
        batch.updated_at = current
        return self.flush_expired(now=current)

    def flush_expired(self, now: float | None = None) -> list[BufferedMessageBatch]:
        current = time.monotonic() if now is None else now
        ready_keys = [
            key for key, batch in self._batches.items() if current - batch.updated_at >= self.buffer_seconds
        ]
        return [self._batches.pop(key) for key in ready_keys]

    def next_flush_seconds(self, now: float | None = None) -> float | None:
        if not self._batches:
            return None
        current = time.monotonic() if now is None else now
        remaining = [
            self.buffer_seconds - (current - batch.updated_at)
            for batch in self._batches.values()
        ]
        return max(0.0, min(remaining))

    def has_pending(self) -> bool:
        return bool(self._batches)

    def flush_all(self) -> list[BufferedMessageBatch]:
        batches = list(self._batches.values())
        self._batches.clear()
        return batches
