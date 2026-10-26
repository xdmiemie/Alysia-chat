"""QQ text bridge with local policy, durable deduplication and bounded work."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import sqlite3
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from runtime.core import AlysiaCore, UnifiedMessage
from runtime.qq_adapter.onebot import OneBotClient, OneBotMessage
from runtime.qwen3.config import Qwen3Config
from runtime.qwen3.service import Qwen3ChatSession
from runtime.qq_adapter.agent import AgentSession, split_sentences

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TimingPolicy:
    enabled: bool = True
    mode: str = "smart"
    instant: tuple[float, float] = (0.2, 0.7)
    quick: tuple[float, float] = (0.7, 1.8)
    normal: tuple[float, float] = (1.5, 3.5)
    thoughtful: tuple[float, float] = (3.0, 6.0)
    max_delay: float = 8.0

    def __post_init__(self) -> None:
        if self.mode not in {"smart", "random"} or not 0 <= self.max_delay <= 30:
            raise ValueError("invalid timing mode or cap")
        for name in ("instant", "quick", "normal", "thoughtful"):
            low, high = getattr(self, name)
            if not 0 <= low <= high <= self.max_delay:
                raise ValueError(f"invalid timing range: {name}")

    def delay(self, pace: str) -> float:
        if not self.enabled:
            return 0.0
        selected = getattr(self, pace if self.mode == "smart" else "normal")
        return random.uniform(*selected)

    @staticmethod
    def typing_seconds(content: str) -> float:
        def chinese(character: str) -> bool:
            code = ord(character)
            return (0x3400 <= code <= 0x9FFF or 0x20000 <= code <= 0x2FA1F)
        return sum(0.4 if chinese(character) else 0.05 for character in content)

    def target_seconds(self, content: str, pace: str) -> float:
        if not self.enabled:
            return 0.0
        return self.delay(pace) + self.typing_seconds(content)

    def send_due_at(self, anchor: float, calculation_at: float, content: str, pace: str) -> float:
        return max(anchor + self.target_seconds(content, pace), calculation_at)


@dataclass(frozen=True)
class ChatPolicy:
    # With no local policy, the current QQ friend list is the private whitelist.
    private_users: frozenset[int] = frozenset()
    groups: frozenset[int] = frozenset()
    use_friend_list: bool = True
    group_requires_at: bool = True
    disabled_users: frozenset[int] = frozenset()
    disabled_groups: frozenset[int] = frozenset()
    user_characters: dict[int, str] = field(default_factory=dict)
    group_characters: dict[int, str] = field(default_factory=dict)
    max_message_length: int = 2000
    min_interval_seconds: float = 1.0
    max_queue: int = 64
    task_timeout_seconds: float = 150.0
    character: str = "elysia"
    max_turn_messages: int = 3
    max_turn_characters: int = 500
    max_reply_length: int = 250
    max_turn_seconds: float = 120.0
    always_reply: bool = False
    busy: bool = False
    timing: TimingPolicy = field(default_factory=TimingPolicy)

    @classmethod
    def load(cls, path: Path) -> "ChatPolicy":
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError("QQ policy must be a JSON object")
        def ids(key: str) -> frozenset[int]:
            return frozenset(int(x) for x in raw.get(key, []))
        def roles(key: str) -> dict[int, str]:
            return {int(k): str(v) for k, v in raw.get(key, {}).items()}
        policy = cls(
            private_users=ids("private_users"), groups=ids("groups"),
            use_friend_list=bool(raw.get("use_friend_list", True)),
            group_requires_at=bool(raw.get("group_requires_at", True)),
            disabled_users=ids("disabled_users"), disabled_groups=ids("disabled_groups"),
            user_characters=roles("user_characters"), group_characters=roles("group_characters"),
            max_message_length=int(raw.get("max_message_length", 2000)),
            min_interval_seconds=float(raw.get("min_interval_seconds", 1)),
            max_queue=int(raw.get("max_queue", 64)),
            task_timeout_seconds=float(raw.get("task_timeout_seconds", 150)),
            character=str(raw.get("character", "elysia")),
            max_turn_messages=int(raw.get("max_turn_messages", 3)),
            max_turn_characters=int(raw.get("max_turn_characters", 500)),
            max_reply_length=int(raw.get("max_reply_length", 250)),
            max_turn_seconds=float(raw.get("max_turn_seconds", 120)),
            always_reply=bool(raw.get("always_reply", False)),
            busy=bool(raw.get("busy", False)),
            timing=TimingPolicy(**{key: tuple(value) if key in {"instant", "quick", "normal", "thoughtful"} else value
                                   for key, value in raw.get("timing", {}).items() if key != "continuation"}),
        )
        if (policy.max_message_length < 1 or policy.max_queue < 1 or policy.task_timeout_seconds <= 0
                or policy.min_interval_seconds < 0 or not 1 <= policy.max_turn_messages <= 5
                or policy.max_reply_length < 1 or policy.max_turn_characters < policy.max_reply_length
                or policy.max_turn_seconds <= 0):
            raise ValueError("invalid QQ policy limits")
        return policy


def onebot_to_unified(event: OneBotMessage, bot_id: int, character: str) -> UnifiedMessage:
    if event.message_type == "group" and event.group_id is None:
        raise ValueError("group message has no group_id")
    target = event.user_id if event.message_type == "private" else event.group_id
    text_parts: list[str] = []
    for segment in event.segments:
        data = segment.get("data")
        if not isinstance(data, dict):
            continue
        if segment.get("type") == "text":
            text_parts.append(str(data.get("text", "")))
        elif segment.get("type") == "face":
            text_parts.append(f"[表情:{data.get('id', '')}]")
    return UnifiedMessage(
        request_id=f"qq-{bot_id}-{event.message_id}", channel="qq",
        session_id=f"qq:{event.message_type}:{bot_id}:{target}",
        sender_id=str(event.user_id), sender_name=event.sender_name,
        message_type="text", text="".join(text_parts), character_id=character,
        metadata={"bot_id": bot_id, "message_id": event.message_id,
                  "time": event.time, "segments": list(event.segments),
                  "sub_type": event.sub_type},
    )


class MessageStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS messages (
            bot_id INTEGER NOT NULL, message_id TEXT NOT NULL, request_id TEXT NOT NULL,
            session_id TEXT NOT NULL, sender_id TEXT NOT NULL, received_at REAL NOT NULL,
            event_json TEXT NOT NULL, state TEXT NOT NULL, inference_ms INTEGER,
            character_id TEXT, model_version TEXT, sent_message_id TEXT, error TEXT,
            PRIMARY KEY (bot_id, message_id))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS outbound_messages (
            bot_id INTEGER NOT NULL, incoming_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
            content TEXT NOT NULL, state TEXT NOT NULL, sent_message_id TEXT,
            generation_ready_at REAL, send_due_at REAL, sent_at REAL,
            PRIMARY KEY (bot_id, incoming_id, ordinal))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS decisions (
            bot_id INTEGER NOT NULL, incoming_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
            action TEXT NOT NULL, reason_code TEXT NOT NULL, pace TEXT NOT NULL,
            inference_ms INTEGER NOT NULL, created_at REAL NOT NULL,
            prompt_tokens INTEGER, completion_tokens INTEGER, first_token_estimate_ms REAL,
            PRIMARY KEY (bot_id, incoming_id, ordinal))""")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(decisions)")}
        for name in ("prompt_tokens", "completion_tokens", "first_token_estimate_ms"):
            if name not in columns:
                self.db.execute(f"ALTER TABLE decisions ADD COLUMN {name} {'REAL' if name.endswith('_ms') else 'INTEGER'}")
        self.recover_incomplete()

    def recover_incomplete(self) -> None:
        # A crashed in-flight send cannot safely be repeated.
        self.db.execute("UPDATE messages SET state='unknown' WHERE state IN ('generating','pending_send')")
        self.db.execute("UPDATE outbound_messages SET state='unknown' WHERE state='pending_send'")
        self.db.execute("UPDATE outbound_messages SET state='cancelled' WHERE state='scheduled'")
        self.db.execute("UPDATE messages SET state='failed', error='interrupted before generation' WHERE state IN ('received','queued')")
        self.db.commit()

    def reserve(self, message: UnifiedMessage) -> bool:
        try:
            self.db.execute(
                "INSERT INTO messages (bot_id,message_id,request_id,session_id,sender_id,received_at,event_json,state,character_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (message.metadata["bot_id"], str(message.metadata["message_id"]), message.request_id,
                 message.session_id, message.sender_id, time.time(),
                 json.dumps(message.to_dict(), ensure_ascii=False), "received", message.character_id),
            )
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def update(self, message: UnifiedMessage, state: str, **fields: Any) -> None:
        allowed = {"inference_ms", "character_id", "model_version", "sent_message_id", "error"}
        if not set(fields) <= allowed:
            raise ValueError("invalid message state field")
        columns = ["state=?"] + [f"{key}=?" for key in fields]
        self.db.execute(
            f"UPDATE messages SET {', '.join(columns)} WHERE bot_id=? AND message_id=?",
            [state, *fields.values(), message.metadata["bot_id"], str(message.metadata["message_id"])],
        )
        self.db.commit()

    def record_decision(self, message: UnifiedMessage, ordinal: int, action: Any, inference_ms: int,
                        usage: dict[str, int] | None = None,
                        timings: dict[str, float] | None = None) -> None:
        usage = usage or {}
        timings = timings or {}
        first_token_estimate = (timings.get("prompt_ms", 0) + timings.get("predicted_per_token_ms", 0)) if timings else None
        self.db.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (self._bot(message), self._id(message), ordinal, action.action,
                         action.reason_code, action.pace, inference_ms, time.time(),
                         usage.get("prompt_tokens"), usage.get("completion_tokens"), first_token_estimate))
        self.db.commit()

    def record_outbound(self, message: UnifiedMessage, ordinal: int, content: str,
                        state: str, ready_at: float, due_at: float) -> None:
        self.db.execute("INSERT INTO outbound_messages VALUES (?,?,?,?,?,?,?,?,?)",
                        (self._bot(message), self._id(message), ordinal, content, state,
                         None, ready_at, due_at, None))
        self.db.commit()

    def update_outbound(self, message: UnifiedMessage, ordinal: int, state: str,
                        sent_id: str | None = None) -> None:
        self.db.execute("UPDATE outbound_messages SET state=?, sent_message_id=?, sent_at=? "
                        "WHERE bot_id=? AND incoming_id=? AND ordinal=?",
                        (state, sent_id, time.time() if state == "sent" else None,
                         self._bot(message), self._id(message), ordinal))
        self.db.commit()

    @staticmethod
    def _bot(message: UnifiedMessage) -> int:
        return int(message.metadata["bot_id"])

    @staticmethod
    def _id(message: UnifiedMessage) -> str:
        return str(message.metadata["message_id"])


