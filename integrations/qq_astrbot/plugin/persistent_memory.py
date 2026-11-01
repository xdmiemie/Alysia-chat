"""Q5: automatic fact extraction, scoped recall and bounded read-only model jobs."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import time

from .memory_repository import MemoryRepository, owner_bot
from .prompt_loader import load_prompts


TOPICS = ("music", "food", "games", "reading", "travel", "art", "technology", "daily")
FACT_SCHEMA = {"type": "object", "properties": {"facts": {"type": "array", "maxItems": 8,
    "items": {"type": "object", "properties": {
        "key": {"type": "string", "maxLength": 80},
        "kind": {"type": "string", "enum": ["preference", "fact", "event"]},
        "summary": {"type": "string", "maxLength": 400},
        "evidence": {"type": "string", "minLength": 1, "maxLength": 600},
        "tags": {"type": "array", "items": {"type": "string", "maxLength": 32}, "maxItems": 8},
        "salience": {"type": "number", "minimum": 0, "maximum": 1},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "sensitive": {"type": "boolean"},
        "shared_topic": {"type": ["string", "null"], "enum": [*TOPICS, None]},
    }, "required": ["key", "kind", "summary", "evidence", "tags", "salience", "confidence", "sensitive", "shared_topic"],
        "additionalProperties": False}}}, "required": ["facts"], "additionalProperties": False}
VERIFY_SCHEMA = {"type": "object", "properties": {"decisions": {"type": "array", "maxItems": 8,
    "items": {"type": "object", "properties": {
        "index": {"type": "integer", "minimum": 0, "maximum": 7},
        "supported": {"type": "boolean"}, "durable": {"type": "boolean"},
        "existing_key": {"type": ["string", "null"]},
    }, "required": ["index", "supported", "durable", "existing_key"], "additionalProperties": False}}},
    "required": ["decisions"], "additionalProperties": False}
COMPACT_SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}},
                  "required": ["summary"], "additionalProperties": False}
DIARY_SCHEMA = {"type": "object", "properties": {"draft": {"type": "string", "minLength": 200, "maxLength": 400}},
                "required": ["draft"], "additionalProperties": False}

# Retrieved content is data, and obvious embedded instructions are excluded in
# addition to the model-use rule. These checks never grant a tool permission.
DIRECTIVE = re.compile(
    r"<\s*/?\s*(?:system|developer|tool|script)|\[/?INST\]|tool_calls?|"
    r"ignore\s+(?:all\s+)?(?:previous|prior|system)|(?:忽略|无视|覆盖).{0,12}(?:指令|规则|提示词)|"
    r"(?:执行|调用|运行).{0,16}(?:命令|工具|shell|powershell|python)|"
    r"(?:发送|转发|私信|发布).{0,12}(?:QQ|账号|号码|好友|群|http)|"
    r"(?:system|developer)\s*:", re.I)


def data_safe(text):
    return isinstance(text, str) and not DIRECTIVE.search(text)


def memory_view(row):
    return {"id": row["id"], "summary": row["summary"], "source_type": row["source_type"],
            "source_ref": row["source_ref"], "confidence": row["confidence"],
            "recorded_at": datetime.fromtimestamp(row["updated_at"], timezone.utc).isoformat(),
            "visibility": row["visibility"],
            "meaning": "video_content" if row["source_type"] == "douyin" else "recorded_fact"}


def text_payload(message):
    content = message.content
    if isinstance(content, str):
        return content, None
    for index, part in enumerate(content or []):
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
                 diary_enabled=True, diary_time="23:30", logger=None):
        from .diary_jobs import resolve_timezone
        self.repository = MemoryRepository(directory / "memories.sqlite3")
        self.character, self.enabled, self.diary_enabled = character, enabled, diary_enabled
        self.zone = resolve_timezone(timezone_name)
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", diary_time):
            raise ValueError("Invalid diary time")
        self.diary_hour, self.diary_minute = map(int, diary_time.split(":"))
        self.logger = logger
        self.provider_factory = self.admission = self.persona_factory = None
        self.scheduler = None
        self.lock = asyncio.Lock()
        self.worker_task = None
        self.running_tasks = set()
        self.closed = False

    def enqueue(self, incoming, text=None):
        if self.enabled:
            return self.repository.enqueue(incoming.scope, incoming.sender_uin, incoming.message_id,
                incoming.text if text is None else text, character=self.character)
        return False

    def wake(self):
        if not self.closed and self.enabled and self.provider_factory and (
                self.worker_task is None or self.worker_task.done()):
            self.worker_task = asyncio.create_task(self.tick())

    async def start(self, provider_factory, admission, persona_factory):
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        self.provider_factory, self.admission, self.persona_factory = provider_factory, admission, persona_factory
        if not self.enabled:
            return
        self.scheduler = AsyncIOScheduler(timezone=self.zone)
        # Polling also recovers missed cron dates and leased jobs after crashes.
        self.scheduler.add_job(self.tick, "interval", seconds=60, id="q5-memory-recovery",
            coalesce=True, max_instances=1, misfire_grace_time=60)
        self.scheduler.add_job(self.tick, "cron", hour=self.diary_hour, minute=self.diary_minute,
            id="q5-diary", coalesce=True, max_instances=1, misfire_grace_time=3600)
        self.scheduler.start()
        if self.logger:
            self.logger.info("Q5 memory ready; diary timezone=%s time=%02d:%02d", self.zone, self.diary_hour, self.diary_minute)
        self.wake()

    async def close(self):
        self.closed = True
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
        if self.worker_task and not self.worker_task.done():
            self.worker_task.cancel()
            await asyncio.gather(self.worker_task, return_exceptions=True)
        tasks = [task for task in self.running_tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _utility(self, provider, prompt, payload, schema, *, max_tokens=1536, wait=None, consume=None):
        """A provider request, never an Agent: no tools, transport or shell access."""
        import jsonschema
        from astrbot.core.agent.message import Message
        from .context_budget import RequestBudget
        from .request_provider import request_chat
        messages = [Message(role="system", content=prompt), Message(role="user", content=json.dumps(payload, ensure_ascii=False))]
        budget = RequestBudget(provider, "memory")
        budget.output_tokens = max_tokens
        if budget.window:
            from .context_budget import ContextCapacityError
            while True:
                try:
                    messages, _, _ = await budget.fit(messages)
                    break
                except ContextCapacityError:
                    if not payload.get("known_facts"):
                        raise
                    # Preserve the entire current source; old reference facts
                    # are an aid for stable keys, not additional new evidence.
                    payload = dict(payload, known_facts=payload["known_facts"][:len(payload["known_facts"]) // 2])
                    messages[-1] = Message(role="user", content=json.dumps(payload, ensure_ascii=False))
        if consume:
            consume()
        operation = request_chat(provider, phase="memory", contexts=messages, func_tool=None,
            response_format={"type": "json_schema", "json_schema": {"name": "memory_job", "strict": True, "schema": schema}},
            max_tokens=max_tokens, temperature=0.0, request_max_retries=1)
        response = await wait(operation) if wait else await asyncio.wait_for(operation, 60)
        if response.tools_call_name:
            raise ValueError("memory_unexpected_tool_call")
        if getattr(response, "raw_completion", None):
            choices = getattr(response.raw_completion, "choices", None)
            if choices and choices[0].finish_reason in {"length", "content_filter"}:
                raise ValueError("memory_incomplete_output")
        value = json.loads(response.completion_text or "")
        jsonschema.validate(value, schema)
        return value

    async def _compress(self, provider, memories, target, *, wait=None, consume=None):
        from .context_budget import ContextCapacityError
        try:
            return (await self._utility(provider, load_prompts()["memory_compress"],
                {"memories": memories, "target_tokens": target}, COMPACT_SCHEMA,
                max_tokens=min(2048, max(128, target // 2)), wait=wait, consume=consume))["summary"]
        except ContextCapacityError:
            if len(memories) <= 1:
                raise
            # One logical compaction may need several bounded read-only calls.
            # No input facts are lost just to fit the compressor's own window.
            middle = len(memories) // 2
            left = await self._compress(provider, memories[:middle], max(64, target // 2), wait=wait, consume=consume)
            right = await self._compress(provider, memories[middle:], max(64, target // 2), wait=wait, consume=consume)
            return left + "\n" + right

    async def extract_one(self, provider):
        source = self.repository.claim_source(character=self.character)
        if not source:
            return False
        try:
            text = json.loads(source["payload"])["text"]
            if not data_safe(text):
                self.repository.save_extraction(source, [], {})
                return True
            payload = {"message": text, "speaker": source["sender_key"],
                "recorded_at": datetime.fromtimestamp(source["created_at"], self.zone).isoformat(),
                "known_facts": self.repository.known(source["owner_key"], source["sender_key"], self.character)}
            value = await self._utility(provider, load_prompts()["memory_extract"], payload, FACT_SCHEMA)
            facts = [f for f in value["facts"] if f["confidence"] >= 0.65 and f["summary"].strip()
                     and f["evidence"].strip() and f["evidence"].strip() in text
                     and re.fullmatch(r"[\w.-]{1,80}", f["key"]) and data_safe(f["summary"])
                     and all(data_safe(tag) for tag in f["tags"])]
            if facts:
                # A second, independent task checks meaning and permanence. An
                # exact quote alone cannot detect a reversed subject or invented cause.
                review = await self._utility(provider, load_prompts()["memory_verify"],
                    {**payload, "candidates": facts}, VERIFY_SCHEMA)
                decisions = review["decisions"]
                if (len(decisions) != len(facts)
                        or {d["index"] for d in decisions} != set(range(len(facts)))):
                    raise ValueError("memory_incomplete_verification")
                known_keys = {r["fact_key"] for r in payload["known_facts"]}
                accepted = []
                for decision in decisions:
                    if not decision["supported"] or not decision["durable"]:
                        continue
                    fact = dict(facts[decision["index"]])
                    existing = decision["existing_key"]
                    if existing is not None:
                        if existing not in known_keys or ":" in existing:
                            raise ValueError("memory_invalid_existing_key")
                        fact["key"] = existing
                    elif fact["key"] in known_keys:
                        # Do not overwrite an unrelated known property just
                        # because extraction reused its name.
                        continue
                    accepted.append(fact)
                facts = accepted
            topics = json.loads(load_prompts()["memory_shared_topics"])
            self.repository.save_extraction(source, facts, topics)
            return True
        except asyncio.CancelledError:
            self.repository.fail_source(source["id"], "canceled")
            raise
        except Exception as exc:
            self.repository.fail_source(source["id"], type(exc).__name__)
            if self.logger:
                self.logger.warning("Q5 extraction deferred: %s", type(exc).__name__)
            return False

    async def tick(self):
        if self.closed or not self.enabled or self.lock.locked():
            return
        async with self.lock:
            task = asyncio.current_task()
            self.running_tasks.add(task)
            try:
                provider = await self.provider_factory()
                if provider is None:
                    return
                # The whole background job holds a normal Broker lease. It
                # waits through cover/vision/training and never starts a model.
                async with self.admission():
                    for _ in range(16):
                        if not await self.extract_one(provider):
                            break
                    if self.diary_enabled:
                        from .diary_jobs import due_dates
                        now = datetime.now(self.zone)
                        for account in self.repository.accounts(self.character):
                            completed = 0
                            for day in due_dates(account["first_seen"], now, self.diary_hour, self.diary_minute):
                                if not self.repository.diary(account["bot"], self.character, day.isoformat()):
                                    await self.generate_diary(provider, account["bot"], day)
                                    completed += 1
                                    if completed >= 7:
                                        break  # bounded catch-up; next tick continues
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.logger:
                    self.logger.warning("Q5 background job deferred: %s", type(exc).__name__)
            finally:
                self.running_tasks.discard(task)

    async def prepare(self, messages, provider, scope, tools=None, *, wait=None, consume=None, record=None):
        try:
            return await self._prepare(messages, provider, scope, tools, wait=wait, consume=consume, record=record)
        except (sqlite3.Error, OSError) as exc:
            if self.logger:
                self.logger.warning("Q5 recall unavailable: %s", type(exc).__name__)
            return messages

    async def _prepare(self, messages, provider, scope, tools=None, *, wait=None, consume=None, record=None):
        """Inject only into a copied CURRENT user payload, never durable history."""
        from astrbot.core.agent.message import Message
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
        rows = [r for r in self.repository.retrieve(scope, payload["current_message"].get("text", ""), character=self.character)
                if data_safe(r["summary"])]
        result = list(messages)
        result[current] = messages[current].model_copy(deep=True)
        self._replace_payload(result[current], payload, part_index)
        if not rows:
            return result
        budget = RequestBudget(provider, "agent")
        if budget.window <= 0:
            return result  # No declared context window means no guessed allowance.
        # Reserve system/tool schemas plus the entire current user/tool round;
        # older complete rounds can subsequently be dropped by RequestBudget.
        systems = [m for m in messages[:current] if m.role == "system"]
        base_current = messages[current].model_copy(deep=True)
        self._replace_payload(base_current, payload, part_index)
        base_tokens = await budget.count(systems + [base_current] + messages[current + 1:], tools)
        limit = budget.window - budget.output_tokens - budget.margin_tokens
        allowance = max(0, min(budget.window // 4, limit - base_tokens))
        if allowance < 128:
            return result

        async def size(items):
            return await budget.count([Message(role="user", content=json.dumps(items, ensure_ascii=False))])

        selected = [memory_view(r) for r in rows]
        compression_error = None
        # Count the envelope as well as fact text. The final full-request fit is
        # still authoritative and includes actual tool schemas and template.
        if await size(selected) > allowance:
            key, compact = self.repository.compacted(scope, self.character, rows, allowance)
            if compact is None:
                try:
                    summary = await self._compress(provider,
                        [{k: item[k] for k in ("summary", "source_type", "recorded_at", "confidence", "visibility")}
                         for item in selected], max(64, allowance - 128), wait=wait, consume=consume)
                    if not summary.strip() or not data_safe(summary):
                        raise ValueError("invalid_memory_compaction")
                    compact = [{"id": key, "summary": summary, "source_type": "compressed_memory",
                                "source_kinds": sorted({r["source_type"] for r in rows}),
                                "visibility": "current_session_only"}]
                    if await size(compact) <= allowance:
                        self.repository.save_compaction(key, scope, self.character, compact, [r["id"] for r in rows])
                    else:
                        compact = None
                except Exception as exc:
                    # A failed read-only compression cannot block ordinary chat.
                    # Global cancellation/deadline still propagates below.
                    from .agent_state import Superseded, BudgetExhausted
                    if isinstance(exc, (Superseded, BudgetExhausted, TimeoutError)):
                        raise
                    compression_error = type(exc).__name__
                    if self.logger:
                        self.logger.warning("Q5 compression deferred: %s", compression_error)
                    compact = None
            if compact is not None:
                selected = compact
            else:
                # Keep complete high-ranked facts, never cut JSON or invent a
                # summary when the model failed. Original records stay intact.
                while selected and await size(selected) > allowance:
                    selected.pop()
        payload["relevant_memories"] = selected
        self._replace_payload(result[current], payload, part_index)
        if record:
            record({"allowance_tokens": allowance, "retrieved": len(rows), "injected": len(selected),
                    "compressed": bool(selected and selected[0].get("source_type") == "compressed_memory"),
                    "compression_error": compression_error})
        return result

    @staticmethod
    def _replace_payload(message, payload, part_index):
        raw = json.dumps(payload, ensure_ascii=False)
        if part_index is None:
            message.content = raw
        else:
            message.content[part_index].text = raw

    async def generate_diary(self, provider, bot, day):
        from .diary_jobs import diary_window
        key = f"diary:{bot}:{self.character}:{day.isoformat()}"
        if self.repository.diary(bot, self.character, day.isoformat()) or not self.repository.claim_job(key):
            return False
        try:
            start, end = diary_window(day, self.zone, self.diary_hour, self.diary_minute)
            if self.repository.sources_pending(bot, self.character, end.timestamp()):
                self.repository.finish_job(key, "pending_extraction")
                return False
            private = self.repository.diary_material(bot, self.character, start.timestamp(), end.timestamp())
            public = self.repository.diary_material(bot, self.character, start.timestamp(), end.timestamp(), shared=True)
            persona = await self.persona_factory() if self.persona_factory else ""
            prompt = load_prompts()["diary_generate"]
            payload = {"date": day.isoformat(), "timezone": str(self.zone), "persona": persona,
                       "materials": [memory_view(r) for r in private if data_safe(r["summary"])]}
            draft = (await self._utility(provider, prompt, payload, DIARY_SCHEMA, max_tokens=1024))["draft"]
            if not data_safe(draft):
                raise ValueError("invalid_diary")
            public_draft = None
            if public:
                # Separate request, no private facts, IDs, source refs or draft.
                public_payload = {"date": day.isoformat(), "timezone": str(self.zone), "persona": persona,
                    "materials": [{"summary": r["summary"], "source_type": "processed_summary"} for r in public]}
                public_draft = (await self._utility(provider, load_prompts()["diary_public"],
                    public_payload, DIARY_SCHEMA, max_tokens=1024))["draft"]
                if not data_safe(public_draft) or re.search(r"https?://|[0-9]{5,}|qq[:：]", public_draft, re.I):
                    raise ValueError("invalid_public_diary")
            result = self.repository.save_diary(bot, self.character, day.isoformat(), draft, public_draft, private)
            self.repository.finish_job(key)
            return result
        except asyncio.CancelledError:
            self.repository.finish_job(key, "canceled")
            raise
        except Exception as exc:
            self.repository.finish_job(key, type(exc).__name__)
            if self.logger:
                self.logger.warning("Q5 diary deferred: %s", type(exc).__name__)
            return False
