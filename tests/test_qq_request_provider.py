"""Exercise the pinned Provider and real OpenAI SDK up to the HTTP body."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

if sys.version_info < (3, 12):
    pytest.skip('Pinned AstrBot requires Python 3.12', allow_module_level=True)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'external/AstrBot'))

import httpx
from openai import AsyncOpenAI
from astrbot.core.agent.tool import ToolSet
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from integrations.qq_astrbot.plugin.main import FOLLOWUP_FORMAT
from integrations.qq_astrbot.plugin.request_provider import RequestProvider, request_chat
from scripts.qq_companion.probe_q3_local import LocalLoop


@pytest.fixture(autouse=True)
def isolated_model_debug(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRBOT_CHAT_MODEL_DEBUG_DIR", str(tmp_path / "model_debug"))


def debug_records(tmp_path):
    return [record for path in sorted((tmp_path / "model_debug").glob("*.json"))
            for record in json.loads(path.read_text(encoding="utf-8"))]


def test_q7_actual_pixels_and_question_reach_pinned_provider_wire(tmp_path, monkeypatch):
    from io import BytesIO
    import base64
    from PIL import Image
    monkeypatch.setenv("ASTRBOT_CHAT_STATE_DIR", str(tmp_path))
    output = BytesIO()
    Image.new("RGB", (64, 48), "red").save(output, format="JPEG")
    pixels = "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode()
    async def check():
        bodies = []
        def respond(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "q7", "object": "chat.completion", "created": 0,
                "model": "qwen3", "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "red"}}]})
        provider = ProviderOpenAIOfficial({"id": "astrbot_chat_qwen3", "model": "qwen3", "key": ["local"],
            "api_base": "http://127.0.0.1:8080/v1", "modalities": ["text", "image"]}, {})
        await provider.client.close()
        provider.client = AsyncOpenAI(api_key="local", base_url="http://127.0.0.1:8080/v1", max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        try:
            await request_chat(provider, phase="agent", prompt="What color?", image_urls=[pixels, pixels], request_max_retries=1)
            current = bodies[0]["messages"][-1]["content"]
            assert any(p.get("type") == "text" and p["text"] == "What color?" for p in current)
            assert any(p.get("type") == "image_url" and p["image_url"]["url"] == pixels for p in current)
            positions = [i for i, p in enumerate(current) if p.get("type") == "image_url"]
            assert len(positions) == 2 and positions[1] > positions[0] + 1
        finally:
            await provider.terminate()
    loop = LocalLoop()
    try:
        loop.run_until_complete(check())
    finally:
        loop.close()


def test_final_main_and_tool_free_followup_options_reach_wire_without_shared_mutation(tmp_path):
    async def check():
        bodies = []
        def respond(request):
            body = json.loads(request.content)
            bodies.append(body)
            text = {"follow_up": None, "thinking_level": 1}
            return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0,
                "model": "qwen3", "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(text)}}]})
        provider = ProviderOpenAIOfficial({"id": "test", "model": "qwen3", "key": ["local"],
            "api_base": "http://127.0.0.1:8080/v1", "custom_extra_body": {
                "max_tokens": 1536, "reasoning_budget_tokens": 1024,
                "response_format": FOLLOWUP_FORMAT,
                "chat_template_kwargs": {"enable_thinking": True, "other": "keep"}}}, {})
        await provider.client.close()
        provider.client = AsyncOpenAI(api_key="local", base_url="http://127.0.0.1:8080/v1", max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        before = deepcopy(provider.provider_config)
        try:
            for phase in ("agent", "selection", "followup", "music_result", "compression"):
                await request_chat(provider, phase=phase, prompt=phase, request_max_retries=1)
            from integrations.qq_astrbot.plugin.agent_tools import AgentTools
            from integrations.qq_astrbot.plugin.prompt_loader import load_prompts
            from types import SimpleNamespace
            tools = ToolSet(AgentTools(SimpleNamespace()).registered())
            prompts = load_prompts()
            systems = [{"role": "system", "content": "CURRENT PERSONA"},
                       {"role": "system", "content": "\n".join((prompts["reply_output"], prompts["agent_policy"]))}]
            await RequestProvider(provider, phase="agent", response_format=FOLLOWUP_FORMAT).text_chat(
                prompt="main tools", contexts=systems, func_tool=tools,
                response_format={"type": "json_object"}, request_max_retries=1)
            await request_chat(provider, phase="agent", prompt="direct tools", func_tool=tools,
                response_format={"type": "json_object"}, request_max_retries=1)
            await request_chat(provider, phase="followup", prompt="post agent", func_tool=None,
                response_format=FOLLOWUP_FORMAT, max_tokens=160, temperature=0.5, request_max_retries=1)
            by_prompt = {body["messages"][-1]["content"]: body for body in bodies}
            for body in bodies[:5]:
                enabled = body["messages"][-1]["content"] in {"agent", "selection"}
                assert body["chat_template_kwargs"]["enable_thinking"] is enabled
                assert body["reasoning_budget_tokens"] == (1024 if enabled else 0)
                assert "phase" not in body and "thinking_phases" not in body
            main, followup = by_prompt["main tools"], by_prompt["post agent"]
            assert main["messages"][0] == {"role": "system", "content": "\n\n".join(m["content"] for m in systems)}
            assert sum(m["role"] == "system" for m in main["messages"]) == 1
            assert main["tool_choice"] == "auto" and "response_format" not in main
            assert "response_format" not in by_prompt["direct tools"]
            assert {tool["function"]["name"] for tool in main["tools"]} == {
                "emotion", "musiccover", "music", "sticker", "weapi"}
            assert "tools" not in followup and "tool_choice" not in followup
            assert followup["response_format"] == FOLLOWUP_FORMAT
            assert followup["max_tokens"] == 160 and followup["temperature"] == 0.5
            assert followup["chat_template_kwargs"] == {"enable_thinking": False, "other": "keep"}
            assert followup["reasoning_budget_tokens"] == 0
            assert provider.provider_config == before
            logs = debug_records(tmp_path)
            assert len(logs) == len(bodies) == 8
            assert [record["request"] for record in logs] == bodies
            assert [record["phase"] for record in logs] == [
                "agent", "selection", "followup", "music_result", "compression", "agent", "agent", "followup"]
            assert all(record["request_source"] == "http_body" for record in logs)
            assert all(record["response"]["choices"][0]["finish_reason"] == "stop" for record in logs)
            assert "authorization" not in json.dumps(logs).lower()
        finally:
            await provider.terminate()
    loop = LocalLoop()
    try:
        loop.run_until_complete(check())
    finally:
        loop.close()


def test_background_notifications_and_selection_use_wire_options_and_keep_persona(tmp_path):
    from types import SimpleNamespace
    from integrations.qq_astrbot.plugin.media_router import MediaRouter
    from integrations.qq_astrbot.plugin.prompt_loader import load_prompts
    from scripts.qq_companion.configure_model import configure
    async def check():
        bodies = []
        def respond(request):
            body = json.loads(request.content)
            bodies.append(body)
            text = {"candidate": 2} if body['messages'][0]['content'].startswith(load_prompts()['music_selection']) else {
                "follow_up": "ready", "thinking_level": 1}
            return httpx.Response(200, json={'id': 'test', 'object': 'chat.completion', 'created': 0,
                'model': 'qwen3', 'choices': [{'index': 0, 'finish_reason': 'stop',
                    'message': {'role': 'assistant', 'content': json.dumps(text)}}]})
        model = configure({}, 'qwen3', 'elysia')['provider'][0]
        provider = ProviderOpenAIOfficial({**model, 'key': ['local'], 'api_base': 'http://127.0.0.1:8080/v1'}, {})
        await provider.client.close()
        provider.client = AsyncOpenAI(api_key='local', base_url='http://127.0.0.1:8080/v1', max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        class Manager:
            async def get_provider_by_id(self, key):
                assert key == 'astrbot_chat_qwen3'
                return provider
        media = MediaRouter(SimpleNamespace(provider_manager=Manager()), tmp_path)
        payload = {'persona': 'CURRENT PERSONA', 'music': {'mode': 'cover', 'query': 'song'}, 'user_request': 'sing'}
        try:
            assert await media.result_line(payload, {'status': 'completed', 'song': {
                'song_id': 98765, 'title': 'song', 'album': 'album-secret'}, 'has_audio': True}, key='a'*32) == 'ready'
            candidates = [{'song_id': 98765 + i, 'title': 'song', 'album': 'album-secret'} for i in range(12)]
            assert await media.select_song('a'*32, payload, candidates) == 98766
            result_body, select_body = bodies
            assert result_body['chat_template_kwargs']['enable_thinking'] is False
            assert result_body['reasoning_budget_tokens'] == 0 and result_body['temperature'] == 0.5
            assert 'CURRENT PERSONA' in result_body['messages'][0]['content']
            assert 'album-secret' not in repr(bodies) and 'song_id' not in repr(bodies)
            assert select_body['chat_template_kwargs']['enable_thinking'] is True
            assert select_body['reasoning_budget_tokens'] == 1024 and select_body['top_k'] == 20
            assert len(json.loads(select_body['messages'][-1]['content'].split('\n')[-1])['candidates']['songs']) == 10
            assert [record["request"] for record in debug_records(tmp_path)] == bodies
        finally:
            await provider.terminate()
    loop = LocalLoop()
    try:
        loop.run_until_complete(check())
    finally:
        loop.close()


def test_post_agent_http_request_and_debug_log_keep_complete_system_and_disable_tools(tmp_path):
    from types import SimpleNamespace
    from astrbot.core.agent.message import Message
    from integrations.qq_astrbot.plugin.agent_runtime import NativeAgentLifecycle
    from integrations.qq_astrbot.plugin.agent_state import AgentStore
    from integrations.qq_astrbot.plugin.main import Q1Guard
    from integrations.qq_astrbot.plugin.prompt_loader import load_prompts
    async def check():
        prompts = load_prompts()
        main_system = "\n".join(["COMPLETE PERSONA", prompts["qq_context"]])
        rules_system = "\n".join([prompts["reply_output"], prompts["agent_policy"]])
        bodies, failures = [], []
        def respond(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "followup-test", "object": "chat.completion", "created": 0,
                "model": "qwen3", "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": '{"follow_up":null,"thinking_level":1}'}}]})
        provider = ProviderOpenAIOfficial({"id": "test", "model": "qwen3", "key": ["local"],
            "api_base": "http://127.0.0.1:8080/v1"}, {})
        await provider.client.close()
        provider.client = AsyncOpenAI(api_key="local", base_url="http://127.0.0.1:8080/v1", max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        store = AgentStore(tmp_path / "agent.sqlite3")
        version = store.observe("qq:999:dm:101", "message")
        run = store.begin("qq:999:dm:101", version, "message")
        run.remember_song({"song_id": 42, "title": "known song"})
        lifecycle = object.__new__(NativeAgentLifecycle)
        lifecycle.store, lifecycle.run = store, run
        lifecycle.runner = SimpleNamespace(provider=provider, req=SimpleNamespace(tool_calls_result=None))
        lifecycle.initial_tool_results, lifecycle.tool_outputs = 0, []
        lifecycle.initial_messages = [Message(role="system", content=main_system),
            Message(role="system", content=rules_system),
            Message(role="system", content=[{"type": "text", "text": "ADDITIONAL SYSTEM RULE"}]),
            Message(role="user", content="user request")]
        lifecycle.sent_messages = [Message(role="assistant", content="primary sent")]
        extras = {"qq_agent_run": run, "qq_agent_lifecycle": lifecycle,
                  "qq_persona_prompt": "OUTDATED PERSONA", "qq_primary_reply": "primary sent"}
        event = SimpleNamespace(get_extra=extras.get, set_extra=extras.__setitem__, get_group_id=lambda: "")
        owner = object.__new__(Q1Guard)
        owner.agent_store = store
        owner._event_active = lambda event: store.current(run.scope, version)
        owner.logger = SimpleNamespace(warning=lambda *args: failures.append(args))
        try:
            await owner._maybe_followup(event)
            assert not failures and len(bodies) == run.followups == 1
            body = bodies[0]
            texts = [m["content"] if isinstance(m["content"], str) else
                     "\n".join(part["text"] for part in m["content"]) for m in body["messages"] if m["role"] == "system"]
            assert len(texts) == 1
            assert texts[0].startswith(main_system + "\n\n" + rules_system)
            assert "ADDITIONAL SYSTEM RULE" in texts[0]
            assert "OUTDATED PERSONA" not in "\n".join(texts)
            assert texts[0].count(prompts["agent_execution_state"]) == 1
            state = texts[0].split(prompts["agent_execution_state"], 1)[1].strip().split("\n")[0]
            assert json.loads(state)["registered_song_candidates"] == [1]
            assert texts[0].endswith(prompts["followup_phase"] + "\n\n" + prompts["followup_system"])
            assert "tools" not in body and "tool_choice" not in body
            assert body["response_format"] == FOLLOWUP_FORMAT
            assert body["chat_template_kwargs"]["enable_thinking"] is False
            logs = debug_records(tmp_path)
            assert len(logs) == 1 and logs[0]["request"] == body and logs[0]["session_id"] == run.scope
        finally:
            await provider.terminate()
    loop = LocalLoop()
    try:
        loop.run_until_complete(check())
    finally:
        loop.close()


@pytest.mark.parametrize("native", [False, True])
def test_debug_keeps_complete_wire_response_before_sdk_and_astrbot_parsing(tmp_path, native):
    from types import SimpleNamespace
    from integrations.qq_astrbot.plugin.agent_tools import AgentTools
    async def check():
        requests = []
        message = {"role": "assistant", "content": '<think>private reasoning</think>{"name":"weapi"}',
                   "reasoning_content": "separate reasoning", "custom_message": {"preserved": True}}
        if native:
            message["tool_calls"] = [{"id": "call-real", "type": "function", "function": {
                "name": "weapi", "arguments": '{"action":"search","query":"song","page":1}'}}]
        raw_response = {"id": "chatcmpl-complete", "object": "chat.completion", "created": 123,
            "model": "qwen3", "choices": [{"index": 0, "finish_reason": "tool_calls" if native else "stop",
                "message": message, "logprobs": None}],
            "usage": {"prompt_tokens": 99, "completion_tokens": 12, "total_tokens": 111},
            "timings": {"predicted_ms": 12}, "unknown_top_level": [1, 2, 3]}
        def respond(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=raw_response)
        provider = ProviderOpenAIOfficial({"id": "test", "model": "qwen3", "key": ["local-secret"],
            "api_base": "http://127.0.0.1:8080/v1"}, {})
        await provider.client.close()
        provider.client = AsyncOpenAI(api_key="local-secret", base_url="http://127.0.0.1:8080/v1", max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        client, completions = provider.client, provider.client.chat.completions
        try:
            response = await request_chat(provider, phase="agent", session_id="debug-session",
                prompt="current request", system_prompt="system input",
                contexts=[{"role": "user", "content": "earlier request"},
                          {"role": "assistant", "content": "earlier reply"}],
                func_tool=ToolSet(AgentTools(SimpleNamespace()).registered()), request_max_retries=1)
            rows = debug_records(tmp_path)
            assert len(rows) == 1
            assert rows[0]["request"] == requests[0]
            assert rows[0]["response"] == raw_response
            assert rows[0]["session_id"] == "debug-session"
            assert rows[0]["status"] == "received" and rows[0]["http_status"] == 200
            assert [m["role"] for m in rows[0]["request"]["messages"]] == ["system", "user", "assistant", "user"]
            assert response.tools_call_name == (["weapi"] if native else [])
            assert provider.client is client and client.chat.completions is completions
            assert not client.is_closed() and client.api_key == "local-secret"
            assert "local-secret" not in json.dumps(rows)
        finally:
            await provider.terminate()
    loop = LocalLoop()
    try:
        loop.run_until_complete(check())
    finally:
        loop.close()


@pytest.mark.parametrize("failure", ["http", "connection", "cancel", "invalid_json"])
def test_debug_preserves_errors_cancellation_and_retries(tmp_path, failure):
    async def check():
        bodies = []
        def respond(request):
            bodies.append(json.loads(request.content))
            if failure == "connection":
                raise httpx.ConnectError("test connection failure", request=request)
            if failure == "cancel":
                raise asyncio.CancelledError()
            if failure == "invalid_json":
                return httpx.Response(200, content=b'{"invalid":', headers={"Content-Type": "application/json"})
            return httpx.Response(503, json={"error": {"message": "test unavailable", "code": "busy"}})
        provider = ProviderOpenAIOfficial({"id": "test", "model": "qwen3", "key": ["local"],
            "api_base": "http://127.0.0.1:8080/v1"}, {})
        await provider.client.close()
        provider.client = AsyncOpenAI(api_key="local", base_url="http://127.0.0.1:8080/v1", max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        try:
            with pytest.raises(asyncio.CancelledError if failure == "cancel" else Exception):
                await request_chat(provider, phase="agent", prompt="failure input",
                    request_max_retries=2 if failure == "http" else 1)
            rows = debug_records(tmp_path)
            assert len(rows) == len(bodies) == (2 if failure == "http" else 1)
            assert [row["request"] for row in rows] == bodies
            assert all(row["status"] == ("canceled" if failure == "cancel" else "error") for row in rows)
            assert all(row["error"]["type"] for row in rows)
            if failure == "http":
                assert all(row["http_status"] == 503 and row["response"]["error"]["code"] == "busy" for row in rows)
            if failure == "invalid_json":
                assert rows[0]["response"] == '{"invalid":'
        finally:
            await provider.terminate()
    loop = LocalLoop()
    try:
        loop.run_until_complete(check())
    finally:
        loop.close()
