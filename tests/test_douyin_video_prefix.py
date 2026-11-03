"""Real local decoding and deadline/cancellation regressions; no network or sends."""
import asyncio
import subprocess
import sys
from types import SimpleNamespace

import pytest

from integrations.qq_astrbot.plugin.vision_config import load_vision_config
from integrations.qq_astrbot.plugin.vision_media import ffmpeg_tool, video_info
from services.douyin_worker.media import _run, video_prefix
from services.douyin_worker.worker import Worker


@pytest.fixture
def loop():
    value = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
    yield value
    value.close()


@pytest.mark.parametrize("duration", [179, 180, 181, 240])
def test_long_video_retains_only_first_180_seconds_with_audio(tmp_path, loop, duration):
    config = load_vision_config()
    source = tmp_path / "source.mp4"
    subprocess.run([ffmpeg_tool("ffmpeg"), "-v", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i", f"color=red:size=160x90:rate=2:duration={duration}",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=16000:duration={duration}",
        "-vf", "drawbox=color=blue:t=fill:enable='gte(t,180)'", "-c:v", "libx264", "-preset", "ultrafast",
        "-threads", "2", "-c:a", "aac", str(source)], capture_output=True, check=True, timeout=30)
    original = source.read_bytes()
    data, full_duration = loop.run_until_complete(video_prefix(original, tmp_path, config))
    target = tmp_path / "result.mp4"
    target.write_bytes(data)
    actual_duration, audio = video_info(target, config)
    assert full_duration == pytest.approx(duration, abs=.01)
    assert actual_duration == pytest.approx(min(duration, 180), abs=.01)
    assert audio
    assert data == original if duration <= 180 else data != original
    assert not list(tmp_path.glob("douyin-prefix-*"))
    # A blue ending after 180s must never reach the model's input asset.
    rgb = subprocess.run([ffmpeg_tool("ffmpeg"), "-v", "error", "-ss", "179.5", "-i", str(target),
        "-frames:v", "1", "-vf", "scale=1:1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"],
        capture_output=True, check=True, timeout=10).stdout
    if duration >= 180:
        assert len(rgb) == 3 and rgb[0] > rgb[2] + 100


def test_viewing_deadline_advances_while_analysis_is_still_running(tmp_path, loop):
    async def scenario():
        worker = Worker(tmp_path)
        worker.checkpoint = lambda: None
        events, finished = [], asyncio.Event()
        async def pause(): events.append("pause")
        async def advance(): events.append("next")
        async def process(*args): await finished.wait()
        worker.browser = SimpleNamespace(pause=pause)
        worker.feed.next, worker.process = advance, process
        checkpoint = {"watch_until": worker.clock() - 1}
        task = asyncio.create_task(worker.process_visible(None, {}, checkpoint))
        for _ in range(20):
            if checkpoint.get("advanced"):
                break
            await asyncio.sleep(.01)
        assert checkpoint["advanced"] and events == ["pause", "next", "pause"]
        assert not task.done()
        finished.set()
        await task
        assert events.count("next") == 1
    loop.run_until_complete(scenario())


