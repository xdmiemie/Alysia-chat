from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import io
import json
import wave

import pytest

from integrations.qq_astrbot.plugin.media_decision import parse_media
from integrations.qq_astrbot.plugin.media_state import MediaState
from services.media_api.songs import SongStore, select_song
from services.media_api.song_source import NetEaseSource, Song
from scripts.qq_companion.cover_dispatcher import CoverDispatcher


def wav_bytes():
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 16000)
    return out.getvalue()


def music(mode="cover"):
    return {"mode": mode, "query": "测试歌", "artist": ""}


def test_same_model_decision_types_and_persona_override():
    assert parse_media('{"voice":true}', voice_input=False).voice
    assert not parse_media('{"voice":false}', voice_input=True).voice
    assert parse_media('{}', voice_input=True).voice
    assert not parse_media('{"voice":"true"}').voice
    assert parse_media('{"music":{"mode":"cover","query":"测试歌"}}').music == music()
    assert parse_media('{"music":{"mode":"shell","query":"cmd"},"sticker":"../../secret"}').music is None
    assert parse_media('not JSON').music is None


def test_cross_process_admission_and_interrupted_send(tmp_path):
    first, second = MediaState(tmp_path), MediaState(tmp_path)
    lease = first.enter()
    second.set_mode("draining")
    assert second.active() == 1 and first.enter() is None
    first.leave(lease)
    assert second.active() == 0
    assert first.remember("job", {"scope": "qq:999:dm:101"})
    assert not second.remember("job", {})
    assert second.claim_send("job") and not first.claim_send("job")
    assert second.recover() == []
    with first.connection() as db:
        assert db.execute("SELECT state FROM delivery WHERE id='job'").fetchone()[0] == "uncertain"


def test_dead_activity_cannot_block_recovery(tmp_path):
    state = MediaState(tmp_path)
    with state.connection() as db:
        db.execute("INSERT INTO activity VALUES ('old',99999999,0)")
    assert state.active() == 0


def test_song_reply_audit_migrates_without_losing_pending_delivery(tmp_path):
    state = MediaState(tmp_path)
    assert state.remember("job", {"scope": "qq:999:dm:101"})
    with state.connection() as db:
        db.execute("DROP TABLE song_reply_attempts")
    state = MediaState(tmp_path)
    state.record_song_reply("job", 1, status="rejected", raw="not JSON", error="invalid_model_output")
    state.record_song_reply("job", 2, status="generated", raw='{"follow_up":"模型回话"}', reply="模型回话")
    with state.connection() as db:
        assert db.execute("SELECT state FROM delivery WHERE id='job'").fetchone()[0] == "pending"
        assert db.execute("SELECT attempt,status,reply,error FROM song_reply_attempts ORDER BY id").fetchall() == [
            (1, "rejected", "", "invalid_model_output"), (2, "generated", "模型回话", "")]


def test_jobs_scope_idempotency_limit_cancel_restart(tmp_path):
    jobs = SongStore(tmp_path)
    key, scope = "a" * 32, "qq:999:dm:101"
    assert jobs.submit(key, scope, music())["status"] == "queued"
    assert jobs.submit(key, scope, music())["id"] == key
    with pytest.raises(ValueError):
        jobs.submit(key, "qq:999:group:201", music())
    with pytest.raises(KeyError):
        jobs.public(key, "qq:999:dm:102")
    jobs.update(key, "running")
    SongStore(tmp_path).recover()
    assert jobs.get(key)["status"] == "queued"
    jobs.cancel(key, scope)
    jobs.update(key, "completed")
    assert jobs.get(key)["status"] == "canceled"
    for i in range(3):
        jobs.submit(f"{i:032x}", scope, music())
    with pytest.raises(ValueError, match="full"):
        jobs.submit("b" * 32, scope, music())
    with pytest.raises(ValueError):
        jobs.output("../../secret")


class Source:
    def search(self, query):
        return [Song("netease", 1, "测试歌", "歌手", 220, True),
                Song("netease", 2, "测试歌 (现场)", "歌手", 230, True)]


def test_ten_song_searches_and_five_wrong_songs():
    for _ in range(10):
        assert select_song(Source(), music())["song_id"] == 1
    for i in range(5):
        with pytest.raises(ValueError, match="song_not_found"):
            select_song(Source(), {"query": f"错误歌{i}", "artist": ""})
    with pytest.raises(ValueError):
        select_song(Source(), {"query": "测试歌", "artist": "另一位歌手"})


def test_weapi_does_not_download_trials_or_bad_hosts(monkeypatch, tmp_path):
    source = NetEaseSource()
    monkeypatch.setattr(source, "_post", lambda *a: {"data": [{"id": 1, "code": 200,
        "url": "https://m.music.126.net/song.mp3", "freeTrialInfo": {"start": 0, "end": 30}}]})
    assert source._playable_ids([1]) == set()
    with pytest.raises(PermissionError):
        source.playable_url(1)
    monkeypatch.setattr(source, "_post", lambda *a: {"data": [{"id": 1, "code": 200,
        "url": "http://127.0.0.1/secret"}]})
    with pytest.raises(ValueError, match="host"):
        source.playable_url(1)


