from __future__ import annotations

from collections import deque

from runtime.adapters.wechat.models import ILinkTextMessage


class MessageDeduplicator:
    def __init__(self, max_recent: int = 2048) -> None:
        self.max_recent = max_recent
        self._seen: set[str] = set()
        self._order: deque[str] = deque()

    def seen(self, message: ILinkTextMessage) -> bool:
        fingerprint = message.fingerprint
        if fingerprint in self._seen:
            return True
        self._seen.add(fingerprint)
        self._order.append(fingerprint)
        while len(self._order) > self.max_recent:
            old = self._order.popleft()
            self._seen.discard(old)
        return False

    def clear(self) -> None:
        self._seen.clear()
        self._order.clear()
