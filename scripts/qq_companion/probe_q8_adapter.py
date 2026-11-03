"""Real Chromium against local HTML fixtures; no external network or messages."""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / "tools/playwright-browsers"))

HTML = """<!doctype html><meta charset=utf-8><style>
video {width:640px;height:360px} #panel {display:none} #share:hover+#panel,#panel:hover {display:block}
</style><div data-e2e=feed-active-video data-e2e-vid=7000000000000000001>
<video></video><div data-e2e=video-desc>可见视频标题</div><div>不应混入的弹幕</div>
<div id=share data-e2e=video-player-share>分享入口</div><div id=panel>
<div><span>miemie</span><div data-userid=89194419808 onclick="window.sent=(window.sent||0)+1;this.innerText='已分享'">分享</div></div>
</div></div><script>
const video=document.querySelector('video');
Object.defineProperty(video,'duration',{value:10,configurable:true});
Object.defineProperty(video,'currentSrc',{value:'https://v.douyinvod.com/test.mp4',configurable:true});
window.addEventListener('keydown',e=>{if(e.key==='ArrowDown')document.querySelector('[data-e2e-vid]').setAttribute('data-e2e-vid','7000000000000000002')});
</script>"""


async def main():
    from playwright.async_api import async_playwright
    from services.douyin_worker.browser_session import BrowserSession, BrowserStopped
    from services.douyin_worker.feed_driver import FeedDriver, UnsupportedRecommendation
    from services.douyin_worker.worker import Paused
    from services.douyin_worker.config import Friend
    friend = Friend(douyin_id="dywklklhl", name="miemie")
    async with async_playwright() as playwright:
        engine = await playwright.chromium.launch(headless=True)
        context = await engine.new_context()
        await context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=HTML))
        browser = BrowserSession(ROOT / "storage/temporary/q8-adapter-probe")
        browser.context, browser.page = context, await context.new_page()
        browser.friend_user_ids[friend.douyin_id] = "89194419808"
        driver = FeedDriver(browser)
        await browser.page.goto("https://www.douyin.com/?recommend=1")
        video = await driver.current()
        assert video["id"] == "7000000000000000001" and video["title"] == "可见视频标题"
        await driver.next()
        assert (await driver.current())["id"] == "7000000000000000002"
        browser.remember_media({"aweme_id": "7000000000000000002", "video": {
            "play_addr": {"url_list": ["https://v.douyinvod.com/visible.mp4"]}}})
        await browser.page.locator('video').evaluate("v=>Object.defineProperty(v,'currentSrc',{value:'blob:https://www.douyin.com/local',configurable:true})")
        assert (await driver.current())["media_url"] == "https://v.douyinvod.com/visible.mp4"
        # A normal native streamed video may report Infinity while buffering.
        await browser.page.locator('video').evaluate("v=>Object.defineProperty(v,'duration',{value:Infinity,configurable:true})")
        assert (await driver.current())["id"] == "7000000000000000002"
        # A livestream can overlay a stale, still-present ordinary feed ID.
        await browser.page.evaluate("""() => {
            document.querySelector('[data-e2e="feed-active-video"]').style.display='none';
            const box=document.createElement('div'); box.className='LivePlayer_Preview';
            box.innerHTML='<video></video>'; document.body.append(box);
            Object.defineProperty(box.firstChild,'duration',{value:Infinity});
        }""")
        try:
            await driver.current()
            raise AssertionError("live item was accepted as an ordinary video")
        except UnsupportedRecommendation as exc:
            assert str(exc) == "live_recommendation"
        await browser.page.evaluate("""() => {
            document.querySelector('.LivePlayer_Preview').style.opacity='0';
            document.querySelector('[data-e2e="feed-active-video"]').style.display='';
            const input=document.createElement('input'); document.body.append(input); input.focus();
            input.addEventListener('keydown', e=>e.stopPropagation());
        }""")
        assert (await driver.current())["id"] == "7000000000000000002"
        await driver.next()
        assert await browser.page.evaluate("document.activeElement.tagName") != 'INPUT'
        reservations = []
        def reserve(): reservations.append("reserved")
        receipt = await driver._share_page(browser.page, video, friend, lambda: None, reserve)
        assert receipt == "native_share_success_receipt"
        assert await browser.page.evaluate("window.sent") == 1 and len(reservations) == 1
        # Pre-send policy changes cancel before a reservation/click.
        def interrupted(): raise Paused("chatting")
        try:
            await driver._share_page(browser.page, video, friend, interrupted, reserve)
            raise AssertionError("chat preemption was ignored")
        except Paused:
            pass
        assert await browser.page.evaluate("window.sent||0") == 0 and len(reservations) == 1
        browser.friend_user_ids.clear()
        try:
            await driver._share_page(browser.page, video, friend, lambda: None, reserve)
            raise AssertionError("unbound recipient was accepted")
        except BrowserStopped as exc:
            assert str(exc) == "share_recipient_identity_unverified"
        assert await browser.page.evaluate("window.sent||0") == 0
        await browser.page.evaluate("""() => {
            window.liveTurns=0;
            window.addEventListener('keydown', e=>{
                if(e.key!=='ArrowDown')return;
                window.liveTurns++;
                const item=document.querySelector('[data-e2e="feed-active-video"]');
                if(window.liveTurns===1){
                    item.style.display='none';
                    const box=document.createElement('div');box.className='LivePlayer_Preview';
                    box.innerHTML='<video></video>';document.body.append(box);
                    Object.defineProperty(box.firstChild,'duration',{value:Infinity});
                } else {document.querySelector('.LivePlayer_Preview')?.remove();item.style.display='';}
            });
        }""")
        await driver.next()
        assert await browser.page.evaluate("window.liveTurns") == 2
        assert (await driver.current())["id"] == "7000000000000000002"
        # Gallery has no visible video; its music URL must never enter the video path.
        await browser.page.evaluate("""() => {
            const item=document.querySelector('[data-e2e="feed-active-video"]');
            item.querySelector('video').remove();
            item.innerHTML='<img style="width:640px;height:360px" src="data:image/svg+xml,%3Csvg xmlns=\"http://www.w3.org/2000/svg\"/%3E">'+item.innerHTML;
        }""")
        browser.remember_media({"aweme_id": "7000000000000000002", "aweme_type": 68,
            "images": [{"url_list": ["https://p.douyinpic.com/one.jpg"]}],
            "video": {"play_addr": {"url_list": ["https://sf11-cdn-tos.douyinstatic.com/music.mp3"]}}})
        gallery = await driver.current()
        assert gallery["kind"] == "image_post" and len(gallery["images"]) == 1
        assert not gallery["media_url"]
        # Native note details put the action in the player, outside the
        # feed-active-video recommendations sidebar (observed on 2026-11-02).
        note_html = HTML.replace('data-e2e=feed-active-video data-e2e-vid=',
            'data-e2e=player-container data-e2e-vid=').replace('<video></video>',
            '<video></video><img alt="gallery image">')
        note_html = note_html.replace('<div data-e2e=player-container',
            '<main data-e2e=note-detail><div data-e2e=player-container').replace('</div></div><script>',
            '</div></div></main><aside data-e2e=feed-active-video>recommendations</aside><script>')
        async def note_route(route):
            if '/video/' in route.request.url:
                await route.fulfill(status=302, headers={"Location": route.request.url.replace('/video/', '/note/')})
            else:
                await route.fulfill(status=200, content_type="text/html", body=note_html)
        await context.unroute("**/*")
        await context.route("**/*", note_route)
        browser.friend_user_ids[friend.douyin_id] = "89194419808"
        receipt = await driver._share_page(browser.page, video, friend, lambda: None, reserve)
        assert '/note/' in browser.page.url and receipt == "native_share_success_receipt"
        assert await browser.page.evaluate("window.sent") == 1 and len(reservations) == 2
        # An observed upstream 502 is a pre-send service outage; it never
        # grants an identity binding or a reservation.
        await context.unroute("**/*")
        await context.route("**/*", lambda route: route.fulfill(status=502,
            content_type="text/html", body="<h1>502 Bad Gateway</h1>"))
        try:
            await browser.verify_friend(friend)
            raise AssertionError("server outage must not verify a friend")
        except BrowserStopped as exc:
            assert str(exc) == "share_service_unavailable"
        assert friend.douyin_id not in browser.friend_user_ids and len(reservations) == 2
        await engine.close()
    print(json.dumps({"result": "Q8_ADAPTER_PASS", "checks": ["visible_item", "native_next", "identity_bound_send",
        "blob_video_source", "streamed_video_infinite_duration", "live_overlay_skipped", "live_landing_next", "focused_input_next", "gallery_without_video", "gallery_detail_share", "share_service_502", "receipt", "pre_send_chat_cancel", "unbound_recipient_blocked"], "external_sends": 0}))


if __name__ == "__main__":
    asyncio.run(main())
