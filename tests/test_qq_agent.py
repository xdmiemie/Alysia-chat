"""Lifecycle integration against the pinned real AstrBot Runner and executor."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

if sys.version_info < (3, 12):
    pytest.skip("Run native AstrBot integration with envs/astrbot/Scripts/python.exe", allow_module_level=True)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "external/AstrBot"))

from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.message import Message
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.agent.tool import ToolSet
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageEventResult, ResultContentType
from astrbot.core.pipeline.process_stage.follow_up import register_active_runner, unregister_active_runner
from astrbot.core.provider.entities import LLMResponse, ProviderRequest

from integrations.qq_astrbot.plugin.agent_runtime import parse_final_reply
from integrations.qq_astrbot.plugin.agent_state import AgentStore, BudgetExhausted, Superseded
from integrations.qq_astrbot.plugin.agent_tools import AgentTools
from integrations.qq_astrbot.plugin.main import FOLLOWUP_FORMAT, Q1Guard
from integrations.qq_astrbot.plugin.message_router import Incoming
from integrations.qq_astrbot.plugin.prompt_loader import load_prompts
from scripts.qq_companion.probe_q3_local import LocalLoop


def candidate(text="ready"):
    return LLMResponse("assistant", completion_text=json.dumps({"reply": text, "thinking_level": 1, "voice": False}))


def assistant_texts(messages):
    from integrations.qq_astrbot.plugin.reply_content import dialogue_view
    return [row['content'] for row in dialogue_view(messages) if row['role'] == 'assistant']


def tool_call(name="weapi", arguments=None, call_id="call_1", prose="internal draft"):
    return LLMResponse("tool", completion_text=prose, tools_call_name=[name],
                       tools_call_args=[arguments or {"action": "search", "query": "song"}], tools_call_ids=[call_id])


class Event:
    unified_msg_origin = "aiocqhttp:FriendMessage:qq:999:dm:101"

    def __init__(self):
        self.extra, self.sent, self.result = {}, [], None
        self.stopped = False
        self.trace = SimpleNamespace(record=lambda *args, **kwargs: None)

    def get_extra(self, key, default=None): return self.extra.get(key, default)
    def set_extra(self, key, value): self.extra[key] = value
    def get_platform_name(self): return "aiocqhttp"
    def get_platform_id(self): return "snowluma"
    def get_group_id(self): return ""
    def get_sender_id(self): return "101"
    def is_stopped(self): return self.stopped
    def stop_event(self): self.stopped = True
    def get_result(self): return self.result
    def set_result(self, result): self.result = result
    def clear_result(self): self.result = None
    async def send(self, chain): self.sent.append(chain)


class Provider:
    provider_config = {"id": "fake", "max_context_tokens": 0}

    def __init__(self, outputs):
        self.outputs, self.followup_outputs = list(outputs), []
        self.main_calls, self.followup_calls, self.contexts, self.requests = 0, 0, [], []
        self.on_main = self.on_followup = None

    def get_model(self): return "fake"
    def meta(self): return SimpleNamespace(id="fake", model="fake", type="openai")

    async def text_chat(self, **kwargs):
        self.requests.append(kwargs)
        if kwargs.get("response_format") == FOLLOWUP_FORMAT:
            self.followup_calls += 1
            assert kwargs.get("func_tool") is None
            if self.on_followup:
                await self.on_followup()
            result = self.followup_outputs.pop(0) if self.followup_outputs else LLMResponse(
                "assistant", completion_text='{"follow_up":null,"thinking_level":1}')
            if isinstance(result, BaseException): raise result
            return result
        self.main_calls += 1
        self.contexts.append([m.model_dump() if hasattr(m, "model_dump") else m for m in kwargs.get("contexts", [])])
        if self.on_main:
            await self.on_main()
        response = self.outputs.pop(0) if self.outputs else candidate("final reply")
        if isinstance(response, BaseException): raise response
        return response


@pytest.fixture
def loop():
    value = LocalLoop()
    yield value
    value.run_until_complete(value.shutdown_asyncgens())
    value.close()


async def make_run(tmp_path, outputs, *, limits=None, system_prompt="", contexts=None, prepare_request=False):
    owner = object.__new__(Q1Guard)
    store = owner.agent_store = AgentStore(tmp_path / "agent.db")
    version = store.observe("qq:999:dm:101", "m1")
    run = store.begin("qq:999:dm:101", version, "m1", **(limits or {}))
    event = Event()
    for k, v in {"qq_agent_run": run, "qq_scope": run.scope, "qq_inbound_sequence": version,
                 "qq_incoming": Incoming("999", "101", "m1", "find a song", ""),
                 "qq_reply_started_at": 0}.items(): event.set_extra(k, v)
    counts = {"search": 0, "released": 0, "commits": 0}

    async def request(path, payload=None, **kwargs):
        counts["search"] += 1
        return {"ok": True, "status": "completed", "songs": [{"song_id": 1, "title": "song", "artists": "artist"}]}

    def leave(lease): counts["released"] += 1
    async def commit(run_id): counts["commits"] += 1
    owner.media = SimpleNamespace(client=SimpleNamespace(request=request, state=SimpleNamespace(leave=leave)), commit_run=commit)
    from integrations.qq_astrbot.plugin.song_tasks import SongTasks
    owner.media.song_tasks = SongTasks(tmp_path / 'song_tasks.db')
    owner.agent_tools = AgentTools(owner)
    tools = owner.agent_tools.registered()
    provider = Provider(outputs)
    runner = ToolLoopAgentRunner()

    class Hooks(BaseAgentRunHooks):
        async def on_agent_begin(self, context): await owner.begin_native_agent(event, context)
        async def on_agent_done(self, context, response): await owner.unpack_paced_reply(event, context, response)

    request = ProviderRequest(prompt="find a song", func_tool=ToolSet(tools),
                              system_prompt=system_prompt, contexts=contexts or [])
    if prepare_request:
        owner.native_tools = tools
        owner.permissions = SimpleNamespace(restrict_tools=lambda tool_set, *args, **kwargs: tool_set)
        owner.router = SimpleNamespace(context=SimpleNamespace(
            text_for=lambda scope, key, text: text, render=lambda scope: "[]"))
        event.set_extra("qq_gpu_lease", "test-lease")
        await owner.request_paced_reply(event, request)
    await runner.reset(provider=provider, request=request,
                       run_context=ContextWrapper(context=SimpleNamespace(event=event), tool_call_timeout=5),
                       tool_executor=FunctionToolExecutor(), agent_hooks=Hooks(), streaming=False)
    register_active_runner(event.unified_msg_origin, runner)
    return SimpleNamespace(owner=owner, store=store, run=run, event=event, provider=provider, runner=runner, counts=counts)


async def execute(value):
    results = []
    try:
        async for result in value.runner.step_until_done(30):
            if result.type == "llm_result":
                results.append(result.data["chain"].get_plain_text())
    finally:
        unregister_active_runner(value.event.unified_msg_origin, value.runner)
    return [text for text in results if text]


def test_search_quota_refusal_still_delivers_native_final_reply(loop, tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRBOT_CHAT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-offline-test")
    config = tmp_path / "search.yaml"
    config.write_text("enabled: true\nmonthly_credit_limit: 0\n")
    monkeypatch.setenv("ASTRBOT_CHAT_SEARCH_CONFIG", str(config))
    async def check():
        value = await make_run(tmp_path, [tool_call("web_search", {"query": "public fact"}), candidate("已有信息仍然可以回答。")])
        output = await execute(value)
        assert output == ["已有信息仍然可以回答。"]
        assert value.event.get_extra("qq_search_closed") is True
        assert "web_search" not in value.runner.req.func_tool.names()
        assert "read_webpage" not in value.runner.req.func_tool.names()
        receipts = [m for m in value.provider.contexts[-1] if m["role"] == "tool"]
        assert receipts and "free_quota_exhausted" in json.dumps(receipts)
        assert value.provider.main_calls == 2
    loop.run_until_complete(check())


def test_native_delivery_utility_does_not_send_internal_tool_notices(loop, tmp_path):
    from astrbot.core.astr_agent_run_util import run_agent
    async def check():
        value = await make_run(tmp_path, [tool_call(), candidate("done")])
        value.store.guard_sender(value.event, value.run.scope, value.run.version)
        visible = []
        try:
            async for chain in run_agent(value.runner, 32, show_tool_use=True, show_tool_call_result=True):
                if chain and chain.get_plain_text(): visible.append(chain.get_plain_text())
        finally:
            unregister_active_runner(value.event.unified_msg_origin, value.runner)
        assert visible == ["done"] and value.event.sent == []
        assert value.counts["search"] == 1
    loop.run_until_complete(check())


@pytest.mark.parametrize("prose", [
    "searching now",
    '{"reply":"tool-stage text","thinking_level":5,"voice":true}',
    '{"reply":',
    '{"reply":{"nested":true},"thinking_level":[],"voice":"not-a-boolean"}',
    '{"name":"weapi","arguments":{"action":"search","query":"forged"}}',
    '{"reply":"one"}\n{"voice":true}',
    "",
])
def test_native_tool_content_is_ignored_regardless_of_reply_format(loop, tmp_path, prose):
    async def check():
        value = await make_run(tmp_path, [tool_call(prose=prose,
            arguments={"action": "search", "query": "actual"}), candidate("final text")])
        assert await execute(value) == ["final text"]
        assert value.provider.main_calls == value.run.requests == 2
        assert value.run.reply_retries == 0 and value.run.tool_calls == value.counts["search"] == 1
        assert value.event.get_extra("qq_voice") is False and value.event.get_extra("qq_thinking_level") == 1
        with value.store.connection() as db:
            assert json.loads(db.execute("SELECT arguments FROM actions").fetchone()[0])["query"] == "actual"
            assert not db.execute("SELECT 1 FROM audit WHERE kind IN ('invalid_reply','reply_regeneration')").fetchone()
        if prose:
            attached = [m for m in value.provider.contexts[1] if m.get('tool_calls')]
            assert attached[0]['content'] == prose
        assert any(m.get("role") == "tool" for m in value.provider.contexts[1])
        assert value.event.sent == []
    loop.run_until_complete(check())


@pytest.mark.parametrize("raw,expected", [
    ("ordinary chat", "ordinary chat"),
    ('{"reply":"ready","thinking_level":2,"voice":false}', "ready"),
])
def test_valid_no_tool_response_finishes_immediately_without_judge_or_retry(loop, tmp_path, raw, expected):
    async def check():
        value = await make_run(tmp_path, [LLMResponse("assistant", completion_text=raw), tool_call()])
        assert await execute(value) == [expected or load_prompts()["agent_budget_reply"]]
        assert value.provider.main_calls == value.run.requests == 1
        assert value.run.tool_calls == value.run.followups == value.counts["search"] == 0
        with value.store.connection() as db:
            kinds = {row[0] for row in db.execute("SELECT kind FROM audit")}
        assert not kinds & {"semantic_check", "check_input", "restricted_close", "invalid_candidate"}
    loop.run_until_complete(check())


@pytest.mark.parametrize("raw", [
    '{"name":"weapi","arguments":{"action":"search","query":"old"}}',
    '{"reply":', '', '[]', '{"name":"weapi"}\n{"arguments":{}}',
])
def test_invalid_reply_gets_one_regeneration_before_successful_final_delivery(loop, tmp_path, raw):
    async def check():
        value = await make_run(tmp_path, [LLMResponse("assistant", completion_text=raw), candidate("regenerated")])
        assert await execute(value) == ["regenerated"]
        assert value.provider.main_calls == value.run.requests == 2
        assert value.run.reply_retries == 1 and value.run.tool_calls == value.counts["search"] == 0
        assert any(load_prompts()["agent_reply_regenerate"] in m.get("content", "")
                   for m in value.provider.contexts[1])
        assert value.event.sent == []
        assert all("invalid_output" not in str(m.content) for m in value.runner.run_context.messages)
    loop.run_until_complete(check())


def test_regenerated_native_tool_executes_only_new_arguments_and_instruction_is_temporary(loop, tmp_path):
    async def check():
        raw = '{"name":"weapi","arguments":{"action":"search","query":"old"}}'
        value = await make_run(tmp_path, [LLMResponse("assistant", completion_text=raw),
            tool_call(arguments={"action": "search", "query": "new"}), candidate("done")])
        assert await execute(value) == ["done"]
        assert value.provider.main_calls == value.run.requests == 3
        assert value.run.reply_retries == value.run.tool_calls == value.counts["search"] == 1
        with value.store.connection() as db:
            arguments = json.loads(db.execute("SELECT arguments FROM actions").fetchone()[0])
        assert arguments["query"] == "new"
        assert not any("invalid_output" in str(m) for m in value.provider.contexts[2])
        assert any(m.get("role") == "tool" for m in value.provider.contexts[2])
    loop.run_until_complete(check())


@pytest.mark.parametrize("tool_between", [False, True])
def test_second_invalid_reply_uses_fallback_without_another_retry_even_after_tools(loop, tmp_path, tool_between):
    async def check():
        bad = lambda: LLMResponse("assistant", completion_text='{"reply":')
        outputs = [bad()] + ([tool_call()] if tool_between else []) + [bad(), candidate("unreachable")]
        value = await make_run(tmp_path, outputs)
        assert await execute(value) == [load_prompts()["agent_budget_reply"]]
        assert value.provider.main_calls == value.run.requests == 2 + int(tool_between)
        assert value.run.reply_retries == 1
        assert value.counts["search"] == int(tool_between)
        with value.store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM audit WHERE kind='reply_regeneration'").fetchone()[0] == 1
    loop.run_until_complete(check())


def test_invalid_reply_regeneration_respects_request_budget(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [LLMResponse("assistant", completion_text='{"reply":')],
                               limits={"max_requests": 1})
        assert await execute(value) == [load_prompts()["agent_budget_reply"]]
        assert value.run.requests == value.provider.main_calls == 1 and value.run.reply_retries == 0
    loop.run_until_complete(check())


def test_new_input_cancels_invalid_reply_regeneration_without_sending_old_output(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [LLMResponse("assistant", completion_text='{"reply":'), candidate("stale")])
        entered = asyncio.Event()
        async def block_retry():
            if value.provider.main_calls == 2:
                entered.set()
                await asyncio.Event().wait()
        value.provider.on_main = block_retry
        task = asyncio.create_task(execute(value))
        await entered.wait()
        value.store.observe(value.run.scope, "new-message")
        assert await asyncio.wait_for(task, timeout=0.5) == []
        assert value.run.reply_retries == 1 and not value.event.sent
        assert all(m.role != "assistant" for m in value.runner.run_context.messages)
    loop.run_until_complete(check())


async def deliver_primary(value):
    assert await execute(value) == ["primary"]
    chain = MessageEventResult().message("primary")
    value.event.set_extra("qq_reply_ready", True)
    value.event.set_extra("qq_delivery_chains", [chain])
    await value.event.send(chain)
    await value.owner.finish_paced_reply(value.event)


def followup(text):
    return LLMResponse("assistant", completion_text=json.dumps({"follow_up": text, "thinking_level": 1}))


def test_separate_system_rules_survive_tool_rounds_and_followup_without_entering_history(loop, tmp_path):
    async def check():
        prompts = load_prompts()
        persona = "CURRENT PERSONA"
        rules = "\n".join((prompts["reply_output"], prompts["agent_policy"], prompts["web_search_policy"]))
        history = [{"role": "user", "content": "past request"},
                   {"role": "assistant", "content": "actually sent"}]
        value = await make_run(tmp_path, [tool_call(), candidate("primary")],
                               system_prompt=persona, contexts=history, prepare_request=True)
        assert value.runner.req.system_prompt == persona + "\n" + prompts["qq_context"]
        assert value.event.get_extra("qq_agent_rules_prompt") == rules
        await deliver_primary(value)
        assert value.provider.main_calls == 2 and value.provider.followup_calls == 1
        assert value.run.reply_retries == 0 and value.counts["search"] == 1
        for request in value.provider.requests:
            messages = [m.model_dump() if hasattr(m, "model_dump") else m for m in request["contexts"]]
            systems = [m["content"] for m in messages if m["role"] == "system"]
            assert systems[:2] == [persona + "\n" + prompts["qq_context"], rules]
            assert systems.count(rules) == 1
        lifecycle = value.event.get_extra("qq_agent_lifecycle")
        assert lifecycle.initial_messages[1] is lifecycle.rules_message
        saved = value.runner.run_context.messages
        assert all(m is not lifecycle.rules_message for m in saved)
        assert rules not in repr(saved)
        assert assistant_texts(saved) == ["actually sent", "primary"]
    loop.run_until_complete(check())


def test_post_agent_followup_is_sent_after_primary_and_stops_at_one(loop, tmp_path, monkeypatch):
    monkeypatch.setattr("integrations.qq_astrbot.plugin.main.remaining_seconds", lambda *a, **kw: 0)
    async def check():
        value = await make_run(tmp_path, [candidate("primary")])
        value.provider.followup_outputs = [followup(text) for text in ["one?", "two?", "three?", "four?"]]
        async def verify_order():
            assert value.runner.done()
            assert value.event.sent[0].get_plain_text() == "primary"
        value.provider.on_followup = verify_order
        await deliver_primary(value)
        assert [chain.get_plain_text() for chain in value.event.sent] == ["primary", "one?"]
        assert value.provider.main_calls == 1 and value.provider.followup_calls == value.run.followups == 1
        assert value.run.requests == 2 and value.run.tool_calls == value.counts["search"] == 0
        assert assistant_texts(value.runner.run_context.messages) == ["primary", "one?"]
        requests = value.provider.requests[1:]
        assert all(request["func_tool"] is None for request in requests)
        assert all(request["chat_template_kwargs"]["enable_thinking"] is False for request in requests)
        await value.owner._maybe_followup(value.event)
        # The durable cap also protects against a repeated hook with reset event flags.
        value.event.set_extra("qq_followup_finished", False)
        await value.owner._maybe_followup(value.event)
        assert value.provider.followup_calls == 1 and value.counts["commits"] == 1
    loop.run_until_complete(check())


def test_followup_pause_sends_full_sentence_and_history_matches_delivery_not_unused_tail(loop, tmp_path, monkeypatch):
    monkeypatch.setattr("integrations.qq_astrbot.plugin.main.remaining_seconds", lambda *a, **kw: 0)
    async def check():
        value = await make_run(tmp_path, [candidate("primary")])
        visible = "嗯…老大你发的是啥图呀？"
        raw = followup(visible + "这是未发送的第二句话。")
        value.provider.followup_outputs = [raw]
        await deliver_primary(value)
        assert [chain.get_plain_text() for chain in value.event.sent] == ["primary", visible]
        assert assistant_texts(value.runner.run_context.messages) == ["primary", visible]
        saved = json.loads(value.runner.run_context.messages[-1].content)
        assert saved == {"follow_up": visible, "thinking_level": 1}
        with value.store.connection() as db:
            generated = json.loads(db.execute("SELECT payload FROM audit WHERE kind='followup_generated'").fetchone()[0])
        assert generated["text"] == raw.completion_text
        assert value.run.followups == 1
    loop.run_until_complete(check())


def test_followups_inherit_complete_system_and_refresh_state_without_stale_persona_snapshot(
        loop, tmp_path, monkeypatch):
    monkeypatch.setattr("integrations.qq_astrbot.plugin.main.remaining_seconds", lambda *args: 0)
    async def check():
        prompts = load_prompts()
        primary_system = "\n".join(["CURRENT PERSONA", prompts["qq_context"],
                                    prompts["reply_output"], prompts["agent_policy"]])
        extra_system = {"role": "system", "content": [{"type": "text", "text": "OTHER SYSTEM RULE"}]}
        value = await make_run(tmp_path, [tool_call(), candidate("primary")],
            system_prompt=primary_system, contexts=[extra_system])
        value.event.set_extra("qq_persona_prompt", "OUTDATED PERSONA SNAPSHOT")
        value.provider.followup_outputs = [followup("one?")]
        assert await execute(value) == ["primary"]
        # A later state update must be visible even though the Agent has ended.
        value.run.remember_song({"song_id": 2, "title": "second song"})
        def system_texts(request):
            rows = [message.model_dump() for message in request["contexts"] if message.role == "system"]
            return [row["content"] if isinstance(row["content"], str) else
                    "\n".join(part["text"] for part in row["content"]) for row in rows]
        chain = MessageEventResult().message("primary")
        value.event.set_extra("qq_reply_ready", True)
        value.event.set_extra("qq_delivery_chains", [chain])
        await value.event.send(chain)
        await value.owner.finish_paced_reply(value.event)
        requests = value.provider.requests[2:]
        assert len(requests) == value.run.followups == 1
        for request in requests:
            texts = system_texts(request)
            assert primary_system in texts and "OTHER SYSTEM RULE" in texts
            assert "OUTDATED PERSONA SNAPSHOT" not in "\n".join(texts)
            assert texts[-2:] == [prompts["followup_phase"], prompts["followup_system"]]
            states = [text for text in texts if text.startswith(prompts["agent_execution_state"])]
            assert len(states) == 1
            state = json.loads(states[0].split("\n")[-1])
            assert state["source_lookup_attempts"] == 1
            assert state["registered_song_candidates"] == [1, 2]
            assert request["func_tool"] is None and request["response_format"] == FOLLOWUP_FORMAT
        assert value.run.tool_calls == value.counts["search"] == 1
        assert [chain.get_plain_text() for chain in value.event.sent] == ["primary", "one?"]
        assert prompts["followup_phase"] not in repr(value.runner.run_context.messages)
        assert prompts["agent_execution_state"] not in repr(value.runner.run_context.messages)
    loop.run_until_complete(check())


@pytest.mark.parametrize("responses,expected,calls", [
    ([followup(None)], [], 1),
    ([followup("one?"), followup(None)], ["one?"], 1),
    ([followup("primary")], [], 1),
    ([followup("one?"), followup("one?")], ["one?"], 1),
    ([tool_call()], [], 1),
    ([LLMResponse("assistant", completion_text="not JSON")], [], 1),
])
def test_followup_null_duplicate_invalid_output_or_tool_call_stops_without_actions(
        loop, tmp_path, monkeypatch, responses, expected, calls):
    monkeypatch.setattr("integrations.qq_astrbot.plugin.main.remaining_seconds", lambda *a, **kw: 0)
    async def check():
        value = await make_run(tmp_path, [candidate("primary")])
        value.provider.followup_outputs = responses
        await deliver_primary(value)
        assert [chain.get_plain_text() for chain in value.event.sent] == ["primary"] + expected
        assert value.provider.followup_calls == value.run.followups == calls
        assert value.run.tool_calls == value.counts["search"] == 0
    loop.run_until_complete(check())


def test_new_input_cancels_post_agent_decision_without_losing_sent_primary(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [candidate("primary")])
        entered = asyncio.Event()
        async def block():
            entered.set()
            await asyncio.Event().wait()
        value.provider.on_followup = block
        task = asyncio.create_task(deliver_primary(value))
        await entered.wait()
        value.store.observe(value.run.scope, "new-input")
        await asyncio.wait_for(task, timeout=0.5)
        assert [chain.get_plain_text() for chain in value.event.sent] == ["primary"]
        assert json.loads(value.runner.run_context.messages[-1].content)['reply'] == "primary"
        assert value.run.followups == 1 and value.counts["released"] == 1
    loop.run_until_complete(check())


def test_group_does_not_start_private_followup_flow(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [candidate("primary")])
        try:
            value.event.get_group_id = lambda: "201"
            await value.owner._maybe_followup(value.event)
            assert value.provider.followup_calls == value.run.followups == 0
        finally:
            unregister_active_runner(value.event.unified_msg_origin, value.runner)
    loop.run_until_complete(check())


@pytest.mark.parametrize('response,draws', [(followup(None),0), (followup('primary'),0),
    (tool_call(),0), (LLMResponse('assistant',completion_text='invalid'),0), (followup('one?'),1)])
def test_only_valid_wanted_followup_enters_probability_gate(loop, tmp_path, monkeypatch, response, draws):
    from integrations.qq_astrbot.plugin.proactive_store import ProactiveStore, DEFAULTS
    monkeypatch.setattr('integrations.qq_astrbot.plugin.main.remaining_seconds', lambda *a, **kw:0)
    async def check():
        value = await make_run(tmp_path, [candidate('primary')])
        store = ProactiveStore(tmp_path, rng=SimpleNamespace(random=lambda:.999999))
        value.owner.proactive = SimpleNamespace(store=store, config=DEFAULTS)
        value.provider.followup_outputs = [response]
        await deliver_primary(value)
        with store.connection() as db:
            assert db.execute('SELECT count(*) FROM draws').fetchone()[0] == draws
        assert [chain.get_plain_text() for chain in value.event.sent] == ['primary']
    loop.run_until_complete(check())


def test_provider_fallback_keeps_native_tool_receipts_and_request_budget(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [tool_call(), RuntimeError("provider unavailable")])
        alternate = Provider([candidate("finished")])
        value.runner.fallback_providers = [alternate]
        assert await execute(value) == ["finished"]
        assert value.run.requests == 3 and value.counts["search"] == 1
        assert any(m.get("role") == "tool" for m in alternate.contexts[0])
    loop.run_until_complete(check())


def test_native_tool_roundtrip_then_direct_final_reply(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [tool_call(), candidate("found it")])
        assert await execute(value) == ["found it"]
        assert value.counts["search"] == 1 and value.provider.main_calls == 2
        assert value.run.requests == 2
        assert any(m.get("role") == "tool" for m in value.provider.contexts[1])
        assert not assistant_texts(value.runner.run_context.messages)
        lifecycle = value.event.get_extra("qq_agent_lifecycle")
        lifecycle.sent("found it")
        assert json.loads(value.runner.run_context.messages[-1].content)['reply'] == "found it"
    loop.run_until_complete(check())


def test_tool_reasoning_is_replayed_only_within_current_run_and_not_sent_or_saved(loop, tmp_path):
    async def check():
        from integrations.qq_astrbot.plugin.model_messages import normalize_messages
        response = tool_call(prose="tool-stage content")
        response.reasoning_content = "PRIVATE TOOL REASONING"
        value = await make_run(tmp_path, [response, candidate("primary")])
        await deliver_primary(value)
        wire = normalize_messages(value.provider.contexts[1])
        tool_message = next(m for m in wire if m.get("tool_calls"))
        assert tool_message["reasoning_content"] == "PRIVATE TOOL REASONING"
        assert response.reasoning_content is None
        assert "PRIVATE TOOL REASONING" not in repr(value.runner.run_context.messages)
        assert "PRIVATE TOOL REASONING" not in repr(value.event.sent)
        followup = next(r for r in value.provider.requests if r.get("response_format") == FOLLOWUP_FORMAT)
        assert "PRIVATE TOOL REASONING" not in repr(followup)
    loop.run_until_complete(check())


def test_full_protocol_survives_delivery_serialization_and_next_request_without_reexecuting_tools(loop, tmp_path):
    async def check():
        raw_tool = '{"reply":"searching","thinking_level":2,"voice":false}'
        raw_final = '{\n  "reply": "primary",\n  "thinking_level": 4,\n  "voice": false\n}'
        value = await make_run(tmp_path / 'first', [tool_call(prose=raw_tool),
            LLMResponse('assistant', completion_text=raw_final)])
        await deliver_primary(value)
        from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import InternalAgentSubStage
        saved = []
        async def update(origin, cid, **kwargs):
            saved.append(json.loads(json.dumps(kwargs['history'])))
        stage = SimpleNamespace(conv_manager=SimpleNamespace(update_conversation=update))
        value.runner.req.conversation = SimpleNamespace(cid='cid', token_usage=0)
        await InternalAgentSubStage._save_to_history(stage, value.event, value.runner.req,
            value.runner.get_final_llm_resp(), value.runner.run_context.messages, value.runner.stats)
        messages = saved[0]
        call = next(m for m in messages if m.get('tool_calls'))
        result = next(m for m in messages if m['role'] == 'tool')
        assert call['content'] == raw_tool
        assert call['tool_calls'][0]['id'] == result['tool_call_id'] == 'call_1'
        assert call['tool_calls'][0]['function']['name'] == 'weapi'
        assert json.loads(call['tool_calls'][0]['function']['arguments']) == {'action': 'search', 'query': 'song'}
        assert messages[-1]['content'] == raw_final
        followup_wire = value.provider.requests[-1]['contexts']
        assert any(m.role == 'assistant' and m.content == raw_final for m in followup_wire)
        assert any(m.role == 'tool' and m.tool_call_id == 'call_1' for m in followup_wire)
        assert value.event.sent[0].get_plain_text() == 'primary'
        newer = await make_run(tmp_path / 'second', [candidate('next')], contexts=messages)
        assert await execute(newer) == ['next']
        assert newer.counts['search'] == 0
        wire = newer.provider.contexts[0]
        assert next(m for m in wire if m.get('tool_calls')) == call
        assert any(m['role'] == 'assistant' and m['content'] == raw_final for m in wire)
        assert any(m['role'] == 'tool' and m['tool_call_id'] == 'call_1' for m in wire)
    loop.run_until_complete(check())
























def test_new_input_cancels_inflight_generation_and_preserves_only_real_history(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [candidate("stale")])
        entered = asyncio.Event()
        async def block():
            entered.set()
            await asyncio.Event().wait()
        value.provider.on_main = block
        task = asyncio.create_task(execute(value))
        await entered.wait()
        value.store.observe(value.run.scope, "m2")
        assert await task == []
        assert value.provider.main_calls == 1 and not value.event.sent
        assert all(m.role != "assistant" for m in value.runner.run_context.messages)
    loop.run_until_complete(check())


def test_final_reply_is_discarded_if_input_changes_before_send(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [candidate("stale")])
        await execute(value)
        value.event.result = MessageEventResult().message("stale").set_result_content_type(ResultContentType.LLM_RESULT)
        value.store.observe(value.run.scope, "m2")
        await value.owner.delay_paced_reply(value.event)
        assert value.event.result.chain == []
        assert all(m.role != "assistant" for m in value.runner.run_context.messages)
    loop.run_until_complete(check())


def test_run_counters_survive_reopening_and_duplicates_have_uncertain_receipt(tmp_path):
    store = AgentStore(tmp_path / "agent.db")
    version = store.observe("qq:999:dm:101", "m1")
    run = store.begin("qq:999:dm:101", version, "m1")
    store.consume(run, "followups")
    key, old = store.reserve_action(run, "music", {"song_id": 1})
    assert old is None
    reopened = AgentStore(store.path)
    same = reopened.begin(run.scope, version, "m1")
    assert same.followups == 1
    assert reopened.reserve_action(same, "music", {"song_id": 1})[1]["status"] == "uncertain"
    reopened.consume(same, "reply_retries")
    with pytest.raises(BudgetExhausted, match="followups"):
        reopened.consume(same, "followups")
    again = AgentStore(store.path).begin(run.scope, version, "m1")
    assert again.followups == 1
    assert again.reply_retries == 1
    with pytest.raises(BudgetExhausted):
        reopened.consume(again, "followups")
    with pytest.raises(BudgetExhausted):
        reopened.consume(again, "reply_retries")


def test_old_audit_database_migrates_followups_without_changing_history(tmp_path):
    import sqlite3
    path = tmp_path / "old-agent.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE runs(id TEXT PRIMARY KEY,scope TEXT,version INTEGER,started REAL,"
                   "checks INTEGER DEFAULT 0,requests INTEGER DEFAULT 0,tool_calls INTEGER DEFAULT 0,status TEXT)")
        db.execute("INSERT INTO runs VALUES ('old','scope',1,0,2,8,0,'sent')")
    store = AgentStore(path)
    with store.connection() as db:
        row = db.execute("SELECT checks,requests,followups,reply_retries,status FROM runs WHERE id='old'").fetchone()
        assert tuple(row) == (2, 8, 0, 0, "sent")


def test_manual_tool_schema_rejects_wrong_types_without_side_effect(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [tool_call(arguments={"action": "search", "query": []}), candidate()])
        await execute(value)
        assert value.counts["search"] == 0
        with value.store.connection() as db:
            assert db.execute("SELECT count(*) FROM audit WHERE kind='invalid_tool_arguments'").fetchone()[0] == 1
    loop.run_until_complete(check())




def test_send_guard_rechecks_after_agent_completion(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [candidate()])
        value.store.guard_sender(value.event, value.run.scope, value.run.version)
        await execute(value)
        value.store.observe(value.run.scope, "m2")
        with pytest.raises(Superseded):
            await value.event.send(MessageEventResult().message("stale"))
        assert not value.event.sent
    loop.run_until_complete(check())


def test_new_input_interrupts_typing_delay_and_releases_lease(loop, tmp_path):
    import time
    async def check():
        value = await make_run(tmp_path, [candidate("long enough to delay")])
        await execute(value)
        value.event.set_extra("qq_reply_started_at", time.monotonic())
        value.event.result = MessageEventResult().message("old").set_result_content_type(ResultContentType.LLM_RESULT)
        task = asyncio.create_task(value.owner.delay_paced_reply(value.event))
        await asyncio.sleep(0)
        value.store.observe(value.run.scope, "m2")
        await asyncio.wait_for(task, timeout=0.5)
        assert value.event.result.chain == [] and value.counts["released"] >= 1
    loop.run_until_complete(check())


@pytest.mark.parametrize("elapsed,level", [(0.1, 1), (0.1, 5), (30.0, 5)])
@pytest.mark.parametrize("group,segments", [
    (False, ["好。", "第二句比较长！", "OK?"]),
    (True, ["好。", "第二句比较长！", "OK?第四句。"]),
    (False, ["一。", "二。", "三。", "四。", "五。", "六。第七句。"]),
])
def test_split_reply_waits_for_each_segment_after_previous_send(
        loop, tmp_path, monkeypatch, elapsed, level, group, segments):
    import time
    from integrations.qq_astrbot.plugin import main, reply_pacing
    from integrations.qq_astrbot.plugin.reply_pacing import THINKING_SECONDS, typing_seconds

    async def check():
        reply = "".join(segments)
        value = await make_run(tmp_path, [candidate(reply)])
        await execute(value)
        value.event.set_extra("qq_thinking_level", level)
        value.event.set_extra("qq_reply_started_at", 100.0)
        value.event.set_extra("qq_followup_finished", True)
        if group:
            value.event.get_group_id = lambda: "201"
            value.owner.router = SimpleNamespace(context=SimpleNamespace(
                add=lambda *args: None, replace_text=lambda *args: None))
        clock = [100.0 + elapsed]
        fake_time = SimpleNamespace(monotonic=lambda: clock[0], time=time.time)
        monkeypatch.setattr(main, "time", fake_time)
        monkeypatch.setattr(reply_pacing, "time", fake_time)
        timeline = []

        async def wait_delivery(run, seconds):
            value.store.ensure_current(run)
            timeline.append(("wait", seconds))
            clock[0] += seconds

        async def send(chain):
            timeline.append(("send", chain.get_plain_text(), clock[0]))
            clock[0] += 8.0  # Transport latency must not consume the next sentence's delay.
            value.event.sent.append(chain)

        monkeypatch.setattr(value.store, "wait_delivery", wait_delivery)
        value.event.send = send
        value.event.result = MessageEventResult().message(reply).set_result_content_type(ResultContentType.LLM_RESULT)
        await value.owner.delay_paced_reply(value.event)
        assert value.event.result.get_plain_text() == segments[0]
        await value.event.send(value.event.result)
        await value.owner.finish_paced_reply(value.event)

        expected = []
        current = 100.0 + elapsed
        for index, segment in enumerate(segments):
            seconds = typing_seconds(segment)
            if index == 0:
                seconds = max(0.0, THINKING_SECONDS[level] + seconds - elapsed)
            if seconds > 0:
                expected.append(("wait", seconds))
                current += seconds
            expected.append(("send", segment, current))
            current += 8.0
        assert len(timeline) == len(expected)
        for actual, target in zip(timeline, expected):
            assert actual[:-1] == target[:-1]
            assert actual[-1] == pytest.approx(target[-1])
        assert assistant_texts(value.runner.run_context.messages) == [reply]
        assert value.provider.followup_calls == 0 and value.counts["released"] == 1

    loop.run_until_complete(check())


def test_new_input_interrupts_second_segment_and_keeps_only_sent_history(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [candidate("第一句。第二句话比较长。第三句。")])
        await execute(value)
        value.event.result = MessageEventResult().message("ready").set_result_content_type(ResultContentType.LLM_RESULT)
        await value.owner.delay_paced_reply(value.event)
        await value.event.send(value.event.result)
        entered = asyncio.Event()
        original_wait = value.store.wait_delivery

        async def wait_delivery(run, seconds):
            entered.set()
            await original_wait(run, seconds)

        value.store.wait_delivery = wait_delivery
        task = asyncio.create_task(value.owner.finish_paced_reply(value.event))
        await entered.wait()
        value.store.observe(value.run.scope, "m2")
        await asyncio.wait_for(task, timeout=0.5)
        assert [chain.get_plain_text() for chain in value.event.sent] == ["第一句。"]
        assert assistant_texts(value.runner.run_context.messages) == ["第一句。"]
        assert value.provider.followup_calls == value.counts["commits"] == 0
        assert value.counts["released"] == 1

    loop.run_until_complete(check())


def test_delivery_delay_survives_expired_generation_budget(loop, tmp_path):
    async def check():
        store = AgentStore(tmp_path / "agent.db")
        version = store.observe("qq:999:dm:101", "m1")
        run = store.begin("qq:999:dm:101", version, "m1")
        run.started -= run.max_seconds + 1
        task = asyncio.create_task(store.wait_delivery(run, 60))
        await asyncio.sleep(0)
        assert not task.done()
        with pytest.raises(BudgetExhausted):
            store.consume(run, "requests")
        store.observe(run.scope, "m2")
        with pytest.raises(Superseded):
            await asyncio.wait_for(task, timeout=0.5)

    loop.run_until_complete(check())


def test_onebot_guard_rechecks_after_async_media_encoding(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [])
        encoded = asyncio.Event()
        resume = asyncio.Event()
        writes = []
        class Bot:
            async def send_private_msg(self, **kwargs): writes.append(kwargs)
        value.event.bot = Bot()
        async def send(chain):
            encoded.set()
            await resume.wait()
            await value.event.bot.send_private_msg(user_id=101, message=[])
        value.event.send = send
        value.store.guard_sender(value.event, value.run.scope, value.run.version)
        task = asyncio.create_task(value.event.send(MessageEventResult().message("old")))
        await encoded.wait()
        value.store.observe(value.run.scope, "m2")
        resume.set()
        with pytest.raises(Superseded): await task
        assert not writes
        unregister_active_runner(value.event.unified_msg_origin, value.runner)
    loop.run_until_complete(check())


def test_only_successfully_sent_chunks_enter_chat_history(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [candidate("first.second.")])
        await execute(value)
        chains = [MessageEventResult().message("first."), MessageEventResult().message("second.")]
        value.event.set_extra("qq_delivery_chains", chains)
        value.event.set_extra("qq_reply_ready", True)
        async def fail(chain): raise OSError("transport failed")
        value.event.send = fail
        await value.owner.finish_paced_reply(value.event)
        assert assistant_texts(value.runner.run_context.messages) == ["first."]
        assert json.loads(value.runner.run_context.messages[-1].content) == {
            'reply': 'first.', 'thinking_level': 1, 'voice': False}
        assert value.counts["commits"] == 0
    loop.run_until_complete(check())


def test_background_onebot_send_checks_revision_after_native_encoding(loop, tmp_path, monkeypatch):
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
    from integrations.qq_astrbot.plugin.media_router import MediaRouter
    async def check():
        store = AgentStore(tmp_path / "background.db")
        scope = "qq:999:dm:101"
        version = store.observe(scope, "m1")
        encoded, resume, writes = asyncio.Event(), asyncio.Event(), []
        class Bot:
            async def send_private_msg(self, **kwargs): writes.append(kwargs)
        platform = SimpleNamespace(bot=Bot(), meta=lambda: SimpleNamespace(name="aiocqhttp"))
        router = MediaRouter(SimpleNamespace(get_platform_inst=lambda key: platform), tmp_path)
        router.agent_store, router.active_version = store, store.current
        async def encode(cls, chain):
            encoded.set()
            await resume.wait()
            return [{"type": "text", "data": {"text": "old"}}]
        monkeypatch.setattr(AiocqhttpMessageEvent, "_parse_onebot_json", classmethod(encode))
        payload = {"scope": scope, "version": version, "origin": "snowluma:FriendMessage:101", "group": ""}
        task = asyncio.create_task(router._send(payload, MessageEventResult().message("old")))
        await encoded.wait()
        store.observe(scope, "m2")
        resume.set()
        with pytest.raises(Superseded): await task
        assert writes == []
        await router.close()
    loop.run_until_complete(check())


def test_native_step_budget_does_not_inject_a_fake_user_continuation(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [tool_call()])
        result = []
        try:
            async for response in value.runner.step_until_done(1):
                if response.type == "llm_result": result.append(response.data["chain"].get_plain_text())
        finally:
            unregister_active_runner(value.event.unified_msg_origin, value.runner)
        assert [x for x in result if x] == [load_prompts()["agent_budget_reply"]]
        assert value.counts["search"] == 0
        assert len([m for m in value.runner.run_context.messages if m.role == "user"]) == 1
    loop.run_until_complete(check())




def test_tool_budget_exhaustion_stops_without_another_model_call(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [tool_call(), tool_call(call_id="call_2")], limits={"max_tools": 1})
        assert await execute(value) == [load_prompts()["agent_budget_reply"]]
        assert value.run.tool_calls == 1 and value.run.followups == 0
    loop.run_until_complete(check())




def test_current_turn_overflow_closes_without_provider_error_or_replayed_tools(loop, tmp_path, monkeypatch):
    from integrations.qq_astrbot.plugin.context_budget import RequestBudget
    async def count(self, messages, tools=None):
        return 9000
    monkeypatch.setattr(RequestBudget, "count", count)
    async def check():
        value = await make_run(tmp_path, [tool_call()])
        value.provider.provider_config = {"id": "fake", "max_context_tokens": 8192}
        assert await execute(value) == [load_prompts()["agent_budget_reply"]]
        assert value.provider.main_calls == value.counts["search"] == 0
        assert value.run.closing and value.run.followups == 0
    loop.run_until_complete(check())


def test_final_provider_error_is_internal_and_cleanup_still_runs(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [RuntimeError("HTTP 400 private diagnostic")])
        assert await execute(value) == [load_prompts()["agent_budget_reply"]]
        assert value.event.get_extra("qq_agent_completed")
        value.event.set_extra("qq_reply_ready", True)
        value.event.set_extra("qq_delivery_chains", [MessageEventResult().message(load_prompts()["agent_budget_reply"])])
        await value.owner.finish_paced_reply(value.event)
        assert value.counts["released"] == 1
        assert value.provider.main_calls == 1
    loop.run_until_complete(check())




def test_expired_global_deadline_stops_work_and_emits_only_bounded_close(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [tool_call()])
        value.run.started -= 1000
        result = await execute(value)
        assert len(result) == 1 and value.counts["search"] == 0 and value.provider.followup_calls == 0
        assert not value.event.get_extra("qq_voice")
    loop.run_until_complete(check())


def test_admission_wait_cannot_outlive_global_budget(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [])
        value.run.started -= 1000
        value.owner.media.client.state.enter = lambda: None
        await value.owner.request_paced_reply(value.event, ProviderRequest())
        assert value.event.is_stopped() and value.run.requests == 0
        assert value.event.sent[0].get_plain_text() == load_prompts()["agent_budget_reply"]
        assert value.store.run_status(value.run.run_id) == "budget_exhausted"
        unregister_active_runner(value.event.unified_msg_origin, value.runner)
    loop.run_until_complete(check())


def test_all_five_registered_native_tools_have_concrete_results(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [])
        media = value.owner.media
        media.character_id = "elysia"
        media.sticker_last = {}
        media.client.directory = tmp_path
        (tmp_path / "stickers").mkdir()
        (tmp_path / "stickers/happy.png").write_bytes(b"fake png")
        audio = tmp_path / "speech.wav"
        async def speech(*args): return b"fake audio"
        def save_audio(data): audio.write_bytes(data); return audio
        media.client.speech, media.client.save_audio = speech, save_audio
        media.client.state.remember = lambda key, payload: True
        spawned = []
        async def deliver(key, payload): return None
        media.deliver_song = deliver
        def spawn(coro): spawned.append(coro)
        media.spawn = spawn
        version = value.run.version
        value.store.guard_sender(value.event, value.run.scope, version)
        calls = [("weapi", {"action": "search", "query": "song"}),
                 ("emotion", {"text": "hello", "emotion": "happy"}),
                 ("sticker", {"tag": "happy"}), ("music", {"candidate": 1}),
                 ("musiccover", {"candidate": 1})]
        try:
            results = [await value.owner.agent_tools.execute(value.event, name, args) for name, args in calls]
            assert [r["status"] for r in results] == ["completed", "prepared", "sent", "queued", "queued"]
            assert value.run.speech["hello"] == audio and len(value.event.sent) == 1
            replay = await value.owner.agent_tools.execute(value.event, "sticker", {"tag": "happy"})
            assert replay["replayed"] and len(value.event.sent) == 1
            value.store.observe(value.run.scope, "m2")
            rejected = await value.owner.agent_tools.execute(value.event, "music", {"candidate": 1})
            assert rejected["status"] == "superseded"
        finally:
            for coro in spawned: coro.close()
            unregister_active_runner(value.event.unified_msg_origin, value.runner)
    loop.run_until_complete(check())


@pytest.mark.parametrize('prepared', [False, True])
def test_voice_reply_sends_face_as_text_with_matching_sent_audit(loop, tmp_path, prepared):
    from astrbot.core.message.components import Record
    from integrations.qq_astrbot.plugin.media_router import MediaRouter
    from integrations.qq_astrbot.plugin.reply_content import spoken_text
    async def check():
        value = await make_run(tmp_path, [])
        media = MediaRouter(SimpleNamespace(), tmp_path / 'media')
        media.commit_run = value.owner.media.commit_run
        value.owner.media = media
        calls = []
        async def speech(text, *args):
            calls.append(text)
            return b'RIFF0000WAVEfake'
        media.client.speech = speech
        reply = '司凤版的《雨爱》正在准备中哦~ 稍等一下！ (๑•̀ㅂ•́)و✧'
        face = '(๑•̀ㅂ•́)و✧'
        try:
            if prepared:
                result = await value.owner.agent_tools.execute(value.event, 'emotion', {'text': reply, 'emotion': 'neutral'})
                assert result['status'] == 'prepared' and result['text'] == spoken_text(reply)
            value.event.set_extra('qq_primary_reply', reply)
            value.event.set_extra('qq_voice', True)
            value.event.set_extra('qq_thinking_level', 1)
            value.event.set_extra('qq_agent_completed', True)
            value.event.set_extra('qq_followup_finished', True)
            value.event.set_extra('qq_agent_lifecycle', SimpleNamespace(sent=lambda text, modality, **kwargs:
                value.store.record(value.run, 'sent', {'text': text, 'modality': modality})))
            value.event.result = MessageEventResult().message(reply).set_result_content_type(ResultContentType.LLM_RESULT)
            await value.owner.delay_paced_reply(value.event)
            chains = value.event.get_extra('qq_delivery_chains')
            assert isinstance(chains[0].chain[0], Record)
            assert chains[-1].get_plain_text() == face and isinstance(chains[-1].chain[0], Plain)
            assert calls == [spoken_text(reply)]
            assert len(chains) == 2
            await value.event.send(chains[0])
            await value.owner.finish_paced_reply(value.event)
            assert value.event.sent[-1].get_plain_text() == face
            with value.store.connection() as db:
                sent = [json.loads(row[0]) for row in db.execute("select payload from audit where kind='sent'")]
            assert sent[-1] == {'text': face, 'modality': 'text'}
            assert all(face not in item['text'] for item in sent if item['modality'] == 'voice')
        finally:
            unregister_active_runner(value.event.unified_msg_origin, value.runner)
    loop.run_until_complete(check())


@pytest.mark.parametrize('voice,tts_failed', [(False, False), (True, False), (True, True)])
def test_model_voice_choice_controls_sentence_delivery_and_complete_text_fallback(loop, tmp_path, voice, tts_failed):
    from astrbot.core.message.components import Record
    from integrations.qq_astrbot.plugin.media_router import MediaRouter
    async def check():
        reply = '吃过啦。你呢？'
        response = LLMResponse('assistant', completion_text=json.dumps(
            {'reply': reply, 'thinking_level': 1, 'voice': voice}))
        value = await make_run(tmp_path, [response])
        assert await execute(value) == [reply]
        media = MediaRouter(SimpleNamespace(), tmp_path / 'media')
        media.commit_run = value.owner.media.commit_run
        value.owner.media = media
        calls = []
        async def speech(text, *args):
            calls.append(text)
            if tts_failed:
                raise RuntimeError('injected TTS failure')
            return b'RIFF0000WAVEfake'
        media.client.speech = speech
        value.event.result = MessageEventResult().message(reply).set_result_content_type(ResultContentType.LLM_RESULT)
        await value.owner.delay_paced_reply(value.event)
        chains = value.event.get_extra('qq_delivery_chains')
        await value.event.send(chains[0])
        await value.owner.finish_paced_reply(value.event)
        assert value.store.run_status(value.run.run_id) == 'sent'
        if voice and not tts_failed:
            assert len(value.event.sent) == 1 and isinstance(value.event.sent[0].chain[0], Record)
            expected_history = [reply]
        else:
            assert [chain.get_plain_text() for chain in value.event.sent] == ['吃过啦。', '你呢？']
            expected_history = [reply]
        assert calls == ([reply] if voice else [])
        assert assistant_texts(value.runner.run_context.messages) == expected_history
        assert value.runner.run_context.messages[-1].content == json.dumps(
            {'reply': reply, 'thinking_level': 1, 'voice': voice})
    loop.run_until_complete(check())


@pytest.mark.parametrize('fault', [None, 'synthesis', 'sample_rate'])
def test_long_voice_is_one_valid_recording_or_complete_fallback(loop, tmp_path, fault):
    import io
    import wave
    from astrbot.core.message.components import Record
    from integrations.qq_astrbot.plugin.media_router import MediaRouter
    async def check():
        media = MediaRouter(SimpleNamespace(), tmp_path)
        reply = '中文。' * 201
        calls, frames = [], []
        async def speech(text, *args):
            calls.append(text)
            if fault == 'synthesis' and len(calls) == 2:
                raise RuntimeError('injected TTS failure')
            output = io.BytesIO()
            samples = len(calls).to_bytes(2, 'little') * 16
            frames.append(samples)
            with wave.open(output, 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000 if fault == 'sample_rate' and len(calls) == 2 else 24000)
                wav.writeframes(samples)
            return output.getvalue()
        media.client.speech = speech
        chains = await media.voice_chains(reply, 'neutral', group=False)
        if fault:
            assert chains == [] and not list((tmp_path / 'media_cache').glob('*.wav'))
        else:
            assert len(chains) == 1 and isinstance(chains[0].chain[0], Record)
            assert ''.join(calls) == reply and max(map(len, calls)) <= 120
            saved = next((tmp_path / 'media_cache').glob('*.wav'))
            with wave.open(str(saved), 'rb') as wav:
                assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 24000)
                assert wav.readframes(wav.getnframes()) == b''.join(frames)
            assert media.speech_texts(reply, group=False) == [reply]
    loop.run_until_complete(check())


def test_emotion_tool_does_not_synthesize_a_face_only(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [])
        try:
            result = await value.owner.agent_tools.execute(value.event, 'emotion', {'text': '(๑•̀ㅂ•́)و✧', 'emotion': 'happy'})
            assert result['status'] == 'skipped' and result['error'] == 'no_spoken_text'
        finally:
            unregister_active_runner(value.event.unified_msg_origin, value.runner)
    loop.run_until_complete(check())


def test_cancel_music_tool_is_owner_scoped_and_does_not_require_new_search(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [])
        tasks = value.owner.media.song_tasks
        tasks.register('a'*32, value.run.scope, '101', 'cover', 'old', 'old song')
        tasks.register('b'*32, value.run.scope, '102', 'cover', 'other', 'other song')
        canceled = []
        async def cancel(rows):
            canceled.extend(row['id'] for row in rows)
            return [{'job_id': row['id'], 'status': 'canceled'} for row in rows]
        value.owner.media.cancel_song_tasks = cancel
        result = await value.owner.agent_tools.execute(value.event, 'musiccover', {'action': 'cancel'})
        assert result['status'] == 'canceled' and canceled == ['a'*32]
        assert [row['id'] for row in tasks.pending(value.run.scope)] == ['b'*32]
        assert value.counts['search'] == 0
        unregister_active_runner(value.event.unified_msg_origin, value.runner)
    loop.run_until_complete(check())


def test_replacement_requires_available_new_song_before_canceling_old(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [])
        tasks = value.owner.media.song_tasks
        tasks.register('a'*32, value.run.scope, '101', 'cover', 'old', 'old song')
        value.run.candidate_refs[1] = 1
        value.run.candidates[1] = {'song_id': 1, 'title': 'unavailable', 'available': False}
        result = await value.owner.agent_tools.execute(value.event, 'musiccover', {'action': 'replace', 'candidate': 1})
        assert result['status'] == 'unavailable'
        assert [row['id'] for row in tasks.pending(value.run.scope)] == ['a'*32]
        unregister_active_runner(value.event.unified_msg_origin, value.runner)
    loop.run_until_complete(check())


@pytest.mark.parametrize('superseded', [False, True])
def test_replacement_rechecks_foreground_after_submission(loop, tmp_path, superseded):
    async def check():
        value = await make_run(tmp_path, [])
        media = value.owner.media
        tasks = media.song_tasks
        tasks.register('a'*32, value.run.scope, '101', 'cover', 'old', 'old song')
        value.run.candidate_refs[1] = 1
        value.run.candidates[1] = {'song_id': 1, 'title': 'new song', 'available': True}
        media.client.state.remember = lambda key, payload: True
        async def request(path, body=None):
            assert path == '/v1/songs/selected'
            if superseded:
                value.store.observe(value.run.scope, 'newer-request')
            return {'status': 'awaiting_commit'}
        media.client.request = request
        spawned, canceled = [], []
        async def deliver(key, payload): return None
        def spawn(coro):
            spawned.append(coro)
            coro.close()
        async def cancel(rows):
            canceled.extend(rows)
            return []
        media.deliver_song, media.spawn, media.cancel_song_tasks = deliver, spawn, cancel
        try:
            result = await value.owner.agent_tools.execute(value.event, 'musiccover', {'action': 'replace', 'candidate': 1})
            assert result['status'] == ('superseded' if superseded else 'queued')
            assert [row['id'] for row in canceled] == ([] if superseded else ['a'*32])
            assert len(spawned) == 1
            assert len(tasks.pending(value.run.scope, owner='101')) == (2 if superseded else 1)
        finally:
            unregister_active_runner(value.event.unified_msg_origin, value.runner)
    loop.run_until_complete(check())


def test_group_chatter_does_not_invalidate_foreground_or_music(loop, tmp_path):
    from integrations.qq_astrbot.plugin.guard import InboundGuard
    from integrations.qq_astrbot.plugin.message_router import MessageRouter
    from astrbot.core.message.components import At
    async def check():
        value = await make_run(tmp_path, [])
        owner = value.owner
        owner.guard = InboundGuard(tmp_path / 'inbound.db')
        from integrations.qq_astrbot.plugin.foreground_activity import ForegroundActivity
        owner.foreground_activity = ForegroundActivity(tmp_path)
        owner.router = MessageRouter(tmp_path / 'routing.db')
        owner.latest_inbound, owner.group_sizes = {}, {}
        owner.media.resume = lambda: None
        scope = 'qq:999:group:201'
        version = value.store.observe(scope, 'foreground')
        row = owner.media.song_tasks.register('a'*32, scope, '101', 'cover', 'old', 'song')
        task = {'task_id': row['id'], 'task_revision': 1, 'owner': '101', 'scope': scope}
        class GroupEvent(Event):
            def __init__(self, key, sender, text, directed=False):
                super().__init__()
                self.sender, self.text = sender, text
                self.chain = ([At(qq='999')] if directed else []) + [Plain(text)]
                self.message_obj = SimpleNamespace(message_id=key, raw_message={'post_type': 'message', 'sender': {}},
                    group=SimpleNamespace(member_count=12))
            def get_message_type(self): return SimpleNamespace(value='GroupMessage')
            def get_messages(self): return self.chain
            def get_self_id(self): return '999'
            def get_sender_id(self): return self.sender
            def get_sender_name(self): return self.sender
            def get_group_id(self): return '201'
            def get_message_str(self): return self.text
        try:
            passive = GroupEvent('ordinary', '101', 'hahaha')
            await owner.filter_inbound(passive)
            assert passive.stopped and value.store.current(scope, version)
            owner.media.song_tasks.guard(task)
            assert 'hahaha' in owner.router.context.render(scope)
            other = GroupEvent('other-request', '102', 'hello', True)
            await owner.filter_inbound(other)
            assert not value.store.current(scope, version)
            owner.media.song_tasks.guard(task)
            directed = GroupEvent('owner-request', '101', 'cancel the song', True)
            await owner.filter_inbound(directed)
            from integrations.qq_astrbot.plugin.song_tasks import SongHeld
            with pytest.raises(SongHeld): owner.media.song_tasks.guard(task)
            owner._release_song_hold(directed)
            owner.media.song_tasks.guard(task)
        finally:
            owner.router.close()
            unregister_active_runner(value.event.unified_msg_origin, value.runner)
    loop.run_until_complete(check())


def test_unavailable_selected_cover_returns_explicit_failure_without_creating_job(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [])
        value.run.candidate_refs[1] = 1
        value.run.candidates[1] = {"song_id": 1, "title": "song", "available": False,
                                   "link": "https://music.163.com/song?id=1"}
        result = await value.owner.agent_tools.execute(value.event, "musiccover", {"candidate": 1})
        assert result["status"] == "unavailable" and result["can_cover"] is False
        assert not result["sent"] and value.counts["search"] == 0
        assert result["song"]["song_id"] == 1
        replay = await value.owner.agent_tools.execute(value.event, "musiccover", {"candidate": 1, "action": "start"})
        assert replay["replayed"] and replay["status"] == "unavailable"
        unregister_active_runner(value.event.unified_msg_origin, value.runner)
    loop.run_until_complete(check())


def foreground_owner(tmp_path):
    from integrations.qq_astrbot.plugin.foreground_activity import ForegroundActivity
    from integrations.qq_astrbot.plugin.media_state import MediaState
    from integrations.qq_astrbot.plugin.song_tasks import SongTasks
    owner = object.__new__(Q1Guard)
    owner.foreground_activity = ForegroundActivity(tmp_path)
    owner.agent_store = AgentStore(tmp_path / 'agent.db')
    owner.latest_inbound = {}
    owner.media = SimpleNamespace(client=SimpleNamespace(state=MediaState(tmp_path)),
                                  song_tasks=SongTasks(tmp_path / 'songs.db'))
    return owner


@pytest.mark.parametrize('exit_path', ['stopped', 'empty_reply', 'exception', 'cancelled', 'delivered'])
def test_pipeline_releases_foreground_even_without_agent_or_reply(loop, tmp_path, exit_path):
    from astrbot.core.pipeline.scheduler import PipelineScheduler
    async def check():
        owner = foreground_owner(tmp_path)
        event = Event()
        incoming = Incoming('999', '101', 'm1', 'hello', '')
        event.set_extra('qq_incoming', incoming)
        started, finish = asyncio.Event(), asyncio.Event()
        class Admit:
            async def process(self, event):
                owner._observe_request(event, incoming)
                started.set()
        class Exit:
            async def process(self, event):
                await finish.wait()
                if exit_path == 'stopped':
                    event.stop_event()
                elif exit_path == 'exception':
                    raise RuntimeError('later pipeline stage failed')
                elif exit_path == 'delivered':
                    # Normal delivery already releases; task completion is idempotent.
                    owner._release_lease(event)
                # empty_reply has neither Agent lifecycle nor after_message_sent.
        scheduler = object.__new__(PipelineScheduler)
        scheduler.stages = [Admit(), Exit()]
        task = asyncio.create_task(scheduler._process_stages(event))
        await started.wait()
        assert owner.foreground_activity.blocked()
        await asyncio.sleep(0)
        assert owner.foreground_activity.blocked()  # Hold through awaited delivery.
        if exit_path == 'cancelled':
            task.cancel()
        else:
            finish.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        assert not owner.foreground_activity.blocked()
        assert event.get_extra('qq_foreground_activity') is None
        with owner.foreground_activity.connection() as db:
            since = db.execute('SELECT since FROM idle').fetchone()[0]
        assert owner.foreground_activity.blocked(60, now=since + 59)
        assert not owner.foreground_activity.blocked(60, now=since + 61)
        owner._release_lease(event)
        with owner.foreground_activity.connection() as db:
            assert db.execute('SELECT since FROM idle').fetchone()[0] == since
    loop.run_until_complete(check())


def test_finished_pipeline_keeps_other_qq_request_busy(loop, tmp_path):
    async def check():
        owner = foreground_owner(tmp_path)
        started = [asyncio.Event(), asyncio.Event()]
        finish = [asyncio.Event(), asyncio.Event()]
        async def pipeline(index):
            incoming = Incoming('999', str(101 + index), 'm' + str(index), 'hello', '')
            event = Event()
            event.set_extra('qq_incoming', incoming)
            owner._observe_request(event, incoming)
            started[index].set()
            await finish[index].wait()
        tasks = [asyncio.create_task(pipeline(i)) for i in range(2)]
        await asyncio.gather(*(ready.wait() for ready in started))
        finish[0].set()
        await tasks[0]
        await asyncio.sleep(0)
        assert owner.foreground_activity.blocked()
        finish[1].set()
        await tasks[1]
        await asyncio.sleep(0)
        assert not owner.foreground_activity.blocked()
    loop.run_until_complete(check())




def test_main_request_state_is_current_replaced_and_never_saved_as_chat(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [tool_call('musiccover', {'candidate': 1}), tool_call(), candidate('ready')])
        assert await execute(value) == ['ready']
        prompt = load_prompts()['agent_execution_state']
        states = []
        for contexts in value.provider.contexts:
            items = [m for m in contexts if m['role'] == 'system' and isinstance(m.get('content'), list)
                     and any(p.get('text', '').startswith(prompt) for p in m['content'])]
            # Native providers receive Message objects/parts, not raw strings.
            if not items:
                items = [m for m in contexts if m['role'] == 'system' and str(m.get('content', '')).startswith(prompt)]
            assert len(items) == 1
            content = items[0]['content']
            text = content if isinstance(content, str) else content[0]['text']
            states.append(json.loads(text.split('\n')[-1]))
        assert states[0]['registered_song_candidates'] == []
        assert states[1]['last_preflight_rejection']['result']['executed'] is False
        assert states[-1]['registered_song_candidates'] == [1] and states[-1]['source_lookup_attempts'] == 1
        assert prompt not in repr(value.runner.run_context.messages)
    loop.run_until_complete(check())






def test_native_summary_preserves_persona_and_shares_run_budget_without_thinking(loop, tmp_path):
    from astrbot.core.agent.context.compressor import LLMSummaryCompressor
    from integrations.qq_astrbot.plugin.agent_runtime import NativeAgentLifecycle
    async def check():
        value = await make_run(tmp_path, [])
        observed = []
        async def summary(**kwargs):
            observed.append(kwargs)
            return LLMResponse("assistant", completion_text="User requested a song; no action yet")
        value.provider.text_chat = summary
        compressor = LLMSummaryCompressor(value.provider)
        value.runner.request_context_manager.compressor = compressor
        NativeAgentLifecycle(value.runner, value.store, value.run, value.event)
        messages = [Message(role="system", content="PERSONA"),
            Message(role="system", content="CHAT JSON AND AGENT RULES"),
            Message(role="user", content="old request"), Message(role="assistant", content="actually sent"),
            Message(role="user", content="current request")]
        try:
            result = await compressor(messages)
            assert result[:2] == messages[:2] and result[-1] == messages[-1]
            assert value.run.requests == 1 and value.run.followups == 0
            wire = observed[0]
            assert wire["chat_template_kwargs"]["enable_thinking"] is False
            assert wire["reasoning_budget_tokens"] == 0 and wire["request_max_retries"] == 1
            assert wire["system_prompt"] == load_prompts()["agent_summary"]
            assert "PERSONA" not in repr(wire) and "CHAT JSON AND AGENT RULES" not in repr(wire)
            value.run.requests = value.run.max_requests - 1
            assert await compressor(messages) == messages
            assert len(observed) == 1  # Native fallback; no hidden over-budget request.
            value.store.observe(value.run.scope, "new input")
            assert await compressor(messages) == messages
            assert len(observed) == 1
        finally:
            unregister_active_runner(value.event.unified_msg_origin, value.runner)
    loop.run_until_complete(check())


def test_selected_song_job_waits_for_commit_and_retains_selection_on_recovery(tmp_path):
    from services.media_api.songs import SongStore
    store = SongStore(tmp_path)
    music = {"mode": "cover", "query": "song", "artist": "artist"}
    args = ("a" * 32, "qq:999:dm:101", music)
    store.submit(*args, selected_song={"song_id": 1, "title": "song"}, await_commit=True)
    assert store.next_job() is None
    assert store.get(args[0])["status"] == "awaiting_commit"
    store.commit(args[0], args[1])
    store.commit(args[0], args[1])
    assert store.next_job()["selection"] == {"song_id": 1}
    store.update(args[0], "running")
    store.recover()
    assert store.get(args[0])["status"] == "waiting_gpu"
    assert store.get(args[0])["song"]["song_id"] == 1


def test_weapi_and_selected_song_endpoints_use_real_source_contracts(tmp_path, monkeypatch):
    from services.media_api.app import SelectedSongPayload, WeapiPayload, create_app
    from services.media_api.song_source import NetEaseSource, Song
    monkeypatch.setenv("ASTRBOT_CHAT_STATE_DIR", str(tmp_path))
    counts = {"detail": 0}
    class Source:
        def search(self, query, page): return [Song("netease", 1, "song", "artist", 200, True)]
        def detail(self, song_id):
            counts["detail"] += 1
            return Song("netease", song_id, "song", "artist", 200, True)
        def playable_url(self, song_id): return "https://m.music.126.net/audio.mp3"
    monkeypatch.setattr(NetEaseSource, "from_environment", lambda: Source())
    routes = {r.path: r for r in create_app().routes}
    result = routes["/v1/music/weapi"].endpoint(WeapiPayload(scope="qq:999:dm:101", action="resolve", song_id=1))
    assert result["songs"][0]["audio_url"].endswith("audio.mp3")
    payload = SelectedSongPayload(request_id="b" * 32, scope="qq:999:dm:101", song_id=1,
                                 source_receipt=result["songs"][0]["source_receipt"],
                                 music={"mode": "original", "query": "song", "artist": "artist"})
    first = routes["/v1/songs/selected"].endpoint(payload)
    second = routes["/v1/songs/selected"].endpoint(payload)
    assert first["status"] == second["status"] == "awaiting_commit" and counts["detail"] == 1
    assert routes["/v1/songs/selected"].dependencies and routes["/v1/music/weapi"].dependencies
    # A receipt never authorizes another scope or a different selected song.
    from fastapi import HTTPException
    for changes in ({"scope": "qq:999:dm:102"}, {"song_id": 2}, {"source_receipt": "0" * 32}):
        invalid = payload.model_copy(update={"request_id": "c" * 32, **changes})
        with pytest.raises(HTTPException) as exc:
            routes["/v1/songs/selected"].endpoint(invalid)
        assert exc.value.status_code == 400
