from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import sys
import time

from PIL import Image
import pytest

from integrations.qq_astrbot.plugin.vision_config import load_vision_config, VisionGates
from integrations.qq_astrbot.plugin.vision_media import MediaError, decode_images, segment_ranges, validate_url
from integrations.qq_astrbot.plugin.vision_store import VisionStore
from integrations.qq_astrbot.plugin.vision_analysis import VisionAnalyzer, validate_analysis
from integrations.qq_astrbot.plugin.inference_queue import InferenceQueue, DuplicateRequest
from scripts.qq_companion.probe_q3_local import LocalLoop


@pytest.fixture
def config():
    return load_vision_config()


@pytest.fixture
def loop():
    value = LocalLoop()
    yield value
    value.close()


def picture(color="red", size=(1600, 800)):
    out = BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


def test_raw_vision_analysis_never_receives_role_memory(tmp_path,monkeypatch,loop):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from integrations.qq_astrbot.plugin.vision_runtime import VisionRuntime
    from integrations.qq_astrbot.plugin.context_budget import RequestBudget
    from integrations.qq_astrbot.plugin import request_provider
    class Media:
        @asynccontextmanager
        async def admission(self,**kwargs): yield
    class Memory:
        enabled=True
        @property
        def shared(self): raise AssertionError("raw analysis must not recall memory")
    calls=[]
    async def chat(*args,**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(completion_text='{}',tools_call_name=[],raw_completion=None)
    async def fit(*args,**kwargs): return [],1,0
    monkeypatch.setattr(request_provider,"request_chat",chat)
    monkeypatch.setattr(RequestBudget,"fit",fit)
    runtime=VisionRuntime(tmp_path,Media(),memory=Memory())
    async def provider(): return SimpleNamespace(provider_config={})
    runtime.provider_factory=provider
    schema={"type":"json_object"}
    assert loop.run_until_complete(runtime.call("vision_analyze",{"source":"synthetic"},[],schema))=='{}'
    assert calls[0]["response_format"] is schema and calls[0]["func_tool"] is None
    assert "shared_memory" not in str(calls[0]["contexts"][-1].content)


def test_activation_rejects_wrong_projector_before_changing_manifest(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from runtime.qwen3 import model_registry
    base, visual = tmp_path / "base.gguf", tmp_path / "visual.gguf"
    base.write_bytes(b"base")
    visual.write_bytes(b"mismatched")
    monkeypatch.setattr(model_registry, "find_model", lambda *args: SimpleNamespace(
        required_files=(base, visual), mmproj_file=visual, mmproj_sha256="0" * 64))
    manifest = tmp_path / "active.json"
    manifest.write_text("original")
    with pytest.raises(ValueError, match="checksum"):
        model_registry.activate_model("elysia", "test", manifest)
    assert manifest.read_text() == "original"


def result(summary="red square", **extra):
    return {"summary": summary, "objects": ["square"], "actions": [], "ocr_text": [], "emotion": None,
            "timestamps": [], "confidence": 0.9, "uncertainty": [], **extra}


def test_sixty_seconds_covers_three_complete_segments(config):
    ranges = segment_ranges(60, config)
    assert [(a, b, len(times)) for a, b, times in ranges] == [(0, 24, 12), (24, 48, 12), (48, 60, 6)]
    assert [t for _, _, times in ranges for t in times] == list(range(0, 60, 2))


@pytest.mark.parametrize("duration", [0.1, 1, 23.9, 24, 24.01, 48, 179.9, 180])
def test_segments_cover_short_and_boundary_durations(config, duration):
    ranges = segment_ranges(duration, config)
    assert ranges[0][0] == 0 and ranges[-1][1] == duration
    assert all(a[1] == b[0] for a, b in zip(ranges, ranges[1:]))
    assert all(len(ts) <= 12 and all(a <= t < b for t in ts) for a, b, ts in ranges)


@pytest.mark.parametrize("duration", [0, -1, 180.01, float("nan"), float("inf")])
def test_video_duration_limit(config, duration):
    with pytest.raises(MediaError):
        segment_ranges(duration, config)


def test_image_normalizes_pixels_and_rejects_bad_inputs(config):
    kind, frames = decode_images(picture(), config)
    assert kind == "image" and len(frames) == 1
    assert (frames[0].width, frames[0].height) == (1024, 512)
    assert frames[0].url.startswith("data:image/jpeg;base64,")
    with pytest.raises(MediaError):
        decode_images(b"<html>fake.jpg</html>", config)
    with pytest.raises(MediaError):
        decode_images(picture(size=(40, 40)), {**config, "max_image_pixels": 1000})


def test_animation_samples_last_frame_and_deduplicates_with_timestamps(config):
    images = [Image.new("RGB", (50, 50), color) for color in ("red", "blue", "green", "yellow")]
    out = BytesIO()
    images[0].save(out, format="GIF", save_all=True, append_images=images[1:], duration=[100, 200, 300, 400], loop=0)
    kind, frames = decode_images(out.getvalue(), config)
    assert kind == "gif" and len(frames) == 4
    assert [f.timestamp for f in frames] == pytest.approx([0, .1, .3, .6])
    assert len({hashlib.sha256(f.data).hexdigest() for f in frames}) == 4


@pytest.mark.parametrize("url", ["file:///C:/secret", "http://127.0.0.1/a", "https://qpic.cn.evil.org/a", "https://user:secret@qpic.cn/a", "https://qpic.cn:9999/a"])
def test_rejects_unsafe_media_urls(config, url):
    with pytest.raises(MediaError):
        validate_url(url, config["allowed_media_domains"])


def test_checks_dns_addresses_before_download(config, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))])
    with pytest.raises(MediaError, match="private"):
        validate_url("https://qpic.cn/a.jpg", config["allowed_media_domains"])


