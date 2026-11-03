"""Gallery routing, bounded output retries and durable recovery regressions."""
import asyncio
from io import BytesIO
import json
import sys
from types import SimpleNamespace

import httpx
from PIL import Image
import pytest

from integrations.qq_astrbot.plugin.vision_config import load_vision_config
from integrations.qq_astrbot.plugin.vision_media import MediaError
from integrations.qq_astrbot.plugin.vision_store import VisionStore
from services.douyin_worker.analysis import DouyinAnalyzer
from services.douyin_worker.browser_session import BrowserSession, video_address
from services.douyin_worker.model_client import ModelClient
from services.douyin_worker.store import Store
from services.douyin_worker.worker import Worker, Paused


@pytest.fixture
def loop():
    value = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
    yield value
    value.close()


def analysis():
    return dict(summary="图文内容", objects=[], actions=[], ocr_text=[], uncertainty=[],
        emotion=None, confidence=.9, timestamps=[])


def picture(color):
    output = BytesIO()
    Image.new("RGB", (32, 24), color).save(output, "PNG")
    return output.getvalue()


def test_gallery_metadata_never_selects_background_music(tmp_path):
    browser = BrowserSession(tmp_path)
    key = "7702400318349489615"
    browser.remember_media({"aweme_id": key, "video": {"play_addr": {"url_list": ["https://v.douyinvod.com/a.mp4"]}}})
    browser.remember_media({"aweme_id": key, "aweme_type": 68, "desc": "图文",
        "video": {"play_addr": {"url_list": ["https://sf11-cdn-tos.douyinstatic.com/music.mp3"]}},
        "images": [{"url_list": ["http://p.douyinpic.com/one", "https://p.douyinpic.com/one"]},
                   {"url_list": ["https://p.douyinpic.com/two"]}]})
    assert key not in browser.media_urls
    assert browser.media_items[key] == {"kind": "image_post", "title": "图文",
        "images": ["https://p.douyinpic.com/one", "https://p.douyinpic.com/two"]}


def test_large_video_selects_native_rendition_within_byte_budget(tmp_path):
    def address(size, width, height, name):
        return {"data_size": size, "width": width, "height": height,
            "url_list": ["https://v.douyinvod.com/" + name + ".mp4"]}
    primary = address(150_000_000, 1920, 1080, "primary")
    video = {"play_addr": primary, "bit_rate": [
        {"bit_rate": 2_000_000, "play_addr": address(140_000_000, 1920, 1080, "too-large")},
        {"bit_rate": 1_200_000, "play_addr": address(90_000_000, 1280, 720, "720")},
        {"bit_rate": 1_000_000, "play_addr": address(80_000_000, 1920, 1080, "1080")}]}
    selected, fallback = video_address(video, 104857600)
    assert fallback and selected["url_list"][0].endswith("/1080.mp4")
    assert video_address(video, 200_000_000) == (primary, False)
    assert video_address(video, 10_000_000) == (primary, False)  # Download still enforces the byte limit.
    browser = BrowserSession(tmp_path)
    browser.remember_media({"aweme_id": "7681954687398006031", "video": video})
    assert browser.media_urls["7681954687398006031"] == selected["url_list"][0]


def test_gallery_covers_every_image_and_resumes_completed_batches(tmp_path, loop):
    async def scenario():
        config = load_vision_config()
        store = VisionStore(tmp_path, config)
        keys = [store.put(picture((i, 1, 2)), "owner", "image") for i in range(43)]
        calls, interrupt = [], True
        async def call(phase, payload, images, fmt):
            nonlocal interrupt
            if phase == "vision_analyze":
                indexes = [f["timestamp"] for f in payload["sampled_frames"]]
                if indexes[0] == 5 and interrupt:
                    interrupt = False
                    raise Paused("chatting")
                assert len(images) <= 4 and payload["audio_status"] == "visual_only"
                calls.extend(indexes)
            return json.dumps(analysis(), ensure_ascii=False)
        analyzer = DouyinAnalyzer(store, config, call)
        with pytest.raises(Paused):
            await analyzer.gallery(keys, "owner", "按图片序号理解")
        value = await analyzer.gallery(keys, "owner", "按图片序号理解")
        assert calls == list(range(1, 44))  # Completed first batch was not sent twice.
        assert value["image_count"] == 43 and value["covered_images"] == list(range(1, 44))
        assert value["source_type"] == "image_post" and value["audio_status"] == "not_applicable"
        assert "duration" not in value
        assert await analyzer.gallery(keys, "owner", "按图片序号理解") == value
    loop.run_until_complete(scenario())


