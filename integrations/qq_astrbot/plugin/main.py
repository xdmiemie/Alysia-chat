"""Q3 QQ routing and permissions. AstrBot remains the only Agent."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
from sys import maxsize
import time

from astrbot.api import llm_tool, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.agent.message import Message
from astrbot.core.astr_main_agent_resources import LLM_SAFETY_MODE_SYSTEM_PROMPT, TOOL_CALL_PROMPT
from astrbot.core.message.components import At, Image, Plain, Record, Reply
from astrbot.core.message.message_event_result import MessageChain

from .guard import InboundGuard
from .agent_state import MAX_FOLLOWUPS, AgentStore, BudgetExhausted, Superseded
from .agent_tools import AgentTools
from .media_router import MediaRouter
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
    remaining_seconds,
    split_sentences,
    text_reply_chains,
    typing_seconds,
)
from .reply_content import chat_history, protocol_view, has_control_markup, spoken_text
from .request_provider import request_chat
from .context_budget import RequestBudget
from .persistent_memory import PersistentMemory
from .qzone_runtime import DiaryPublisher
from .foreground_activity import ForegroundActivity, release_activity_on_error


FOLLOWUP_FORMAT = {"type": "json_schema", "json_schema": {"name": "optional_followup", "strict": True,
    "schema": {"type": "object", "properties": {"follow_up": {"type": ["string", "null"], "maxLength": 80},
        "thinking_level": {"type": "integer", "enum": [1, 2, 3, 4, 5]}},
        "required": ["follow_up", "thinking_level"], "additionalProperties": False}}}


class Q1Guard(star.Star):
    def __init__(self, context: star.Context, config=None) -> None:
        super().__init__(context)
        state_dir = Path(os.environ.get("ASTRBOT_CHAT_STATE_DIR", "data/astrbot_chat_q1"))
        self.test_peer = os.environ.get("QQ_COMPANION_TEST_PEER", "")
        if self.test_peer and not self.test_peer.isdecimal():
            raise ValueError("Invalid test peer")
        self.guard = InboundGuard(state_dir / "inbound.sqlite3")
        self.foreground_activity = ForegroundActivity(state_dir)
        config = config or {}
        self._ui_config = config
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
        self.agent_store = AgentStore(state_dir / "agent.sqlite3")
        self.agent_limits = {"max_requests": max(4, min(64, int(config.get("agent_max_requests", 16)))),
                             "max_tools": max(1, min(64, int(config.get("agent_max_tools", 16)))),
                             "max_seconds": max(10, min(600, float(config.get("agent_max_seconds", 180))))}
        self.agent_tools = AgentTools(self)
        self.native_tools = self.agent_tools.registered()
        self.context.add_llm_tools(*self.native_tools)
        self.group_sizes: dict[tuple[str, str], tuple[float, int | None]] = {}
        self.media = MediaRouter(context, state_dir, character_id=os.environ.get("ASTRBOT_CHAT_CHARACTER_ID", "elysia"))
        self.media.active_group = lambda scope, generation: (
            self.router.is_active(scope) and self.router.generation(scope) == generation)
        self.media.active_version = self.agent_store.current
        self.media.agent_store = self.agent_store
        self.media.run_status = self.agent_store.run_status
        self.media.run_active = lambda run_id: any(r.run_id == run_id and not r.cancel.is_set()
                                                   for r in self.agent_store.active.values())
        self.media.record_delivery = self.agent_store.record_id
        def record_group_result(scope, key, text):
            self.router.context.add(scope, ContextMessage(
                scope.split(":")[1], "bot", text, ["song:" + key], False, None, time.time(), "assistant"))
            self.router.context.remember_protocol(scope, 'song:' + key, [{'role': 'assistant', 'content': text}])
        self.media.record_group_result = record_group_result
        self.memory = PersistentMemory(state_dir,
            character=os.environ.get("ASTRBOT_CHAT_CHARACTER_ID", "elysia"),
            timezone_name=str(config.get("memory_timezone", "system")),
            enabled=not self.test_peer and bool(config.get("memory_enabled", True)),
            diary_enabled=not self.test_peer and bool(config.get("diary_enabled", True)),
            diary_time=str(config.get("diary_time", "23:30")), logger=self.logger)
        from .vision_runtime import VisionRuntime
        self.vision = VisionRuntime(state_dir, self.media.client, context=context, memory=self.memory,
            character=self.memory.character, logger=self.logger)
        self.vision.active_scope = lambda scope: self.router.is_active(scope)
        self.vision.reply_media = self.media
        self.vision.fragment_window = self.router.context.merge_seconds + 1
        from .proactive_runtime import ProactiveRuntime
        self.proactive = ProactiveRuntime(self, state_dir)

    async def initialize(self):
        if hasattr(self.context, "register_web_api"):
            from .web_control import WebControl
            self.web_control = WebControl(self, self.memory.repository.path.parent, self._ui_config)
        async def provider():
            return await self.context.provider_manager.get_provider_by_id("astrbot_chat_qwen3")

        async def persona():
            selected = await self.context.persona_manager.get_persona(self.memory.character)
            if selected is None:
                raise ValueError("Active persona is missing")
            return selected.system_prompt

        await self.memory.start(provider, self.media.client.admission, persona)
        await self.vision.start(provider)
        if self.test_peer:
            return
        await self.proactive.start()
        # Protocol credentials are read locally, never exposed as Agent tools.
        from .qzone_client import OneBotHTTP, QzoneClient
        def qzone_client(account):
            from .prompt_loader import prompt_path
            root = prompt_path().parents[2]
            paths = list((root / 'external/SnowLuma/v2.1.1/config').glob('onebot_*.json'))
            expected = [p for p in paths if p.stem == 'onebot_' + account]
            if len(expected) != 1:
                raise ValueError('qzone_account_config_required')
            data = json.loads(expected[0].read_text(encoding='utf-8-sig'))
            server = next((s for s in data.get('networks', {}).get('httpServers', [])
                           if s.get('port') == 3000 and s.get('host') in {'127.0.0.1', 'localhost'}), None)
            if not server:
                raise ValueError('qzone_loopback_required')
            return QzoneClient(OneBotHTTP(server.get('accessToken', '')), account)
        self.qzone = DiaryPublisher(self.memory.repository.path.parent, self.memory, qzone_client, logger=self.logger)
        await self.qzone.start()

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
    @release_activity_on_error
    async def filter_inbound(self, event: AstrMessageEvent):
        if control := getattr(self, "web_control", None):
            await control.context_gate.wait()
        if (getattr(self, "test_peer", "") and
                (event.get_group_id() or str(event.get_sender_id()) != self.test_peer)):
            event.stop_event()
            return
        if event.get_platform_name() != "aiocqhttp":
            return
        self.media.resume()
        event.set_extra("qq_transport_origin", event.unified_msg_origin)
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
            member_count=None,
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
        event.session_id = scope + ":q7-acceptance" if getattr(self, "test_peer", "") else scope
        event.set_extra("qq_scope", scope)
        event.set_extra("qq_incoming", incoming)
        # Group controls do not need GPU admission or a group-info await.
        generation = self.router.generation(scope)
        control = self.router.control(incoming)
        if control is not None:
            self._observe_request(event, incoming)
            if self.router.generation(scope) != generation:
                rows = self.media.song_tasks.cancel(scope, reason="group_control_changed")
                self.media.spawn(self.media.cancel_song_tasks(rows))
            event.stop_event()
            try:
                await event.send(MessageChain().message(control))
            finally:
                self._release_song_hold(event)
            return
        # Ordinary group chatter remains context, but is not a new bot request.
        # Directed requests invalidate foreground drafts and hold only this
        # requester's accepted music before the first asynchronous operation.
        sequence = self._observe_request(event, incoming) if self.router.explicitly_addressed(incoming) else None
        incoming = replace(incoming, member_count=await self._group_size(event))
        event.set_extra("qq_incoming", incoming)
        if sequence is not None and not self.agent_store.current(scope, sequence):
            event.stop_event()
            self._release_song_hold(event)
            return
        decision = self.router.route(incoming)
        event.set_extra("qq_route", decision)
        event.set_extra("qq_control_generation", self.router.generation(scope))
        # Keep deterministic Q1 diagnostics, subject to group on/off/quiet policy.
        ping = incoming.text.strip() == "/q1ping" and decision.trigger != "disabled"
        if ping:
            if sequence is None:
                self._observe_request(event, incoming)
            event.stop_event()
            try:
                await event.send(MessageChain().message("q1pong"))
            finally:
                self._release_song_hold(event)
            return
        if (decision.trigger != "disabled" and not any(isinstance(part, Record) for part in chain)
                and (memory := getattr(self, "memory", None))):
            # Capture each accepted original message, including fragments that
            # may later be coalesced or superseded. This is not chat history.
            memory.enqueue(incoming)
            memory.wake()
        if not decision.should_reply:
            event.stop_event()
            self._release_song_hold(event)
            return
        if sequence is None:
            self._observe_request(event, incoming, hold_music=decision.trigger != 'auto')
        if vision := getattr(self, "vision", None):
            vision.observe(event, incoming)
        event.is_at_or_wake_command = True
        event.is_wake = True
        # @ and quote-to-bot bypass the fragment delay. Other admitted events
        # coalesce only when the newest fragment belongs to the same sender.
        if decision.trigger not in ("at", "reply", "auto"):
            await asyncio.sleep(self.router.context.merge_seconds)
            if not self.router.is_latest(incoming, decision):
                event.stop_event()
                self._release_song_hold(event)
                return
        if not self._event_active(event):
            event.stop_event()
            self._release_song_hold(event)
            return
        record = next((p for p in chain if isinstance(p, Record)), None)
        if record is not None:
            try:
                transcript = await self.media.input_voice(event, record)
            except Exception:
                event.stop_event()
                try:
                    await event.send(MessageChain().message("这段语音我暂时没听清，可以再说一次吗？"))
                finally:
                    self._release_song_hold(event)
                return
            incoming = replace(incoming, text=transcript)
            event.set_extra("qq_incoming", incoming)
            event.set_extra("qq_voice_input", True)
            self.router.context.replace_text(scope, incoming.message_id, transcript)
        event.message_str = self.router.context.text_for(scope, incoming.message_id, incoming.text)
        if proactive := getattr(self, "proactive", None):
            proactive.observe(event, incoming, replace_text=True)
        if record is not None and (memory := getattr(self, "memory", None)):
            memory.enqueue(incoming, event.message_str)
            memory.wake()

    def _observe_request(self, event, incoming, *, hold_music=True):
        if not event.get_extra("qq_foreground_activity"):
            event.set_extra("qq_foreground_activity", self.foreground_activity.begin())
            # The native pipeline also exits without starting an Agent or
            # sending a reply. Release on that task's completion/cancellation,
            # after any awaited typing, TTS, delivery and follow-up work.
            if task := asyncio.current_task():
                task.add_done_callback(lambda _: self._release_lease(event))
        event.set_extra("qq_agent_cleanup", lambda: self._release_lease(event))
        if proactive := getattr(self, "proactive", None):
            proactive.observe(event, incoming)
        sequence = self.agent_store.observe(incoming.scope, incoming.message_id)
        session = event.unified_msg_origin
        self.latest_inbound[session] = sequence
        if len(self.latest_inbound) > 512:
            self.latest_inbound.pop(next(iter(self.latest_inbound)))
        event.set_extra("qq_inbound_sequence", sequence)
        self.agent_store.guard_sender(event, incoming.scope, sequence)
        if hold_music:
            self.media.song_tasks.hold(incoming.scope, incoming.sender_uin, sequence)
        return sequence

    def _release_song_hold(self, event, *, finish=True):
        if finish and (activity := getattr(self, "foreground_activity", None)):
            activity.end(event.get_extra("qq_foreground_activity"))
            event.set_extra("qq_foreground_activity", None)
        incoming = event.get_extra("qq_incoming")
        tasks = getattr(self.media, 'song_tasks', None)
        if tasks and incoming:
            tasks.release(incoming.scope, incoming.sender_uin, event.get_extra("qq_inbound_sequence"))

    def _event_active(self, event: AstrMessageEvent) -> bool:
        scope = event.get_extra("qq_scope")
        incoming = event.get_extra("qq_incoming")
        return bool(scope and incoming and not event.is_stopped() and
                    self.agent_store.current(scope, event.get_extra("qq_inbound_sequence")) and
                    (not incoming.group_id or (
                        self.router.is_active(scope) and
                        self.router.generation(scope) == event.get_extra("qq_control_generation"))))

    @filter.on_llm_request(priority=-maxsize)
    @release_activity_on_error
    async def request_paced_reply(self, event: AstrMessageEvent, req) -> None:
        if event.get_platform_name() == "aiocqhttp" and event.get_extra("qq_reply_started_at") is not None:
            incoming = event.get_extra("qq_incoming")
            if incoming is None or not self._event_active(event):
                event.stop_event()
                self._release_lease(event)
                return
            try:
                prompts = load_prompts()
            except PromptError as exc:
                event.stop_event()
                self._release_song_hold(event)
                self.logger.error("%s", exc)
                return
            scope = event.get_extra("qq_scope")
            if event.get_extra("qq_agent_run") is None:
                event.set_extra("qq_agent_run", self.agent_store.begin(scope,
                    event.get_extra("qq_inbound_sequence"), incoming.message_id, **self.agent_limits))
            # Atomic admission with supervisor DRAINING; one lease per tool loop.
            if not event.get_extra("qq_gpu_lease"):
                run = event.get_extra("qq_agent_run")
                try:
                    self.agent_store.work(run)
                    while not (lease := self.media.client.state.enter()):
                        await self.agent_store.wait(run, asyncio.sleep(0.25))
                except Superseded:
                    event.stop_event()
                    self._release_song_hold(event)
                    return
                except (BudgetExhausted, TimeoutError):
                    event.stop_event()
                    self.agent_store.status(run, "budget_exhausted")
                    # No Runner/model is admitted, so record the deterministic
                    # close as an actual send in the separate provenance log.
                    await event.send(MessageChain().message(prompts["agent_budget_reply"]))
                    self.agent_store.record(run, "sent", {"text": prompts["agent_budget_reply"], "modality": "text"})
                    self._release_song_hold(event)
                    return
                event.set_extra("qq_gpu_lease", lease)
            req.session_id = event.session_id if getattr(self, "test_peer", "") else scope
            # Unvalidated framework references are replaced by Q7's bounded
            # actual pixels. Ordinary image replies stay in this native Agent.
            req.image_urls = []
            req.extra_user_content_parts = [part for part in req.extra_user_content_parts
                                           if getattr(part, "type", None) != "image_url"]
            media_status = []
            if vision := getattr(self, "vision", None):
                media_status = await vision.prepare(event, req, incoming)
                if not self._event_active(event):
                    event.stop_event()
                    self._release_lease(event)
                    return
            if incoming.group_id:
                # Rebuild group input solely from this group's TTL window.
                # No legacy conversation (or unrelated plugin context) enters it.
                req.contexts = chat_history(self.router.context.model_history(scope))
            else:
                req.contexts = chat_history(req.contexts or [])
            current = asdict(incoming)
            current.pop("is_group_admin")
            current["text"] = self.router.context.text_for(scope, incoming.message_id, incoming.text)
            if media_status and not current["text"].strip():
                current["text"] = event.get_extra("qq_vision_question", "")
            music_context = json.loads(self.router.context.render(scope))[-8:]
            while music_context and len(json.dumps(music_context, ensure_ascii=False).encode("utf-8")) > 6000:
                music_context.pop(0)
            event.set_extra("qq_music_context", music_context)
            req.prompt = json.dumps({"session_id": scope, "current_message": current,
                                     "input_modality": "voice" if event.get_extra("qq_voice_input") else "text",
                                     "image_understanding_available": bool(req.image_urls),
                                     "media_status": media_status,
                                     "recent_messages": [row for row in json.loads(self.router.context.render(scope))
                                                         if row.get('role') != 'assistant']
                                     if incoming.group_id else []}, ensure_ascii=False)
            from astrbot.core.agent.tool import ToolSet
            req.func_tool = req.func_tool or ToolSet()
            for tool in self.native_tools:
                req.func_tool.add_tool(tool)
            handlers = {tool.name: tool.handler for tool in self.native_tools}
            handlers["q2_echo"] = self.q2_echo
            req.func_tool = self.permissions.restrict_tools(
                req.func_tool, lambda: self._event_active(event), handlers=handlers)
            apply_tool_descriptions(req.func_tool, prompts)
            original_base = req.system_prompt or ""
            base = replace_framework_prompts(original_base, prompts, {
                "astrbot_safety": LLM_SAFETY_MODE_SYSTEM_PROMPT,
                "astrbot_tool_call": TOOL_CALL_PROMPT,
            })
            replace_datetime_reminders(req.extra_user_content_parts, prompts)
            # Background song notifications use their own task format. Private
            # follow-ups inherit the complete system from NativeAgentLifecycle.
            event.set_extra("qq_persona_prompt", original_base)
            req.system_prompt = "\n".join((base, prompts["qq_context"]))
            if media_status:
                req.system_prompt += "\n" + prompts["vision_context"]
            # The lifecycle inserts these rules as a second system message,
            # retaining them for this run without saving them as chat history.
            event.set_extra("qq_agent_rules_prompt", "\n".join((prompts["reply_output"], prompts["agent_policy"])))
            if memory := getattr(self, "memory", None):
                event.set_extra("qq_persistent_memory", memory)
                event.set_extra("qq_agent_rules_prompt", event.get_extra("qq_agent_rules_prompt") + "\n" + prompts["memory_use"])
            task_input = json.loads(req.prompt)
            tasks = getattr(self.media, 'song_tasks', None)
            task_input['active_music_tasks'] = [{k: row[k] for k in ('id', 'mode', 'title', 'state')}
                for row in (tasks.pending(scope, incoming.sender_uin)[-6:] if tasks else [])]
            req.prompt = json.dumps(task_input, ensure_ascii=False)

    @filter.on_agent_begin()
    async def begin_native_agent(self, event: AstrMessageEvent, run_context) -> None:
        run = event.get_extra("qq_agent_run")
        if run is None or event.get_extra("qq_agent_lifecycle") is not None:
            return
        from astrbot.core.pipeline.process_stage.follow_up import _ACTIVE_AGENT_RUNNERS
        from .agent_runtime import NativeAgentLifecycle
        runner = _ACTIVE_AGENT_RUNNERS.get(event.unified_msg_origin)
        if runner is None or runner.run_context is not run_context:
            event.stop_event()
            self._release_lease(event)
            raise RuntimeError("Pinned AstrBot active Runner is unavailable")
        event.set_extra("qq_agent_cleanup", lambda: self._release_lease(event))
        incoming = event.get_extra('qq_incoming')
        if incoming and incoming.group_id:
            event.set_extra('qq_protocol_history', lambda messages: self.router.context.remember_protocol(
                incoming.scope, incoming.message_id, messages))
        NativeAgentLifecycle(runner, self.agent_store, run, event).install()

    @filter.on_agent_done()
    async def unpack_paced_reply(self, event: AstrMessageEvent, run_context, response) -> None:
        if event.get_extra("qq_agent_run") is None:
            return
        if not self._event_active(event) or not event.get_extra("qq_agent_completed"):
            response.result_chain = None
            response.completion_text = ""
            self._release_lease(event)
            return
        from .agent_runtime import parse_final_reply
        reply = parse_final_reply(response.completion_text or "")
        if reply is None:
            response.result_chain = None
            response.completion_text = ""
            self._release_lease(event)
            return
        response.completion_text = reply["reply"].strip()
        event.set_extra("qq_thinking_level", reply["thinking_level"])
        event.set_extra("qq_primary_reply", reply["reply"].strip())
        event.set_extra("qq_voice", reply["voice"])
        event.set_extra("qq_run_context", run_context)
        self._release_song_hold(event, finish=False)

    def _release_lease(self, event):
        self._release_song_hold(event)
        self.media.client.state.leave(event.get_extra("qq_gpu_lease"))
        event.set_extra("qq_gpu_lease", None)

    @filter.on_decorating_result()
    @release_activity_on_error
    async def delay_paced_reply(self, event: AstrMessageEvent) -> None:
        if event.get_platform_name() != "aiocqhttp" or event.get_extra("qq_agent_run") is None:
            return
        result = event.get_result()
        if result is None or not result.is_llm_result():
            return
        if not self._event_active(event) or not event.get_extra("qq_agent_completed"):
            result.chain = []
            self._release_lease(event)
            return
        if event.get_extra("qq_reply_delayed", False):
            return
        reply = event.get_extra("qq_primary_reply", "")
        if not reply:
            result.chain = []
            return
        run = event.get_extra("qq_agent_run")
        chains = []
        transcripts = []
        if event.get_extra("qq_voice", False):
            audio = run.speech.get(reply)
            if audio is not None:
                chains = [MessageChain(chain=[Record.fromFileSystem(str(audio))])]
                transcripts = [spoken_text(reply)]
                for content, voice in self.media.speech_segments(reply, group=bool(event.get_group_id())):
                    if not voice:
                        chains.append(MessageChain(chain=[Plain(content)]))
                        transcripts.append(content)
            else:
                try:
                    chains = await self.agent_store.wait(run,
                        self.media.voice_chains(reply, "neutral", group=bool(event.get_group_id())))
                    transcripts = self.media.speech_texts(reply, group=bool(event.get_group_id()))
                except Superseded:
                    result.chain = []
                    self._release_lease(event)
                    return
                except (BudgetExhausted, TimeoutError):
                    chains = []  # Deliver the final text without starting more synthesis.
        if not chains:
            chains, transcripts = text_reply_chains(reply, group=bool(event.get_group_id()))
        event.set_extra("qq_delivery_chains", chains)
        for index, chain in enumerate(chains):
            for part in chain.chain:
                if isinstance(part, Record):
                    self.agent_store.record(run, "speech_prepared", {"text": transcripts[index],
                        "path": str(part.file), "source": "reply"})
        event.set_extra("qq_delivery_transcripts", transcripts)
        event.set_extra("qq_delivery_text", reply)
        event.set_extra("qq_reply_delayed", True)
        wait = 0 if any(isinstance(part, Record) for part in chains[0].chain) else remaining_seconds(
            transcripts[0], event.get_extra("qq_thinking_level", 1),
            event.get_extra("qq_reply_started_at", time.monotonic()))
        if wait > 0:
            try:
                await self.agent_store.wait_delivery(run, wait)
            except Superseded:
                result.chain = []
                self._release_lease(event)
                return
        # Critical: the input may have changed during synthesis or typing.
        if not self._event_active(event):
            result.chain = []
            self._release_lease(event)
            return
        result.chain = chains[0].chain if chains else []
        event.set_extra("qq_reply_ready", bool(chains))

    @filter.after_message_sent()
    async def finish_paced_reply(self, event: AstrMessageEvent) -> None:
        if (event.get_platform_name() != "aiocqhttp" or not event.get_extra("qq_reply_ready", False)
                or event.get_extra("qq_reply_finished", False)):
            return
        event.set_extra("qq_reply_finished", True)
        chains = event.get_extra("qq_delivery_chains", [])
        lifecycle = event.get_extra("qq_agent_lifecycle")
        run = event.get_extra("qq_agent_run")
        def remember(chain, index):
            is_voice = any(isinstance(part, Record) for part in chain.chain)
            # Voice text is the spoken transcript; textual chunks are recorded individually.
            text = event.get_extra("qq_delivery_transcripts", [""])[index] if is_voice else chain.get_plain_text()
            content = None
            if lifecycle:
                content = lifecycle.sent(text, modality="voice" if is_voice else "text",
                    primary=True, complete=index == len(chains) - 1)
            if event.get_group_id() and text:
                incoming = event.get_extra("qq_incoming")
                key = 'assistant:' + incoming.message_id
                if event.get_extra('qq_group_reply_saved'):
                    self.router.context.replace_text(incoming.scope, key, content or text)
                else:
                    self.router.context.add(incoming.scope, ContextMessage(incoming.bot_uin, "bot", content or text,
                        [key], False, incoming.message_id, time.time(), "assistant"))
                    event.set_extra('qq_group_reply_saved', True)
        try:
            if chains:
                remember(chains[0], 0)  # after_message_sent confirms the first successful send.
            for index, chain in enumerate(chains[1:], start=1):
                if not self._event_active(event):
                    return
                if not any(isinstance(part, Record) for part in chain.chain):
                    # Thinking is shared with the first segment. Each later segment
                    # starts its own typing delay only after the previous send finishes.
                    wait = typing_seconds(chain.get_plain_text())
                    if wait > 0:
                        await self.agent_store.wait_delivery(run, wait)
                if not self._event_active(event):
                    return
                await event.send(chain)
                remember(chain, index)
            if self._event_active(event):
                self.agent_store.status(run, "sent")
                await self._maybe_followup(event)
            if self._event_active(event):
                await self.media.commit_run(run.run_id)
        except Superseded:
            return
        except Exception as exc:
            self.agent_store.record(run, "delivery_failed", {"error": type(exc).__name__})
            self.agent_store.status(run, "delivery_failed")
        finally:
            if self.agent_store.active.get(run.scope) is run:
                self.agent_store.active.pop(run.scope)
            self._release_lease(event)

    async def _maybe_followup(self, event):
        """After the Agent and primary delivery, optionally send one text addition."""
        run = event.get_extra("qq_agent_run")
        lifecycle = event.get_extra("qq_agent_lifecycle")
        if (event.get_group_id() or not run or run.closing or not lifecycle
                or not self._event_active(event) or event.get_extra("qq_followup_finished")):
            return
        event.set_extra("qq_followup_finished", True)
        provider = lifecycle.runner.provider
        previous = event.get_extra("qq_primary_reply", "")
        try:
            while run.followups < getattr(self, "followup_limit", MAX_FOLLOWUPS):
                prompts = load_prompts()
                dialogue = protocol_view(lifecycle.initial_messages + lifecycle.tool_history() + lifecycle.sent_messages)
                if not dialogue:
                    return
                messages = lifecycle.followup_system_messages(prompts) + [Message.model_validate(m) for m in dialogue]
                messages.append(Message(role="user", content=prompts["followup_user"]))
                budget = RequestBudget(provider, "followup")
                budget.output_tokens = 160
                if memory := event.get_extra("qq_persistent_memory"):
                    messages = await memory.prepare(messages, provider, run.scope,
                        wait=lambda operation: self.agent_store.wait(run, operation, timeout=60),
                        consume=lambda: self.agent_store.consume(run, "requests"))
                fitted, tokens, dropped = await self.agent_store.wait(run, budget.fit(messages), timeout=10)
                self.agent_store.consume(run, "followups")
                self.agent_store.consume(run, "requests")
                started = time.monotonic()
                self.agent_store.record(run, "followup_request", {"number": run.followups,
                    "dialogue": dialogue, "input_tokens": tokens, "dropped_turns": dropped})
                response = await self.agent_store.wait(run, request_chat(provider, phase="followup",
                    session_id=run.scope, contexts=fitted, func_tool=None,
                    response_format=FOLLOWUP_FORMAT, max_tokens=160, temperature=0.5,
                    request_max_retries=1), timeout=25)
                raw = response.completion_text or ""
                self.agent_store.record(run, "followup_generated", {"number": run.followups, "text": raw})
                # A provider ignoring func_tool=None still cannot execute any tool.
                if response.tools_call_name:
                    self.agent_store.record(run, "followup_rejected", {"reason": "unexpected_tool_call"})
                    return
                text, level, valid = parse_followup(raw, previous)
                if not valid or not text or has_control_markup(text):
                    return
                if not self._event_active(event):
                    return
                if proactive := getattr(self, "proactive", None):
                    decision = proactive.store.draw(run.scope, f"{run.run_id}:{run.followups}",
                        proactive.config["followup_average_probability"], proactive.config["followup_pity_limit"])
                    self.agent_store.record(run, "followup_probability", decision)
                    if not decision["hit"]:
                        return
                wait = remaining_seconds(text, level, started)
                if wait > 0:
                    await self.agent_store.wait(run, asyncio.sleep(wait))
                if not self._event_active(event):
                    return
                await event.send(MessageChain().message(text))
                # The parser may limit a multi-sentence output to one sentence.
                # Keep the untouched generation in audit, and only sent text in history.
                delivered = json.dumps({"follow_up": text, "thinking_level": level}, ensure_ascii=False)
                lifecycle.sent(text, model_content=delivered)
                previous += "\n" + text
        except (Superseded, BudgetExhausted, TimeoutError):
            return
        except Exception as exc:
            self.agent_store.record(run, "followup_failed", {"error": type(exc).__name__})
            self.logger.warning("QQ optional follow-up stopped: %s", type(exc).__name__)

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
        if run := event.get_extra("qq_agent_run"):
            self.agent_store.consume(run, "tool_calls")
        return f"character_id={character_id}; echo={value[:80]}"

    async def terminate(self):
        if proactive := getattr(self, "proactive", None):
            await proactive.close()
        if vision := getattr(self, "vision", None):
            await vision.close()
        if qzone := getattr(self, "qzone", None):
            await qzone.close()
        if memory := getattr(self, "memory", None):
            await memory.close()
        await self.media.close()
        self.foreground_activity.forget_process()
        self.guard.close()
        self.router.close()
        self.permissions.close()
