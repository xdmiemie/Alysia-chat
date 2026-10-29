"""Search retry budgets, permanent failures and recovery; no network or QQ."""

import json
import sqlite3
from types import SimpleNamespace

import pytest
import requests

from services.media_api.song_source import NetEaseSource, Song, SongSourceError
from services.media_api.songs import SongStore, select_song_with_retry
from scripts.qq_companion.cover_dispatcher import CoverDispatcher


MUSIC = {"mode": "original", "query": "测试歌", "artist": ""}
SONG = Song("netease", 1, "测试歌", "歌手", 220, True)


class Source:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = 0

    def search(self, query):
        self.calls += 1
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def response(status=200, data=None):
    result = requests.Response()
    result.status_code = status
    result.url = "https://music.163.com/weapi/test"
    result._content = json.dumps(data or {"code": 200}).encode()
    result._content_consumed = True
    return result


def test_two_transient_failures_then_success_without_changing_song():
    source = Source([requests.Timeout("private diagnostic"), requests.ConnectionError(), [SONG]])
    delays, attempts = [], []
    assert select_song_with_retry(source, MUSIC, sleep=delays.append,
        on_attempt=lambda attempt, error: attempts.append((attempt, error)))["song_id"] == 1
    assert source.calls == 3 and delays == [1, 2]
    assert attempts == [(1, None), (1, "source_timeout"), (2, None),
                        (2, "source_connection_error"), (3, None), (3, "")]


def test_interrupted_response_can_retry_once_then_succeed():
    source = Source([requests.exceptions.ChunkedEncodingError(), [SONG]])
    delays = []
    assert select_song_with_retry(source, MUSIC, sleep=delays.append)["song_id"] == 1
    assert source.calls == 2 and delays == [1]


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
@pytest.mark.parametrize("business", [False, True])
def test_transient_http_and_business_codes_stop_after_three(status, business):
    posts, delays = [], []
    def post(*args, **kwargs):
        posts.append(kwargs)
        return response(data={"code": status}) if business else response(status)
    source = NetEaseSource(session=SimpleNamespace(headers={}, post=post))
    with pytest.raises(SongSourceError) as failed:
        select_song_with_retry(source, MUSIC, sleep=delays.append)
    assert failed.value.code == f"source_{'code' if business else 'http'}_{status}"
    assert len(posts) == 3 and delays == [1, 2]


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_permanent_http_error_is_not_retried(status):
    source = Source([requests.HTTPError(response=response(status))])
    delays = []
    with pytest.raises(SongSourceError, match=f"source_http_{status}"):
        select_song_with_retry(source, MUSIC, sleep=delays.append)
    assert source.calls == 1 and delays == []


@pytest.mark.parametrize("failure", [requests.exceptions.SSLError(), ValueError("malformed"),
                                    SongSourceError("source_redirect_refused")])
def test_invalid_responses_tls_and_redirects_do_not_retry(failure):
    source = Source([failure])
    delays = []
    with pytest.raises(SongSourceError):
        select_song_with_retry(source, MUSIC, sleep=delays.append)
    assert source.calls == 1 and delays == []


def test_not_found_and_unavailable_sources_are_final_results():
    missing = Source([[]])
    with pytest.raises(SongSourceError, match="song_not_found"):
        select_song_with_retry(missing, MUSIC, sleep=lambda _: pytest.fail("No-match retry"))
    unavailable = Source([[Song("netease", 1, "测试歌", "歌手", 220, False)]])
    song = select_song_with_retry(unavailable, MUSIC, sleep=lambda _: pytest.fail("Unavailable retry"))
    assert not song["available"] and missing.calls == unavailable.calls == 1


def test_playability_failure_retries_the_complete_search_with_one_budget():
    calls, delays = [], []
    def post(url, **kwargs):
        calls.append(url)
        if url.endswith("search/get"):
            return response(data={"code": 200, "result": {"songs": [{"id": 1, "name": "测试歌", "duration": 220000}]}})
        if len(calls) == 2:
            return response(503)
        return response(data={"code": 200, "data": [{"id": 1, "code": 200, "url": "https://m.music.126.net/a.mp3"}]})
    source = NetEaseSource(session=SimpleNamespace(headers={}, post=post))
    assert select_song_with_retry(source, MUSIC, sleep=delays.append)["available"]
    assert len(calls) == 4 and delays == [1]


