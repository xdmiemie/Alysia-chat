"""Q3 QQ routing and permissions. AstrBot remains the only Agent."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
from sys import maxsize
import time

from astrbot.api import llm_tool, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.agent.message import Message, TextPart
from astrbot.core.astr_main_agent_resources import LLM_SAFETY_MODE_SYSTEM_PROMPT, TOOL_CALL_PROMPT
from astrbot.core.message.components import At, Image, Plain, Record, Reply
from astrbot.core.message.message_event_result import MessageChain

from .guard import InboundGuard
from .memory_context import ContextMessage, MemoryContext
from .message_router import Incoming, MessageRouter
from .permissions import PermissionPolicy
from .prompt_loader import (
    PromptError,
    apply_tool_descriptions,
    load_prompts,
    replace_datetime_reminders,
    replace_framework_prompts,
)
from .reply_pacing import (
    parse_followup,
    parse_reply,
    remaining_seconds,
    split_sentences,
)


class Q1Guard(star.Star):
    def __init__(self, context: star.Context, config=None) -> None:
        super().__init__(context)
        state_dir = Path(os.environ.get("ASTRBOT_CHAT_STATE_DIR", "data/astrbot_chat_q1"))
        self.guard = InboundGuard(state_dir / "inbound.sqlite3")
        config = config or {}
        memory = MemoryContext(max_messages=int(config.get("context_messages", 20)),
                               ttl=float(config.get("context_ttl_seconds", 300)),
                               token_budget=int(config.get("context_token_budget", 2048)),
                               merge_seconds=float(config.get("merge_seconds", 1.5)))
        self.router = MessageRouter(
            state_dir / "routing.sqlite3", context=memory,
            wake_words=tuple(config.get("wake_words", ["爱莉希雅", "elysia"])),
            auto_probability=float(config.get("auto_probability", 0)),
            auto_hourly_limit=int(config.get("auto_hourly_limit", 3)),
            auto_cooldown=float(config.get("auto_cooldown_seconds", 60)),
            max_group_members=int(config.get("max_group_members", 2000)),
        )
        self.permissions = PermissionPolicy(state_dir / "permissions.sqlite3",
                                            dm_allowlist=tuple(config.get("dm_allowlist", [])),
                                            dm_daily_limit=int(config.get("dm_daily_limit", 1)))
        self.latest_inbound: dict[str, int] = {}
        self.last_followup_at: dict[str, float] = {}
        self.group_sizes: dict[tuple[str, str], tuple[float, int | None]] = {}

    async def _group_size(self, event: AstrMessageEvent) -> int | None:
        group = getattr(event.message_obj, "group", None)
        known = getattr(group, "member_count", None)
        if isinstance(known, int) and known > 0:
            return known
        if not event.get_group_id():
            return None
        key = (str(event.get_self_id()), str(event.get_group_id()))
        cached = self.group_sizes.get(key)
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1]
        count = None
        try:
            info = await asyncio.wait_for(event.bot.call_action(
                "get_group_info", group_id=int(event.get_group_id()), self_id=event.get_self_id()), timeout=3)
            value = info.get("member_count") if isinstance(info, dict) else None
            if value is not None and int(value) > 0:
                count = int(value)
        except (AttributeError, TypeError, ValueError, TimeoutError, OSError):
            pass
        except Exception:
            # Unknown size disables automatic participation, explicit triggers remain usable.
            pass
        self.group_sizes[key] = (time.monotonic(), count)
        if len(self.group_sizes) > 512:
            self.group_sizes.pop(next(iter(self.group_sizes)))
        return count

    @filter.event_message_type(filter.EventMessageType.ALL, priority=maxsize)
    async def filter_inbound(self, event: AstrMessageEvent):
        if event.get_platform_name() != "aiocqhttp":
            return
        event.set_extra("qq_reply_started_at", time.monotonic())
        message = event.message_obj
        chain = event.get_messages()
        raw = getattr(message, "raw_message", {})
        raw = raw if hasattr(raw, "get") else {}
        if (event.get_message_type().value not in ("FriendMessage", "GroupMessage") or
                raw.get("post_type", "message") != "message"):
            event.stop_event()
            return
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
        sender = raw.get("sender") or {}
        incoming = Incoming(
            bot_uin=str(event.get_self_id()), sender_uin=str(event.get_sender_id()),
            message_id=str(message.message_id),
            text="".join(p.text for p in chain if isinstance(p, Plain)) or event.get_message_str(),
            group_id=str(event.get_group_id() or ""), display_name=event.get_sender_name(),
            at_bot=any(isinstance(part, At) and str(part.qq) == str(event.get_self_id()) for part in chain),
            reply_to=next((str(p.id) for p in chain if isinstance(p, Reply)), None),
            reply_sender=next((str(p.sender_id) for p in chain if isinstance(p, Reply)), ""),
            member_count=await self._group_size(event),
            is_group_admin=sender.get("role") in ("owner", "admin"),
        )
        try:
            scope = incoming.scope
        except ValueError:
            event.stop_event()
            return
        # This runs before the Runner's session lock and conversation lookup.
        # AiocqhttpMessageEvent.send routes using get_group_id/get_sender_id,
        # so the actual QQ recipient is independent of this history key.
        event.session_id = scope
        event.set_extra("qq_scope", scope)
        event.set_extra("qq_incoming", incoming)
        session = event.unified_msg_origin
        sequence = self.latest_inbound.get(session, 0) + 1
        self.latest_inbound[session] = sequence
        if len(self.latest_inbound) > 512:
            expired = next(iter(self.latest_inbound))
            self.latest_inbound.pop(expired)
            self.last_followup_at.pop(expired, None)
        event.set_extra("qq_inbound_sequence", sequence)
        control = self.router.control(incoming)
        if control is not None:
            event.stop_event()
            await event.send(MessageChain().message(control))
            return
        decision = self.router.route(incoming)
        event.set_extra("qq_route", decision)
        event.set_extra("qq_control_generation", self.router.generation(scope))
        # Keep deterministic Q1 diagnostics, subject to group on/off/quiet policy.
        ping = incoming.text.strip() == "/q1ping" and decision.trigger != "disabled"
        if ping:
            event.stop_event()
            await event.send(MessageChain().message("q1pong"))
            return
        if not decision.should_reply:
            event.stop_event()
            return
        event.is_at_or_wake_command = True
        event.is_wake = True
        # @ and quote-to-bot bypass the fragment delay. Other admitted events
        # coalesce only when the newest fragment belongs to the same sender.
        if decision.trigger not in ("at", "reply", "auto"):
            await asyncio.sleep(self.router.context.merge_seconds)
            if not self.router.is_latest(incoming, decision):
                event.stop_event()
                return
        if not self._event_active(event):
            event.stop_event()
            return
        event.message_str = self.router.context.text_for(scope, incoming.message_id, incoming.text)

    def _event_active(self, event: AstrMessageEvent) -> bool:
        scope = event.get_extra("qq_scope")
        incoming = event.get_extra("qq_incoming")
        return bool(scope and incoming and not event.is_stopped() and
                    (not incoming.group_id or (
                        self.router.is_active(scope) and
                        self.router.generation(scope) == event.get_extra("qq_control_generation"))))

    @filter.on_llm_request(priority=-maxsize)
    async def request_paced_reply(self, event: AstrMessageEvent, req) -> None:
        if event.get_platform_name() == "aiocqhttp" and event.get_extra("qq_reply_started_at") is not None:
            incoming = event.get_extra("qq_incoming")
            if incoming is None or not self._event_active(event):
                event.stop_event()
                return
            try:
                prompts = load_prompts()
            except PromptError as exc:
                event.stop_event()
                self.logger.error("%s", exc)
                return
            scope = event.get_extra("qq_scope")
            req.session_id = scope
            if incoming.group_id:
                # Rebuild group input solely from this group's TTL window.
                # No legacy conversation (or unrelated plugin context) enters it.
                req.contexts = []
            current = asdict(incoming)
            current.pop("is_group_admin")
            current["text"] = self.router.context.text_for(scope, incoming.message_id, incoming.text)
            req.prompt = json.dumps({"session_id": scope, "current_message": current,
                                     "recent_messages": json.loads(self.router.context.render(scope))
                                     if incoming.group_id else []}, ensure_ascii=False)
            req.func_tool = self.permissions.restrict_tools(
                req.func_tool, lambda: self._event_active(event), handlers={"q2_echo": self.q2_echo})
            apply_tool_descriptions(req.func_tool, prompts)
            original_base = req.system_prompt or ""
            base = replace_framework_prompts(original_base, prompts, {
                "astrbot_safety": LLM_SAFETY_MODE_SYSTEM_PROMPT,
                "astrbot_tool_call": TOOL_CALL_PROMPT,
            })
            replace_datetime_reminders(req.extra_user_content_parts, prompts)
            event.set_extra("qq_persona_prompt", original_base)
            req.system_prompt = "\n".join((base, prompts["qq_context"], prompts["reply_output"]))

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
        if not self._event_active(event):
            result.chain = []
            event.stop_event()
            return
        reply = "".join(part.text for part in result.chain if isinstance(part, Plain))
        if not reply:
            return
        if all(isinstance(part, Plain) for part in result.chain):
            sentences = split_sentences(reply)
            limit = 3 if event.get_group_id() else 6
            if len(sentences) > limit:
                sentences = sentences[:limit - 1] + ["".join(sentences[limit - 1:])]
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
        if not self._event_active(event):
            result.chain = []
            event.stop_event()

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
            if not self._event_active(event):
                return
            try:
                await event.send(MessageChain().message(sentence))
            except Exception as exc:
                self.logger.warning("QQ sentence send failed: %s", type(exc).__name__)
                return
        incoming = event.get_extra("qq_incoming")
        if incoming and incoming.group_id and self._event_active(event):
            self.router.context.add(incoming.scope, ContextMessage(
                incoming.bot_uin, "bot", event.get_extra("qq_primary_reply", ""),
                ["assistant:" + incoming.message_id], False, incoming.message_id, time.time(), "assistant"))
        await self._maybe_followup(event)

    async def _maybe_followup(self, event: AstrMessageEvent) -> None:
        # Groups never receive optional continuation: one admitted reply is enough.
        if event.get_group_id() or not self._event_active(event):
            return
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
        try:
            prompts = load_prompts()
        except PromptError as exc:
            self.logger.error("%s", exc)
            return
        contexts.append({"role": "user", "content": prompts["followup_user"]})
        base = replace_framework_prompts(event.get_extra("qq_persona_prompt", ""), prompts, {
            "astrbot_safety": LLM_SAFETY_MODE_SYSTEM_PROMPT,
            "astrbot_tool_call": TOOL_CALL_PROMPT,
        })
        started_at = time.monotonic()
        try:
            response = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id="astrbot_chat_qwen3",
                    system_prompt=base + "\n" + prompts["followup_system"],
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
        if not valid or not followup or sequence != self.latest_inbound.get(session) or not self._event_active(event):
            return
        wait = remaining_seconds(followup, level, started_at)
        if wait > 0:
            await asyncio.sleep(wait)
        if sequence != self.latest_inbound.get(session) or not self._event_active(event):
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
    async def q2_echo(self, event: AstrMessageEvent, value: str) -> str:
        """Echo a short value for the local Qwen3 tool-call acceptance test.

        Args:
            value(string): Short text to echo.
        """
        character_id = os.environ.get("ASTRBOT_CHAT_CHARACTER_ID", "elysia")
        return f"character_id={character_id}; echo={value[:80]}"

    async def terminate(self):
        self.guard.close()
        self.router.close()
        self.permissions.close()
