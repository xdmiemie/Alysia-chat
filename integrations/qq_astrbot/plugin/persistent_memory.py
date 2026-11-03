"""QQ adapter for the common short-term memory service and registered policies."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .memory_repository import MemoryRepository, owner_bot
from .prompt_loader import load_prompts
from .shared_memory import SharedMemory, RequestContext, encoded, digest
from .memory_tasks import MemoryTasks, InputTooLarge


DIRECTIVE = re.compile(r"<\s*/?\s*(?:system|developer|tool|script)|\[/?INST\]|tool_calls?|"
    r"ignore\s+(?:all\s+)?(?:previous|prior|system)|(?:忽略|无视|覆盖).{0,12}(?:指令|规则|提示词)|"
    r"(?:执行|调用|运行).{0,16}(?:命令|工具|shell|powershell|python)|"
    r"(?:发送|转发|私信|发布).{0,12}(?:QQ|账号|号码|好友|群|http)|(?:system|developer)\s*:", re.I)


def data_safe(text):
    return isinstance(text, str) and not DIRECTIVE.search(text)


def memory_view(row):
    return {"id": row["id"], "summary": row["summary"], "source_type": row["source_type"], "data_only": True}


def text_payload(message):
    if isinstance(message.content, str):
        return message.content, None
    for index, part in enumerate(message.content or []):
        text = getattr(part, "text", None)
        if isinstance(text, str):
            try:
                value = json.loads(text)
            except ValueError:
                continue
            if isinstance(value, dict) and "current_message" in value:
                return text, index
    return "", None


class PersistentMemory:
    def __init__(self, directory: Path, *, character="elysia", timezone_name="system", enabled=True,
                 diary_enabled=True, diary_time="00:05", logger=None):
        from zoneinfo import ZoneInfo
        from .foreground_activity import ForegroundActivity
        self.repository = MemoryRepository(Path(directory) / "memories.sqlite3")
        self.shared = SharedMemory(self.repository.path, initial_timezone=timezone_name)
        with self.shared.connection() as db:
            configured = db.execute("SELECT 1 FROM sm_meta WHERE key='adapter_configured'").fetchone()
        if not configured:
            with self.shared.connection() as db:
                db.execute("INSERT OR IGNORE INTO sm_meta VALUES('adapter_configured','true')")
        self.character, self.enabled, self.diary_enabled = character, enabled, diary_enabled
        self.zone = ZoneInfo(self.shared.config["timezone"])
        self.diary_hour, self.diary_minute = map(int, self.shared.config["finalize_time"].split(":"))
        self.logger = logger
        self.foreground = ForegroundActivity(directory)
        self.provider_factory = self.admission = self.persona_factory = None
        self.scheduler = self.worker_task = None
        self.lock = asyncio.Lock()
        self.running_tasks = set()
        self.closed = False

    def enqueue(self, incoming, text=None):
        if not self.enabled:
            return False
        text = incoming.text if text is None else text
        if not isinstance(text, str) or not text.strip():
            return False
        try:
            controls = {"/memory no_store": "no_store", "/memory no_long_term": "no_long_term",
                        "/memory allow": "normal", "不要保存我的聊天内容": "no_store", "不要长期保存我的聊天内容": "no_long_term"}
            if text.strip() in controls:
                if incoming.group_id:
                    self.shared.set_subject_retention(incoming.bot_uin, self.character, incoming.scope,
                        incoming.sender_uin, controls[text.strip()])
                else:
                    self.shared.set_retention(incoming.bot_uin, self.character, incoming.scope, controls[text.strip()])
                self.shared.scrub_backups()
                return True
            with self.repository.connection() as db:
                db.execute("INSERT OR IGNORE INTO memory_accounts VALUES(?,?,?)", (incoming.bot_uin, self.character, time.time()))
            return self.shared.append_event(bot=incoming.bot_uin, character=self.character, module="qq",
                event_id=str(incoming.message_id), scope=incoming.scope, subject=incoming.sender_uin,
                occurred=getattr(incoming, "occurred_at", None), payload={"text": text})
        except (sqlite3.Error, OSError) as exc:
            self._warn("source_persistence_failed", exc)
            return False

    def record_sent(self, scope, text, *, event_id=None, role="assistant"):
        if not self.enabled or not text:
            return
        try:
            self.shared.append_event(bot=owner_bot(scope), character=self.character, module="qq", scope=scope,
                event_id=event_id or uuid.uuid4().hex, subject=owner_bot(scope), role=role, payload={"text": text})
        except (sqlite3.Error, OSError) as exc:
            self._warn("sent_receipt_persistence_failed", exc)

    def enqueue_video(self, *, bot, scope, event_id, material_id, analysis, occurred, shared=False):
        if not self.enabled:
            return None
        return self.shared.append_event(bot=bot, character=self.character, module="video", event_id=event_id,
            scope=scope, subject="character", role="source", occurred=occurred,
            payload={"material_id": material_id, "analysis": analysis}, visibility="shared" if shared else "session")

    def _warn(self, code, exc):
        if self.logger:
            self.logger.warning("Shared memory %s: %s", code, type(exc).__name__)

    def wake(self):
        if not self.closed and self.enabled and self.provider_factory and (self.worker_task is None or self.worker_task.done()):
            self.worker_task = asyncio.create_task(self.tick())

    async def start(self, provider_factory, admission, persona_factory):
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        self.provider_factory, self.admission, self.persona_factory = provider_factory, admission, persona_factory
        if not self.enabled or self.scheduler:
            return
        self.scheduler = AsyncIOScheduler(timezone=self.zone)
        self.scheduler.add_job(self.tick, "interval", seconds=15, id="q5-memory-recovery", coalesce=True, max_instances=1)
        self.scheduler.add_job(self.tick, "cron", hour=self.diary_hour, minute=self.diary_minute, id="q5-diary", coalesce=True, max_instances=1)
        self.scheduler.start()
        self.wake()

    async def close(self):
        self.closed = True
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
        tasks = set(self.running_tasks)
        if self.worker_task:
            tasks.add(self.worker_task)
        tasks.discard(asyncio.current_task())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _utility(self, provider, prompt, payload, schema, *, max_tokens=1536, wait=None, consume=None, context=None):
        import jsonschema
        from astrbot.core.agent.message import Message
        from .context_budget import RequestBudget, ContextCapacityError
        from .request_provider import request_chat
        payload = dict(payload)
        persona = payload.pop("persona", "")
        messages = [Message(role="system", content=persona + "\n" + prompt), Message(role="user", content=encoded(payload))]
        budget = RequestBudget(provider, "memory")
        budget.output_tokens = max_tokens
        if not budget.window:
            raise ContextCapacityError("runtime_context_window_required")
        total = await budget.count(messages)
        if total + budget.output_tokens + budget.margin_tokens > budget.window:
            raise InputTooLarge("mandatory_material_exceeds_runtime_window")
        usage = None
        if context:
            from .memory_request import assemble
            async def count_request(value):
                return await budget.count([messages[0], Message(role="user", content=encoded(value))])
            payload, usage = await assemble(self.shared, context, payload, count_request=count_request,
                count_text=budget.count_text, window=budget.window, output=max_tokens, margin=budget.margin_tokens,
                model=digest(encoded(provider.provider_config)))
            messages[-1] = Message(role="user", content=encoded(payload))
        if consume:
            consume()
        operation = request_chat(provider, phase="memory", contexts=messages, func_tool=None,
            response_format={"type": "json_schema", "json_schema": {"name": "shared_memory_task", "strict": True, "schema": schema}},
            max_tokens=max_tokens, temperature=0.0, request_max_retries=1)
        response = await wait(operation) if wait else await asyncio.wait_for(operation, 180)
        if response.tools_call_name:
            raise ValueError("memory_unexpected_tool_call")
        choices = getattr(getattr(response, "raw_completion", None), "choices", None)
        if choices and choices[0].finish_reason in {"length", "content_filter"}:
            raise ValueError("memory_incomplete_output")
        value = json.loads(response.completion_text or "")
        jsonschema.validate(value, schema)
        if context and usage:
            usage["model"] = provider.provider_config.get("model", provider.provider_config.get("id"))
            self.shared.record_usage(context, **usage)
        return value

    async def tasks(self, provider, *, background=True):
        from .context_budget import RequestBudget
        budget = RequestBudget(provider, "memory")
        async def model(key, payload, schema, tokens):
            module = tasks.job["module"] if tasks.job else ("video" if "video" in key else "diary" if "diary" in key else "qq")
            bot, scope, allowed = (tasks.job["bot"] if tasks.job else ""), "", ()
            if tasks.job:
                snapshot = json.loads(tasks.job["snapshot"])
                allowed = tuple(item[1] for item in snapshot if item[0] == "sm_records")
                if snapshot:
                    with self.shared.connection() as db:
                        row = db.execute(f"SELECT scope FROM {snapshot[0][0]} WHERE id=?", (snapshot[0][1],)).fetchone()
                        scope = row[0] if row else ""
            context = RequestContext(self.character, bot, module, scope,
                internal=(module == "diary" and not key.endswith("_public")) or key in {"shared_video_daily", "shared_video_compact"},
                allowed_ids=allowed,
                purpose=key, activity="memory_maintenance" if key != "shared_video_watch" else "watching_video")
            return await self._utility(provider, load_prompts()[key] + "\n" + load_prompts()["shared_memory_use"],
                payload, schema, max_tokens=tokens, context=context)
        def checkpoint():
            if self.closed or (background and self.foreground.blocked()):
                raise asyncio.CancelledError()
        persona = await self.persona_factory() if self.persona_factory else ""
        tasks = MemoryTasks(self.shared, model, count_text=budget.count_text, persona=persona, checkpoint=checkpoint)
        return tasks

    async def tick(self):
        if self.closed or not self.enabled or self.lock.locked() or not self.provider_factory:
            return
        async with self.lock:
            task = asyncio.current_task()
            self.running_tasks.add(task)
            try:
                provider = await self.provider_factory()
                if provider is None:
                    return
                idle = not self.foreground.blocked()
                # request_chat owns the per-call queue; nesting a queue lease here deadlocks.
                async with self.admission():
                    tasks = await self.tasks(provider, background=idle)
                    await tasks.process_idle(character=self.character, force=idle)
                    if idle and not self.foreground.blocked():
                        await tasks.process_videos(self.character)
                        await tasks.catch_up(self.character, diary=self.diary_enabled)
                self.shared.cleanup()
            except asyncio.CancelledError:
                if self.closed:
                    raise
            except Exception as exc:
                self._warn("background_deferred", exc)
            finally:
                self.running_tasks.discard(task)

    async def process_idle(self):
        await self.tick()
        return not self.pending_sessions()

    def pending_sessions(self):
        with self.shared.connection() as db:
            return bool(db.execute("SELECT 1 FROM sm_events WHERE character=? AND module='qq' AND status='pending' LIMIT 1", (self.character,)).fetchone())

    async def extract_one(self, provider):
        """Compatibility scheduler hook: session summarization, never fact extraction."""
        return await (await self.tasks(provider, background=False)).process_idle(character=self.character, force=True)

    async def generate_diary(self, provider, bot, day):
        return await (await self.tasks(provider, background=False)).diary_day(bot, self.character, day.isoformat())

    async def prepare(self, messages, provider, scope, tools=None, *, wait=None, consume=None, record=None, phase="agent", output_tokens=None):
        try:
            return await self._prepare(messages, provider, scope, tools, record=record, phase=phase, output_tokens=output_tokens)
        except (sqlite3.Error, OSError) as exc:
            self._warn("recall_unavailable", exc)
            return messages

    async def _prepare(self, messages, provider, scope, tools=None, *, record=None, phase="agent", output_tokens=None):
        from .context_budget import RequestBudget
        if not self.enabled:
            return messages
        current = None
        for index in range(len(messages) - 1, -1, -1):
            if messages[index].role != "user":
                continue
            raw, part_index = text_payload(messages[index])
            try:
                payload = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict) and isinstance(payload.get("current_message"), dict):
                current = index
                break
        if current is None:
            return messages
        if payload.get("session_id") != scope:
            raise ValueError("memory_scope_mismatch")
        payload.pop("relevant_memories", None)
        payload.pop("shared_memory", None)
        result = list(messages)
        copied = result[current] = messages[current].model_copy(deep=True)
        self._replace_payload(copied, payload, part_index)
        budget = RequestBudget(provider, phase)
        if output_tokens is not None:
            budget.output_tokens = output_tokens
        if not budget.window:
            return result
        # Fit baseline first. Optional memory never evicts additional dialogue or receipts.
        result, base, _ = await budget.fit(result, tools)
        if not any(m is copied for m in result):
            return result
        context = RequestContext(self.character, owner_bot(scope), "qq", scope,
            query=payload["current_message"].get("text", ""), activity="chatting", purpose=phase)
        allowance = max(0, budget.window - budget.output_tokens - budget.margin_tokens - base)
        view = await self.shared.build_memory_view(context, count=budget.count_text, available=allowance)
        for row in self.repository.retrieve(scope, context.query, character=self.character, limit=8):
            item = dict(memory_view(row), module="legacy", kind="legacy", coverage=[row["created_at"], row["updated_at"]])
            cost = await budget.count_text(encoded(item))
            used = sum(a["tokens"] for a in view["attribution"]) + view["common_tokens"]
            if data_safe(row["summary"]) and used + cost <= view["available"]:
                view["items"].append(item)
                view["attribution"].append(dict(module="legacy", layer="long", tokens=cost))
        while True:
            payload["relevant_memories"] = view["items"]
            payload["shared_memory"] = {"recent": view["recent"], "activity": "chatting", "data_only": True}
            self._replace_payload(copied, payload, part_index)
            total = await budget.count(result, tools)
            if total + budget.output_tokens + budget.margin_tokens <= budget.window and total - base <= self.shared.config["memory_tokens"]:
                break
            if view["items"]:
                view["items"].pop()
                view["attribution"].pop()
            elif view["recent"]:
                view["recent"], view["common_tokens"] = [], 0
            else:
                payload.pop("shared_memory", None)
                payload.pop("relevant_memories", None)
                self._replace_payload(copied, payload, part_index)
                total = base
                break
        attributed = sum(a["tokens"] for a in view["attribution"]) + view["common_tokens"]
        usage = dict(model=provider.provider_config.get("model", provider.provider_config.get("id")), window=budget.window,
            total_input=total, reserved_output=budget.output_tokens, memory_budget=view["available"], memory_tokens=max(0, total-base),
            attribution=view["attribution"], common_tokens=view["common_tokens"], wrapper_tokens=max(0, total-base)-attributed,
            count_method="actual_template_tokenizer", attribution_method="separate_blocks_estimate", purpose=phase)
        self.shared.record_usage(context, **usage)
        if record:
            record(dict(request_id=context.request_id, allowance_tokens=view["available"], injected=len(view["items"]), **usage))
        return result

    @staticmethod
    def _replace_payload(message, payload, part_index):
        if part_index is None:
            message.content = encoded(payload)
        else:
            message.content[part_index].text = encoded(payload)