def test_authorized_ids_cache_isolation_and_expiration(tmp_path, config):
    store = VisionStore(tmp_path, config)
    key = store.put(picture(), "ownerA", "image")
    assert store.asset(key, "ownerA")[0].is_file()
    assert store.put(picture(), "ownerB", "image") != key
    for invalid, owner in [(key, "ownerB"), ("../../secret", "ownerA")]:
        with pytest.raises(MediaError):
            store.asset(invalid, owner)
    store.save("cacheA", key, "ownerA", result())
    assert store.cached("cacheA", "ownerA") and store.cached("cacheA", "ownerB") is None
    with store.connection() as db:
        db.execute("UPDATE assets SET created=0 WHERE id=?", (key,))
    with pytest.raises(MediaError):
        store.asset(key, "ownerA")
    store.cleanup()
    assert not (store.cache / key).exists()


def test_job_dedup_recovery_retry_budget_and_uncertain_send(tmp_path, config):
    store = VisionStore(tmp_path, config)
    key = store.put(b"test video bytes", "owner", "video")
    job_id = store.submit(key, "owner", "question", origin="dm", request_id="event1")
    assert store.submit(key, "owner", "question", origin="dm", request_id="event1") == job_id
    with pytest.raises(MediaError):
        store.job(job_id, "someone-else")
    job = store.next_job()
    store.recover()
    assert store.job(job_id, "owner")["status"] == "running"  # Live worker is never stolen.
    with store.connection() as db:
        db.execute("UPDATE video_jobs SET worker_pid=99999999")
    store.recover()
    assert store.job(job_id, "owner")["status"] == "pending"
    for _ in range(config["max_attempts"] - 1):
        with store.connection() as db:
            db.execute("UPDATE video_jobs SET next_attempt=0")
        job = store.next_job()
        store.fail_job(job, "bad_format")
    assert store.job(job_id, "owner")["status"] == "failed"
    with store.connection() as db:
        db.execute("UPDATE video_jobs SET status='running' WHERE id=?", (job_id,))
    store.finish_job(job_id, result())
    assert store.claim_send(job_id) and not store.claim_send(job_id)
    store.delivery(job_id, "uncertain")
    store.fail_job(job, "transport_error")
    assert store.job(job_id, "owner")["status"] == "uncertain"