def test_canceling_processing_removes_its_navigation_timer(tmp_path, loop):
    async def scenario():
        worker = Worker(tmp_path)
        worker.checkpoint = lambda: None
        started, ended = asyncio.Event(), asyncio.Event()
        async def process(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                ended.set()
        worker.process = process
        checkpoint = {"watch_until": worker.clock() + 180}
        task = asyncio.create_task(worker.process_visible(None, {}, checkpoint))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ended.is_set() and not checkpoint.get("advanced")
    loop.run_until_complete(scenario())


def test_canceling_decoder_kills_child_process(tmp_path, loop):
    async def scenario():
        pid_file = tmp_path / "decoder.pid"
        script = "import os, pathlib, time; pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(120)" % str(pid_file)
        task = asyncio.create_task(_run(getattr(sys, "_base_executable", sys.executable), "-c", script, timeout=180))
        for _ in range(100):
            if pid_file.exists():
                break
            await asyncio.sleep(.01)
        assert pid_file.exists()
        import psutil
        process = psutil.Process(int(pid_file.read_text()))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.wait(timeout=2) is not None
    loop.run_until_complete(scenario())


def test_worker_analyzes_long_prefix_and_keeps_scope_after_resume(tmp_path, loop, monkeypatch):
    from services.douyin_worker import worker as module
    from services.douyin_worker.config import ConfigStore
    from services.douyin_worker.interest_scorer import FLAGS
    import json

    config = ConfigStore(tmp_path / "config.json")
    config.update({"account": {"douyin_id": "12345", "name": "test"}})
    worker = Worker(tmp_path, config=config)
    worker.checkpoint = lambda: None
    monkeypatch.setattr(module.VisionGates, "enabled", lambda *args: True)
    async def prefix(*args): return b"local prefix", 240
    monkeypatch.setattr(module, "video_prefix", prefix)
    async def download(*args): return b"source"
    worker.download = download
    calls = []
    async def analyze(*args, **kwargs):
        calls.append(args[2])
        return {"summary": "已读取片段", "duration": 180, "confidence": .9,
                "uncertainty": [], "uncovered_intervals": []}
    worker.analyzer.video = analyze
    async def score(*args):
        return {"interesting": False, "funny": False, "high_quality": False,
                **{k: False for k in FLAGS}, "confidence": .1}
    monkeypatch.setattr(module, "score", score)
    active = tmp_path / "model_store/active/qwen3.json"
    active.parent.mkdir(parents=True)
    active.write_text(json.dumps({"character_id": "test"}))
    monkeypatch.setattr(module, "project_root", lambda: tmp_path)
    from services.douyin_worker import persona
    monkeypatch.setattr(persona, "active_persona", lambda *args: "test")
    v = {"id": "7000000000000000001", "url": "https://www.douyin.com/video/7000000000000000001",
         "title": "test", "media_url": "https://v.douyinvod.com/visible.mp4", "duration": 240}
    account = config.read().account.douyin_id
    worker.store.encounter(account, v)
    loop.run_until_complete(worker.process(config.read(), v))
    row = worker.store.video(account, v["id"])
    result = json.loads(row["analysis"])
    assert row["status"] == "filtered" and not row["error"]
    assert result["truncated"] and result["source_duration"] == 240
    assert result["analyzed_range"] == [0, 180] and "仅视频前180秒" in result["summary"]
    assert "不推断后续内容" in calls[0]
    # Resume an interrupted job after the prefix was stored but before analysis.
    with worker.store.connection() as db:
        db.execute("UPDATE videos SET status='pending',analysis=NULL,interest=NULL")
    v["duration"] = None
    loop.run_until_complete(worker.process(config.read(), v))
    result = json.loads(worker.store.video(account, v["id"])["analysis"])
    assert result["source_duration"] == 240 and "不推断后续内容" in calls[1]


@pytest.mark.parametrize("http_status", [None, 403])
def test_bad_media_skips_one_item_without_halting_worker(tmp_path, loop, http_status):
    from services.douyin_worker.config import ConfigStore
    from integrations.qq_astrbot.plugin.vision_media import MediaError
    async def scenario():
        config = ConfigStore(tmp_path / "config.json")
        config.update({"account": {"douyin_id": "12345", "name": "test"}})
        worker = Worker(tmp_path, config=config)
        worker.checkpoint = lambda: None
        v = {"id": "7000000000000000001", "url": "https://www.douyin.com/video/7000000000000000001"}
        worker.store.encounter(config.read().account.douyin_id, v)
        events = []
        async def pause(): pass
        async def advance(): events.append("next")
        async def broken(*args):
            if http_status:
                import httpx
                response = httpx.Response(http_status, request=httpx.Request("GET", "https://v.douyinvod.com/test.mp4"))
                response.raise_for_status()
            raise MediaError("invalid_video_stream")
        worker.browser = SimpleNamespace(pause=pause)
        worker.feed.next, worker.process = advance, broken
        await worker.process_visible(config.read(), v, {"watch_until": worker.clock() + 180})
        assert events == ["next"] and worker.store.get("halt") is None
        expected = "media_http_403" if http_status else "invalid_video_stream"
        assert worker.store.video(config.read().account.douyin_id, v["id"])["error"] == expected
    loop.run_until_complete(scenario())