def test_cancel_during_backoff_prevents_another_network_attempt():
    source = Source([requests.Timeout()])
    canceled = []
    with pytest.raises(SongSourceError, match="song_search_canceled"):
        select_song_with_retry(source, MUSIC, should_stop=lambda: bool(canceled),
                               sleep=lambda _: canceled.append(True))
    assert source.calls == 1


def test_dispatcher_records_failures_and_does_not_requeue_finished_search(tmp_path, monkeypatch):
    monkeypatch.setattr("services.media_api.songs.time.sleep", lambda _: None)
    source = Source([requests.Timeout("sensitive upstream information")] * 3)
    jobs = SongStore(tmp_path)
    key, scope = "a" * 32, "qq:999:dm:101"
    jobs.submit(key, scope, MUSIC)
    def forbidden(*args):
        pytest.fail("Failed search must not start or stop GPU models")
    dispatcher = CoverDispatcher(tmp_path, broker=SimpleNamespace(file=object()), source=source,
        stop_models=forbidden, restore_models=forbidden, verify_gpu=forbidden, run_worker=forbidden)
    dispatcher.step()
    result = jobs.public(key, scope)
    assert result["status"] == "failed" and result["error"] == "source_timeout"
    assert result["search_attempts"] == 3 and result["search_error"] == "source_timeout"
    assert result["search_history"] == [{"attempt": i, "error": "source_timeout"} for i in (1, 2, 3)]
    assert "sensitive" not in json.dumps(result)
    assert jobs.submit(key, scope, MUSIC)["status"] == "failed"
    dispatcher.step()
    assert source.calls == 3


def test_restart_preserves_remaining_search_budget(tmp_path):
    jobs = SongStore(tmp_path)
    key, scope = "a" * 32, "qq:999:dm:101"
    jobs.submit(key, scope, MUSIC)
    jobs.update(key, "searching")
    for attempt in (1, 2):
        jobs.record_search_attempt(key, attempt, None)
        jobs.record_search_attempt(key, attempt, "source_timeout")
    restarted = SongStore(tmp_path)
    restarted.recover()
    source = Source([requests.Timeout()])
    def forbidden(*args):
        pytest.fail("No GPU work on search failure")
    dispatcher = CoverDispatcher(tmp_path, broker=SimpleNamespace(file=object()), source=source,
        stop_models=forbidden, restore_models=forbidden, verify_gpu=forbidden, run_worker=forbidden)
    dispatcher.step()
    assert source.calls == 1 and restarted.get(key)["search_attempts"] == 3


def test_exhausted_crashed_search_never_calls_source_again():
    source = Source([])
    with pytest.raises(SongSourceError, match="source_timeout"):
        select_song_with_retry(source, MUSIC, attempts_used=3, last_error="source_timeout")
    assert source.calls == 0


def test_recovered_worker_reuses_selected_song_without_another_search(tmp_path):
    jobs = SongStore(tmp_path)
    key, scope = "a" * 32, "qq:999:dm:101"
    jobs.submit(key, scope, MUSIC)
    jobs.update(key, "searching")
    jobs.record_search_attempt(key, 1, None)
    jobs.record_search_attempt(key, 1, "")
    song = SONG.public()
    song["link"] = "https://music.163.com/song?id=1"
    jobs.update(key, "running", song=song)
    jobs.recover()
    source = Source([])
    def work(job_id):
        jobs.output(job_id).parent.mkdir(parents=True)
        jobs.output(job_id).write_bytes(b"test audio")
    def forbidden(*args):
        pytest.fail("Original playback must not stop GPU models")
    CoverDispatcher(tmp_path, broker=SimpleNamespace(file=object()), source=source,
        stop_models=forbidden, restore_models=forbidden, verify_gpu=forbidden, run_worker=work).step()
    assert source.calls == 0 and jobs.get(key)["status"] == "completed"
    assert jobs.get(key)["search_attempts"] == 1


def test_legacy_song_database_migration_preserves_jobs(tmp_path):
    with sqlite3.connect(tmp_path / "songs.sqlite3") as db:
        db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY,scope TEXT,payload TEXT,status TEXT,song TEXT,error TEXT,updated REAL)")
        db.execute("INSERT INTO jobs VALUES (?,?,?,'queued','{}','',0)", ("a" * 32, "qq:999:dm:101", json.dumps(MUSIC)))
    for _ in range(2):
        result = SongStore(tmp_path).public("a" * 32, "qq:999:dm:101")
        assert result["status"] == "queued" and result["search_attempts"] == 0 and result["search_history"] == []