def test_video_cancel_keeps_result_and_retry_from_resurrecting_job(tmp_path, config):
    store = VisionStore(tmp_path, config)
    key = store.put(b"video bytes", "owner", "video")
    job_id = store.submit(key, "owner", "question")
    job = store.next_job()
    with pytest.raises(MediaError):
        store.cancel(job_id, "another-owner")
    assert store.cancel(job_id, "owner")
    store.finish_job(job_id, result())
    store.fail_job(job, "late_error")
    store.recover()
    assert store.job(job_id, "owner")["status"] == "canceled"
    assert not store.claim_send(job_id) and store.next_job() is None


def test_video_delivery_extracts_prose_and_honors_scope_disable(tmp_path, config, monkeypatch, loop):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from integrations.qq_astrbot.plugin.vision_runtime import VisionRuntime
    from integrations.qq_astrbot.plugin.context_budget import RequestBudget
    from integrations.qq_astrbot.plugin import request_provider
    class Media:
        @asynccontextmanager
        async def admission(self):
            yield
    sent = []
    class Context:
        async def send_message(self, origin, chain):
            sent.append((origin, chain.chain[0].text))
            return True
    async def chat(*args, **kwargs):
        return SimpleNamespace(completion_text='{"reply":"视频里的方块向右移动。","thinking_level":1,"voice":false}', tools_call_name=[])
    async def fit(*args, **kwargs):
        return [], 1, 0
    monkeypatch.setattr(request_provider, "request_chat", chat)
    monkeypatch.setattr(RequestBudget, "fit", fit)
    from integrations.qq_astrbot.plugin import reply_pacing
    monkeypatch.setattr(reply_pacing, "remaining_seconds", lambda *args: 0)
    runtime = VisionRuntime(tmp_path, Media(), context=Context())
    async def provider():
        return SimpleNamespace(provider_config={})
    runtime.provider_factory = provider
    key = runtime.store.put(b"video", "qq:999:dm:101", "video")
    def completed(request):
        identity = runtime.store.submit(key, "qq:999:dm:101", "question", origin="trusted-origin", request_id=request)
        job = runtime.store.next_job()
        runtime.store.finish_job(identity, result())
        return job
    first = completed("first")
    loop.run_until_complete(runtime.deliver(first, result()))
    assert sent == [("trusted-origin", "视频里的方块向右移动。")]
    assert runtime.store.job(first["id"], first["owner"])["status"] == "sent"
    second = completed("second")
    runtime.active_scope = lambda owner: False
    loop.run_until_complete(runtime.deliver(second, result()))
    assert len(sent) == 1 and runtime.store.job(second["id"], second["owner"])["status"] == "canceled"


