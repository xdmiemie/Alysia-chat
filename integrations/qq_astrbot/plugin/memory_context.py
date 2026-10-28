"""Bounded, ephemeral QQ context. AstrBot still owns durable chat history."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict, dataclass
import json
import re
import time


def session_key(bot_uin: str, sender_uin: str, group_id: str = "") -> str:
    values = (bot_uin, sender_uin, group_id or sender_uin)
    if not all(re.fullmatch(r"[0-9]{1,20}", value) for value in values):
        raise ValueError("QQ identities must be numeric")
    return f"qq:{bot_uin}:group:{group_id}" if group_id else f"qq:{bot_uin}:dm:{sender_uin}"


@dataclass
class ContextMessage:
    sender_uin: str
    display_name: str
    text: str
    message_ids: list[str]
    at_bot: bool
    reply_to: str | None
    received_at: float
    role: str = "user"


class MemoryContext:
    def __init__(self, *, max_messages: int = 20, ttl: float = 300,
                 token_budget: int = 2048, merge_seconds: float = 1.5,
                 max_sessions: int = 512) -> None:
        if not (1 <= max_messages <= 100 and 1 <= ttl <= 3600 and
                512 <= token_budget <= 16384 and 0 <= merge_seconds <= 5 and max_sessions > 0):
            raise ValueError("Invalid QQ context bounds")
        self.max_messages, self.ttl = max_messages, ttl
        self.token_budget, self.merge_seconds = token_budget, merge_seconds
        self.max_sessions = max_sessions
        self._windows: OrderedDict[str, list[ContextMessage]] = OrderedDict()

    def _prune(self, now: float) -> None:
        for scope in list(self._windows):
            self._windows[scope] = [m for m in self._windows[scope] if now - m.received_at < self.ttl]
            if not self._windows[scope]:
                del self._windows[scope]

    def add(self, scope: str, message: ContextMessage) -> ContextMessage:
        self._prune(message.received_at)
        window = self._windows.setdefault(scope, [])
        self._windows.move_to_end(scope)
        message.text = message.text[:self.token_budget]
        message.display_name = message.display_name[:64]
        # Only adjacent fragments of the same sender with the same reply target merge.
        if (window and message.role == "user" and window[-1].role == "user" and
                window[-1].sender_uin == message.sender_uin and not message.at_bot and
                not window[-1].at_bot and window[-1].reply_to == message.reply_to and
                0 <= message.received_at - window[-1].received_at < self.merge_seconds):
            last = window[-1]
            last.text = (last.text + "\n" + message.text)[-self.token_budget:]
            last.message_ids = (last.message_ids + message.message_ids)[-20:]
            last.received_at = message.received_at
        else:
            window.append(message)
        del window[:-self.max_messages]
        while len(self._windows) > self.max_sessions:
            self._windows.popitem(last=False)
        return window[-1]

    def render(self, scope: str, *, now: float | None = None) -> str:
        self._prune(time.time() if now is None else now)
        rows = [asdict(m) for m in self._windows.get(scope, [])]
        # A UTF-8 byte is a conservative upper bound on tokenizer consumption.
        # Include JSON field/escaping overhead in the limit, never split JSON.
        encode = lambda: json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        while rows and len(encode().encode("utf-8")) > self.token_budget:
            if len(rows) > 1:
                rows.pop(0)
            elif len(rows[0]["text"]) > 16:
                rows[0]["text"] = rows[0]["text"][len(rows[0]["text"]) // 4:]
            else:
                rows.pop()
        return encode()

    def text_for(self, scope: str, message_id: str, fallback: str) -> str:
        for message in reversed(self._windows.get(scope, [])):
            if message_id in message.message_ids:
                return message.text
        return fallback

    @staticmethod
    def filter_memories(scope: str, items: list[dict]) -> list[dict]:
        """Q5 retrieval boundary: reject unscoped and foreign memories first."""
        return [item for item in items if item.get("owner_key") == scope and not item.get("deleted_at")]