@pytest.mark.parametrize("phase,budgets", [("vision_analyze", [3072, 6144]), ("vision_merge", [4096, 8192])])
@pytest.mark.parametrize("always_length", [False, True])
def test_output_retry_uses_larger_wire_budget_and_keeps_truncated_response(tmp_path, loop, monkeypatch, phase, budgets, always_length):
    from integrations.qq_astrbot.plugin import model_debug
    archive, wire = [], []
    monkeypatch.setattr(model_debug, "append_exchange", lambda record, directory: archive.append(record))
    factory = httpx.AsyncClient
    def handler(request):
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": [1, 2, 3]})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "local"}]})
        body = json.loads(request.content)
        wire.append(body)
        reason = "length" if len(wire) == 1 or always_length else "stop"
        content = '{"summary":' if reason == "length" else '{"ok":true}'
        chunk = {"choices": [{"delta": {"content": content}, "finish_reason": reason}],
            "usage": {"prompt_tokens": 3, "completion_tokens": body["max_tokens"]}}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: factory(**kw, transport=httpx.MockTransport(handler)))
    model = ModelClient(tmp_path, load_vision_config())
    operation = model.complete([{"role": "user", "content": "识图"}], {"type": "json_object"}, phase=phase)
    if always_length:
        with pytest.raises(MediaError, match="incomplete_douyin_output"):
            loop.run_until_complete(operation)
    else:
        assert json.loads(loop.run_until_complete(operation)) == {"ok": True}
    assert [body["max_tokens"] for body in wire] == budgets
    assert len(archive) == 2 and archive[0]["status"] == "incomplete"
    assert archive[0]["response"]["choices"][0]["message"]["content"] == '{"summary":'
    assert archive[0]["response"]["usage"]["completion_tokens"] == budgets[0]
    assert model.media.state.active() == 0


def test_recovery_queue_is_account_scoped_idempotent_and_preserves_progress(tmp_path):
    store = Store(tmp_path)
    def add(account, number, status, error):
        item = {"id": str(7000000000000000000 + number), "url": "https://www.douyin.com/video/" + str(7000000000000000000 + number)}
        store.encounter(account, item)
        store.save_video(account, item["id"], status, media_id="cached-prefix", analysis=analysis(), error=error)
        return item["id"]
    keys = [add("account", i, "unavailable", error) for i, error in enumerate(
        ["incomplete_douyin_output", "media_domain_rejected", "video_too_long"])]
    add("other", 4, "unavailable", "incomplete_douyin_output")
    add("account", 5, "done", "incomplete_douyin_output")
    add("account", 6, "revoked", "media_domain_rejected")
    add("account", 7, "unavailable", "media_http_404")
    assert store.queue_recoverable("account") == keys
    assert store.queue_recoverable("account") == []
    store = Store(tmp_path)  # Restart resumes the persisted queue.
    row = store.next_retry("account")
    assert row["id"] == keys[0] and row["media_id"] == "cached-prefix" and row["analysis"]
    store.save_video("account", keys[0], "unavailable", error="incomplete_douyin_output")
    store.finish_retry("account", keys[0])
    assert store.queue_recoverable("account") == []
    assert store.next_retry("account")["id"] == keys[1]
    assert store.retries("account")[0]["state"] == "failed"
    # A newly repaired cause gets its own one-shot recovery, preserving old audit rows.
    store.save_video("account", keys[0], "unavailable", error="media_file_too_large")
    assert store.queue_recoverable("account") == [keys[0]]
    assert store.queue_recoverable("account") == []
    assert len(store.retries("account")) == 3
    store.save_video("account", keys[0], "unavailable", error="media_file_too_large")
    store.finish_retry("account", keys[0])
    assert store.queue_recoverable("account") == []


def test_gallery_worker_downloads_only_pictures_and_resumes_partial_download(tmp_path, loop):
    async def scenario():
        worker = Worker(tmp_path)
        worker.checkpoint = lambda: None
        source = "image_post:account:id"
        video = {"images": ["https://p.douyinpic.com/1", "https://p.douyinpic.com/2"], "title": "图文"}
        downloaded, first = [], True
        async def download(url, *, image=False):
            nonlocal first
            assert image
            if url.endswith("/2") and first:
                first = False
                raise Paused("chatting")
            downloaded.append(url)
            return picture("red" if url.endswith("/1") else "blue")
        worker.download = download
        async def gallery(keys, owner, question, **kw):
            assert len(keys) == 2 and "图片序号" in question
            return analysis()
        worker.analyzer.gallery = gallery
        with pytest.raises(Paused):
            await worker.process_gallery(None, video, "owner", source, None)
        assert worker.store.get(source)["title"] == "图文"
        video["title"] = "浏览器重启后可能含展开标签的标题"
        await worker.process_gallery(None, video, "owner", source, worker.store.get(source))
        assert downloaded == video["images"]
    loop.run_until_complete(scenario())
