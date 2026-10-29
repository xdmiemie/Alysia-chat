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

from integrations.qq_astrbot.plugin.agent_runtime import parse_candidate
from integrations.qq_astrbot.plugin.agent_state import AgentStore, BudgetExhausted, Superseded
from integrations.qq_astrbot.plugin.agent_tools import AgentTools
from integrations.qq_astrbot.plugin.main import Q1Guard
from integrations.qq_astrbot.plugin.message_router import Incoming
from integrations.qq_astrbot.plugin.prompt_loader import load_prompts
from scripts.qq_companion.probe_q3_local import LocalLoop


def candidate(text="ready"):
    return LLMResponse("assistant", completion_text=json.dumps({"reply": text, "thinking_level": 1, "voice": False}))


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

    def __init__(self, outputs, verdicts):
        self.outputs, self.verdicts = list(outputs), list(verdicts)
        self.main_calls, self.check_calls, self.close_calls, self.contexts = 0, 0, 0, []
        self.on_main = None
        self.on_check = None

    def get_model(self): return "fake"
    def meta(self): return SimpleNamespace(id="fake", model="fake", type="openai")

    async def text_chat(self, **kwargs):
        prompts = load_prompts()
        if kwargs.get("system_prompt") == prompts["agent_completion_check"]:
            self.check_calls += 1
            if self.on_check:
                await self.on_check()
            value = self.verdicts.pop(0) if self.verdicts else "finish"
            if isinstance(value, BaseException): raise value
            return LLMResponse("assistant", completion_text=value if value.startswith("{") else json.dumps(
                {"claims_music_started": False, "claims_music_completed": False, "claims_link_sent": False, "claims_audio_sent": False,
                 "decision": value, "instruction": "complete the missing detail" if value == "continue" else ""}))
        if kwargs.get("system_prompt") == prompts["agent_restricted_close"]:
            self.close_calls += 1
            assert kwargs.get("func_tool") is None
            return candidate("bounded close")
        self.main_calls += 1
        self.contexts.append([m.model_dump() if hasattr(m, "model_dump") else m for m in kwargs.get("contexts", [])])
        if self.on_main:
            await self.on_main()
        response = self.outputs.pop(0) if self.outputs else candidate("fallback candidate")
        if isinstance(response, BaseException): raise response
        return response


@pytest.fixture
def loop():
    value = LocalLoop()
    yield value
    value.run_until_complete(value.shutdown_asyncgens())
    value.close()


