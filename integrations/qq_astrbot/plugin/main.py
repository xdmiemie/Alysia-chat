"""Q1 AstrBot guard. The AstrBot Runner remains the only Agent."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from sys import maxsize
import time

from astrbot.api import llm_tool, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.agent.message import Message, TextPart
from astrbot.core.message.components import At, Image, Plain, Record, Reply
from astrbot.core.message.message_event_result import MessageChain

from .guard import InboundGuard
from .reply_pacing import (
    FOLLOWUP_INSTRUCTION,
    OUTPUT_INSTRUCTION,
    parse_followup,
    parse_reply,
    remaining_seconds,
    split_sentences,
)


class Q1Guard(star.Star):
    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        state_dir = Path(os.environ.get("ASTRBOT_CHAT_STATE_DIR", "data/astrbot_chat_q1"))
        self.guard = InboundGuard(state_dir / "inbound.sqlite3")
        self.latest_inbound: dict[str, int] = {}
        self.last_followup_at: dict[str, float] = {}

    @filter.event_message_type(filter.EventMessageType.ALL, priority=maxsize)
    async def filter_inbound(self, event: AstrMessageEvent):
        if event.get_platform_name() != "aiocqhttp":
            return
        event.set_extra("qq_reply_started_at", time.monotonic())
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
            return
        session = event.unified_msg_origin
        sequence = self.latest_inbound.get(session, 0) + 1
        self.latest_inbound[session] = sequence
        event.set_extra("qq_inbound_sequence", sequence)

    @filter.on_llm_request()
    async def request_paced_reply(self, event: AstrMessageEvent, req) -> None:
        if event.get_platform_name() == "aiocqhttp" and event.get_extra("qq_reply_started_at") is not None:
            event.set_extra("qq_persona_prompt", req.system_prompt or "")
            req.system_prompt = f"{req.system_prompt or ''}\n{OUTPUT_INSTRUCTION}"

    @filter.on_agent_done()
    async def unpack_paced_reply(self, event: AstrMessageEvent, run_context, response) -> None:
        if (event.get_platform_name() != "aiocqhttp" or
                event.get_extra("qq_reply_started_at") is None or
                response is None or response.role != "assistant" or not response.completion_text):
            return
        original = response.completion_text
        reply, level, _ = parse_reply(original)
        response.completion_text = reply
        event.set_extra("qq_thinking_level", level)
        event.set_extra("qq_primary_reply", reply)
        event.set_extra("qq_run_context", run_context)
        # AstrBot appends the assistant message before calling on_agent_done.
        # Keep conversation history free of the pacing JSON as well.
        if reply != original:
            for message in reversed(run_context.messages):
                if message.role != "assistant" or not isinstance(message.content, list):
                    continue
                for part in message.content:
                    if getattr(part, "type", None) == "text" and part.text == original:
                        part.text = reply
                        return

    @filter.on_decorating_result()
    async def delay_paced_reply(self, event: AstrMessageEvent) -> None:
        if event.get_platform_name() != "aiocqhttp" or event.get_extra("qq_reply_delayed", False):
            return
        started_at = event.get_extra("qq_reply_started_at")
        level = event.get_extra("qq_thinking_level")
        result = event.get_result()
        if started_at is None or level is None or result is None or not result.is_llm_result():
            return
        reply = "".join(part.text for part in result.chain if isinstance(part, Plain))
        if not reply:
            return
        if all(isinstance(part, Plain) for part in result.chain):
            sentences = split_sentences(reply)
            if sentences:
                result.chain = [Plain(sentences[0])]
                event.set_extra("qq_remaining_sentences", sentences[1:])
                reply = sentences[0]
                event.set_extra("qq_reply_ready", True)
                self.logger.info("QQ paced reply has %d sentence(s)", len(sentences))
        event.set_extra("qq_reply_delayed", True)
        wait = remaining_seconds(reply, level, started_at)
        if wait > 0:
            await asyncio.sleep(wait)

    @filter.after_message_sent()
    async def finish_paced_reply(self, event: AstrMessageEvent) -> None:
        if (event.get_platform_name() != "aiocqhttp" or
                not event.get_extra("qq_reply_ready", False) or
                event.get_extra("qq_reply_finished", False)):
            return
        event.set_extra("qq_reply_finished", True)
        started_at = event.get_extra("qq_reply_started_at")
        level = event.get_extra("qq_thinking_level")
        cumulative = event.get_result().get_plain_text()
        for sentence in event.get_extra("qq_remaining_sentences", []):
            cumulative += sentence
            wait = remaining_seconds(cumulative, level, started_at)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                await event.send(MessageChain().message(sentence))
            except Exception as exc:
                self.logger.warning("QQ sentence send failed: %s", type(exc).__name__)
                return
        await self._maybe_followup(event)

    async def _maybe_followup(self, event: AstrMessageEvent) -> None:
        session = event.unified_msg_origin
        sequence = event.get_extra("qq_inbound_sequence")
        if sequence != self.latest_inbound.get(session):
            return
        is_group = event.get_message_type().value == "GroupMessage"
        cooldown = 180 if is_group else 60
        if time.monotonic() - self.last_followup_at.get(session, float("-inf")) < cooldown:
            return
        run_context = event.get_extra("qq_run_context")
        if run_context is None:
            return
        contexts = self._recent_dialogue(run_context.messages)
        if not contexts:
            return
        contexts.append({"role": "user", "content": "现在只判断刚才回复后是否自然补发一句。请按指定 JSON 格式回答。"})
        started_at = time.monotonic()
        try:
            response = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id="astrbot_chat_qwen3",
                    system_prompt=event.get_extra("qq_persona_prompt", "") + "\n" + FOLLOWUP_INSTRUCTION,
                    contexts=contexts,
                    max_tokens=160,
                    temperature=0.5,
                    request_max_retries=1,
                ),
                timeout=25,
            )
        except Exception as exc:
            self.logger.warning("QQ follow-up decision failed: %s", type(exc).__name__)
            return
        followup, level, valid = parse_followup(response.completion_text or "", event.get_extra("qq_primary_reply", ""))
        if not valid or not followup or sequence != self.latest_inbound.get(session):
            return
        wait = remaining_seconds(followup, level, started_at)
        if wait > 0:
            await asyncio.sleep(wait)
        if sequence != self.latest_inbound.get(session):
            return
        try:
            await event.send(MessageChain().message(followup))
        except Exception as exc:
            self.logger.warning("QQ follow-up send failed: %s", type(exc).__name__)
            return
        run_context.messages.append(Message(role="assistant", content=[TextPart(text=followup)]))
        self.last_followup_at[session] = time.monotonic()
        self.logger.info("QQ optional follow-up sent")

    @staticmethod
    def _recent_dialogue(messages) -> list[dict[str, str]]:
        dialogue = []
        for message in messages:
            if message.role not in ("user", "assistant"):
                continue
            if isinstance(message.content, str):
                content = message.content
            elif isinstance(message.content, list):
                content = "".join(part.text for part in message.content if getattr(part, "type", None) == "text")
            else:
                continue
            content = content.strip()
            if content:
                dialogue.append({"role": message.role, "content": content[:600]})
        return dialogue[-8:]

    @filter.command("q1ping")
    async def q1ping(self, event: AstrMessageEvent):
        """Deterministic text send probe; no LLM/model is involved."""
        if event.get_platform_name() == "aiocqhttp":
            yield event.plain_result("q1pong")

    @llm_tool("q2_echo")
    async def q2_echo(self, value: str) -> str:
        """Echo a short value for the local Qwen3 tool-call acceptance test.

        Args:
            value(string): Short text to echo.
        """
        character_id = os.environ.get("ASTRBOT_CHAT_CHARACTER_ID", "elysia")
        return f"character_id={character_id}; echo={value[:80]}"

    async def terminate(self):
        self.guard.close()
