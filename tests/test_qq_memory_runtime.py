"""Q5 request/worker integration with the pinned AstrBot types and fake inference."""
import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

if sys.version_info < (3, 12):
    pytest.skip("Q5 runtime needs the AstrBot Python 3.12 environment", allow_module_level=True)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "external/AstrBot"))

from astrbot.core.agent.message import Message
from astrbot.core.provider.entities import LLMResponse
from integrations.qq_astrbot.plugin.context_budget import RequestBudget
from integrations.qq_astrbot.plugin.persistent_memory import PersistentMemory, text_payload
from tests.test_qq_memory import remember
from scripts.qq_companion.probe_q3_local import LocalLoop


def test_qq_source_keeps_transport_event_day(tmp_path):
    from integrations.qq_astrbot.plugin.message_router import Incoming
    service=PersistentMemory(tmp_path, timezone_name="Asia/Hong_Kong")
    stamp=datetime.fromisoformat("2026-01-01T23:59:59+08:00").timestamp()
    assert service.enqueue(Incoming("999","101","late","source at midnight",occurred_at=stamp))
    with service.shared.connection() as db:
        row=db.execute("SELECT occurred,day FROM sm_events").fetchone()
        assert row[0]==stamp and row[1]=="2026-01-01"


@pytest.fixture
def loop():
    value = LocalLoop()
    yield value
    value.run_until_complete(value.shutdown_asyncgens())
    value.close()


class Provider:
    provider_config = {"id": "fake", "max_context_tokens": 4096,
                       "custom_extra_body": {"max_tokens": 512}, "api_base": "http://127.0.0.1:8080/v1"}

    def __init__(self, *outputs):
        self.outputs, self.calls = list(outputs), []

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        assert kwargs.get("func_tool") is None
        if kwargs.get("response_format", {}).get("json_schema", {}).get("schema", {}).get("required") == ["decisions"]:
            payload = json.loads(kwargs["contexts"][-1].content)
            known = {r["fact_key"] for r in payload["known_facts"]}
            value = {"decisions": [{"index": i, "supported": True, "durable": True,
                "existing_key": f["key"] if f["key"] in known else None}
                for i, f in enumerate(payload["candidates"])]}
            return LLMResponse("assistant", completion_text=json.dumps(value, ensure_ascii=False))
        if self.outputs:
            value = self.outputs.pop(0)
            if isinstance(value, BaseException):
                raise value
            if isinstance(value, LLMResponse):
                return value
        else:
            value = {"summary": "该用户偏好音乐，近期曾谈及相关话题。"}
        return LLMResponse("assistant", completion_text=json.dumps(value, ensure_ascii=False))


@pytest.fixture(autouse=True)
def token_counter(monkeypatch):
    async def count(self, messages, tools=None):
        # An exact deterministic tokenizer for sizing behavior; the live probe
        # separately exercises llama.cpp's real template/tokenize endpoints.
        return len(json.dumps([m.model_dump() for m in messages], ensure_ascii=False))
    monkeypatch.setattr(RequestBudget, "count", count)
    async def count_text(self, text):
        return len(text)
    monkeypatch.setattr(RequestBudget, "count_text", count_text)


def seed(service, text="private preference", scope="qq:999:dm:101", index="seed"):
    return service.shared.append_short_term(bot="999",character=service.character,module="qq",kind="qq_session_summary",
        event_id=index,body=text,scope=scope,visibility="session")


def test_session_summary_is_short_only_with_real_sources(loop,tmp_path):
    from integrations.qq_astrbot.plugin.message_router import Incoming
    async def check():
        service=PersistentMemory(tmp_path,timezone_name="UTC")
        service.enqueue(Incoming("999","101","m1","actual user content"))
        service.record_sent("qq:999:dm:101","actual sent answer",event_id="sent1")
        provider=Provider({"summary":"separate subjects and actual result"},{"entries":[]})
        assert await service.extract_one(provider)
        rows=service.shared.rows(bot="999",character="elysia")
        assert len(rows)==1 and rows[0]["short"] and not rows[0]["long"]
        assert all(c["func_tool"] is None for c in provider.calls)
        assert "actual user content" in str(provider.calls[0]["contexts"])
        assert not service.repository.retrieve("qq:999:dm:101","user")
    loop.run_until_complete(check())


