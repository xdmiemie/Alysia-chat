from __future__ import annotations

import asyncio
import json
import time

import pytest

from runtime.qq_adapter.agent import (AgentSession, ConversationAction, parse_action,
                                      split_plain_fallback, split_sentences)
from runtime.qq_adapter.chat import ChatPolicy, MessageStore, QQChatBridge, TimingPolicy
from runtime.qq_adapter.onebot import OneBotMessage
from runtime.qwen3.config import Qwen3Config


def event(number: int, text: str = "你好呀，你叫什么名字？", user: int = 123) -> OneBotMessage:
    return OneBotMessage("private", user, None, number, ({"type": "text", "data": {"text": text}},), 999)


class Client:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_private_message(self, user: int, text: str) -> dict:
        self.sent.append(text)
        return {"message_id": len(self.sent)}


def bridge(tmp_path, *, timing: TimingPolicy | None = None) -> QQChatBridge:
    policy = ChatPolicy(private_users=frozenset({123}), min_interval_seconds=0,
                        timing=timing or TimingPolicy(enabled=False))
    return QQChatBridge(999, policy, MessageStore(tmp_path / "messages.sqlite3"), Qwen3Config(), "test")


async def run_worker(chat: QQChatBridge, client: Client) -> None:
    task = asyncio.create_task(chat.worker(client))
    try:
        await asyncio.wait_for(chat.queue.join(), 3)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_two_messages_use_sent_context_and_independent_receipts(tmp_path):
    class Agent:
        history = []
        calls = []

        def decide(self, user, sent, max_length, **kwargs):
            self.calls.append((user, sent))
            if not sent:
                return ConversationAction("send_message", "你好呀。", True, "quick", "greeting")
            return ConversationAction("send_message", "我是爱莉希雅♪", False, "quick", "identity")

        def commit(self, user, sent):
            self.history.append((user, sent))

    async def scenario():
        chat, client, agent = bridge(tmp_path), Client(), Agent()
        chat._agent = lambda _: agent
        chat.accept(event(1))
        await run_worker(chat, client)
        assert client.sent == ["你好呀。", "我是爱莉希雅♪"]
        assert agent.calls == [("你好呀，你叫什么名字？", ()),
                               ("你好呀，你叫什么名字？", ("你好呀。",))]
        assert agent.history == [("你好呀，你叫什么名字？", tuple(client.sent))]
        assert list(chat.store.db.execute("SELECT ordinal,state,sent_message_id FROM outbound_messages ORDER BY ordinal")) == [
            (0, "sent", "1"), (1, "sent", "2")]

    asyncio.run(scenario())


def test_one_candidate_sends_one_sentence_per_receipt(tmp_path):
    original = "故事嘛……让我想想看。那个关于火焰、命运和羁绊的故事……很长很长哦。"
    sentences = ["故事嘛……让我想想看。", "那个关于火焰、命运和羁绊的故事……很长很长哦。"]
    assert split_sentences(original) == sentences

    class Agent:
        calls = 0

        def decide(self, *_args, **_kwargs):
            self.calls += 1
            return ConversationAction("send_message", original, False, "normal", "story")

        def commit(self, _user, sent):
            assert list(sent) == sentences

    async def scenario():
        chat, client, agent = bridge(tmp_path), Client(), Agent()
        chat._agent = lambda _: agent
        chat.accept(event(1, "讲个故事"))
        await run_worker(chat, client)
        assert client.sent == sentences
        assert agent.calls == 1
        assert list(chat.store.db.execute(
            "SELECT ordinal,content,sent_message_id FROM outbound_messages ORDER BY ordinal")) == [
                (0, sentences[0], "1"), (1, sentences[1], "2")]

    asyncio.run(scenario())


