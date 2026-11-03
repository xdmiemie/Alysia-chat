"""Small DOM adapter for the visible recommendation item and native sharing UI."""
from __future__ import annotations

import asyncio
import json
import re
import time
from urllib.parse import parse_qs, urlsplit

from .browser_session import BrowserStopped

VERSION = "douyin-web-visible-v5"


class UnsupportedRecommendation(Exception):
    """A live/unsupported item is immediately skipped, never analyzed/shared."""


def canonical_video(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc not in {"www.douyin.com", "douyin.com"}:
        raise BrowserStopped("invalid_video_link")
    match = re.fullmatch(r"/(?:video|note)/([0-9]{10,24})/?", parsed.path)
    key = match[1] if match else (parse_qs(parsed.query).get("modal_id") or [None])[0]
    if not key or not re.fullmatch(r"[0-9]{10,24}", key):
        raise BrowserStopped("visible_video_link_unavailable")
    return key, "https://www.douyin.com/video/" + key


class FeedDriver:
    def __init__(self, browser):
        self.browser = browser

    async def current(self):
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        page = self.browser.page
        await self.browser.check_page(page)
        try:
            await page.locator('video, [data-e2e="feed-active-video"], [data-e2e="feed-active-video-container"]').first.wait_for(state="attached", timeout=2000)
        except PlaywrightTimeoutError as exc:
            raise UnsupportedRecommendation("visible_video_unavailable") from exc
        data = await page.locator("video").evaluate_all("""videos => {
            const scored = videos.map(v => {const r=v.getBoundingClientRect();return {v, area:
                v.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}) ? Math.max(0,Math.min(r.right,innerWidth)-Math.max(r.left,0))*Math.max(0,Math.min(r.bottom,innerHeight)-Math.max(r.top,0)) : 0}})
                .sort((a,b)=>b.area-a.area);
            const v=scored[0]?.area>=10000 ? scored[0].v : null;
            const feed=v?.closest('[data-e2e="feed-active-video"], [data-e2e="feed-active-video-container"]');
            const itemId=feed && (feed.getAttribute('data-e2e-vid') || feed.getAttribute('data-aweme-id') || feed.getAttribute('data-id'));
            // Native streamed ordinary videos can temporarily report Infinity.
            // Explicit live containers still override any stale feed identity.
            if(v && (v.closest('[class*="LivePlayer"], [data-e2e="feed-live"], [data-e2e="live-player"]') ||
                v.duration === Infinity && !/^[0-9]{10,24}$/.test(itemId || '')))
                return {kind:'live'};
            const box=(v && (v.closest('[data-e2e="feed-active-video"]') || v.closest('[data-e2e="feed-active-video-container"]'))) ||
                [...document.querySelectorAll('[data-e2e="feed-active-video"], [data-e2e="feed-active-video-container"]')]
                    .find(e=>e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}) && [...e.querySelectorAll('img')].some(i=>{
                        const r=i.getBoundingClientRect();return r.width*r.height>10000 && r.top<innerHeight && r.bottom>0;}));
            if(!box) return null;
            const links=[...box.querySelectorAll('a[href*="/video/"], a[href*="/note/"]')].map(a=>a.href);
            const id=box.getAttribute('data-e2e-vid') || box.getAttribute('data-aweme-id') || box.getAttribute('data-id') || '';
            return {links, id, title:(box.querySelector('[data-e2e="video-desc"]')?.innerText||'').slice(0,500), media_url:v?.currentSrc||v?.src||'', duration:v&&Number.isFinite(v.duration)?v.duration:null, position:v?.currentTime||0};
        }""")
        if not data:
            raise UnsupportedRecommendation("visible_video_unavailable")
        if data.get("kind") == "live":
            raise UnsupportedRecommendation("live_recommendation")
        candidates = data["links"] + (["https://www.douyin.com/video/" + data["id"]] if data["id"] else []) + [page.url]
        for link in candidates:
            try:
                key, url = canonical_video(link)
                break
            except BrowserStopped:
                continue
        else:
            raise UnsupportedRecommendation("visible_video_link_unavailable")
        media_url = data["media_url"]
        if not media_url or urlsplit(media_url).scheme not in {"http", "https"}:
            media_url = self.browser.media_urls.get(key) or media_url
        item = getattr(self.browser, "media_items", {}).get(key, {})
        return {"id": key, "url": url, "title": data["title"][:500],
                "media_url": media_url, "duration": data["duration"], "position": data["position"], **item}

    async def next(self):
        while True:
            await self.browser.check_page(self.browser.page)
            # Search boxes and player controls can retain focus and swallow ArrowDown.
            await self.browser.page.locator("body").evaluate("body => {document.activeElement?.blur(); body.focus();}")
            await self.browser.page.keyboard.press("ArrowDown")
            await asyncio.sleep(0.8)
            try:
                await self.current()
            except UnsupportedRecommendation as exc:
                if str(exc) == "live_recommendation":
                    # This also applies to the viewing timer while the previous
                    # video's background analysis is still running.
                    continue
            return

    async def share(self, video, friend, checkpoint, before_send, config):
        """One native video share. Never selects by nickname alone or retries a click."""
        browser = self.browser
        checkpoint()
        await browser.verify_account(config)
        checkpoint()
        await browser.verify_friend(friend)
        checkpoint()
        page = await browser.context.new_page()
        try:
            return await self._share_page(page, video, friend, checkpoint, before_send)
        finally:
            await page.close()

    async def _share_page(self, page, video, friend, checkpoint, before_send):
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        browser = self.browser
        await page.goto(video["url"], wait_until="domcontentloaded", timeout=30000)
        await browser.check_page(page)
        button = page.locator('[data-e2e="feed-active-video"] [data-e2e="video-player-share"], '
            '[data-e2e="detail-video-info"] [data-e2e="video-share-icon-container"], '
            '[data-e2e="note-detail"] [data-e2e="player-container"] [data-e2e="video-player-share"]').filter(visible=True)
        try:
            await button.first.wait_for(state="visible", timeout=10000)
        except PlaywrightTimeoutError as exc:
            # A login/captcha/rate-limit page must retain its global stop reason.
            await browser.check_page(page)
            await page.screenshot(path=str(browser.directory / "douyin_share_verification.png"))
            shape = {"url": page.url, "title": await page.title(),
                "controls": await page.locator('[data-e2e]').evaluate_all(
                    "els=>els.filter(e=>e.getBoundingClientRect().width>0).map(e=>({key:e.getAttribute('data-e2e'),tag:e.tagName,text:e.innerText.slice(0,60)})).slice(0,100)")}
            (browser.directory / "douyin_share_verification.json").write_text(json.dumps(shape, ensure_ascii=False), encoding="utf-8")
            raise BrowserStopped("native_share_entry_unavailable") from exc
        await browser.check_page(page)
        if await button.count() != 1:
            raise BrowserStopped("ambiguous_share_control")
        await page.locator("video").evaluate_all("vs=>vs.forEach(v=>v.pause())")
        await button.hover()
        # Current native UI sends directly from a per-user button. Its UID is
        # bound to the exact public ID on the normal profile's network response.
        uid = browser.friend_user_ids.get(friend.douyin_id)
        if not uid:
            raise BrowserStopped("share_recipient_identity_unverified")
        send = page.locator('[data-userid="' + uid + '"]').filter(visible=True)
        await send.first.wait_for(state="visible", timeout=5000)
        if await send.count() != 1:
            raise BrowserStopped("share_confirmation_unavailable")
        if await send.inner_text() != "分享" or not await send.locator("..").get_by_text(friend.name, exact=True).is_visible():
            raise BrowserStopped("share_recipient_identity_unverified")
        checkpoint()
        before_send()
        await send.click(no_wait_after=True)
        # A visible platform receipt is required; click completion is not a receipt.
        receipt = page.get_by_text(re.compile(r"^(分享成功|发送成功)$")).filter(visible=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            checkpoint()
            if await receipt.count() or (await send.count() == 1 and await send.inner_text() in {"已分享", "已发送"}):
                return "native_share_success_receipt"
            await asyncio.sleep(.05)
        raise BrowserStopped("share_result_uncertain")