async def make_run(tmp_path, outputs, verdicts, *, limits=None):
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
    provider = Provider(outputs, verdicts)
    runner = ToolLoopAgentRunner()

    class Hooks(BaseAgentRunHooks):
        async def on_agent_begin(self, context): await owner.begin_native_agent(event, context)
        async def on_agent_done(self, context, response): await owner.unpack_paced_reply(event, context, response)

    await runner.reset(provider=provider, request=ProviderRequest(prompt="find a song", func_tool=ToolSet(tools)),
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


def test_native_delivery_utility_does_not_send_internal_tool_notices(loop, tmp_path):
    from astrbot.core.astr_agent_run_util import run_agent
    async def check():
        value = await make_run(tmp_path, [tool_call(), candidate("done")], ["finish"])
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


def test_native_tool_roundtrip_then_semantic_finish_without_draft_delivery(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [tool_call(), candidate("found it")], ["finish"])
        assert await execute(value) == ["found it"]
        assert value.counts["search"] == 1 and value.provider.check_calls == 1
        assert value.run.checks == 1
        assert any(m.get("role") == "tool" for m in value.provider.contexts[1])
        assert all(m.role != "assistant" for m in value.runner.run_context.messages)
        lifecycle = value.event.get_extra("qq_agent_lifecycle")
        lifecycle.sent("found it")
        assert value.runner.run_context.messages[-1].content == "found it"
    loop.run_until_complete(check())


def test_checker_receives_current_receipts_and_real_dialogue_without_rejected_drafts(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [candidate("draft"), tool_call(), candidate("final")], ["continue", "finish"])
        value.runner.run_context.messages.insert(0, Message(role="system", content="persona is not dialogue"))
        original, inputs = value.provider.text_chat, []
        async def chat(**kwargs):
            if kwargs.get("system_prompt") == load_prompts()["agent_completion_check"]:
                inputs.append(json.loads(kwargs["prompt"]))
            return await original(**kwargs)
        value.provider.text_chat = chat
        assert await execute(value) == ["final"]
        assert inputs[0]["actions"] == []
        assert inputs[0]["sent_this_run"] == []
        assert inputs[0]['delivery_facts'] == {'any_message_sent': False, 'link_sent': False, 'audio_sent': False}
        assert inputs[0]["current_request"] == "find a song"
        assert {t["name"] for t in inputs[0]["available_tools"]} == {"emotion", "music", "musiccover", "sticker", "weapi"}
        assert "draft" not in str(inputs[1]["dialogue"]) and "persona" not in str(inputs[1]["dialogue"])
        assert inputs[1]["actions"][0]["result"]["status"] == "completed"
        assert inputs[1]["actions"][0]["arguments"]["action"] == "search"
        assert inputs[1]['reply_delivery'] == {'state': 'after_approval', 'automatic_tts': True}
    loop.run_until_complete(check())


def test_checker_receives_accepted_background_job_as_foreground_release_boundary(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [tool_call(), tool_call('musiccover', {'candidate': 1, 'action': 'start'}), candidate('preparing')], ['finish'])
        original = value.owner.agent_tools.dispatch
        async def dispatch(event, name, args, key):
            if name == 'musiccover':
                return {'ok': True, 'status': 'queued', 'job_id': 'job', 'song': {'title': 'song'}, 'completed': False, 'sent': False}
            return await original(event, name, args, key)
        value.owner.agent_tools.dispatch = dispatch
        assert await execute(value) == ['preparing']
        with value.store.connection() as db:
            data = json.loads(db.execute("SELECT payload FROM audit WHERE run_id=? AND kind='check_input'", (value.run.run_id,)).fetchone()[0])
        assert data['background_tasks'] == [{'tool': 'musiccover', 'job_id': 'job', 'song': {'title': 'song'},
                                             'state': 'accepted_waiting_for_foreground_release'}]
        assert value.provider.check_calls == 1
    loop.run_until_complete(check())


@pytest.mark.parametrize("text", ["sticker:开心", "自然回话~ sticker:开心", "[已发送《歌曲》原版演唱] 回话"])
def test_control_markup_is_repaired_before_semantic_check_or_tts(loop, tmp_path, text):
    async def check():
        value = await make_run(tmp_path, [candidate(text), candidate("clean")], ["finish"])
        assert await execute(value) == ["clean"]
        assert value.run.checks == 1 and value.provider.main_calls == 2
        rejected = await value.owner.agent_tools.execute(value.event, "emotion", {"text": text, "emotion": "happy"})
        assert rejected["status"] == "rejected" and rejected["detail"] == "speech_contains_control_markup"
        from integrations.qq_astrbot.plugin.media_router import MediaRouter
        with pytest.raises(ValueError, match="speech_contains_control_markup"):
            MediaRouter.speech_texts(text, group=False)
    loop.run_until_complete(check())


def test_three_checks_are_shared_across_tools_and_internal_continuations(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [candidate("draft1"), tool_call(), candidate("draft2"),
                                         tool_call(call_id="call_2"), candidate("draft3")], ["continue"] * 3)
        assert await execute(value) == ["bounded close"]
        assert value.run.checks == 3 and value.provider.check_calls == 3 and value.provider.close_calls == 1
        assert value.counts["search"] == 1  # same action with a new native call ID is cached
        assert not any(m.get("role") == "user" and "complete the missing detail" in str(m) for ctx in value.provider.contexts for m in ctx)
        assert len([m for m in value.runner.run_context.messages if m.role == "user"]) == 1
    loop.run_until_complete(check())


@pytest.mark.parametrize("verdict", ["{}", TimeoutError("checker timed out")])
def test_bad_check_consumes_budget_and_cannot_retry_forever(loop, tmp_path, verdict):
    async def check():
        value = await make_run(tmp_path, [candidate() for _ in range(3)], [verdict] * 3)
        assert await execute(value) == ["bounded close"]
        assert value.run.checks == 3 and value.provider.check_calls == 3
    loop.run_until_complete(check())


def test_invalid_candidate_never_enters_semantic_check_and_global_budget_closes(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [LLMResponse("assistant", completion_text="not JSON") for _ in range(20)],
                               [], limits={"max_requests": 4})
        assert await execute(value) == ["bounded close"]
        assert value.run.requests == 3 and value.run.checks == 0
        assert value.provider.main_calls == 2 and value.provider.close_calls == 1
    loop.run_until_complete(check())


def test_repair_limit_is_shared_across_tool_steps_and_discards_bad_drafts(loop, tmp_path):
    async def check():
        bad = lambda text: LLMResponse('assistant', completion_text=text)
        value = await make_run(tmp_path, [bad('invalid one'), candidate('needs action'), tool_call(),
                                        bad('invalid two'), bad('invalid three')], ['continue'])
        assert await execute(value) == ['bounded close']
        assert value.provider.main_calls == 5 and value.run.checks == 1
        assert value.counts['search'] == 1 and value.provider.close_calls == 1
        assert not any('invalid one' in str(m) or 'invalid two' in str(m)
                       for context in value.provider.contexts[1:] for m in context)
        assert any(m.get('role') == 'tool' for m in value.provider.contexts[-1])
    loop.run_until_complete(check())


def test_restricted_close_gets_actual_state_instead_of_rejected_prose(loop, tmp_path):
    async def check():
        bad = candidate('[已发送《雨爱》整首翻唱]')
        value = await make_run(tmp_path, [bad, candidate('[已发送《雨爱》整首翻唱]')], [])
        original, inputs = value.provider.text_chat, []
        async def chat(**kwargs):
            if kwargs.get('system_prompt') == load_prompts()['agent_restricted_close']:
                inputs.extend(kwargs['contexts'])
            return await original(**kwargs)
        value.provider.text_chat = chat
        assert await execute(value) == ['bounded close']
        data = json.loads(inputs[0].content)
        assert data['actions'] == [] and data['sent_this_run'] == []
        assert '[已发送' not in str(data)
        assert value.provider.main_calls == 2
    loop.run_until_complete(check())


def test_format_repair_cannot_execute_unexpected_tools_and_restores_native_tools(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [LLMResponse('assistant', completion_text='not JSON'),
            tool_call(), candidate('needs search'), tool_call(call_id='real'), candidate('done')], ['continue', 'finish'])
        original, phases = value.provider.text_chat, []
        async def chat(**kwargs):
            if not kwargs.get('system_prompt'):
                phases.append(kwargs.get('func_tool') is None)
            return await original(**kwargs)
        value.provider.text_chat = chat
        assert await execute(value) == ['done']
        assert value.counts['search'] == 1 and value.run.tool_calls == 1
        assert phases == [False, True, True, False, False]
        assert value.run.checks == 2
    loop.run_until_complete(check())


def test_plain_text_tool_request_is_regenerated_as_native_call_once(loop, tmp_path):
    async def check():
        malformed = LLMResponse('assistant', completion_text=json.dumps({
            'name': 'weapi', 'arguments': {'action': 'search', 'query': 'discarded draft'}
        }))
        value = await make_run(tmp_path, [malformed, tool_call(), candidate('done')], ['finish'])
        original, choices = value.provider.text_chat, []
        async def chat(**kwargs):
            if kwargs.get('system_prompt') != load_prompts()['agent_completion_check']:
                choices.append(kwargs.get('tool_choice'))
            return await original(**kwargs)
        value.provider.text_chat = chat
        assert await execute(value) == ['done']
        assert choices == [None, {'type': 'function', 'function': {'name': 'weapi'}}, None]
        assert value.counts['search'] == value.run.tool_calls == 1
        with value.store.connection() as db:
            actions = db.execute('SELECT arguments FROM actions WHERE run_id=?', (value.run.run_id,)).fetchall()
            repairs = db.execute("SELECT payload FROM audit WHERE run_id=? AND kind='tool_protocol_repair'",
                                 (value.run.run_id,)).fetchall()
        assert len(actions) == 1 and json.loads(actions[0][0])['query'] == 'song'
        assert [json.loads(row[0]) for row in repairs] == [{'tool': 'weapi'}]
    loop.run_until_complete(check())


def test_unregistered_plain_text_tool_cannot_force_a_native_call(loop, tmp_path):
    async def check():
        malformed = LLMResponse('assistant', completion_text=json.dumps({
            'name': 'unregistered', 'arguments': {'query': 'song'}
        }))
        value = await make_run(tmp_path, [malformed, candidate('could not start')], ['finish'])
        original, choices = value.provider.text_chat, []
        async def chat(**kwargs):
            choices.append(kwargs.get('tool_choice'))
            return await original(**kwargs)
        value.provider.text_chat = chat
        assert await execute(value) == ['could not start']
        assert not any(choices) and value.counts['search'] == value.run.tool_calls == 0
    loop.run_until_complete(check())


def test_new_input_cancels_inflight_generation_and_preserves_only_real_history(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [candidate("stale")], ["finish"])
        entered = asyncio.Event()
        async def block():
            entered.set()
            await asyncio.Event().wait()
        value.provider.on_main = block
        task = asyncio.create_task(execute(value))
        await entered.wait()
        value.store.observe(value.run.scope, "m2")
        assert await task == []
        assert value.provider.check_calls == 0 and not value.event.sent
        assert all(m.role != "assistant" for m in value.runner.run_context.messages)
    loop.run_until_complete(check())


def test_checked_reply_is_discarded_if_input_changes_before_send(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [candidate("stale")], ["finish"])
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
    store.consume(run, "checks")
    key, old = store.reserve_action(run, "music", {"song_id": 1})
    assert old is None
    reopened = AgentStore(store.path)
    same = reopened.begin(run.scope, version, "m1")
    assert same.checks == 1
    assert reopened.reserve_action(same, "music", {"song_id": 1})[1]["status"] == "uncertain"


def test_manual_tool_schema_rejects_wrong_types_without_side_effect(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [tool_call(arguments={"action": "search", "query": []}), candidate()], ["finish"])
        await execute(value)
        assert value.counts["search"] == 0
        with value.store.connection() as db:
            assert db.execute("SELECT count(*) FROM audit WHERE kind='invalid_tool_arguments'").fetchone()[0] == 1
    loop.run_until_complete(check())


def test_new_input_during_checker_cancels_without_sending_checked_draft(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [candidate("stale")], ["finish"])
        async def interrupt(): value.store.observe(value.run.scope, "m2")
        value.provider.on_check = interrupt
        assert await execute(value) == []
        assert value.run.checks == 1
        assert all(m.role != "assistant" for m in value.runner.run_context.messages)
        assert value.counts["released"] >= 1
    loop.run_until_complete(check())


def test_send_guard_rechecks_after_semantic_approval(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [candidate()], ["finish"])
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
        value = await make_run(tmp_path, [candidate("long enough to delay")], ["finish"])
        await execute(value)
        value.event.set_extra("qq_reply_started_at", time.monotonic())
        value.event.result = MessageEventResult().message("old").set_result_content_type(ResultContentType.LLM_RESULT)
        task = asyncio.create_task(value.owner.delay_paced_reply(value.event))
        await asyncio.sleep(0)
        value.store.observe(value.run.scope, "m2")
        await asyncio.wait_for(task, timeout=0.5)
        assert value.event.result.chain == [] and value.counts["released"] >= 1
    loop.run_until_complete(check())


def test_onebot_guard_rechecks_after_async_media_encoding(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [], [])
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
        value = await make_run(tmp_path, [candidate("first.second.")], ["finish"])
        await execute(value)
        chains = [MessageEventResult().message("first."), MessageEventResult().message("second.")]
        value.event.set_extra("qq_delivery_chains", chains)
        value.event.set_extra("qq_reply_ready", True)
        async def fail(chain): raise OSError("transport failed")
        value.event.send = fail
        await value.owner.finish_paced_reply(value.event)
        assert [m.content for m in value.runner.run_context.messages if m.role == "assistant"] == ["first."]
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
        value = await make_run(tmp_path, [tool_call()], ["finish"])
        result = []
        try:
            async for response in value.runner.step_until_done(1):
                if response.type == "llm_result": result.append(response.data["chain"].get_plain_text())
        finally:
            unregister_active_runner(value.event.unified_msg_origin, value.runner)
        assert [x for x in result if x] == ["bounded close"]
        assert value.counts["search"] == 0
        assert len([m for m in value.runner.run_context.messages if m.role == "user"]) == 1
    loop.run_until_complete(check())


def test_restricted_close_rejects_unexpected_voice_action_even_if_provider_ignores_schema(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [candidate(), candidate(), candidate()], ["continue"] * 3)
        original = value.provider.text_chat
        async def chat(**kwargs):
            if kwargs.get("system_prompt") == load_prompts()["agent_restricted_close"]:
                return LLMResponse("assistant", completion_text=json.dumps(
                    {"reply": "unexpected synthesis", "thinking_level": 4, "voice": True}))
            return await original(**kwargs)
        value.provider.text_chat = chat
        assert await execute(value) == [load_prompts()["agent_budget_reply"]]
        assert value.event.get_extra("qq_voice") is False and value.run.checks == 3
    loop.run_until_complete(check())


def test_tool_budget_exhaustion_closes_without_semantic_retries(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [tool_call(), tool_call(call_id="call_2")], [], limits={"max_tools": 1})
        assert await execute(value) == ["bounded close"]
        assert value.run.tool_calls == 1 and value.run.checks == 0
    loop.run_until_complete(check())


def test_provider_fallback_does_not_reset_semantic_check_count(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [candidate("draft1"), RuntimeError("provider unavailable")], ["continue"])
        alternate = Provider([candidate("finished")], ["finish"])
        value.runner.fallback_providers = [alternate]
        assert await execute(value) == ["finished"]
        assert value.run.checks == 2
        assert value.provider.check_calls == alternate.check_calls == 1
    loop.run_until_complete(check())


def test_current_turn_overflow_closes_without_provider_error_or_replayed_tools(loop, tmp_path, monkeypatch):
    from integrations.qq_astrbot.plugin.context_budget import RequestBudget
    async def count(self, messages, tools=None):
        return 9000
    monkeypatch.setattr(RequestBudget, "count", count)
    async def check():
        value = await make_run(tmp_path, [tool_call()], [])
        value.provider.provider_config = {"id": "fake", "max_context_tokens": 8192}
        assert await execute(value) == [load_prompts()["agent_budget_reply"]]
        assert value.provider.main_calls == value.counts["search"] == 0
        assert value.run.closing and value.run.checks == 0
    loop.run_until_complete(check())


def test_final_provider_error_is_internal_and_cleanup_still_runs(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [RuntimeError("HTTP 400 private diagnostic")], [])
        assert await execute(value) == [load_prompts()["agent_budget_reply"]]
        assert value.event.get_extra("qq_agent_approved")
        value.event.set_extra("qq_reply_ready", True)
        value.event.set_extra("qq_delivery_chains", [MessageEventResult().message(load_prompts()["agent_budget_reply"])])
        await value.owner.finish_paced_reply(value.event)
        assert value.counts["released"] == 1
        assert value.provider.main_calls == 1
    loop.run_until_complete(check())


def test_context_replacement_does_not_reset_semantic_budget(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [candidate("draft1"), candidate("draft2"), candidate("draft3")], ["continue"] * 3)
        calls = []
        async def compact(messages, **kwargs):
            calls.append(len(messages))
            return [Message.model_validate(m.model_dump()) for m in messages]
        value.runner.request_context_manager.process = compact
        assert await execute(value) == ["bounded close"]
        assert value.run.checks == 3 and len(calls) == 3
    loop.run_until_complete(check())


def test_expired_global_deadline_stops_work_and_emits_only_bounded_close(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [tool_call()], [])
        value.run.started -= 1000
        result = await execute(value)
        assert len(result) == 1 and value.counts["search"] == 0 and value.provider.check_calls == 0
        assert not value.event.get_extra("qq_voice")
    loop.run_until_complete(check())


def test_admission_wait_cannot_outlive_global_budget(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [], [])
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
        value = await make_run(tmp_path, [], [])
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
        value = await make_run(tmp_path, [], [])
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
            value.event.set_extra('qq_agent_approved', True)
            value.event.set_extra('qq_agent_lifecycle', SimpleNamespace(sent=lambda text, modality:
                value.store.record(value.run, 'sent', {'text': text, 'modality': modality})))
            value.event.result = MessageEventResult().message(reply).set_result_content_type(ResultContentType.LLM_RESULT)
            await value.owner.delay_paced_reply(value.event)
            chains = value.event.get_extra('qq_delivery_chains')
            assert isinstance(chains[0].chain[0], Record)
            assert chains[-1].get_plain_text() == face and isinstance(chains[-1].chain[0], Plain)
            assert calls == ([spoken_text(reply)] if prepared else ['司凤版的《雨爱》正在准备中哦~', '稍等一下！'])
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


def test_emotion_tool_does_not_synthesize_a_face_only(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [], [])
        try:
            result = await value.owner.agent_tools.execute(value.event, 'emotion', {'text': '(๑•̀ㅂ•́)و✧', 'emotion': 'happy'})
            assert result['status'] == 'skipped' and result['error'] == 'no_spoken_text'
        finally:
            unregister_active_runner(value.event.unified_msg_origin, value.runner)
    loop.run_until_complete(check())


def test_cancel_music_tool_is_owner_scoped_and_does_not_require_new_search(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [], [])
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
        value = await make_run(tmp_path, [], [])
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
        value = await make_run(tmp_path, [], [])
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
        value = await make_run(tmp_path, [], [])
        owner = value.owner
        owner.guard = InboundGuard(tmp_path / 'inbound.db')
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
        value = await make_run(tmp_path, [], [])
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


def test_early_unknown_candidate_does_not_poison_later_selection(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [], [])
        try:
            tools = value.owner.agent_tools
            rejected = await tools.execute(value.event, "musiccover", {"candidate": 1})
            assert rejected["status"] == "rejected" and rejected["executed"] is False
            assert rejected["error"] == "candidate_not_registered" and rejected["required_next_step"] == "weapi_search"
            from integrations.qq_astrbot.plugin.agent_runtime import NativeAgentLifecycle
            lifecycle = NativeAgentLifecycle(value.runner, value.store, value.run, value.event)
            data = lifecycle.completion_input({"reply": "draft", "thinking_level": 1, "voice": False})
            assert data["draft_reply"]["reply"] == "draft" and "candidate" not in data
            assert data["preflight_rejections"][0]["result"]["required_next_step"] == "weapi_search"
            assert data["current_task_facts"]["source_lookup_attempts"] == 0
            assert data["actions"] == []  # No action key poisoned by preflight.
            value.run.remember_song({"song_id": 98765, "title": "song", "available": False})
            result = await tools.execute(value.event, "musiccover", {"candidate": 1})
            assert result["status"] == "unavailable" and not result.get("replayed")
            updated = lifecycle.completion_input()
            assert updated["preflight_rejections"] == []
            assert updated["current_task_facts"]["last_preflight_rejection"] is None
            with value.store.connection() as db:
                assert db.execute("SELECT COUNT(*) FROM audit WHERE run_id=? AND kind='tool_rejected'",
                                  (value.run.run_id,)).fetchone()[0] == 1
            assert updated["current_task_facts"]["known_songs"] == [{"candidate": 1, "title": "song",
                "artists": None, "aliases": None, "available": False}]
        finally:
            unregister_active_runner(value.event.unified_msg_origin, value.runner)
    loop.run_until_complete(check())


def test_main_request_state_is_current_replaced_and_never_saved_as_chat(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [tool_call('musiccover', {'candidate': 1}), tool_call(), candidate('ready')], ['finish'])
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


@pytest.mark.parametrize('claim', ['claims_music_started', 'claims_music_completed', 'claims_link_sent', 'claims_audio_sent'])
def test_finish_verdict_cannot_override_unverified_semantic_claims(loop, tmp_path, claim):
    async def check():
        raw = json.dumps({'claims_music_started': False, 'claims_music_completed': False,
            'claims_link_sent': False, 'claims_audio_sent': False, 'decision': 'finish', 'instruction': '', claim: True})
        value = await make_run(tmp_path, [candidate('sung and link sent'), candidate('not started')], [raw, 'finish'])
        assert await execute(value) == ['not started']
        assert value.run.checks == 2
        with value.store.connection() as db:
            verdict = json.loads(db.execute("SELECT payload FROM audit WHERE run_id=? AND kind='semantic_check' ORDER BY id LIMIT 1",
                                           (value.run.run_id,)).fetchone()[0])
        assert verdict['decision'] == 'continue' and verdict[claim] is True
    loop.run_until_complete(check())


def test_restricted_close_keeps_persona_and_disables_thinking(loop, tmp_path):
    async def check():
        value = await make_run(tmp_path, [], [])
        value.event.set_extra("qq_persona_prompt", "CURRENT PERSONA")
        observed = []
        async def close(**kwargs):
            observed.append(kwargs)
            return candidate("bounded close")
        value.provider.text_chat = close
        from integrations.qq_astrbot.plugin.agent_runtime import NativeAgentLifecycle
        value.runner.run_context.messages.insert(0, Message(role="assistant", content="OLD FAILURE SHOULD NOT OVERRIDE THIS RUN"))
        lifecycle = NativeAgentLifecycle(value.runner, value.store, value.run, value.event)
        try:
            assert (await lifecycle.restricted_close()).completion_text == candidate("bounded close").completion_text
            assert observed[0]["system_prompt"] == "CURRENT PERSONA\n" + load_prompts()["agent_restricted_close"]
            assert observed[0]["chat_template_kwargs"]["enable_thinking"] is False
            assert observed[0]["func_tool"] is None and value.run.requests == 1
            assert "OLD FAILURE" not in repr(observed[0])
        finally:
            unregister_active_runner(value.event.unified_msg_origin, value.runner)
    loop.run_until_complete(check())


def test_native_summary_preserves_persona_and_shares_run_budget_without_thinking(loop, tmp_path):
    from astrbot.core.agent.context.compressor import LLMSummaryCompressor
    from integrations.qq_astrbot.plugin.agent_runtime import NativeAgentLifecycle
    async def check():
        value = await make_run(tmp_path, [], [])
        observed = []
        async def summary(**kwargs):
            observed.append(kwargs)
            return LLMResponse("assistant", completion_text="User requested a song; no action yet")
        value.provider.text_chat = summary
        compressor = LLMSummaryCompressor(value.provider)
        value.runner.request_context_manager.compressor = compressor
        NativeAgentLifecycle(value.runner, value.store, value.run, value.event)
        messages = [Message(role="system", content="PERSONA AND CHAT JSON"),
            Message(role="user", content="old request"), Message(role="assistant", content="actually sent"),
            Message(role="user", content="current request")]
        try:
            result = await compressor(messages)
            assert result[0] == messages[0] and result[-1] == messages[-1]
            assert value.run.requests == 1 and value.run.checks == 0
            wire = observed[0]
            assert wire["chat_template_kwargs"]["enable_thinking"] is False
            assert wire["reasoning_budget_tokens"] == 0 and wire["request_max_retries"] == 1
            assert wire["system_prompt"] == load_prompts()["agent_summary"]
            assert "PERSONA AND CHAT JSON" not in repr(wire)
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
