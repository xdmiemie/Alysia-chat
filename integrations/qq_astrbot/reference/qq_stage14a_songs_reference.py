"""Stage 14A contract tests that do not depend on online music or GPU models."""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import subprocess
import wave
from pathlib import Path

import pytest

from runtime.qq_adapter.song_audio import SongAudioConfig, SongAudioPipeline, inspect_audio
from runtime.qq_adapter.song_source import NetEaseSource, Song, parse_song_id, weapi_form
from runtime.qq_adapter.songs import SongPolicy, SongService, _matching_song, handle_song_command
from runtime.qq_adapter.chat import ChatPolicy, MessageStore, QQChatBridge
from runtime.qq_adapter.agent import AgentSession, ConversationAction, parse_action
from runtime.qq_adapter.onebot import OneBotMessage
from runtime.qq_adapter.chat import TimingPolicy
from runtime.qq_adapter.tools import ToolContext, ToolDefinition, ToolExecutor, ToolRegistry
from runtime.qwen3.config import Qwen3Config


class FakeSource:
    def search(self, query: str, page: int = 1) -> list[Song]:
        return [Song("netease", 123, query, "Singer A", 150, True),
                Song("netease", 456, query, "Singer B", 150, True)]

    def detail(self, song_id: int) -> Song:
        return Song("netease", song_id, "Direct", "Singer", 150, True)


def test_model_singing_fields_are_validated() -> None:
    action = {"action": "send_message", "content": "好呀，等我一下。", "send_voice": False,
              "sing": True, "song_query": "年轮", "emotion": "gentle", "continue_turn": False,
              "pace": "quick", "reason_code": "song_request"}
    parsed = parse_action(json.dumps(action, ensure_ascii=False), max_length=80, previous=())
    assert parsed.sing and parsed.song_query == "年轮" and not parsed.send_voice
    with pytest.raises(ValueError):
        parse_action(json.dumps({**action, "song_query": ""}), max_length=80, previous=())
    with pytest.raises(ValueError):
        parse_action(json.dumps({**action, "sing": False}), max_length=80, previous=())
    with pytest.raises(ValueError):
        parse_action(json.dumps({**action, "action": "finish_turn", "content": ""}),
                     max_length=80, previous=())


def test_model_classifies_then_writes_singing_reply() -> None:
    agent = AgentSession(Qwen3Config())
    calls = []

    class Client:
        def complete_with_schema(self, _messages, _schema, name, *, temperature=None):
            calls.append((name, temperature))
            if name == "qq_song_intent":
                return json.dumps({"sing": True, "song_query": "年轮"})
            return json.dumps({"action": "send_message", "content": "好呀，等我一下。",
                               "send_voice": False, "sing": True, "song_query": "年轮",
                               "emotion": "gentle", "continue_turn": False,
                               "pace": "quick", "reason_code": "song_request"})

    agent.client = Client()
    result = agent.decide("大肥鱼，我还想听你唱年轮", (), 100, song_enabled=True)
    assert result.sing and result.song_query == "年轮" and result.content == "好呀，等我一下。"
    assert calls == [("qq_song_intent", 0.0), ("qq_agent_action", None)]


def test_enabled_policy_exposes_singing_as_action_field(tmp_path: Path) -> None:
    policy = ChatPolicy(song=SongPolicy(enabled=True))
    bridge = QQChatBridge(9, policy, MessageStore(tmp_path / "messages.sqlite3"),
                          Qwen3Config(), "test-v1")
    names = {item.name for item in bridge.registry.definitions()}
    assert not any(name.startswith("song_") for name in names)