class QQChatBridge:
    def __init__(self, bot_id: int, policy: ChatPolicy, store: MessageStore, qwen_config: Qwen3Config,
                 model_version: str) -> None:
        self.bot_id, self.policy, self.store = bot_id, policy, store
        self.model_version = model_version
        self.friends: set[int] = set()
        self.last_accept: dict[str, float] = {}
        self.queue: asyncio.Queue[UnifiedMessage] = asyncio.Queue(maxsize=policy.max_queue)
        self.cores: dict[str, AlysiaCore] = {}
        self.qwen_config = qwen_config
        self.agents: dict[tuple[str, str], AgentSession] = {}
        self.generations: dict[str, int] = {}
        self.session_locks: dict[str, asyncio.Lock] = {}
        self.received_at: dict[str, float] = {}
        self.message_generations: dict[str, int] = {}
        self.session_states: dict[str, str] = {}
        self.active_sessions: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._capacity = asyncio.Semaphore(4)

    def _agent(self, message: UnifiedMessage) -> AgentSession:
        key = (message.character_id, message.session_id)
        if key not in self.agents:
            root = Path("configs/characters") / message.character_id
            if not (root / "character.yaml").is_file():
                raise ValueError(f"unknown character: {message.character_id}")
            config = replace(self.qwen_config, character_config=root / "character.yaml",
                             system_prompt=root / "system_prompt.txt", examples_path=root / "examples.jsonl")
            self.agents[key] = AgentSession(config)
        return self.agents[key]

    def _core(self, character: str) -> AlysiaCore:
        if character not in self.cores:
            root = Path("configs/characters") / character
            if not (root / "character.yaml").is_file():
                raise ValueError(f"unknown character: {character}")
            config = replace(self.qwen_config, character_config=root / "character.yaml",
                             system_prompt=root / "system_prompt.txt", examples_path=root / "examples.jsonl")
            self.cores[character] = AlysiaCore(
                transcriber=None, synthesizer=None, output_dir="storage/generated_audio",
                chat_session_factory=lambda _session: Qwen3ChatSession.from_config(config),
            )
        return self.cores[character]

    async def load_friends(self, client: OneBotClient) -> None:
        if not self.policy.use_friend_list:
            return
        data = await client.call_api("get_friend_list")
        if not isinstance(data, list):
            raise ValueError("get_friend_list returned invalid data")
        self.friends = {int(item["user_id"]) for item in data if isinstance(item, dict) and item.get("user_id")}
        LOGGER.info("Loaded %d QQ friends into private whitelist", len(self.friends))

    def accept(self, event: OneBotMessage) -> UnifiedMessage | None:
        if self.policy.busy or event.user_id == self.bot_id or (event.self_id is not None and event.self_id != self.bot_id):
            return None
        if event.message_type == "private":
            if event.user_id in self.policy.disabled_users or event.user_id not in (self.policy.private_users | self.friends):
                return None
            character = self.policy.user_characters.get(event.user_id, self.policy.character)
        else:
            group = event.group_id
            if group is None or group in self.policy.disabled_groups or group not in self.policy.groups:
                return None
            if self.policy.group_requires_at and not any(
                item.get("type") == "at" and str((item.get("data") or {}).get("qq")) == str(self.bot_id)
                for item in event.segments
            ):
                return None
            character = self.policy.group_characters.get(group, self.policy.character)
        try:
            message = onebot_to_unified(event, self.bot_id, character)
        except (ValueError, TypeError, KeyError):
            LOGGER.warning("Ignoring malformed QQ message")
            return None
        if not (message.text or "").strip() or len(message.text or "") > self.policy.max_message_length:
            return None
        silence = (message.text or "").strip().rstrip("。.!！ ") in {
            "别回复了", "不要回复了", "不用回复", "别回了", "先别说话"}
        now = time.monotonic()
        if (not silence and message.session_id not in self.active_sessions
                and now - self.last_accept.get(message.session_id, -float("inf")) < self.policy.min_interval_seconds):
            return None
        if not self.store.reserve(message):
            return None
        self.last_accept[message.session_id] = now
        if silence:
            self.generations[message.session_id] = self.generations.get(message.session_id, 0) + 1
            self.active_sessions.discard(message.session_id)
            self.store.update(message, "no_reply", error="user_requested_silence")
            return None
        if self.queue.full():
            self.store.update(message, "failed", error="queue full")
            return None
        self.store.update(message, "queued")
        self.generations[message.session_id] = self.generations.get(message.session_id, 0) + 1
        self.message_generations[message.request_id] = self.generations[message.session_id]
        self.active_sessions.add(message.session_id)
        self.received_at[message.request_id] = now
        self.queue.put_nowait(message)
        return message

    async def worker(self, client: OneBotClient) -> None:
        try:
            while True:
                message = await self.queue.get()
                generation = self.message_generations[message.request_id]
                task = asyncio.create_task(self._run_message(client, message, generation))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        finally:
            for task in tuple(self._tasks):
                task.cancel()
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            self.store.recover_incomplete()

    async def _run_message(self, client: OneBotClient, message: UnifiedMessage, generation: int) -> None:
        try:
            async with self._capacity:
                lock = self.session_locks.setdefault(message.session_id, asyncio.Lock())
                async with lock:
                    if "_core" not in self.__dict__ and generation != self.generations[message.session_id]:
                        self.store.update(message, "cancelled")
                        return
                    if "_core" in self.__dict__:
                        await self._run_legacy(client, message)
                    else:
                        await self._run_agent(client, message, generation)
        except asyncio.CancelledError:
            state = self.store.db.execute("SELECT state FROM messages WHERE bot_id=? AND message_id=?",
                                          (self.bot_id, str(message.metadata["message_id"]))).fetchone()[0]
            self.store.update(message, "unknown" if state == "pending_send" else "cancelled")
            raise
        except Exception as exc:
            self.store.update(message, "failed", error=type(exc).__name__)
            LOGGER.exception("QQ message failed: %s", message.request_id)
        finally:
            self.received_at.pop(message.request_id, None)
            self.message_generations.pop(message.request_id, None)
            if self.generations.get(message.session_id) == generation:
                self.active_sessions.discard(message.session_id)
            self.queue.task_done()

    async def _send(self, client: OneBotClient, message: UnifiedMessage, content: str) -> Any:
        if message.session_id.startswith("qq:private:"):
            return await client.send_private_message(int(message.sender_id), content)
        return await client.send_group_message(int(message.session_id.rsplit(":", 1)[1]), content)

    async def _run_agent(self, client: OneBotClient, message: UnifiedMessage, generation: int) -> None:
        self.session_states[message.session_id] = "EVALUATING"
        self.store.update(message, "generating", model_version=self.model_version)
        sent: list[str] = []
        started = time.monotonic()
        last_sent_at = started
        agent = self._agent(message)
        final_state = "no_reply"
        try:
            decision_index = 0
            while len(sent) < self.policy.max_turn_messages:
                if generation != self.generations[message.session_id]:
                    final_state = "cancelled"
                    break
                if time.monotonic() - started >= self.policy.max_turn_seconds:
                    final_state = "sent" if sent else "failed"
                    break
                before = time.monotonic()
                self.session_states[message.session_id] = "GENERATING"
                action = await asyncio.wait_for(
                    asyncio.to_thread(agent.decide, message.text or "", tuple(sent),
                                      min(self.policy.max_reply_length,
                                          self.policy.max_turn_characters - sum(map(len, sent))),
                                      force_reply=self.policy.always_reply),
                    timeout=min(self.policy.task_timeout_seconds,
                                self.policy.max_turn_seconds - (before - started)),
                )
                elapsed_ms = round((time.monotonic() - before) * 1000)
                client_usage = getattr(getattr(agent, "client", None), "last_usage", None)
                client_timings = getattr(getattr(agent, "client", None), "last_timings", None)
                self.store.record_decision(message, decision_index, action, elapsed_ms,
                                           client_usage, client_timings)
                decision_index += 1
                if action.action == "finish_turn":
                    final_state = "sent" if sent else "no_reply"
                    break
                stop_turn = False
                for sentence in split_sentences(action.content):
                    if (len(sent) >= self.policy.max_turn_messages
                            or sum(map(len, sent)) + len(sentence) > self.policy.max_turn_characters):
                        stop_turn = True
                        break
                    if generation != self.generations[message.session_id]:
                        final_state = "cancelled"
                        break
                    # Elapsed runtime is measured up to this calculation; any excess
                    # over thinking + typing time means the sentence is ready now.
                    ready_mono = time.monotonic()
                    ready_wall = time.time()
                    anchor = self.received_at.get(message.request_id, started) if not sent else last_sent_at
                    due_mono = self.policy.timing.send_due_at(anchor, ready_mono, sentence, action.pace)
                    if due_mono > started + self.policy.max_turn_seconds:
                        final_state = "sent" if sent else "failed"
                        stop_turn = True
                        break
                    ordinal = len(sent)
                    due_wall = ready_wall + max(0, due_mono - ready_mono)
                    self.store.record_outbound(message, ordinal, sentence, "scheduled", ready_wall, due_wall)
                    self.session_states[message.session_id] = "PENDING_SEND"
                    while time.monotonic() < due_mono:
                        await asyncio.sleep(min(0.05, due_mono - time.monotonic()))
                        if generation != self.generations[message.session_id]:
                            break
                    # Let a newly received user message invalidate even an immediate send.
                    await asyncio.sleep(0)
                    if generation != self.generations[message.session_id]:
                        self.store.update_outbound(message, ordinal, "cancelled")
                        final_state = "cancelled"
                        break
                    self.store.update_outbound(message, ordinal, "pending_send")
                    self.store.update(message, "pending_send", inference_ms=elapsed_ms)
                    try:
                        result = await self._send(client, message, sentence)
                    except Exception as exc:
                        self.store.update_outbound(message, ordinal, "unknown")
                        self.store.update(message, "unknown", error=type(exc).__name__)
                        final_state = "unknown"
                        return
                    sent_id = result.get("message_id") if isinstance(result, dict) else None
                    self.store.update_outbound(message, ordinal, "sent", str(sent_id) if sent_id is not None else "")
                    sent.append(sentence)
                    last_sent_at = time.monotonic()
                    self.store.update(message, "sent", sent_message_id=str(sent_id) if sent_id is not None else "")
                    final_state = "sent"
                if (stop_turn or final_state in {"cancelled", "failed"}
                        or len(sent) >= self.policy.max_turn_messages
                        or sum(map(len, sent)) >= self.policy.max_turn_characters
                        or not action.continue_turn):
                    break
                self.session_states[message.session_id] = "WAITING_CONTINUATION"
            self.store.update(message, final_state)
        except Exception as exc:
            if isinstance(exc, asyncio.TimeoutError):
                self.agents.pop((message.character_id, message.session_id), None)
            final_state = "sent" if sent else "failed"
            self.store.update(message, final_state, error=type(exc).__name__)
            LOGGER.exception("QQ Agent decision failed: %s", message.request_id)
        finally:
            if sent:
                agent.commit(message.text or "", tuple(sent))
            self.session_states[message.session_id] = "WAITING_USER" if final_state in {"sent", "no_reply"} else "IDLE"

    async def _run_legacy(self, client: OneBotClient, message: UnifiedMessage) -> None:
        """Compatibility seam for existing Core integrations and tests."""
        self.store.update(message, "generating")
        reply = await asyncio.wait_for(asyncio.to_thread(self._core(message.character_id).process_text, message),
                                       timeout=self.policy.task_timeout_seconds)
        if not reply.text:
            self.store.update(message, "failed", error=reply.error)
            return
        self.store.update(message, "pending_send", inference_ms=reply.timings.llm_total_ms,
                          model_version=self.model_version)
        try:
            result = await self._send(client, message, reply.text)
        except Exception as exc:
            self.store.update(message, "unknown", error=type(exc).__name__)
            return
        sent_id = result.get("message_id") if isinstance(result, dict) else None
        self.store.update(message, "sent", sent_message_id=str(sent_id) if sent_id is not None else "")


class ConversationOrchestrator(QQChatBridge):
    """Entry point for QQ conversation events; future stages add timer/tool producers."""

    def handle_event(self, kind: str, event: OneBotMessage | None = None) -> UnifiedMessage | None:
        if kind == "incoming_message" and event is not None:
            return self.accept(event)
        if kind in {"timer_due", "tool_result"}:
            # Stage 13C/13D will register these event sources.
            return None
        raise ValueError("invalid conversation event")
