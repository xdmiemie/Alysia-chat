from __future__ import annotations

import json
from collections import deque
from pathlib import Path

from runtime.adapters.wechat_desktop.models import DesktopMessage


class MessageDeduplicator:
    def __init__(self, *, max_recent: int = 4096, state_path: str | Path | None = None) -> None:
        self.max_recent = max_recent
        self.state_path = Path(state_path) if state_path else None
        self._seen: set[str] = set()
        self._order: deque[str] = deque()
        self._load()

    def seen(self, message: DesktopMessage) -> bool:
        fingerprint = message.fingerprint
        if fingerprint in self._seen:
            return True
        self._seen.add(fingerprint)
        self._order.append(fingerprint)
        while len(self._order) > self.max_recent:
            old = self._order.popleft()
            self._seen.discard(old)
        self._append(fingerprint)
        return False

    def is_seen(self, message: DesktopMessage) -> bool:
        return message.fingerprint in self._seen

    def mark_processed(self, fingerprint: str) -> None:
        if fingerprint not in self._seen:
            self._seen.add(fingerprint)
            self._order.append(fingerprint)
            self._append(fingerprint)

    def clear(self) -> None:
        self._seen.clear()
        self._order.clear()
        if self.state_path and self.state_path.exists():
            self.state_path.unlink()

    def _load(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        for line in self.state_path.read_text(encoding="utf-8-sig").splitlines()[-self.max_recent :]:
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            fingerprint = str(data.get("fingerprint", ""))
            if fingerprint:
                self._seen.add(fingerprint)
                self._order.append(fingerprint)

    def _append(self, fingerprint: str) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self.state_path.open("a", encoding="utf-8") as state_file:
            state_file.write(json.dumps({"fingerprint": fingerprint}, ensure_ascii=False) + "\n")