def test_program_does_not_parse_natural_singing_intent(tmp_path: Path) -> None:
    songs = service(tmp_path)
    session = "qq:private:9:10"
    reply = asyncio.run(handle_song_command(songs, bot_id=9, session_id=session,
                                            user_id=10, message_id="natural",
                                            content="大肥鱼，我还想听你唱年轮"))
    assert reply is None
    assert songs.db.execute("SELECT count(*) FROM song_jobs").fetchone()[0] == 0
    job_id = asyncio.run(songs.request_cover(9, session, 10, "natural", "年轮"))
    assert songs.status(session, 10, job_id)["status"] == "queued"
    assert json.loads(songs.db.execute("SELECT song_json FROM song_jobs").fetchone()[0])["title"] == "年轮"
    assert _matching_song([{"index": 1, "song_id": 1, "title": "雨爱", "artists": "某人"},
                           {"index": 2, "song_id": 2, "title": "雨爱", "artists": "杨丞琳"}],
                          "雨爱 杨丞琳")["song_id"] == 2
    assert _matching_song([{"index": 1, "song_id": 1, "title": "雨爱 (Live)", "artists": "某人"}],
                          "雨爱") is None
    assert _matching_song([{"index": 1, "song_id": 1, "title": "年轮", "artists": "甲",
                            "duration_seconds": 113},
                           {"index": 2, "song_id": 2, "title": "年轮", "artists": "乙",
                            "duration_seconds": 274}], "年轮")["song_id"] == 2


def test_model_ack_is_sent_before_song_is_queued(tmp_path: Path) -> None:
    policy = ChatPolicy(private_users=frozenset({10}), min_interval_seconds=0,
                        timing=TimingPolicy(enabled=False),
                        song=SongPolicy(enabled=True, work_dir=str(tmp_path / "jobs")))
    bridge = QQChatBridge(9, policy, MessageStore(tmp_path / "bridge.sqlite3"),
                          Qwen3Config(), "test-v1")
    bridge.songs.source = FakeSource()

    class Agent:
        def decide(self, user, sent, *_args, **_kwargs):
            assert user == "大肥鱼，我还想听你唱年轮" and not sent
            return ConversationAction("send_message", "好呀，你等等我。", False, "quick",
                                      "song_request", sing=True, song_query="年轮")

        def commit(self, *_args):
            pass

    class Client:
        def __init__(self):
            self.sent = []

        async def send_private_message(self, _user, content):
            queued = bridge.store.db.execute("SELECT count(*) FROM song_jobs").fetchone()[0]
            self.sent.append((content, queued))
            return {"message_id": 77}

    bridge._agent = lambda _message: Agent()
    client = Client()
    event = OneBotMessage("private", 10, None, 1,
                          ({"type": "text", "data": {"text": "大肥鱼，我还想听你唱年轮"}},), 9)
    incoming = bridge.accept(event)
    assert incoming is not None
    asyncio.run(bridge._run_message(client, incoming, bridge.message_generations[incoming.request_id]))
    assert client.sent == [("好呀，你等等我。", 0)]
    assert bridge.store.db.execute("SELECT count(*) FROM song_jobs").fetchone()[0] == 1
    assert bridge.store.db.execute("SELECT sing,song_query FROM decisions").fetchone() == (1, "年轮")


def service(tmp_path: Path, *, authorized=True) -> SongService:
    db = sqlite3.connect(tmp_path / "jobs.sqlite3")
    return SongService(db, SongPolicy(enabled=True, work_dir=str(tmp_path / "jobs")),
                       source=FakeSource(), permitted=lambda _session, _user: authorized)


def test_weapi_encryption_and_link_validation() -> None:
    form = weapi_form({"s": "晴天"})
    assert set(form) == {"params", "encSecKey"}
    assert len(form["encSecKey"]) == 256
    assert parse_song_id("https://music.163.com/#/song?id=12345") == 12345
    assert parse_song_id("https://evil.example/song?id=12345") is None


def test_search_checks_actual_playability(monkeypatch) -> None:
    source = NetEaseSource()

    def post(route, _payload):
        if route == "search/get":
            return {"result": {"songs": [
                {"id": 1, "name": "VIP", "dt": 120000, "ar": [{"name": "A"}]},
                {"id": 2, "name": "Playable", "dt": 120000, "ar": [{"name": "B"}]},
            ]}}
        assert route == "song/enhance/player/url/v1"
        return {"data": [{"id": 1, "code": -110, "url": None},
                         {"id": 2, "code": 200, "url": "https://m1.music.126.net/a.mp3"}]}

    monkeypatch.setattr(source, "_post", post)
    result = source.search("test")
    assert [song.available for song in result] == [False, True]


