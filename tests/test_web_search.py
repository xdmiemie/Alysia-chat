"""Free-only billing boundaries and cross-module graceful degradation."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json

import httpx
import pytest

from integrations.qq_astrbot.plugin.web_search import SearchService, free_account, rejected
from integrations.qq_astrbot.plugin.search_loop import complete_with_search, SearchContextOverflow, ToolCallStream


def usage(used=0, **account):
    return {"key": {"usage": used, "limit": None}, "account": {
        "current_plan": "Researcher", "plan_usage": used, "plan_limit": 1000,
        "paygo_usage": 0, "paygo_limit": None, **account}}


def service(tmp_path, handler, limit=1000, **kwargs):
    return SearchService(tmp_path, api_key="tvly-test-secret", config={"enabled": True, "monthly_credit_limit": limit},
                         transport=httpx.MockTransport(handler), **kwargs)


def test_success_uses_basic_only_and_returns_bounded_public_sources(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.path == "/usage":
            return httpx.Response(200, json=usage())
        assert request.headers["authorization"] == "Bearer tvly-test-secret"
        data = json.loads(request.content)
        assert data["search_depth"] == "basic" and data["auto_parameters"] is False
        assert data["include_answer"] is False and data["include_raw_content"] is False
        return httpx.Response(200, json={"results": [{"title": "Title", "url": "https://example.com", "content": "x" * 5000}]})
    search = service(tmp_path, handler)
    result = asyncio.run(search.execute("web_search", {"query": "public topic"}))
    assert result["ok"] and len(result["results"][0]["content"]) == 900
    assert [r.url.path for r in calls] == ["/usage", "/search"]
    assert search.status()["reserved_credits"] == 1
    assert "tvly-test-secret" not in json.dumps(result)
    assert b"tvly-test-secret" not in search.path.read_bytes()


@pytest.mark.parametrize("account", [
    {"plan_usage": 1000}, {"plan_usage": 999.2}, {"current_plan": "Project", "plan_limit": 4000},
    {"paygo_limit": 10}, {"paygo_usage": 1}, {"plan_limit": None}, {"plan_usage": "0"}, {"plan_usage": float("inf")},
])
def test_remote_budget_or_unverifiable_billing_never_issues_search(tmp_path, account):
    calls = []
    def handler(request):
        calls.append(request.url.path)
        assert request.method == "GET"
        # JSON cannot encode infinity; mock the response bytes for that case.
        return httpx.Response(200, content=json.dumps(usage(**account)))
    result = asyncio.run(service(tmp_path, handler).execute("web_search", {"query": "test"}))
    assert not result["ok"] and result["continue_without_search"] and calls == ["/usage"]


def test_provider_key_limit_also_restricts_search(tmp_path):
    def handler(request):
        assert request.method == "GET"
        data = usage(20)
        data["key"]["limit"] = 20
        return httpx.Response(200, json=data)
    assert asyncio.run(service(tmp_path, handler).execute("web_search", {"query": "test"}))["reason"] == "free_quota_exhausted"


def test_local_limit_survives_restart_and_key_rotation(tmp_path):
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json=usage() if request.method == "GET" else {"results": []})
    assert asyncio.run(service(tmp_path, handler, 1).execute("web_search", {"query": "first"}))["ok"]
    restarted = service(tmp_path, handler, 1)
    restarted.api_key = "tvly-other-key"
    result = asyncio.run(restarted.execute("web_search", {"query": "second"}))
    assert result["reason"] == "free_quota_exhausted" and calls == ["GET", "POST"]


def test_atomic_reservations_share_budget_across_instances(tmp_path):
    # Different SQLite connections, as used by chat and worker processes.
    def reserve(_):
        return service(tmp_path, lambda r: None).reserve(998, 1000, 2, 1000)
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(reserve, range(20)))
    assert values.count("") == 2 and values.count("free_quota_exhausted") == 18


@pytest.mark.parametrize("status", [402, 432, 433])
def test_upstream_quota_rejection_latches_until_new_month(tmp_path, status):
    clock = [datetime(2026, 11, 4, tzinfo=timezone.utc).timestamp()]
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json=usage()) if request.method == "GET" else httpx.Response(status)
    search = service(tmp_path, handler, clock=lambda: clock[0])
    assert asyncio.run(search.execute("web_search", {"query": "one"}))["reason"] == "free_quota_exhausted"
    assert asyncio.run(search.execute("read_webpage", {"url": "https://example.com"}))["reason"] == "free_quota_exhausted"
    assert calls == ["GET", "POST"]
    clock[0] = datetime(2026, 12, 1, tzinfo=timezone.utc).timestamp()
    assert search.status()["available"]


def test_timeout_keeps_reservation_and_has_no_automatic_retry(tmp_path):
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json=usage())
        raise httpx.ReadTimeout("ambiguous delivery")
    search = service(tmp_path, handler, 1)
    result = asyncio.run(search.execute("web_search", {"query": "test"}))
    assert result["continue_without_search"] and calls == ["GET", "POST"]
    assert search.status()["reserved_credits"] == 1
    assert not asyncio.run(search.execute("web_search", {"query": "test again"}))["ok"]
    assert len(calls) == 2


@pytest.mark.parametrize("name,args", [
    ("web_search", {"query": "test", "search_depth": "advanced"}),
    ("read_webpage", {"url": "http://127.0.0.1/private"}),
    ("read_webpage", {"url": "file:///etc/passwd"}),
    ("shell", {"query": "test"}),
])
def test_invalid_calls_never_reach_network(tmp_path, name, args):
    def handler(request):
        pytest.fail("unexpected outbound request")
    assert not asyncio.run(service(tmp_path, handler).execute(name, args))["ok"]


def test_extract_uses_same_ledger_and_conservative_credit(tmp_path):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=usage())
        data = json.loads(request.content)
        assert data["extract_depth"] == "basic" and len(data["urls"]) == 1
        return httpx.Response(200, json={"results": [{"url": "https://example.com", "raw_content": "page"}], "usage": {"credits": .2}})
    search = service(tmp_path, handler, 1)
    assert asyncio.run(search.execute("read_webpage", {"url": "https://example.com"}))["ok"]
    assert asyncio.run(search.execute("web_search", {"query": "next"}))["reason"] == "free_quota_exhausted"


def completion(content="", calls=None):
    return {"choices": [{"finish_reason": "tool_calls" if calls else "stop", "message": {
        "role": "assistant", "content": content, **({"tool_calls": calls} if calls else {})}}]}


def tool_call(name="web_search", args=None, id="call1"):
    return {"id": id, "type": "function", "function": {"name": name, "arguments": json.dumps(args or {"query": "test"})}}


FORMAT = {"type": "json_schema", "json_schema": {"name": "test", "schema": {
    "type": "object", "properties": {"reply": {"type": "string"}}, "required": ["reply"]}}}


def test_exhaustion_closes_tools_and_preserves_normal_structured_reply(tmp_path):
    async def check():
        seen = []
        async def call(messages, schemas, fmt):
            seen.append((messages, schemas, fmt))
            if len(seen) == 1:
                return completion(calls=[tool_call(), tool_call(id="call2")])
            assert schemas is None and fmt == FORMAT
            receipts = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            assert len(receipts) == 2 and all(r["continue_without_search"] for r in receipts)
            return completion('{"reply":"I can still answer using known information."}')
        result = await complete_with_search(call, [{"role": "user", "content": "question"}], FORMAT,
            service=service(tmp_path, lambda r: pytest.fail("quota zero must not issue HTTP"), 0))
        assert len(seen) == 2 and json.loads(result["choices"][0]["message"]["content"])["reply"]
    asyncio.run(check())


def test_read_and_search_receipts_are_paired_and_final_grammar_restored(tmp_path):
    async def check():
        search = service(tmp_path, lambda r: httpx.Response(200, json=usage() if r.method == "GET" else {"results": []}))
        rounds = []
        async def call(messages, schemas, fmt):
            rounds.append((messages, schemas, fmt))
            if len(rounds) == 1:
                return completion(calls=[tool_call()])
            if len(rounds) == 2:
                return completion(calls=[tool_call("read_webpage", {"url": "https://example.com"}, "call2")])
            assert schemas is None and fmt == FORMAT
            assert [m["tool_call_id"] for m in messages if m["role"] == "tool"] == ["call1", "call2"]
            return completion('{"reply":"done"}')
        await complete_with_search(call, [{"role": "user", "content": "question"}], FORMAT, service=search)
        assert search.status()["reserved_credits"] == 2 and len(rounds) == 3
    asyncio.run(check())


def test_context_pressure_falls_back_to_original_task(tmp_path):
    async def check():
        original = [{"role": "user", "content": "mandatory material"}]
        async def call(messages, tools, fmt):
            if tools:
                raise SearchContextOverflow()
            assert messages == original and fmt == FORMAT
            return completion('{"reply":"done"}')
        await complete_with_search(call, original, FORMAT, service=service(tmp_path, lambda r: None))
    asyncio.run(check())


def test_streamed_tool_arguments_are_assembled_before_execution():
    stream = ToolCallStream()
    stream.add([{"index": 0, "id": "a", "function": {"name": "web_search", "arguments": '{"que'}}])
    stream.add([{"index": 0, "function": {"arguments": 'ry":"test"}'}}])
    call = stream.message("")["tool_calls"][0]
    assert call["id"] == "a" and call["function"]["name"] == "web_search"
    assert json.loads(call["function"]["arguments"]) == {"query": "test"}
