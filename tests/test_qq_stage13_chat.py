from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from runtime.core.models import CoreTiming, AlysiaReply
from runtime.qq_adapter.chat import ChatPolicy, MessageStore, QQChatBridge, onebot_to_unified
from runtime.qq_adapter.onebot import OneBotMessage
from runtime.qwen3.config import Qwen3Config
from scripts.qq_chat import local_onebot_config


def event(number: int, *, kind: str = "private", user: int = 123, group: int = 456,
          at: bool = False, self_id: int = 999) -> OneBotMessage:
    segments = [{"type": "text", "data": {"text": "你好\n世界"}}, {"type": "face", "data": {"id": "14"}}]
    if at:
        segments.insert(0, {"type": "at", "data": {"qq": str(self_id)}})
    return OneBotMessage(kind, user, group if kind == "group" else None, number,
                         tuple(segments), self_id, "Alice", 1234567890)


def test_conversion_and_policy(tmp_path):
    policy = ChatPolicy(private_users=frozenset({123}), groups=frozenset({456}), min_interval_seconds=0)
    store = MessageStore(tmp_path / "messages.sqlite3")
    bridge = QQChatBridge(999, policy, store, Qwen3Config(), "test-v1")
    private = bridge.accept(event(1))
    assert private is not None
    assert private.session_id == "qq:private:999:123"
    assert private.text == "你好\n世界[表情:14]"
    assert private.metadata["message_id"] == 1
    assert private.metadata["time"] == 1234567890
    assert bridge.accept(event(1)) is None
    assert bridge.accept(event(2, user=321)) is None
    assert bridge.accept(event(3, user=999)) is None
    assert bridge.accept(event(4, kind="group")) is None
    group = bridge.accept(event(5, kind="group", at=True))
    assert group is not None and group.session_id == "qq:group:999:456"
    assert group.session_id != private.session_id
    assert bridge.accept(event(6, kind="group", group=999, at=True)) is None


def test_store_deduplicates_after_restart_and_marks_uncertain_send(tmp_path):
    path = tmp_path / "messages.sqlite3"
    store = MessageStore(path)
    message = onebot_to_unified(event(7), 999, "elysia")
    assert store.reserve(message)
    store.update(message, "pending_send")
    store.db.close()
    reopened = MessageStore(path)
    assert not reopened.reserve(message)
    state = reopened.db.execute("SELECT state FROM messages WHERE message_id='7'").fetchone()[0]
    assert state == "unknown"


def test_worker_handles_twenty_messages_and_one_failure(tmp_path):
    class Core:
        calls = 0

        def process_text(self, message):
            self.calls += 1
            if message.metadata["message_id"] == 5:
                raise RuntimeError("model failure")
            return AlysiaReply(
                request_id=message.request_id, session_id=message.session_id, channel="qq", ok=True,
                text="回答", message_type="text", audio_path="", state="COMPLETED",
                states=("COMPLETED",), error_code="", error="", transcription=None, reply=None,
                timings=CoreTiming(llm_total_ms=1),
            )

    class Client:
        sent = []

        async def send_private_message(self, user_id, text):
            self.sent.append((user_id, text))
            return {"message_id": len(self.sent)}

    async def scenario():
        store = MessageStore(tmp_path / "messages.sqlite3")
        bridge = QQChatBridge(999, ChatPolicy(private_users=frozenset({123}),
                                               min_interval_seconds=0), store, Qwen3Config(), "test-v1")
        core = Core()
        bridge._core = lambda _character: core
        client = Client()
        worker = asyncio.create_task(bridge.worker(client))
        try:
            for index in range(20):
                assert bridge.accept(event(index)) is not None
            await asyncio.wait_for(bridge.queue.join(), 5)
        finally:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        assert core.calls == 20
        assert len(client.sent) == 19
        counts = dict(store.db.execute("SELECT state, count(*) FROM messages GROUP BY state"))
        assert counts == {"failed": 1, "sent": 19}

    asyncio.run(scenario())


def test_token_is_read_from_local_config_without_command_line(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "onebot_999.json").write_text(json.dumps({"networks": {"wsServers": [
        {"role": "Universal", "host": "127.0.0.1", "port": 3001,
         "path": "/", "accessToken": "test-secret"}]}}), encoding="utf-8")
    config = local_onebot_config(tmp_path, 999)
    assert config.token == "test-secret"
    assert config.ws_url == "ws://127.0.0.1:3001/"


def test_uncertain_send_is_not_retried_and_next_message_continues(tmp_path):
    class Core:
        def process_text(self, message):
            return AlysiaReply(
                request_id=message.request_id, session_id=message.session_id, channel="qq", ok=True,
                text="回答", message_type="text", audio_path="", state="COMPLETED",
                states=("COMPLETED",), error_code="", error="", transcription=None, reply=None,
                timings=CoreTiming(llm_total_ms=1),
            )

    class Client:
        calls = 0

        async def send_private_message(self, user_id, text):
            self.calls += 1
            if self.calls == 1:
                raise asyncio.TimeoutError()
            return {"message_id": 42}

    async def scenario():
        store = MessageStore(tmp_path / "messages.sqlite3")
        bridge = QQChatBridge(999, ChatPolicy(private_users=frozenset({123}),
                                               min_interval_seconds=0), store, Qwen3Config(), "test-v1")
        bridge._core = lambda _character: Core()
        client = Client()
        worker = asyncio.create_task(bridge.worker(client))
        try:
            bridge.accept(event(1))
            bridge.accept(event(2))
            await asyncio.wait_for(bridge.queue.join(), 5)
            assert bridge.accept(event(1)) is None
        finally:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        assert client.calls == 2
        assert dict(store.db.execute("SELECT message_id, state FROM messages")) == {"1": "unknown", "2": "sent"}

    asyncio.run(scenario())
