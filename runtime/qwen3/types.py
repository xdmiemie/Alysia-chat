from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal


Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class CharacterReply:
    reply: str
    emotion: str
    intensity: float
    speed: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ChatTurn:
    user: str
    assistant: CharacterReply