@pytest.mark.parametrize("fault", [None, "stop", "vram", "worker", "restore", "cancel"])
def test_cover_exclusive_order_and_restore_on_every_failure(tmp_path, fault):
    state, jobs = MediaState(tmp_path), SongStore(tmp_path)
    key, scope = "a" * 32, "qq:999:dm:101"
    jobs.submit(key, scope, music())
    events = []

    def stop(names):
        assert state.mode() == "draining" and state.active() == 0
        names.append("qwen3")
        events.append("stop")
        if fault == "stop":
            raise RuntimeError("test")

    def verify():
        events.append("vram")
        if fault == "vram":
            raise RuntimeError("test")

    def work(job_id):
        assert state.mode() == "cover" and state.enter() is None
        events.append("worker")
        if fault == "worker":
            raise RuntimeError("test")
        jobs.output(job_id).parent.mkdir(parents=True)
        jobs.output(job_id).write_bytes(wav_bytes())
        if fault == "cancel":
            jobs.cancel(job_id, scope)

    def restore(names):
        assert state.mode() == "restoring" and names == ["qwen3"]
        events.append("restore")
        if fault == "restore":
            raise RuntimeError("test")

    dispatcher = CoverDispatcher(tmp_path, broker=SimpleNamespace(file=object()),
        stop_models=stop, restore_models=restore, verify_gpu=verify, run_worker=work, source=Source())
    dispatcher.step()
    assert jobs.get(key)["status"] == "awaiting_selection" and events == []
    jobs.choose(key, scope, 1)
    dispatcher.step()
    assert events[0] == "stop" and events[-1] == "restore"
    assert state.mode() == ("recovering" if fault == "restore" else "ready")
    assert jobs.get(key)["status"] == ("completed" if fault is None else "canceled" if fault == "cancel" else "failed")
    if fault is None:
        assert events == ["stop", "vram", "worker", "restore"]


def test_inflight_generation_is_not_killed_for_cover(tmp_path):
    state, jobs = MediaState(tmp_path), SongStore(tmp_path)
    lease = state.enter()
    jobs.submit("a" * 32, "qq:999:dm:101", music())
    def forbidden(*args):
        raise AssertionError("An in-flight model was stopped")
    dispatcher = CoverDispatcher(tmp_path, broker=SimpleNamespace(file=object()),
        stop_models=forbidden, restore_models=forbidden, verify_gpu=forbidden,
        run_worker=forbidden, source=Source(), drain_timeout=-1)
    dispatcher.step()
    jobs.choose("a" * 32, "qq:999:dm:101", 1)
    dispatcher.step()
    assert state.mode() == "ready"
    assert jobs.get("a" * 32)["error"] == "chat_drain_timeout"
    state.leave(lease)


def test_original_download_never_unloads_or_calls_rvc(tmp_path):
    jobs = SongStore(tmp_path)
    jobs.submit("a" * 32, "qq:999:dm:101", music("original"))
    events = []
    def forbidden(*args):
        raise AssertionError("Original playback requested GPU switching")
    def work(key):
        events.append(key)
        jobs.output(key).parent.mkdir(parents=True)
        jobs.output(key).write_bytes(wav_bytes())
    dispatcher = CoverDispatcher(tmp_path, broker=SimpleNamespace(file=object()),
        stop_models=forbidden, restore_models=forbidden, verify_gpu=forbidden,
        run_worker=work, source=Source())
    dispatcher.step()
    assert jobs.get("a" * 32)["status"] == "awaiting_selection" and events == []
    jobs.choose("a" * 32, "qq:999:dm:101", 1)
    dispatcher.step()
    assert events == ["a" * 32] and jobs.get("a" * 32)["status"] == "completed"


def test_song_api_requires_scope_and_authenticated_routes(tmp_path, monkeypatch):
    from services.media_api.app import SongPayload, create_app
    monkeypatch.setenv("ASTRBOT_CHAT_STATE_DIR", str(tmp_path))
    app = create_app()
    routes = {r.path: r for r in app.routes}
    for path in ("/v1/songs", "/v1/songs/{job_id}", "/v1/songs/{job_id}/audio", "/v1/songs/{job_id}/cancel"):
        assert routes[path].dependencies
    result = routes["/v1/songs"].endpoint(SongPayload(request_id="a" * 32, scope="qq:999:dm:101", music=music()))
    assert result["status"] == "queued" and not result["has_audio"]


def test_weapi_redirect_cannot_forward_account_cookie():
    seen = {}
    def post(*args, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(is_redirect=True)
    source = NetEaseSource(cookie="test-only", session=SimpleNamespace(headers={}, post=post))
    with pytest.raises(ValueError, match="redirect"):
        source._post("search/get", {"s": "测试"})
    assert seen["allow_redirects"] is False


def test_orphan_cleanup_excludes_detached_local_chat_and_live_supervisors(monkeypatch):
    from scripts.qq_companion import cleanup_orphans as module
    class Process:
        def __init__(self, pid, parent, role):
            self.info = {"pid": pid, "ppid": parent, "exe": str(module.ROOT / "runtime_env/python.exe"),
                         "cmdline": [str(module.ROOT / "runtime_env/python.exe"), "-m", role], "create_time": 100}
        def ppid(self):
            return self.info["ppid"]
        def create_time(self):
            return 100
    processes = [Process(1, 900, "astrbot.cli"), Process(2, 900, "services.media_api.app"),
                 Process(3, 900, "runtime.sensevoice.api"), Process(4, 901, "llama-server.exe"),
                 Process(5, 902, "astrbot.cli"), Process(6, 902, "services.media_api.app"),
                 Process(7, 900, "training_jobs.py runtime.sensevoice.api")]
    monkeypatch.setattr(module.psutil, "process_iter", lambda attrs: processes)
    monkeypatch.setattr(module.psutil, "pid_exists", lambda pid: pid == 902)
    monkeypatch.setattr(module.psutil, "Process", lambda pid: next(p for p in processes if p.info["pid"] == pid))
    assert {pid for pid, _, _ in module.find_orphans()} == {1, 2, 3}
