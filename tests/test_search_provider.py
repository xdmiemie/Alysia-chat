"""Native provider wire keeps tool pairs, formats and existing scheduling."""
import json
from pathlib import Path
import sys
import pytest

if sys.version_info < (3, 12):
    pytest.skip("Pinned AstrBot requires Python 3.12", allow_module_level=True)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "external/AstrBot"))

import httpx
from openai import AsyncOpenAI
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from integrations.qq_astrbot.plugin.search_provider import request_search_chat
from integrations.qq_astrbot.plugin.web_search import SearchService
from scripts.qq_companion.probe_q3_local import LocalLoop


def test_background_tool_refusal_restores_json_format_on_wire(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRBOT_CHAT_MODEL_DEBUG_DIR", str(tmp_path / "debug"))
    async def check():
        bodies = []
        def respond(request):
            bodies.append(json.loads(request.content))
            message = {"role": "assistant", "content": '{"result":"normal answer"}'}
            finish = "stop"
            if len(bodies) == 1:
                message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_search", "type": "function",
                    "function": {"name": "web_search", "arguments": '{"query":"public topic"}'}}]}
                finish = "tool_calls"
            return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0, "model": "test",
                "choices": [{"index": 0, "finish_reason": finish, "message": message}]})
        provider = ProviderOpenAIOfficial({"id": "fake", "model": "test", "key": ["local"],
            "api_base": "http://localhost/v1", "custom_extra_body": {"response_format": {"type": "json_object"}}}, {})
        await provider.client.close()
        provider.client = AsyncOpenAI(api_key="local", base_url="http://localhost/v1", max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        search = SearchService(tmp_path, api_key="tvly-test", config={"enabled": True, "monthly_credit_limit": 0},
            transport=httpx.MockTransport(lambda r: pytest.fail("No network after quota exhausted")))
        fmt = {"type": "json_object"}
        consumed = []
        try:
            response = await request_search_chat(provider, phase="memory", contexts=[{"role": "user", "content": "task"}],
                response_format=fmt, search_service=search, before_request=lambda: consumed.append(True))
            assert json.loads(response.completion_text) == {"result": "normal answer"}
            assert len(bodies) == len(consumed) == 2
            assert "tools" in bodies[0] and "response_format" not in bodies[0]
            assert "tools" not in bodies[1] and bodies[1]["response_format"] == fmt
            assert bodies[1]["messages"][-1]["role"] == "tool"
            assert bodies[1]["messages"][-1]["tool_call_id"] == "call_search"
        finally:
            await provider.terminate()
    loop = LocalLoop()
    try:
        loop.run_until_complete(check())
    finally:
        loop.close()