@pytest.mark.parametrize("mode", ["text", "voice", "action_text", "escaped_action_text", "bad_metadata", "partial_failure"])
def test_background_video_obeys_sentence_voice_and_transport_contract(tmp_path, monkeypatch, loop, mode):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from astrbot.core.message.components import Plain, Record
    from astrbot.core.message.message_event_result import MessageChain
    from integrations.qq_astrbot.plugin import request_provider, reply_pacing
    from integrations.qq_astrbot.plugin.vision_runtime import VisionRuntime
    from integrations.qq_astrbot.plugin.media_router import MediaRouter
    from integrations.qq_astrbot.plugin.context_budget import RequestBudget
    class Media:
        @asynccontextmanager
        async def admission(self): yield
    sent, voice_requests = [], []
    class Context:
        async def send_message(self, origin, chain):
            if mode == "partial_failure" and sent:
                raise ConnectionError("transport response unknown")
            sent.append((origin, chain))
            return True
    text = "先是红色。\n随后变蓝色！"
    envelope = {"reply": text, "thinking_level": 2, "voice": mode == "voice"}
    if mode in {"action_text", "escaped_action_text"}: envelope["reply"] = "（发语音：随后变蓝色）"
    if mode == "bad_metadata": envelope["voice"] = 1
    async def chat(*args, **kwargs):
        assert kwargs["func_tool"] is None and kwargs["response_format"]["json_schema"]["name"] == "video_reply"
        return SimpleNamespace(completion_text=json.dumps(envelope, ensure_ascii=mode == "escaped_action_text"), tools_call_name=[])
    async def fit(*args, **kwargs): return [], 1, 0
    monkeypatch.setattr(request_provider, "request_chat", chat)
    monkeypatch.setattr(RequestBudget, "fit", fit)
    monkeypatch.setattr(reply_pacing, "remaining_seconds", lambda *args: 0)
    runtime = VisionRuntime(tmp_path, Media(), context=Context())
    router = object.__new__(MediaRouter)
    async def voices(text, emotion, *, group):
        voice_requests.append(text)
        return [MessageChain(chain=[Record(file="file:///approved-speech.wav")])]
    router.voice_chains = voices
    runtime.reply_media = router
    async def provider(): return SimpleNamespace(provider_config={})
    runtime.provider_factory = provider
    key = runtime.store.put(b"video", "qq:999:dm:101", "video")
    identity = runtime.store.submit(key, "qq:999:dm:101", "question", origin="authorized-origin")
    job = runtime.store.next_job()
    runtime.store.finish_job(identity, result())
    if mode in {"action_text", "escaped_action_text", "bad_metadata", "partial_failure"}:
        with pytest.raises(ConnectionError if mode == "partial_failure" else MediaError):
            loop.run_until_complete(runtime.deliver(job, result()))
        assert len(sent) == (1 if mode == "partial_failure" else 0)
        assert runtime.store.job(identity, job["owner"])["status"] == ("uncertain" if mode == "partial_failure" else "completed")
    else:
        loop.run_until_complete(runtime.deliver(job, result()))
        if mode == "voice":
            assert voice_requests == [text] and len(sent) == 1 and isinstance(sent[0][1].chain[0], Record)
        else:
            assert [chain.chain[0].text for _, chain in sent] == ["先是红色。", "随后变蓝色！"]
            assert not voice_requests and all(isinstance(chain.chain[0], Plain) for _, chain in sent)
        assert runtime.store.job(identity, job["owner"])["status"] == "sent"


def test_picture_fragment_survives_following_question_without_crossing_senders(tmp_path, monkeypatch, loop):
    from types import SimpleNamespace
    from astrbot.core.message.components import Image as QQImage
    from integrations.qq_astrbot.plugin.vision_runtime import VisionRuntime
    from integrations.qq_astrbot.plugin import vision_runtime
    from integrations.qq_astrbot.plugin.message_router import Incoming
    class Event:
        def __init__(self, parts):
            self.parts, self.extra = parts, {}
            self.message_obj = SimpleNamespace(raw_message={})
        def get_messages(self): return self.parts
        def get_extra(self, key): return self.extra.get(key)
        def set_extra(self, key, value): self.extra[key] = value
    runtime = VisionRuntime(tmp_path, None)
    runtime.gates.enabled = lambda kind: kind == "image"
    monkeypatch.setattr(vision_runtime, "fetch_url", lambda *a, **kw: (picture(), "https://qpic.cn/test", "image/png"))
    sender = Incoming("999", "101", "image-id", "")
    runtime.observe(Event([QQImage(url="https://qpic.cn/test", file="https://qpic.cn/test")]), sender)
    other = Incoming("999", "102", "other-question", "color?")
    foreign_req = SimpleNamespace(image_urls=[])
    assert loop.run_until_complete(runtime.prepare(Event([]), foreign_req, other)) == []
    assert not foreign_req.image_urls
    request = SimpleNamespace(image_urls=[])
    same_sender = Incoming("999", "101", "question-id", "color?")
    status = loop.run_until_complete(runtime.prepare(Event([]), request, same_sender))
    assert len(request.image_urls) == 1 and status[0]["state"] == "pixels_attached"


