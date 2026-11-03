"""Normal persistent Chromium session; credentials remain in its local profile."""
from __future__ import annotations

from contextlib import suppress
import asyncio
import json
import math
import re
from urllib.parse import urljoin, urlsplit


class BrowserStopped(RuntimeError):
    pass


def video_address(video, limit):
    """Use a native bounded-size rendition when the default file is too large."""
    primary = video.get("play_addr") or {}
    def number(value):
        return value if isinstance(value, (int, float)) and value > 0 else 0
    size = number(primary.get("data_size"))
    if 0 < size <= limit:
        return primary, False
    candidates = []
    for variant in video.get("bit_rate") or []:
        if not isinstance(variant, dict):
            continue
        address = variant.get("play_addr") or {}
        if (0 < number(address.get("data_size")) <= limit and address.get("url_list")):
            candidates.append((number(address.get("width")) * number(address.get("height")),
                number(variant.get("bit_rate")), address))
    if candidates:
        return max(candidates, key=lambda item: item[:2])[2], True
    return primary, False


def public_profile_uid(value, friend, sec_uid):
    """Bind only a complete public profile identity, never a nickname/UID alone."""
    if isinstance(value, dict):
        public_id = value.get("unique_id", value.get("uniqueId")) or value.get("short_id", value.get("shortId"))
        secure = value.get("sec_uid", value.get("secUid"))
        uid = str(value.get("uid", ""))
        if (str(public_id) == friend.douyin_id and value.get("nickname") == friend.name
                and secure == sec_uid and re.fullmatch(r"[0-9]{5,24}", uid)):
            return uid
        matches = {found for child in value.values() if (found := public_profile_uid(child, friend, sec_uid))}
    elif isinstance(value, list):
        matches = {found for child in value if (found := public_profile_uid(child, friend, sec_uid))}
    else:
        return None
    return next(iter(matches)) if len(matches) == 1 else None


