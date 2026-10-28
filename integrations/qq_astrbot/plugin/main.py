"""Q1 AstrBot guard. The AstrBot Runner remains the only Agent."""

from __future__ import annotations

import os
from pathlib import Path
from sys import maxsize

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.message.components import At, Image, Record, Reply

from .guard import InboundGuard


class Q1Guard(star.Star):
    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        state_dir = Path(os.environ.get("ASTRBOT_CHAT_STATE_DIR", "data/astrbot_chat_q1"))
        self.guard = InboundGuard(state_dir / "inbound.sqlite3")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=maxsize)
    async def filter_inbound(self, event: AstrMessageEvent):
        if event.get_platform_name() != "aiocqhttp":
            return
        message = event.message_obj
        chain = event.get_messages()
        if not self.guard.should_process(
            str(event.get_self_id()),
            str(getattr(message, "message_id", "")),
            str(event.get_sender_id()),
            kind=event.get_message_type().value,
            has_at=any(isinstance(part, At) and str(part.qq) == str(event.get_self_id()) for part in chain),
            has_image=any(isinstance(part, Image) for part in chain),
            has_voice=any(isinstance(part, Record) for part in chain),
            has_reply=any(isinstance(part, Reply) for part in chain),
        ):
            event.stop_event()

    @filter.command("q1ping")
    async def q1ping(self, event: AstrMessageEvent):
        """Deterministic text send probe; no LLM/model is involved."""
        if event.get_platform_name() == "aiocqhttp":
            yield event.plain_result("q1pong")

    async def terminate(self):
        self.guard.close()
