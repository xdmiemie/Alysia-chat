"""Offline pre-watch policy, persistent quotas and duration boundary regressions."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import sys

import jsonschema
import pytest

from services.douyin_worker import worker as module, persona
from services.douyin_worker.browser_session import BrowserSession
from services.douyin_worker.config import ConfigStore, DouyinConfig
from services.douyin_worker.store import Store
from services.douyin_worker.worker import Paused, Worker


@pytest.fixture
def loop():
    value = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
    yield value
    value.close()


def video(number=1, duration=240):
    key = str(7000000000000000000 + number)
    return dict(id=key, url="https://www.douyin.com/video/" + key, title="curious title",
                kind="video", duration=duration, media_url="https://v.douyinvod.com/test.mp4")


@pytest.fixture
def worker(tmp_path, monkeypatch):
    config = ConfigStore(tmp_path / "config.json")
    config.update({"account": {"douyin_id": "test", "name": "test"}})
    clock = [10000.0]
    value = Worker(tmp_path, config=config, clock=lambda: clock[0])
    value.test_clock, value.test_calls = clock, []
    value.checkpoint = lambda: None
    active = tmp_path / "model_store/active/qwen3.json"
    active.parent.mkdir(parents=True)
    active.write_text(json.dumps({"character_id": "test"}))
    monkeypatch.setattr(module, "project_root", lambda: tmp_path)
    monkeypatch.setattr(persona, "active_persona", lambda *args: "test persona")
    async def complete(messages, fmt, **kw):
        value.test_calls.append((messages, kw))
        return json.dumps({"interested": True, "reason": "I want to see this"})
    value.model.complete = complete
    return value


@pytest.mark.parametrize("duration,reason,long", [
    (179, "", False), (180, "", False), (180.001, "", True), (599, "", True), (600, "", True),
    (600.001, "video_too_long", False), (601, "video_too_long", False),
    (None, "video_duration_unknown", False), (0, "video_duration_unknown", False),
    (float("inf"), "video_duration_unknown", False), (float("nan"), "video_duration_unknown", False),
    (True, "video_duration_unknown", False)])
def test_interest_then_duration_boundaries(worker, loop, duration, reason, long):
    item, config = video(duration=duration), worker.config.read()
    result = loop.run_until_complete(worker.prepare_watch(config, item))
    assert result == (not reason)
    assert len(worker.test_calls) == 1
    messages, kwargs = worker.test_calls[0]
    assert kwargs == {"phase": "douyin_title_interest"}
    assert json.loads(messages[1]["content"]) == {"title": "curious title"}
    assert "test persona" in messages[0]["content"]
    assert worker.store.long_quota(config)["used"] == int(long)
    if reason:
        assert worker.store.video("test", item["id"])["error"] == reason
        with pytest.raises(ValueError, match="watch_time_unknown"):
            worker.store.watch_time("test", item["id"])
        assert worker.store.delay(config) == 0


def test_uninterested_is_skipped_before_duration_resolution(worker, loop):
    async def uninterested(*args, **kw): return '{"interested": false, "reason": "not my thing"}'
    async def forbidden(*args, **kw): pytest.fail("uninterested title must not resolve/download/analyze")
    worker.model.complete = uninterested
    worker.browser.context = object()
    worker.browser.resolve_item = worker.download = worker.analyzer.video = forbidden
    item = video(duration=None)
    item.pop("kind")
    loop.run_until_complete(worker.process(worker.config.read(), item))
    assert worker.store.video("test", item["id"])["error"] == "title_not_interested"
    assert worker.store.admission("test", item["id"]) is None
    with worker.memory.connection() as db:
        assert db.execute("SELECT count(*) FROM sm_events").fetchone()[0] == 0


@pytest.mark.parametrize("raw", ['{"interested":"true","reason":"x"}', '{}',
    '{"interested":true,"reason":"x","duration":1}'])
def test_invalid_title_decision_never_admits(worker, loop, raw):
    async def complete(*args, **kw): return raw
    worker.model.complete = complete
    with pytest.raises(jsonschema.ValidationError):
        loop.run_until_complete(worker.prepare_watch(worker.config.read(), video()))
    assert worker.store.admission("test", video()["id"]) is None


def test_restart_resume_is_idempotent_but_new_title_is_rechecked(worker, loop):
    config, item = worker.config.read(), video()
    for _ in range(2):
        assert loop.run_until_complete(worker.prepare_watch(config, item))
        worker.store = Store(worker.directory, worker.clock)
    assert len(worker.test_calls) == 1
    assert worker.store.long_quota(config)["used"] == 1
    item["title"] = "a changed title"
    assert loop.run_until_complete(worker.prepare_watch(config, item))
    assert len(worker.test_calls) == 2
    with worker.store.connection() as db:
        assert db.execute("SELECT count(*) FROM views").fetchone()[0] == 1


def test_quota_exhaustion_keeps_short_videos_and_recovers_after_rolling_hour(worker, loop):
    worker.config.update({"hourly_videos": 5})  # 40% => 2, within the total of 5.
    config = worker.config.read()
    start = worker.clock()
    for number in (1, 2):
        assert loop.run_until_complete(worker.prepare_watch(config, video(number)))
        worker.test_clock[0] += 720
    assert not loop.run_until_complete(worker.prepare_watch(config, video(3)))
    assert worker.store.video("test", video(3)["id"])["error"] == "long_video_quota_exhausted"
    assert loop.run_until_complete(worker.prepare_watch(config, video(4, 180)))
    assert worker.store.long_quota(config) == {"limit": 2, "used": 2, "remaining": 0, "window_seconds": 3600}
    worker.test_clock[0] = start + 3600
    worker.store = Store(worker.directory, worker.clock)
    assert loop.run_until_complete(worker.prepare_watch(config, video(3)))
    assert worker.store.long_quota(config)["used"] == 2
    assert len(worker.test_calls) == 4  # cached title decision on the skipped candidate


def test_quota_hot_change_zero_and_floor(worker, loop):
    assert DouyinConfig(hourly_videos=3).long_video_limit == 1
    worker.config.update({"long_video_percent": 0})
    assert not loop.run_until_complete(worker.prepare_watch(worker.config.read(), video()))
    worker.config.update({"long_video_percent": 100})
    assert loop.run_until_complete(worker.prepare_watch(worker.config.read(), video()))
    worker.config.update({"long_video_percent": 0})
    # An already admitted watch may finish; no additional admission after the change.
    assert loop.run_until_complete(worker.prepare_watch(worker.config.read(), video()))
    assert not loop.run_until_complete(worker.prepare_watch(worker.config.read(), video(2)))


@pytest.mark.parametrize("percent", [-1, 101, True, "40", 40.5])
def test_invalid_quota_configuration(worker, percent):
    with pytest.raises(ValueError):
        worker.config.update({"long_video_percent": percent})
    assert worker.config.read().long_video_percent == 40


def test_concurrent_admissions_cannot_spend_last_quota_twice(worker):
    config = worker.config.update({"hourly_videos": 100, "long_video_percent": 1})
    for number in (1, 2):
        worker.store.observe("test", video(number))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda n: worker.store.admit_watch(config, video(n), duration=240), (1, 2)))
    assert sorted(results) == ["", "long_video_quota_exhausted"]
    assert Store(worker.directory, worker.clock).long_quota(config)["used"] == 1


def test_old_prefix_cannot_be_reused_as_full_video(worker, loop):
    item = video()
    worker.store.observe("test", item)
    worker.store.save_video("test", item["id"], "pending", media_id="old-prefix",
        analysis={"truncated": True, "duration": 180}, interest={"interesting": True})
    assert loop.run_until_complete(worker.prepare_watch(worker.config.read(), item))
    row = worker.store.video("test", item["id"])
    assert row["media_id"] is row["analysis"] is row["interest"] is None


def test_metadata_duration_and_title_are_available_before_playback(tmp_path):
    browser = BrowserSession(tmp_path)
    browser.remember_media({"aweme_id": video()["id"], "desc": "native title", "video": {
        "duration": 600001, "play_addr": {"url_list": [video()["media_url"]]}}})
    item = browser.media_items[video()["id"]]
    assert item["title"] == "native title" and item["duration"] == 600.001


@pytest.mark.parametrize("interested,duration,expected", [(False, 120, ["title", "next"]),
    (True, 601, ["title", "next"]), (True, 600, ["title", "resume", "analyze", "next"])])
def test_visible_pipeline_order_and_immediate_skip(worker, loop, interested, duration, expected):
    events = []
    worker.reason = lambda: None
    worker.browser.verified_account = worker.config.read().account.model_dump()
    async def noop(*args): pass
    async def resume(): events.append("resume")
    async def advance(): events.append("next")
    async def current(): return video(duration=duration)
    async def complete(*args, **kwargs):
        events.append("title")
        return json.dumps({"interested": interested, "reason": "my choice"})
    async def process(config, item):
        assert await worker.prepare_watch(config, item)
        events.append("analyze")
    worker.browser.open = worker.browser.pause = noop
    worker.browser.resume, worker.feed.next, worker.feed.current = resume, advance, current
    worker.model.complete, worker.process = complete, process
    loop.run_until_complete(worker.step())
    assert events == expected
    assert worker.store.get("checkpoint") is None


@pytest.mark.parametrize("actual", [240, 601])
def test_actual_file_duration_cannot_bypass_quota_or_maximum(worker, loop, monkeypatch, actual):
    worker.config.update({"long_video_percent": 0})
    async def download(*args): return b"fake video"
    async def probe(*args):
        if actual > 600:
            raise module.MediaError("video_too_long")
        return b"fake video", actual
    async def forbidden(*args, **kwargs): pytest.fail("must not analyze, remember or share")
    worker.download, worker.analyzer.video, worker.remember, worker.share = download, forbidden, forbidden, forbidden
    monkeypatch.setattr(module, "prepare_video", probe)
    monkeypatch.setattr(module.VisionGates, "enabled", lambda *args: True)
    loop.run_until_complete(worker.process(worker.config.read(), video(duration=120)))
    expected = "video_too_long" if actual > 600 else "long_video_quota_exhausted"
    assert worker.store.video("test", video()["id"])["error"] == expected


def test_retry_pacing_is_preserved_without_finishing_job(worker, loop):
    config = worker.config.read()
    assert loop.run_until_complete(worker.prepare_watch(config, video()))
    retry = video(2)
    worker.store.observe("test", retry)
    worker.store.save_video("test", retry["id"], "unavailable", error="video_too_long")
    worker.store.queue_recoverable("test")
    # A legacy retry has no duration: fetch native metadata from the exact item.
    worker.browser.media_items[retry["id"]] = retry
    with pytest.raises(Paused, match="rate_wait"):
        loop.run_until_complete(worker.process_retry(config, worker.store.next_retry("test")))
    assert worker.store.next_retry("test")["id"] == retry["id"]


def test_gallery_has_title_gate_but_never_spends_long_quota(worker, loop):
    item = dict(video(duration=None), kind="image_post", images=["https://p.douyinpic.com/one"])
    assert loop.run_until_complete(worker.prepare_watch(worker.config.read(), item))
    assert worker.store.admission("test", item["id"])["kind"] == "gallery"
    assert worker.store.long_quota(worker.config.read())["used"] == 0


def test_600_second_analysis_covers_entire_timeline_in_bounded_batches(worker, loop, monkeypatch):
    from integrations.qq_astrbot.plugin import vision_analysis
    from integrations.qq_astrbot.plugin.vision_media import Segment, Frame
    from io import BytesIO
    from PIL import Image
    stream = BytesIO()
    Image.new("RGB", (32, 32), "red").save(stream, "JPEG")
    picture = stream.getvalue()
    monkeypatch.setattr(vision_analysis, "video_info", lambda *args: (600, False))
    monkeypatch.setattr(vision_analysis, "extract_segment", lambda path, a, b, times, cfg:
        Segment(a, b, [Frame(t, picture, 32, 32) for t in times]))
    calls = []
    async def model(phase, payload, images, schema):
        calls.append((phase, payload, images))
        return json.dumps(dict(summary="complete timeline", objects=[], actions=[], ocr_text=[],
            uncertainty=[], emotion=None, confidence=.9, timestamps=[]))
    worker.analyzer.call = model
    key = worker.assets.put(b"synthetic complete video", "owner", "video")
    value = loop.run_until_complete(worker.analyzer.video(key, "owner", "full video"))
    assert value["duration"] == 600 and value["uncovered_intervals"] == []
    assert value["covered_intervals"] == [[n, n + 24] for n in range(0, 600, 24)]
    batches = [(payload, images) for phase, payload, images in calls if phase == "vision_analyze"]
    assert len(batches) == 75 and all(len(images) <= 4 for _, images in batches)
    assert [frame["timestamp"] for payload, _ in batches for frame in payload["sampled_frames"]] == list(range(0, 600, 2))
    assert all(len(payload["parts"]) <= 8 for phase, payload, _ in calls if phase == "vision_merge")