class BrowserSession:
    def __init__(self, directory, *, max_video_bytes=100 * 1024 * 1024):
        self.directory = directory
        self.max_video_bytes = max_video_bytes
        self.playwright = self.context = self.page = None
        self.verified_account = None
        self.friend_profiles = {}
        self.friend_user_ids = {}
        self.friend_metadata = {}
        self.media_urls = {}
        self.media_items = {}
        self.media_responses = set()

    def observe_media(self, response):
        """Keep public video URLs from the page's own normal feed responses."""
        parsed = urlsplit(response.url)
        if parsed.hostname != "www.douyin.com" or parsed.path not in {
                "/aweme/v1/web/tab/feed/", "/aweme/v1/web/aweme/detail/", "/aweme/v1/web/aweme/related/"}:
            return
        async def capture():
            with suppress(Exception):
                body = await response.body()
                if len(body) <= 8 * 1024 * 1024:
                    self.remember_media(json.loads(body))
        task = asyncio.create_task(capture())
        self.media_responses.add(task)
        task.add_done_callback(self.media_responses.discard)

    def remember_media(self, value):
        if isinstance(value, dict):
            key, video = str(value.get("aweme_id", "")), value.get("video")
            def preferred(urls):
                candidates = [u for u in urls if isinstance(u, str) and urlsplit(u).scheme in {"http", "https"}]
                return next(iter(sorted(candidates, key=lambda u: urlsplit(u).scheme != "https")), None)
            if re.fullmatch(r"[0-9]{10,24}", key):
                images = value.get("images")
                if images or value.get("aweme_type") == 68:
                    # A gallery's video.play_addr is background music, never a video asset.
                    urls = [preferred(image.get("url_list") or []) for image in (images or []) if isinstance(image, dict)]
                    self.media_items[key] = {"kind": "image_post", "images": urls,
                        "title": str(value.get("desc") or "")[:500]}
                    self.media_urls.pop(key, None)
                elif isinstance(video, dict):
                    address, fallback = video_address(video, self.max_video_bytes)
                    if url := preferred(address.get("url_list") or []):
                        self.media_urls[key] = url
                        self.media_items[key] = {"kind": "video", "media_url": url}
                        if title := value.get("desc"):
                            self.media_items[key]["title"] = str(title)[:500]
                        duration = video.get("duration")
                        if type(duration) in (int, float) and math.isfinite(duration) and duration > 0:
                            self.media_items[key]["duration"] = duration / 1000
                        if fallback:
                            self.media_items[key]["media_variant"] = {"bytes": address.get("data_size"),
                                "width": address.get("width"), "height": address.get("height"), "fallback": True}
                while len(self.media_items) > 100:
                    expired = next(iter(self.media_items))
                    self.media_items.pop(expired)
                    self.media_urls.pop(expired, None)
            for child in value.values():
                self.remember_media(child)
        elif isinstance(value, list):
            for child in value:
                self.remember_media(child)

    async def resolve_media(self, video, *, refresh=False):
        return (await self.resolve_item(video, refresh=refresh)).get("media_url")

    async def resolve_item(self, video, *, refresh=False):
        if refresh:
            self.media_urls.pop(video["id"], None)
            self.media_items.pop(video["id"], None)
        if video["id"] in self.media_items:
            return dict(self.media_items[video["id"]])
        # A saved checkpoint may belong to the previous browser session. Load
        # that exact public video normally; never guess a URL from another item.
        page = await self.context.new_page()
        try:
            await page.goto(video["url"], wait_until="domcontentloaded", timeout=30000)
            await self.check_page(page)
            for _ in range(50):
                if item := self.media_items.get(video["id"]):
                    return dict(item)
                items = await page.locator("video").evaluate_all(
                    "vs=>vs.map(v=>{v.pause();return {kind:'video',media_url:v.currentSrc||v.src,"
                    "duration:Number.isFinite(v.duration)?v.duration:null};}).filter(v=>/^https?:/.test(v.media_url))")
                await asyncio.sleep(.1)
            # Only use the DOM fallback after giving gallery metadata time to arrive.
            return items[0] if len(items) == 1 else {}
        finally:
            await page.close()

    async def open(self, config):
        if self.context and self.page and not self.page.is_closed():
            return
        if self.context or self.playwright:
            await self.close()
        from playwright.async_api import async_playwright
        from .security import protect
        profile = self.directory / "browser_profile"
        profile.mkdir(parents=True, exist_ok=True)
        protect(profile, directory=True)
        self.playwright = await async_playwright().start()
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(profile), headless=config.headless,
                viewport={"width": 1360, "height": 900}, accept_downloads=False)
            self.context.set_default_timeout(5000)
            self.context.on("response", self.observe_media)
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            await self.page.goto("https://www.douyin.com/?recommend=1&from_nav=1", wait_until="domcontentloaded", timeout=30000)
        except BaseException:
            await self.close()
            raise

    @staticmethod
    async def check_page(page):
        host = urlsplit(page.url).hostname
        if host not in {"www.douyin.com", "douyin.com"}:
            raise BrowserStopped("unexpected_browser_origin")
        for text, code in (("请完成下方验证", "captcha"), ("拖动滑块", "captcha"),
                ("访问过于频繁", "rate_limited"), ("账号已被封禁", "account_blocked"),
                ("安全验证", "captcha"), ("访问异常", "access_denied")):
            if await page.get_by_text(text, exact=False).first.is_visible():
                raise BrowserStopped(code)
        for selector in ('iframe[src*="captcha"]', '[id*="captcha_container"]'):
            if await page.locator(selector).first.is_visible():
                raise BrowserStopped("captcha")

    @staticmethod
    async def profile_identity(page, account):
        # Full public ID is authoritative. A nickname alone never grants send permission.
        expression = re.compile(r"抖音号\s*[:：]\s*" + re.escape(account.douyin_id) + r"(?![\w.-])")
        await page.get_by_text(expression).filter(visible=True).first.wait_for(state="visible", timeout=15000)
        await page.get_by_text(account.name, exact=True).filter(visible=True).first.wait_for(state="visible", timeout=15000)

    async def verify_account(self, config):
        self.verified_account = None
        if not config.account:
            raise BrowserStopped("account_not_configured")
        await self.check_page(self.page)
        page = await self.context.new_page()
        try:
            await page.goto("https://www.douyin.com/user/self", wait_until="domcontentloaded", timeout=30000)
            await self.check_page(page)
            await self.profile_identity(page, config.account)
        except BrowserStopped:
            raise
        except Exception as exc:
            await page.screenshot(path=str(self.directory / "douyin_account_verification.png"))
            (self.directory / "douyin_account_verification.txt").write_text(
                "url=" + page.url + "\n" + str(exc)[:600] + "\n" + (await page.locator("body").inner_text())[:5000], encoding="utf-8")
            raise BrowserStopped("login_required_or_wrong_account") from exc
        finally:
            await page.close()
        self.verified_account = config.account.model_dump()
        return {"account_verified": True}

    async def verify_friend(self, friend):
        self.friend_user_ids.pop(friend.douyin_id, None)
        page = await self.context.new_page()
        identities, pending = [], set()
        async def capture(response):
            parsed = urlsplit(response.url)
            if parsed.hostname == "www.douyin.com" and ("user/profile" in parsed.path
                    or parsed.path == "/aweme/v1/web/discover/search/"):
                with suppress(Exception):
                    body = await response.body()
                    if len(body) <= 2 * 1024 * 1024:
                        identities.append(json.loads(body))
        def observe(response):
            task = asyncio.create_task(capture(response))
            pending.add(task)
            task.add_done_callback(pending.discard)
        page.on("response", observe)
        try:
            response = await page.goto("https://www.douyin.com/search/" + friend.douyin_id + "?type=user",
                            wait_until="domcontentloaded", timeout=30000)
            await self.check_page(page)
            if response and 500 <= response.status < 600:
                raise BrowserStopped("share_service_unavailable")
            # Search only this approved ID, then require its profile to expose the exact ID.
            card = page.locator('a[href*="/user/"]').filter(has_text=friend.name).first
            await card.wait_for(state="visible", timeout=10000)
            href = await card.get_attribute("href")
            target = urlsplit(urljoin("https://www.douyin.com", href or ""))
            if (target.scheme != "https" or target.netloc != "www.douyin.com"
                    or not re.fullmatch(r"/user/[A-Za-z0-9_-]+", target.path)):
                raise BrowserStopped("friend_profile_unavailable")
            response = await page.goto(target.geturl(),
                            wait_until="domcontentloaded", timeout=30000)
            await self.check_page(page)
            if response and 500 <= response.status < 600:
                raise BrowserStopped("share_service_unavailable")
            await self.profile_identity(page, friend)
            try:
                await page.get_by_text(re.compile(r"^(相互关注|互相关注)$")).filter(visible=True).first.wait_for(state="visible", timeout=10000)
            except Exception as exc:
                raise BrowserStopped("friend_not_mutual") from exc
            self.friend_profiles[friend.douyin_id] = page.url.split("?")[0]
            if pending:
                await asyncio.gather(*pending)
            if uid := public_profile_uid(identities, friend, target.path.rsplit("/", 1)[-1]):
                self.friend_user_ids[friend.douyin_id] = uid
            self.friend_metadata = {"profile_verified": True, "share_identity_bound": bool(uid)}
            return {"friend_verified": True, "share_identity_bound": friend.douyin_id in self.friend_user_ids}
        except Exception as exc:
            await page.screenshot(path=str(self.directory / "douyin_friend_verification.png"))
            (self.directory / "douyin_friend_verification.txt").write_text(
                "url=" + page.url + "\n" + (await page.locator("body").inner_text())[:10000], encoding="utf-8")
            if isinstance(exc, BrowserStopped):
                raise
            raise BrowserStopped("friend_identity_unverified") from exc
        finally:
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await page.close()

    async def pause(self):
        if self.page and not self.page.is_closed():
            with suppress(Exception):
                await self.page.locator("video").evaluate_all("videos => videos.forEach(v => v.pause())")

    async def diagnostics(self):
        """Operator-only DOM shape, without cookies, storage or private messages."""
        if not self.page or self.page.is_closed():
            return {"opened": False}
        await self.check_page(self.page)
        return {"opened": True, "url": self.page.url, "title": await self.page.title(),
            "navigation": await self.page.locator("a[href]").evaluate_all(
                "els => els.filter(e=>e.getBoundingClientRect().width>0).map(e=>({text:e.innerText.slice(0,40),href:e.getAttribute('href')})).slice(0,60)"),
            "profile_labels": await self.page.get_by_text(re.compile(r"^抖音号")).all_text_contents(),
            "visible_buttons": await self.page.get_by_role("button").evaluate_all(
                "els => els.filter(e=>e.getBoundingClientRect().width>0).map(e=>({text:e.innerText.slice(0,50),label:e.getAttribute('aria-label')})).slice(0,30)"),
            "feed_controls": await self.page.locator('[data-e2e="feed-active-video"] [data-e2e]').evaluate_all(
                "els=>els.map(e=>({key:e.getAttribute('data-e2e'),text:e.innerText.slice(0,80),tag:e.tagName,role:e.getAttribute('role')}))"),
            "videos": await self.page.locator("video").evaluate_all("""vs => vs.slice(0,5).map(v=>{
                const parents=[]; let e=v; for(let n=0;e&&n<7;n++,e=e.parentElement)
                    parents.push({tag:e.tagName,attrs:[...e.attributes].filter(a=>a.name!=='src'&&a.name!=='style').map(a=>[a.name,a.value]),
                        links:[...e.querySelectorAll('a[href*="/video/"]')].slice(0,3).map(a=>a.href)});
                const r=v.getBoundingClientRect();
                return {parents,media_scheme:(v.currentSrc||v.src||'').split(':')[0],
                    duration:Number.isFinite(v.duration)?v.duration:null, position:v.currentTime, paused:v.paused,
                    visible_area:v.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})?Math.max(0,Math.min(r.right,innerWidth)-Math.max(r.left,0))*Math.max(0,Math.min(r.bottom,innerHeight)-Math.max(r.top,0)):0};})""")}

    async def resume(self):
        if self.page and not self.page.is_closed():
            await self.check_page(self.page)
            tutorial = self.page.get_by_text("我知道了", exact=True)
            if await tutorial.count() == 1 and await tutorial.is_visible():
                await tutorial.click()
            await self.page.locator("video").evaluate_all(
                """videos => { const candidates = videos.filter(v=>v.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}) &&
                    v.closest('[data-e2e="feed-active-video"], [data-e2e="feed-active-video-container"]') &&
                    !v.closest('[class*="LivePlayer"]')).map(v=>{const r=v.getBoundingClientRect(); return {v,area:
                        Math.max(0,Math.min(r.right,innerWidth)-Math.max(r.left,0))*Math.max(0,Math.min(r.bottom,innerHeight)-Math.max(r.top,0))};})
                    .sort((a,b)=>b.area-a.area); if(candidates[0]?.area>10000) candidates[0].v.play().catch(()=>{}); }""")

    async def close(self):
        if self.context:
            with suppress(Exception):
                await self.context.close()
        if self.playwright:
            with suppress(Exception):
                await self.playwright.stop()
        for task in self.media_responses:
            task.cancel()
        await asyncio.gather(*self.media_responses, return_exceptions=True)
        self.media_responses.clear()
        self.media_urls.clear()
        self.media_items.clear()
        self.playwright = self.context = self.page = None
        self.verified_account = None
        self.friend_profiles.clear()
        self.friend_user_ids.clear()