def test_interruption_after_first_sentence_cancels_rest(tmp_path):
    class Agent:
        def decide(self, user, *_args, **_kwargs):
            content = "第一句。第二句。" if user == "旧问题" else "新答案。"
            return ConversationAction("send_message", content, False, "quick", "answer")

        def commit(self, *_args):
            pass

    async def scenario():
        chat = bridge(tmp_path)
        chat._agent = lambda _: Agent()

        class InterruptingClient(Client):
            async def send_private_message(self, user, text):
                result = await super().send_private_message(user, text)
                if text == "第一句。":
                    chat.accept(event(2, "新问题"))
                return result

        client = InterruptingClient()
        chat.accept(event(1, "旧问题"))
        await run_worker(chat, client)
        assert client.sent == ["第一句。", "新答案。"]
        assert dict(chat.store.db.execute("SELECT message_id,state FROM messages")) == {
            "1": "cancelled", "2": "sent"}

    asyncio.run(scenario())


def test_thinking_plus_typing_subtracts_elapsed_runtime():
    text = "故事嘛……让我想想看。"
    policy = TimingPolicy(normal=(1.7, 1.7))
    assert policy.typing_seconds(text) == pytest.approx(3.35)
    assert policy.send_due_at(100, 102, text, "normal") == pytest.approx(105.05)
    assert policy.send_due_at(100, 106, text, "normal") == 106
    assert TimingPolicy(enabled=False).send_due_at(100, 102, text, "normal") == 102


def test_each_sentence_uses_previous_send_as_timing_anchor(tmp_path):
    class Agent:
        def decide(self, *_args, **_kwargs):
            return ConversationAction("send_message", "甲。乙。", False, "normal", "answer")

        def commit(self, *_args):
            pass

    class TimedClient(Client):
        sent_at = []

        async def send_private_message(self, user, text):
            self.sent_at.append(time.monotonic())
            return await super().send_private_message(user, text)

    async def scenario():
        zero = TimingPolicy(instant=(0, 0), quick=(0, 0), normal=(0, 0),
                            thoughtful=(0, 0), max_delay=0)
        chat, client = bridge(tmp_path, timing=zero), TimedClient()
        chat._agent = lambda _: Agent()
        chat.accept(event(1))
        await run_worker(chat, client)
        assert client.sent == ["甲。", "乙。"]
        assert client.sent_at[1] - client.sent_at[0] >= 0.4

    asyncio.run(scenario())


def test_finish_first_is_audited_without_send(tmp_path):
    class Agent:
        def decide(self, *_args, **_kwargs):
            return ConversationAction("finish_turn", reason_code="conversation_closed")

        def commit(self, *_args):
            raise AssertionError("nothing was sent")

    async def scenario():
        chat, client = bridge(tmp_path), Client()
        chat._agent = lambda _: Agent()
        chat.accept(event(1, "晚安大肥鱼"))
        await run_worker(chat, client)
        assert not client.sent
        assert chat.store.db.execute("SELECT state FROM messages").fetchone()[0] == "no_reply"
        assert chat.store.db.execute("SELECT reason_code FROM decisions").fetchone()[0] == "conversation_closed"

    asyncio.run(scenario())


def test_endless_continue_is_capped(tmp_path):
    class Agent:
        def decide(self, _user, sent, *_args, **_kwargs):
            return ConversationAction("send_message", f"第{len(sent) + 1}条", True, "quick", "continue")

        def commit(self, *_args):
            pass

    async def scenario():
        chat, client = bridge(tmp_path), Client()
        chat._agent = lambda _: Agent()
        chat.accept(event(1))
        await run_worker(chat, client)
        assert client.sent == ["第1条", "第2条", "第3条"]
        assert chat.store.db.execute("SELECT count(*) FROM decisions").fetchone()[0] == 3

    asyncio.run(scenario())


def test_sentence_splitting_counts_against_message_cap(tmp_path):
    class Agent:
        calls = 0

        def decide(self, *_args, **_kwargs):
            self.calls += 1
            return ConversationAction("send_message", "一。二。三。四。", True, "quick", "story")

        def commit(self, *_args):
            pass

    async def scenario():
        chat, client, agent = bridge(tmp_path), Client(), Agent()
        chat._agent = lambda _: agent
        chat.accept(event(1))
        await run_worker(chat, client)
        assert client.sent == ["一。", "二。", "三。"]
        assert agent.calls == 1

    asyncio.run(scenario())