def test_delayed_media_uses_only_recent_question_from_same_owner_sender(tmp_path, loop):
    from types import SimpleNamespace
    from integrations.qq_astrbot.plugin.vision_runtime import VisionRuntime
    from integrations.qq_astrbot.plugin.message_router import Incoming
    runtime = VisionRuntime(tmp_path, None)
    extra = {}
    event = SimpleNamespace(message_obj=SimpleNamespace(raw_message={}), get_messages=lambda: [],
        get_extra=lambda key: extra.get(key), set_extra=lambda key,value: extra.__setitem__(key,value))
    asked = Incoming("999", "101", "question", "Describe colors in time order")
    runtime.observe(event, asked)
    # Outside the short fragment window, but inside the media upload window.
    key = (asked.scope, asked.sender_uin)
    runtime.questions[key] = (runtime.questions[key][0] - 10, asked.text)
    incoming = Incoming("999", "101", "media", "")
    loop.run_until_complete(runtime.prepare(event, SimpleNamespace(image_urls=[]), incoming))
    assert extra["qq_vision_question"] == asked.text
    extra.clear()
    other = Incoming("999", "102", "other-media", "")
    loop.run_until_complete(runtime.prepare(event, SimpleNamespace(image_urls=[]), other))
    assert extra["qq_vision_question"] == ""
    runtime.questions[key] = (0, asked.text)
    extra.clear()
    loop.run_until_complete(runtime.prepare(event, SimpleNamespace(image_urls=[]), incoming))
    assert extra["qq_vision_question"] == ""


def test_onebot_original_url_survives_astrbot_preprocessing_without_reading_local_paths(tmp_path, monkeypatch, loop):
    from types import SimpleNamespace
    from astrbot.core.message.components import Image as QQImage
    from integrations.qq_astrbot.plugin import vision_runtime
    from integrations.qq_astrbot.plugin.message_router import Incoming
    runtime = vision_runtime.VisionRuntime(tmp_path, None)
    runtime.gates.enabled = lambda kind: kind == "image"
    # The pinned AstrBot preprocess stage has already rewritten these fields.
    part = QQImage(file="C:/arbitrary/local.png", url="C:/arbitrary/local.png")
    original_url = "https://multimedia.nt.qq.com.cn/authorized-event-image"
    extra = {}
    event = SimpleNamespace(message_obj=SimpleNamespace(raw_message={"message": [
        {"type": "image", "data": {"file": "image-hash.png", "url": original_url}}]}),
        get_messages=lambda: [part], get_extra=lambda key: extra.get(key),
        set_extra=lambda key, value: extra.__setitem__(key, value))
    calls = []
    def fetch(url, *args, **kwargs):
        calls.append(url)
        return picture(), original_url, "image/png"
    monkeypatch.setattr(vision_runtime, "fetch_url", fetch)
    incoming = Incoming("999", "101", "original-url", "color?")
    runtime.observe(event, incoming)
    request = SimpleNamespace(image_urls=[])
    status = loop.run_until_complete(runtime.prepare(event, request, incoming))
    assert calls == [original_url]
    assert status[0]["state"] == "pixels_attached" and len(request.image_urls) == 1
    event.message_obj.raw_message = {}
    with pytest.raises(MediaError, match="media_download_url_unavailable"):
        runtime.component_url(runtime.event_media(event)[0])


def test_quoted_image_source_is_captured_before_preprocess_without_extra_message_lookup(loop):
    from types import SimpleNamespace
    from astrbot.core.message.components import Image as QQImage, Reply
    from integrations.qq_astrbot.plugin.vision_runtime import install_media_source_capture, VisionRuntime
    source = "https://qpic.cn/current-quote"
    image = QQImage(file="hash.png", url=source)
    quoted = Reply(id="current-quote-id", chain=[image], sender_id="101")
    class Adapter:
        calls = 0
        async def _convert_handle_message_event(self, event):
            self.calls += 1
            return SimpleNamespace(message=[quoted])
    install_media_source_capture(Adapter)
    installed = Adapter._convert_handle_message_event
    install_media_source_capture(Adapter)
    assert Adapter._convert_handle_message_event is installed
    adapter = Adapter()
    message = loop.run_until_complete(adapter._convert_handle_message_event(None))
    image.url = image.file = "C:/astrbot/temp/materialized-quote.png"
    event = SimpleNamespace(message_obj=SimpleNamespace(raw_message={}), get_messages=lambda: message.message)
    parts = VisionRuntime.event_media(event)
    assert len(parts) == 1 and VisionRuntime.component_url(parts[0]) == source and adapter.calls == 1