def test_authorized_download_resumes_without_forwarding_cookie(tmp_path: Path, monkeypatch) -> None:
    from runtime.qq_adapter import song_source

    class Response:
        def __init__(self, status, chunks, headers):
            self.status_code, self.chunks, self.headers = status, chunks, headers
            self.is_redirect = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, _size):
            for item in self.chunks:
                if isinstance(item, Exception):
                    raise item
                yield item

    seen = []

    def get(_url, *, headers, **_kwargs):
        seen.append(headers)
        if len(seen) == 1:
            return Response(200, [b"a" * 1200, OSError("network down")],
                            {"Content-Type": "audio/mpeg", "ETag": '"v1"'})
        return Response(206, [b"b" * 1200],
                        {"Content-Type": "audio/mpeg", "Content-Range": "bytes 1200-2399/2400"})

    monkeypatch.setattr(song_source.requests, "get", get)
    source = NetEaseSource(cookie="private-cookie")
    monkeypatch.setattr(source, "playable_url", lambda _song_id: "https://m1.music.126.net/a.mp3")
    target = tmp_path / "song.mp3"
    with pytest.raises(OSError):
        source.download(123, target)
    assert not target.exists() and target.with_suffix(".mp3.part").stat().st_size == 1200
    digest = source.download(123, target)
    assert len(digest) == 64 and target.read_bytes() == b"a" * 1200 + b"b" * 1200
    assert seen[1]["Range"] == "bytes=1200-"
    assert all("Cookie" not in item for item in seen)


def test_full_song_job_recovery_and_access(tmp_path: Path) -> None:
    songs = service(tmp_path)
    songs.policy = SongPolicy(enabled=True, full_song=True, max_duration_seconds=600,
                              work_dir=str(tmp_path / "jobs"))
    assert songs.policy.audio_config().max_duration_seconds == 600
    session = "qq:private:9:10"
    reply = asyncio.run(handle_song_command(songs, bot_id=9, session_id=session,
                                            user_id=10, message_id="1", content="/sing Test"))
    assert "等我一下" in reply
    row = songs.db.execute("SELECT job_id,song_json,duration_seconds FROM song_jobs").fetchone()
    assert json.loads(row[1])["song_id"] == 123 and row[2] == 150
    assert songs.enqueue(9, session, 10, "1") == row[0]
    with pytest.raises(ValueError):
        songs.status(session, 11, row[0])
    songs.db.execute("UPDATE song_jobs SET status='converting'")
    songs.db.commit()
    recovered = service(tmp_path)
    assert recovered.status(session, 10, row[0])["status"] == "queued"
    assert recovered.cancel(session, 10, row[0])
    assert recovered.status(session, 10, row[0])["status"] == "cancelled"


def test_unauthorized_media_job_denied(tmp_path: Path) -> None:
    registry = ToolRegistry()
    called = []
    registry.register(ToolDefinition("cover", {"properties": {}, "required": []},
                                     permission="media_job", event_loop_safe=True),
                      lambda _args, _ctx: called.append(True))
    executor = ToolExecutor(registry, lambda *_args: None)
    denied = asyncio.run(executor.execute("cover", {}, ToolContext(9, "qq:private:9:10", "1", 1), {}))
    assert denied["error"] == "PermissionError" and not called
    approved = asyncio.run(executor.execute("cover", {},
                                            ToolContext(9, "qq:private:9:10", "1", 1,
                                                        media_job_allowed=True), {}))
    assert approved["ok"] and called