def test_new_user_message_cancels_scheduled_old_message(tmp_path):
    class Agent:
        def decide(self, user, sent, *_args, **_kwargs):
            return ConversationAction("send_message", user, False, "normal", "answer")

        def commit(self, *_args):
            pass

    async def scenario():
        timing = TimingPolicy(instant=(0, 0), quick=(0, 0), normal=(0.2, 0.2),
                              thoughtful=(0, 0), max_delay=1)
        chat, client = bridge(tmp_path, timing=timing), Client()
        chat._agent = lambda _: Agent()
        worker = asyncio.create_task(chat.worker(client))
        try:
            chat.accept(event(1, "旧问题"))
            await asyncio.sleep(0.05)
            chat.accept(event(2, "新问题"))
            await asyncio.wait_for(chat.queue.join(), 3)
        finally:
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
        assert client.sent == ["新问题"]
        assert dict(chat.store.db.execute("SELECT message_id,state FROM messages")) == {"1": "cancelled", "2": "sent"}

    asyncio.run(scenario())


def test_invalid_actions_and_policy_persistence(tmp_path):
    valid = {"action": "send_message", "content": "你好", "continue_turn": False,
             "pace": "quick", "reason_code": "greeting"}
    assert parse_action(json.dumps(valid), max_length=10, previous=()).content == "你好"
    for bad in ("not json", json.dumps({**valid, "content": ""}),
                json.dumps({**valid, "content": "你好"}),
                json.dumps({**valid, "content": "这句话太长了"}),
                json.dumps({**valid, "action": "call_tool"})):
        with pytest.raises((ValueError, json.JSONDecodeError)):
            parse_action(bad, max_length=4, previous=("你好",))
    config = tmp_path / "local.json"
    config.write_text(json.dumps({"timing": {"mode": "random", "normal": [0.1, 0.2],
                                                  "max_delay": 8}, "max_turn_messages": 2}), encoding="utf-8")
    loaded = ChatPolicy.load(config)
    assert loaded.timing.mode == "random"
    assert loaded.timing.normal == (0.1, 0.2)
    assert loaded.max_turn_messages == 2


def test_invalid_model_output_retries_once_then_finishes():
    class Client:
        calls = 0

        def complete_with_schema(self, *_args):
            self.calls += 1
            return "broken output"

    session = AgentSession.__new__(AgentSession)
    session.config = Qwen3Config(max_history_turns=2)
    session.client = Client()
    session.system_prompt = "Test character"
    session.history = []
    action = session.decide("你好", (), 20)
    assert action.action == "finish_turn"
    assert session.client.calls == 2
    fallback = split_plain_fallback("你好。今天很开心！", max_length=20, previous=())
    assert fallback.content == "你好。今天很开心！"
    assert not fallback.continue_turn


def test_code_gate_silence_skips_agent(tmp_path):
    chat = bridge(tmp_path)
    assert chat.accept(event(1, "别回复了")) is None
    assert chat.queue.empty()
    assert chat.store.db.execute("SELECT state FROM messages").fetchone()[0] == "no_reply"


def test_immediate_silence_cancels_pending_send_despite_rate_limit(tmp_path):
    class Agent:
        def decide(self, *_args, **_kwargs):
            return ConversationAction("send_message", "旧回答", False, "normal", "answer")

        def commit(self, *_args):
            pass

    async def scenario():
        timing = TimingPolicy(instant=(0, 0), quick=(0, 0), normal=(0.2, 0.2),
                              thoughtful=(0, 0), max_delay=1)
        chat = bridge(tmp_path, timing=timing)
        chat.policy = ChatPolicy(private_users=frozenset({123}), min_interval_seconds=1, timing=timing)
        chat._agent = lambda _: Agent()
        client = Client()
        worker = asyncio.create_task(chat.worker(client))
        try:
            chat.accept(event(1, "一个问题"))
            await asyncio.sleep(0.05)
            assert chat.accept(event(2, "别回复了")) is None
            await asyncio.wait_for(chat.queue.join(), 3)
        finally:
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
        assert client.sent == []
        assert dict(chat.store.db.execute("SELECT message_id,state FROM messages")) == {
            "1": "cancelled", "2": "no_reply"}

    asyncio.run(scenario())
