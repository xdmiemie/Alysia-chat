"""QQ text bridge with local policy, durable deduplication and bounded work."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from runtime.core import AlysiaCore, UnifiedMessage
from runtime.qq_adapter.onebot import OneBotClient, OneBotMessage
from runtime.qwen3.config import Qwen3Config
from runtime.qwen3.service import Qwen3ChatSession

LOGGER = logging.getLogger(__name__)


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
        )
        if policy.max_message_length < 1 or policy.max_queue < 1 or policy.task_timeout_seconds <= 0 or policy.min_interval_seconds < 0:
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
        self.recover_incomplete()

    def recover_incomplete(self) -> None:
        # A crashed in-flight send cannot safely be repeated.
        self.db.execute("UPDATE messages SET state='unknown' WHERE state IN ('generating','pending_send')")
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
        if event.user_id == self.bot_id or (event.self_id is not None and event.self_id != self.bot_id):
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
        now = time.monotonic()
        if now - self.last_accept.get(message.session_id, -float("inf")) < self.policy.min_interval_seconds:
            return None
        if not self.store.reserve(message):
            return None
        self.last_accept[message.session_id] = now
        if self.queue.full():
            self.store.update(message, "failed", error="queue full")
            return None
        self.store.update(message, "queued")
        self.queue.put_nowait(message)
        return message

    async def worker(self, client: OneBotClient) -> None:
        while True:
            message = await self.queue.get()
            try:
                self.store.update(message, "generating")
                reply = await asyncio.wait_for(
                    asyncio.to_thread(self._core(message.character_id).process_text, message),
                    timeout=self.policy.task_timeout_seconds,
                )
                if not reply.ok:
                    LOGGER.warning("QQ inference failed for %s; sending fallback", message.request_id)
                    if not reply.text:
                        self.store.update(message, "failed", error=reply.error)
                        continue
                self.store.update(message, "pending_send", inference_ms=reply.timings.llm_total_ms,
                                  model_version=self.model_version,
                                  error=reply.error if not reply.ok else "")
                try:
                    if message.session_id.startswith("qq:private:"):
                        result = await client.send_private_message(int(message.sender_id), reply.text)
                    else:
                        result = await client.send_group_message(int(message.session_id.rsplit(":", 1)[1]), reply.text)
                except Exception as exc:
                    self.store.update(message, "unknown", error=type(exc).__name__)
                    LOGGER.warning("QQ send outcome unknown for %s", message.request_id)
                    continue
                sent_id = result.get("message_id") if isinstance(result, dict) else None
                self.store.update(message, "sent", sent_message_id=str(sent_id) if sent_id is not None else "")
                LOGGER.info("QQ reply sent for %s", message.request_id)
            except Exception as exc:
                self.store.update(message, "failed", error=type(exc).__name__)
                LOGGER.exception("QQ message failed: %s", message.request_id)
            finally:
                self.queue.task_done()