def test_queue_foreground_priority_single_slot_and_duplicate(tmp_path, loop):
    async def check():
        queue = InferenceQueue(tmp_path)
        started, release = asyncio.Event(), asyncio.Event()
        order, inflight, peak = [], 0, 0
        async def request(key, priority):
            nonlocal inflight, peak
            async with queue.admission(request_id=key, priority=priority):
                inflight += 1
                peak = max(peak, inflight)
                order.append(key)
                if key == "first":
                    started.set()
                    await release.wait()
                inflight -= 1
        first = asyncio.create_task(request("first", 10))
        await started.wait()
        background = asyncio.create_task(request("background", 10))
        await asyncio.sleep(.01)
        foreground = asyncio.create_task(request("foreground", 0))
        await asyncio.sleep(.01)
        release.set()
        await asyncio.gather(first, background, foreground)
        assert order == ["first", "foreground", "background"] and peak == 1
        with pytest.raises(DuplicateRequest):
            async with queue.admission(request_id="first"):
                pass
    loop.run_until_complete(check())


def test_queue_timeout_cancel_and_dead_process_recovery(tmp_path, loop):
    async def check():
        queue = InferenceQueue(tmp_path)
        queue.enqueue("dead", 0, "", "vision", 180, 0)
        assert queue.claim("dead")
        with queue.connection() as db:
            db.execute("UPDATE inference_requests SET pid=99999999 WHERE id='dead'")
        async with queue.admission(request_id="recovered"):
            async def wait():
                async with queue.admission(request_id="cancel"):
                    pass
            task = asyncio.create_task(wait())
            await asyncio.sleep(.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            with pytest.raises(TimeoutError):
                async with queue.admission(request_id="expired", timeout=.01):
                    pass
        with queue.connection() as db:
            states = dict(db.execute("SELECT id,status FROM inference_requests"))
        assert states == {"dead": "failed", "recovered": "completed", "cancel": "canceled", "expired": "failed"}
    loop.run_until_complete(check())


def test_independent_gates_invalidate_when_budgets_change(tmp_path, config):
    gates = VisionGates(tmp_path, config)
    assert not gates.enabled("image")
    gates.record("image", {"passed": True, "report": "local evidence"})
    assert gates.enabled("image") and not gates.enabled("gif") and not gates.enabled("video")
    changed = {**config, "max_frames_per_request": 2}
    assert not VisionGates(tmp_path, changed).enabled("image")


def test_memory_code_change_expires_analysis_cache_without_disabling_vision(tmp_path, config, monkeypatch):
    from integrations.qq_astrbot.plugin import vision_config as module
    gates = VisionGates(tmp_path, config)
    gates.record("sticker", {"passed": True, "report": "tested pixels"})
    original = module.fingerprint(config)
    read = Path.read_bytes
    runtime = Path(module.__file__).with_name("vision_runtime.py")
    # Regression: adding memory/reply bookkeeping used to turn all vision off.
    monkeypatch.setattr(Path, "read_bytes", lambda path: read(path) + b"\n# memory bookkeeping changed\n"
                        if path == runtime else read(path))
    assert module.fingerprint(config) != original
    assert gates.enabled("sticker")
    assert not gates.enabled("video")  # No evidence for a different capability.


def test_reply_prompt_and_operational_timeouts_do_not_disable_pixels(tmp_path, config, monkeypatch):
    from integrations.qq_astrbot.plugin import vision_config as module, prompt_loader
    gates = VisionGates(tmp_path, config)
    gates.record("image", {"passed": True, "report": "tested pixels"})
    original = module.fingerprint(config)
    prompts = prompt_loader.load_prompts()
    monkeypatch.setattr(prompt_loader, "load_prompts", lambda: {**prompts, "vision_reply": "changed reply wording"})
    assert module.fingerprint(config) != original
    assert gates.enabled("image")
    changed = {**config, "cache_ttl_seconds": 600, "request_timeout_seconds": 120, "media_question_window_seconds": 90}
    assert VisionGates(tmp_path, changed).enabled("image")


@pytest.mark.parametrize("change", ["model", "projector", "backend", "context", "contract"])
def test_visual_compatibility_changes_still_require_evidence(tmp_path, config, monkeypatch, change):
    from integrations.qq_astrbot.plugin import vision_config as module
    root = tmp_path / "project"
    active_path = root / "model_store/active/qwen3.json"
    settings_path = root / "configs/runtime/qwen3.yaml"
    binary = root / "tools/llama.cpp/llama-server.exe"
    for path in (active_path, settings_path, binary):
        path.parent.mkdir(parents=True, exist_ok=True)
    active = {"model_id": "model-a", "runtime": {"mmproj_sha256": "projector-a"}}
    active_path.write_text(json.dumps(active))
    settings_path.write_text("context_size: 32768\n")
    binary.write_bytes(b"backend-a")
    monkeypatch.setattr(module, "project_root", lambda: root)
    gates = VisionGates(tmp_path, config)
    gates.record("image", {"passed": True, "report": "tested pixels"})
    if change == "model":
        active_path.write_text(json.dumps({**active, "model_id": "model-b"}))
    elif change == "projector":
        active_path.write_text(json.dumps({**active, "runtime": {"mmproj_sha256": "projector-b"}}))
    elif change == "backend":
        binary.write_bytes(b"backend-b-different")
    elif change == "context":
        settings_path.write_text("context_size: 8192\n")
    else:
        monkeypatch.setattr(module, "CAPABILITY_CONTRACT_VERSION", 999)
    assert gates.status("image") == {"enabled": False, "reason": "model_or_visual_capacity_changed"}


def test_gate_upgrade_requires_real_evidence_and_rejects_changed_model(tmp_path, config):
    from integrations.qq_astrbot.plugin.vision_config import capability_fingerprint, fingerprint
    gates = VisionGates(tmp_path, config)
    gates.path.write_text(json.dumps({"fingerprint": fingerprint(config),
        "capabilities": {"image": {"passed": True, "report": "legacy evidence"}}}))
    assert gates.status("image")["reason"] == "legacy_evidence_requires_upgrade"
    with pytest.raises(ValueError, match="vision_changed_during_acceptance"):
        gates.record("image", {"passed": True, "report": "new evidence"}, expected_fingerprint="another model")
    assert not gates.enabled("image")
    gates.record("image", {"passed": True, "report": "new evidence"}, expected_fingerprint=capability_fingerprint(config))
    assert gates.enabled("image")


@pytest.mark.parametrize("extra", [{"confidence": float("nan")}, {"confidence": 1.1},
    {"timestamps": [{"timestamp": 25, "description": "outside"}]}, {"timestamps": [{"timestamp": -1, "description": "negative"}]}])
def test_rejects_untrustworthy_analysis(extra):
    with pytest.raises(Exception):
        validate_analysis(result(**extra), 0, 24)


def test_analysis_preserves_pixels_and_question_and_owner_cache(tmp_path, config, loop):
    async def check():
        store = VisionStore(tmp_path, config)
        key = store.put(picture(), "owner", "image")
        calls = []
        async def model(prompt, payload, images, schema):
            calls.append((prompt, payload, images))
            assert schema["type"] == "json_schema"
            return json.dumps(result())
        analyzer = VisionAnalyzer(store, config, model)
        actual = await analyzer.image(key, "owner", "What color?")
        assert actual["media_id"] == key and actual["source_type"] == "image"
        assert calls[0][1]["question"] == "What color?"
        assert calls[0][2][0].startswith("data:image/jpeg;base64,")
        await analyzer.image(key, "owner", "What color?")
        assert len(calls) == 1
        await analyzer.image(key, "owner", "What shape?")
        assert len(calls) == 2
    loop.run_until_complete(check())


def test_video_processes_whole_timeline_in_small_batches_and_marks_audio_failure(tmp_path, config, loop, monkeypatch):
    from integrations.qq_astrbot.plugin import vision_analysis as module
    from integrations.qq_astrbot.plugin.vision_media import Segment, Frame
    async def check():
        store = VisionStore(tmp_path, config)
        key = store.put(b"video fixture", "owner", "video")
        calls = []
        monkeypatch.setattr(module, "video_info", lambda *args: (60, True))
        monkeypatch.setattr(module, "extract_segment", lambda path, a, b, ts, cfg:
            Segment(a, b, [Frame(t, picture(size=(32, 32)), 32, 32) for t in ts]))
        def broken_asr(*args):
            raise MediaError("broken_audio")
        monkeypatch.setattr(module, "extract_audio", broken_asr)
        async def model(prompt, payload, images, schema):
            calls.append((prompt, payload, images))
            return json.dumps(result())
        async def transcribe(data):
            raise AssertionError("No audio file available")
        analyzer = VisionAnalyzer(store, config, model, transcribe=transcribe)
        value = await analyzer.video(key, "owner", "What happened?")
        assert value["covered_intervals"] == [[0, 24], [24, 48], [48, 60]]
        assert value["uncovered_intervals"] == []
        assert value["audio_status"] == ["asr_failed"] * 3
        batches = [(p, imgs) for name, p, imgs in calls if name == "vision_analyze"]
        assert [len(imgs) for _, imgs in batches] == [4, 4, 4, 4, 4, 4, 4, 2]
        assert [f["timestamp"] for p, _ in batches for f in p["sampled_frames"]] == list(range(0, 60, 2))
        assert "uncertainty" in value and value["uncertainty"]
        previous = len(calls)
        assert await analyzer.video(key, "owner", "What happened?") == value
        assert len(calls) == previous
    loop.run_until_complete(check())


def test_capacity_rejection_splits_batches_instead_of_omitting_frames(tmp_path, config, loop):
    from integrations.qq_astrbot.plugin.vision_media import Frame
    async def check():
        store = VisionStore(tmp_path, config)
        frames = [Frame(t, b"pixels", 32, 32) for t in range(12)]
        observed = []
        async def model(prompt, payload, images, schema):
            if len(images) > 2:
                raise MediaError("multimodal_context_capacity_exceeded")
            observed.extend(f["timestamp"] for f in payload["sampled_frames"])
            return json.dumps(result())
        analyzer = VisionAnalyzer(store, config, model)
        parts = await analyzer.analyze_frames(frames, "", "video", end=24)
        assert len(parts) == 6 and observed == list(range(12))
    loop.run_until_complete(check())


def test_optional_api_requires_own_token_and_never_accepts_paths(tmp_path, config, monkeypatch, loop):
    from fastapi import HTTPException
    from pydantic import ValidationError
    from services.vision_api.app import app, authorized, AnalysisRequest, analyze_image, health, lifespan
    monkeypatch.setenv("ASTRBOT_CHAT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("QQ_COMPANION_VISION_TOKEN", "local-test-credential")
    with pytest.raises(HTTPException) as error:
        authorized("")
    assert error.value.status_code == 401
    authorized("Bearer local-test-credential")
    assert health()["model_owner"] is False
    with pytest.raises(ValidationError):
        AnalysisRequest(media_id="C:/secret", owner="owner")
    async def check():
        async with lifespan(app):
            with pytest.raises(HTTPException) as error:
                await analyze_image(AnalysisRequest(media_id="a" * 64, owner="owner"))
            assert error.value.status_code == 409
    loop.run_until_complete(check())