def test_scoped_prompt_copy_real_usage_and_no_private_diary(loop,tmp_path):
    async def check():
        service=PersistentMemory(tmp_path,timezone_name="UTC")
        seed(service)
        service.shared.append_short_term(bot="999",character="elysia",module="diary",kind="diary_daily",
            event_id="private",body="private mixed diary",visibility="internal",derived=True,experience=False)
        original=request()
        prepared=await service.prepare(original,Provider(),"qq:999:dm:101")
        assert "private preference" in str(prepared)
        assert "private mixed diary" not in str(prepared)
        assert "relevant_memories" not in str(original)
        other=await service.prepare(request("qq:999:group:201"),Provider(),"qq:999:group:201")
        assert "private preference" not in str(other)
        usage=service.shared.get_request_usage(character="elysia")
        assert len(usage)==2 and usage[-1]["payload"]["memory_tokens"]>0
        assert usage[-1]["scope"]=="qq:999:dm:101"
        with pytest.raises(ValueError):
            await service.prepare(original,Provider(),"qq:999:dm:102")
    loop.run_until_complete(check())


@pytest.mark.parametrize("window",[2048,4096,32768])
def test_complete_budget_preserves_question_tools_and_never_injects_all(loop,tmp_path,window):
    async def check():
        service=PersistentMemory(tmp_path,timezone_name="UTC")
        for i in range(80): seed(service,text=f"memory {i} "+"x"*200,index=str(i))
        provider=Provider()
        provider.provider_config={**Provider.provider_config,"max_context_tokens":window}
        messages=request(text="KEEP CURRENT QUESTION")
        class Tools:
            def openai_schema(self): return [{"type":"function","function":{"name":"actual_tool"}}]
        result=await service.prepare(messages,provider,"qq:999:dm:101",Tools())
        assert "KEEP CURRENT QUESTION" in str(result)
        budget=RequestBudget(provider,"agent")
        assert await budget.count(result,Tools())+budget.output_tokens+budget.margin_tokens<=window
        data=json.loads(text_payload(result[-1])[0])
        assert len(data.get("relevant_memories",[]))<80
        assert not provider.calls
    loop.run_until_complete(check())


def test_invalid_output_and_tool_calls_keep_sources_pending(loop,tmp_path):
    from integrations.qq_astrbot.plugin.message_router import Incoming
    async def check():
        service=PersistentMemory(tmp_path,timezone_name="UTC")
        service.enqueue(Incoming("999","101","m1","real source"))
        provider=Provider(LLMResponse("tool",tools_call_name=["weapi"],tools_call_args=[{}]))
        assert not await service.extract_one(provider)
        assert service.pending_sessions()
        assert not service.shared.rows(bot="999",character="elysia")
        assert len(provider.calls)==1
    loop.run_until_complete(check())


def test_native_runner_recall_kept_out_of_saved_history(loop,tmp_path):
    from tests.test_qq_agent import make_run,execute
    async def check():
        service=PersistentMemory(tmp_path/"memory",timezone_name="UTC")
        seed(service)
        value=await make_run(tmp_path,[LLMResponse("assistant",completion_text='{"reply":"sent response","thinking_level":1,"voice":false}')],prepare_request=True)
        value.provider.provider_config={**Provider.provider_config,"max_context_tokens":32768}
        value.event.set_extra("qq_persistent_memory",service)
        assert await execute(value)==["sent response"]
        assert "private preference" in str(value.provider.contexts[0])
        assert "relevant_memories" not in str(value.runner.run_context.messages)
    loop.run_until_complete(check())


def test_scheduler_summarizes_with_no_video_account_and_closes(loop,tmp_path):
    from integrations.qq_astrbot.plugin.message_router import Incoming
    async def check():
        service=PersistentMemory(tmp_path,timezone_name="UTC",diary_enabled=False)
        service.enqueue(Incoming("999","101","m1","hello"))
        provider=Provider({"summary":"online conversation"},{"entries":[]})
        admitted=[]
        async def get_provider():return provider
        async def persona():return "persona"
        @asynccontextmanager
        async def admission():
            admitted.append(True)
            yield
        await service.start(get_provider,admission,persona)
        await service.worker_task
        assert admitted and not service.pending_sessions()
        assert len(service.scheduler.get_jobs())==2
        await service.close()
    loop.run_until_complete(check())


def request(scope="qq:999:dm:101", text="喜欢什么音乐"):
    return [Message(role="system", content="PERSONA AND RULES"), Message(role="user", content=json.dumps({
        "session_id": scope, "current_message": {"text": text}}, ensure_ascii=False))]


def facts(summary="101喜欢周杰伦", **changes):
    return {"facts": [{"key": "music_preference", "kind": "preference", "summary": summary,
        "evidence": "喜欢音乐", "tags": ["音乐"],
        "salience": 0.9, "confidence": 0.95, "sensitive": False, "shared_topic": "music", **changes}]}