def test_clip_and_mix_use_local_ffmpeg(tmp_path: Path) -> None:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("FFmpeg unavailable")
    source = tmp_path / "source.wav"
    with wave.open(str(source), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes((b"\x00\x20" * 24000) * 7)
    pipeline = SongAudioPipeline(SongAudioConfig(max_duration_seconds=30))
    clip = tmp_path / "clip.wav"
    info = asyncio.run(pipeline.clip(source, clip, 0, 5))
    assert 4.9 <= info["seconds"] <= 5.1
    mixed = tmp_path / "mixed.wav"
    result = asyncio.run(pipeline.mix(clip, clip, mixed))
    assert 4.9 <= result["seconds"] <= 5.1
    assert inspect_audio(mixed, "ffprobe")["sample_rate"] == 44100


def test_background_job_send_and_uncertain_receipt(tmp_path: Path, monkeypatch) -> None:
    from runtime.qq_adapter import songs as song_module

    source = tmp_path / "fixture.wav"
    with wave.open(str(source), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes((b"\x00\x20" * 24000) * 6)

    class FixtureSource(FakeSource):
        def download(self, _song_id, destination):
            shutil.copyfile(source, destination)
            return "fixture"

    class FixturePipeline:
        async def clip(self, input_path, output_path, _start, _duration):
            shutil.copyfile(input_path, output_path)

        async def separate(self, input_path, directory):
            directory.mkdir(parents=True, exist_ok=True)
            vocals, instrumental = directory / "vocals.wav", directory / "instrumental.wav"
            shutil.copyfile(input_path, vocals)
            shutil.copyfile(input_path, instrumental)
            return vocals, instrumental

        async def convert(self, input_path, directory):
            directory.mkdir(parents=True, exist_ok=True)
            output_path = directory / "rvc_native.wav"
            shutil.copyfile(input_path, output_path)
            (directory / "cover_result.json").write_text("{}")
            return output_path

        async def mix(self, vocals, _instrumental, output_path):
            shutil.copyfile(vocals, output_path)

    jobs = service(tmp_path)
    jobs.source = FixtureSource()
    jobs.pipeline = FixturePipeline()
    session = "qq:private:9:10"

    async def setup_and_run(message_id: str, sender):
        await jobs.search(9, session, 10, "fixture")
        jobs.select(session, 10, 1)
        job_id = jobs.enqueue(9, session, 10, message_id, duration=5)
        monkeypatch.setattr(song_module, "send_voice_file", sender)
        await jobs._process(job_id, None, None, None)
        return job_id

    async def success(*_args, **_kwargs):
        return type("Delivery", (), {"result": {"message_id": 77}})()

    first = asyncio.run(setup_and_run("first", success))
    assert jobs.status(session, 10, first)["status"] == "completed"
    assert jobs.status(session, 10, first)["stage_seconds"] >= 0
    assert jobs.db.execute("SELECT sent_message_id FROM song_jobs WHERE job_id=?", (first,)).fetchone()[0] == "77"

    async def timeout(*_args, **_kwargs):
        raise TimeoutError("unknown OneBot result")

    second = asyncio.run(setup_and_run("second", timeout))
    assert second != first
    assert jobs.status(session, 10, second)["status"] == "send_uncertain"
    assert not jobs.db.execute("SELECT job_id FROM song_jobs WHERE status='queued'").fetchone()

    class BrokenPipeline(FixturePipeline):
        async def convert(self, input_path, directory):
            raise RuntimeError("conversion failed")

    class FakeClient:
        def __init__(self):
            self.messages = []

        async def send_private_message(self, user_id, content):
            self.messages.append((user_id, content))

    async def failed_job():
        jobs.pipeline = BrokenPipeline()
        client = FakeClient()
        third = jobs.enqueue(9, session, 10, "third", duration=6)
        await jobs._process(third, client, None, None)
        return third, client.messages

    third, messages = asyncio.run(failed_job())
    assert jobs.status(session, 10, third)["status"] == "failed"
    assert messages == [(10, "《fixture》我刚才没能唱出来，换一首试试好吗？")]
