"""Candidate persistence and model-choice validation; no model, network or QQ."""
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from integrations.qq_astrbot.plugin.media_decision import parse_song_reply, parse_song_selection
from integrations.qq_astrbot.plugin.media_state import MediaState
from scripts.qq_companion.cover_dispatcher import CoverDispatcher
from services.media_api.app import SongSelectionPayload, create_app
from services.media_api.song_source import NetEaseSource, Song
from services.media_api.songs import SongStore

KEY, SCOPE = "a" * 32, "qq:999:dm:101"
MUSIC = {"mode": "cover", "query": "雨爱", "artist": ""}
CANDIDATES = [Song("netease", 1, "雨爱", "原唱歌手", 260, False).public(),
              Song("netease", 2, "雨爱DJ版", "DJ歌手", 220, True).public(),
              Song("netease", 3, "雨爱司凤版", "司凤", 250, True).public()]


@pytest.mark.parametrize("raw,valid,chosen", [('{"song_id":3}', True, 3), ('{"song_id":null}', True, None),
    ('{"song_id":true}', False, None), ('{"song_id":"3"}', False, None), ('{"song_id":99}', False, None),
    ('{}', False, None), ('选第三首', False, None)])
def test_only_model_candidate_ids_or_clarification_are_accepted(raw, valid, chosen):
    assert parse_song_selection(raw, CANDIDATES) == (valid, chosen)


def test_search_waits_for_agent_and_keeps_user_version_and_all_candidates(tmp_path):
    calls = []
    class Source:
        def search(self, query):
            calls.append(query)
            return [Song("netease", **{k: v for k, v in item.items() if k not in ("source", "aliases")})
                    for item in CANDIDATES]
    def forbidden(*args):
        pytest.fail("GPU work before model choice")
    jobs = SongStore(tmp_path)
    jobs.submit(KEY, SCOPE, MUSIC, user_request="翻唱雨爱的司凤版", request_context=[{"text": "想听司凤"}])
    dispatcher = CoverDispatcher(tmp_path, broker=SimpleNamespace(file=object()), source=Source(),
        stop_models=forbidden, restore_models=forbidden, verify_gpu=forbidden, run_worker=forbidden)
    dispatcher.step()
    result = SongStore(tmp_path).get(KEY)
    assert calls == ["雨爱"] and result["status"] == "awaiting_selection"
    assert result["user_request"] == "翻唱雨爱的司凤版"
    assert [s["song_id"] for s in result["search_results"]] == [1, 2, 3]
    assert result["song"] == {}
    jobs.recover()
    assert jobs.get(KEY)["status"] == "awaiting_selection"
    jobs.choose(KEY, SCOPE, 3)
    assert jobs.get(KEY)["song"]["title"] == "雨爱司凤版"
    jobs.choose(KEY, SCOPE, 3)
    with pytest.raises(ValueError):
        jobs.choose(KEY, SCOPE, 2)
    assert jobs.get(KEY)["selection"] == {"song_id": 3}


def test_unknown_cross_scope_and_late_canceled_choices_are_rejected(tmp_path):
    jobs = SongStore(tmp_path)
    jobs.submit(KEY, SCOPE, MUSIC)
    jobs.update(KEY, "searching")
    jobs.record_candidates(KEY, CANDIDATES)
    with pytest.raises(KeyError):
        jobs.choose(KEY, "qq:999:dm:102", 3)
    with pytest.raises(ValueError):
        jobs.choose(KEY, SCOPE, 99)
    jobs.cancel(KEY, SCOPE)
    with pytest.raises(ValueError):
        jobs.choose(KEY, SCOPE, 3)
    assert jobs.get(KEY)["status"] == "canceled"


@pytest.mark.parametrize("empty", [True, False])
def test_no_result_or_model_no_match_requests_clarification(tmp_path, empty):
    jobs = SongStore(tmp_path)
    jobs.submit(KEY, SCOPE, MUSIC)
    jobs.update(KEY, "searching")
    jobs.record_candidates(KEY, [] if empty else CANDIDATES)
    if not empty:
        jobs.choose(KEY, SCOPE, None)
    assert jobs.public(KEY, SCOPE)["status"] == "needs_clarification"
    assert not jobs.public(KEY, SCOPE)["has_audio"] and jobs.next_job() is None
    for i in range(3):
        jobs.submit(f"{i:032x}", SCOPE, MUSIC)  # Clarification frees the active queue slot.


def test_selected_unavailable_version_does_not_switch_to_playable_remix(tmp_path):
    jobs = SongStore(tmp_path)
    jobs.submit(KEY, SCOPE, MUSIC)
    jobs.update(KEY, "searching")
    jobs.record_candidates(KEY, CANDIDATES)
    jobs.choose(KEY, SCOPE, 1)
    def forbidden(*args):
        pytest.fail("Unavailable selected version must remain link only")
    CoverDispatcher(tmp_path, broker=SimpleNamespace(file=object()), stop_models=forbidden,
        restore_models=forbidden, verify_gpu=forbidden, run_worker=forbidden).step()
    result = jobs.public(KEY, SCOPE)
    assert result["status"] == "completed" and result["song"]["song_id"] == 1
    assert not result["has_audio"] and result["error"] == "source_unavailable_link_only"


def test_authenticated_selection_endpoint_validates_scope_and_strict_id(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRBOT_CHAT_STATE_DIR", str(tmp_path))
    jobs = SongStore(tmp_path)
    jobs.submit(KEY, SCOPE, MUSIC)
    jobs.update(KEY, "searching")
    jobs.record_candidates(KEY, CANDIDATES)
    endpoint = next(r for r in create_app().routes if r.path == "/v1/songs/{job_id}/select")
    assert endpoint.dependencies
    with pytest.raises(ValidationError):
        SongSelectionPayload(song_id=True)
    with pytest.raises(HTTPException) as bad:
        endpoint.endpoint(KEY, SCOPE, SongSelectionPayload(song_id=999))
    assert bad.value.status_code == 409
    assert endpoint.endpoint(KEY, SCOPE, SongSelectionPayload(song_id=3))["selection"] == {"song_id": 3}


def test_selection_audit_does_not_store_reasoning_text(tmp_path):
    state = MediaState(tmp_path)
    state.record_song_selection(KEY, 1, status="selected", raw='{"song_id":3}', song_id=3, reasoning_chars=200)
    with state.connection() as db:
        assert db.execute("SELECT raw,song_id,reasoning_chars FROM song_selection_attempts").fetchone() == ('{"song_id":3}', 3, 200)


def test_alias_version_and_full_clarification_are_preserved():
    song = NetEaseSource._song({"id": 3, "name": "雨爱", "artists": [{"name": "DJ歌手"}],
                               "alias": ["司凤版"], "album": {"name": "雨爱"}, "duration": 220000})
    assert song.public()["aliases"] == ["司凤版"] and song.album == "雨爱"
    text = "嗯…没有找到这个版本，具体叫什么呀？"
    assert parse_song_reply(json.dumps({"follow_up": text, "thinking_level": 2})) == text
    assert parse_song_reply('{"follow_up":null,"thinking_level":2}') is None
