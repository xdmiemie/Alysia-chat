from __future__ import annotations

from typing import Callable, Protocol

from runtime.qwen3.types import CharacterReply


class ChatSession(Protocol):
    def chat(self, user_text: str) -> CharacterReply:
        ...


class ChatSessionManager:
    def __init__(self, factory: Callable[[str], ChatSession]) -> None:
        self.factory = factory
        self._sessions: dict[str, ChatSession] = {}

    def get(self, session_id: str) -> ChatSession:
        if session_id not in self._sessions:
            self._sessions[session_id] = self.factory(session_id)
        return self._sessions[session_id]

    def clear(self, session_id: str | None = None) -> None:
        if session_id is None:
            self._sessions.clear()
            return
        self._sessions.pop(session_id, None)
