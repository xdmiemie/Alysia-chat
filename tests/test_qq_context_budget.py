from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

if sys.version_info < (3, 12):
    pytest.skip("Pinned AstrBot requires Python 3.12", allow_module_level=True)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "external/AstrBot"))

from astrbot.core.agent.message import Message
from integrations.qq_astrbot.plugin.context_budget import RequestBudget, ContextCapacityError, tool_result_view
from scripts.qq_companion.probe_q3_local import LocalLoop


@pytest.fixture
def loop():
    value = LocalLoop()
    yield value
    value.close()


def test_song_table_preserves_all_versions_and_resolve_urls_without_receipts():
    songs = [{"song_id": i, "title": "title", "artists": "artist", "album": "version", "aliases": ["live"],
              "duration_seconds": 20, "available": False, "link": "https://music.163.com/song?id=" + str(i),
              "source_receipt": "private-program-receipt", "audio_url": "https://m.music.126.net/audio"} for i in range(20)]
    original = {"ok": True, "status": "completed", "songs": songs}
    view = tool_result_view(original)
    recovered = [dict(zip(view["song_columns"], row)) for row in view["songs"]]
    assert len(recovered) == 20
    assert all(row["aliases"] == ["live"] and "album" not in row and "song_id" not in row for row in recovered)
    assert recovered[-1]["candidate"] == 20 and recovered[-1]["audio_url"] == songs[-1]["audio_url"]
    assert "source_receipt" not in repr(view)
    assert original["songs"][0]["source_receipt"] == "private-program-receipt"


def test_budget_drops_whole_old_round_and_keeps_current_tool_pair(loop):
    async def check():
        budget = RequestBudget(SimpleNamespace(provider_config={"max_context_tokens": 8192}))
        observed = []
        async def count(messages, tools):
            observed.append(tools)
            return 9000 if any(m.content == "old" for m in messages) else 6000
        budget.count = count
        tools = object()
        messages = [Message(role="system", content="persona"), Message(role="system", content="reply and agent rules"),
            Message(role="user", content="old"),
            Message(role="assistant", content="already sent"), Message(role="user", content="current"),
            Message(role="assistant", content="", tool_calls=[{"id": "call", "type": "function", "function": {"name": "weapi", "arguments": "{}"}}]),
            Message(role="tool", content="result", tool_call_id="call")]
        fitted, count, dropped = await budget.fit(messages, tools)
        assert dropped == 1 and count == 6000
        assert [m.role for m in fitted] == ["system", "system", "user", "assistant", "tool"]
        assert [m.content for m in fitted[:2]] == ["persona", "reply and agent rules"]
        assert fitted[-1].tool_call_id == fitted[-2].tool_calls[0].id
        assert all(item is tools for item in observed)
    loop.run_until_complete(check())


def test_single_large_current_turn_fails_instead_of_sending_overflow(loop):
    async def check():
        budget = RequestBudget(SimpleNamespace(provider_config={"max_context_tokens": 8192}))
        async def count(messages, tools):
            return 6500  # Fits the window alone, but leaves insufficient output space.
        budget.count = count
        with pytest.raises(ContextCapacityError):
            await budget.fit([Message(role="user", content="current")])
    loop.run_until_complete(check())


def test_budget_uses_provider_output_and_margin_from_shared_config(loop):
    async def check():
        provider = SimpleNamespace(provider_config={'max_context_tokens': 16384,
            'context_margin_tokens': 512, 'custom_extra_body': {'max_tokens': 8192}})
        budget = RequestBudget(provider)
        async def count(messages, tools): return 8000
        budget.count = count
        with pytest.raises(ContextCapacityError):
            await budget.fit([Message(role='user', content='current')])
        provider.provider_config['max_context_tokens'] = 32768
        budget = RequestBudget(provider)
        budget.count = count
        assert (await budget.fit([Message(role='user', content='current')]))[1] == 8000
    loop.run_until_complete(check())


def test_candidate_references_survive_pages_and_hide_internal_metadata():
    from integrations.qq_astrbot.plugin.agent_state import AgentRun
    import time
    run = AgentRun("run", "scope", 1, time.monotonic())
    songs = [{"song_id": 90001, "title": "first", "album": "private album"},
             {"song_id": 90002, "title": "second", "album": "private album"}]
    for song in songs:
        run.remember_song(song)
    run.remember_song(songs[0])
    run.remember_song({"song_id": 90003, "title": "next page"})
    assert run.song_id(1) == 90001 and run.song_id(3) == 90003
    assert len(run.candidate_refs) == 3
    result = tool_result_view({"song": songs[1]}, run)
    assert result == {"song": {"candidate": 2, "title": "second"}}
    assert run.candidates[90002]["album"] == "private album"


def test_budget_uses_same_phase_thinking_as_provider_request():
    provider = SimpleNamespace(provider_config={"custom_extra_body": {
        "chat_template_kwargs": {"enable_thinking": True}}, "thinking_phases": {"followup": False}})
    assert RequestBudget(provider, "agent").enable_thinking is True
    assert RequestBudget(provider, "followup").enable_thinking is False
