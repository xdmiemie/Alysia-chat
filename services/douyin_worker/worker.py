"""One resumable idle-time browser loop; QQ always takes precedence."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import math
import time
from urllib.parse import urljoin

import httpx
import jsonschema

from integrations.qq_astrbot.plugin.foreground_activity import ForegroundActivity
from integrations.qq_astrbot.plugin.media_state import MediaState
from integrations.qq_astrbot.plugin.memory_repository import MemoryRepository, shared_owner
from integrations.qq_astrbot.plugin.vision_config import VisionGates, load_vision_config, project_root
from integrations.qq_astrbot.plugin.vision_media import MediaError, validate_url, decode_images
from integrations.qq_astrbot.plugin.vision_store import VisionStore

from .browser_session import BrowserSession, BrowserStopped
from .analysis import DouyinAnalyzer
from .config import ConfigStore
from .feed_driver import FeedDriver, UnsupportedRecommendation
from .interest_scorer import acceptable, recommend, score
from .model_client import ModelClient
from .media import MAX_VIDEO_SECONDS, video_prefix
from .store import Store


class Paused(Exception):
    pass


class Worker:
    def __init__(self, directory, *, config=None, browser=None, model=None, clock=time.time):
        self.directory, self.clock = directory, clock
        self.config = config or ConfigStore()
        self.store = Store(directory, clock)
        self.foreground = ForegroundActivity(directory)
        self.media_state = MediaState(directory)
        self.vision_config = load_vision_config()
        self.browser = browser or BrowserSession(directory, max_video_bytes=self.vision_config["max_video_bytes"])
        self.feed = FeedDriver(self.browser)
        self.assets = VisionStore(directory, self.vision_config)
        self.model = model or ModelClient(directory, self.vision_config)
        self.analyzer = DouyinAnalyzer(self.assets, self.vision_config, self.model.call,
                                       transcribe=getattr(self.model, "transcribe", self.model.media.transcribe))
        self.closed = False
        self.task = None
        self.operation_lock = asyncio.Lock()
        self.state = "disabled"
        self.errors = 0
        self.running_account = None

    def reason(self):
        config = self.config.read()
        if self.closed or (self.directory / "stop.requested").exists():
            return "stopping"
        if self.store.get("halt"):
            return "halted"
        if not config.enabled:
            return "disabled"
        if not config.account:
            return "account_not_configured"
        if self.running_account and config.account.model_dump() != self.running_account:
            return "account_changed"
        if not config.awake(self.clock()):
            return "sleeping"
        if self.foreground.blocked(config.idle_resume_seconds):
            return "chatting_or_idle_cooldown"
        if self.media_state.mode() != "ready":
            return "resource_busy"
        return None

    def checkpoint(self):
        if reason := self.reason():
            raise Paused(reason)

    async def controlled(self, operation):
        task = asyncio.ensure_future(operation)
        try:
            while not task.done():
                self.checkpoint()
                await asyncio.wait({task}, timeout=0.1)
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def download(self, url, *, image=False):
        # No browser cookies are exported. Only the visible video's normal media URL is fetched.
        async with httpx.AsyncClient(trust_env=False, timeout=25, follow_redirects=False,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.douyin.com/"}) as client:
            for _ in range(5):
                self.checkpoint()
                await asyncio.to_thread(validate_url, url, self.vision_config["allowed_media_domains"])
                async with client.stream("GET", url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        url = urljoin(url, response.headers.get("location", ""))
                        continue
                    response.raise_for_status()
                    limit = self.vision_config["max_image_bytes" if image else "max_video_bytes"]
                    if int(response.headers.get("content-length", "0")) > limit:
                        raise MediaError("media_file_too_large")
                    data = bytearray()
                    async for block in response.aiter_bytes():
                        self.checkpoint()
                        data.extend(block)
                        if len(data) > limit:
                            raise MediaError("media_file_too_large")
                    if not data:
                        raise MediaError("media_empty")
                    return bytes(data)
        raise MediaError("media_redirect_limit")

    def remember(self, config, video, analysis):
        # This public source has no private conversation input. Owner/account mappings
        # come only from Q5's existing registered accounts, never the browser/model.
        active = json.loads((project_root() / "model_store/active/qwen3.json").read_text(encoding="utf-8-sig"))
        character = active["character_id"]
        repo = MemoryRepository(self.directory / "memories.sqlite3")
        owners = []
        for registered in repo.accounts(character):
            bot = registered["bot"]
            repo.add_public_video(bot, video["url"], analysis["summary"], analysis.get("objects", [])[:12],
                                  character=character, confidence=analysis["confidence"], now=self.clock())
            owners.append([shared_owner(bot, character), character])
        self.store.set("memory_owners:" + config.account.douyin_id + ":" + video["id"], owners)

    async def process(self, config, video):
        account = config.account.douyin_id
        row = self.store.video(account, video["id"])
        if not row or row["status"] in {"done", "filtered", "revoked", "unavailable"}:
            return
        owner = "douyin:" + account
        key = row["media_id"]
        source_key = "image_post:" + account + ":" + video["id"]
        gallery = self.store.get(source_key)
        if not key and not gallery:
            metadata = getattr(self.browser, "media_items", {}).get(video["id"], {})
            if metadata:
                video.update(metadata)
            if not video.get("kind") and (getattr(self.browser, "context", None) or
                    not (video.get("media_url") or "").startswith(("https://", "http://"))):
                video.update(await self.browser.resolve_item(video))
        if gallery or video.get("kind") == "image_post":
            if not VisionGates(self.directory, self.vision_config).enabled("image"):
                raise BrowserStopped("q7_image_gate_required")
            analysis = json.loads(row["analysis"]) if row["analysis"] else await self.process_gallery(
                config, video, owner, source_key, gallery)
        else:
            if not VisionGates(self.directory, self.vision_config).enabled("video"):
                raise BrowserStopped("q7_video_gate_required")
            analysis = await self.process_video(config, video, row, owner)
            if analysis is None:
                return
        self.checkpoint()
        self.store.save_video(account, video["id"], "analyzed", analysis=analysis)
        active = json.loads((project_root() / "model_store/active/qwen3.json").read_text(encoding="utf-8-sig"))
        character = active["character_id"]
        from .persona import active_persona
        persona = active_persona(project_root(), character)
        interest = json.loads(row["interest"]) if row["interest"] else await score(self.model, analysis, video["title"], persona)
        self.checkpoint()
        self.store.save_video(account, video["id"], "rated", interest=interest)
        if acceptable(analysis, interest):
            self.remember(config, video, analysis)
        if recommend(analysis, interest):
            await self.share(config, video, analysis, interest)
        self.store.save_video(account, video["id"], "done" if acceptable(analysis, interest) else "filtered")

    async def process_gallery(self, config, video, owner, source_key, gallery):
        if not gallery:
            images = video.get("images")
            if not images or not all(isinstance(url, str) and url for url in images):
                raise MediaError("gallery_images_unavailable")
            gallery = {"images": images, "keys": [], "title": video.get("title", "")}
            self.store.set(source_key, gallery)
        for index in range(len(gallery["keys"]), len(gallery["images"])):
            self.checkpoint()
            try:
                data = await self.download(gallery["images"][index], image=True)
            except httpx.HTTPStatusError:
                refreshed = await self.browser.resolve_item(video, refresh=True)
                images = refreshed.get("images", [])
                if refreshed.get("kind") != "image_post" or len(images) != len(gallery["images"]):
                    raise MediaError("gallery_source_changed")
                gallery["images"] = images
                self.store.set(source_key, gallery)
                data = await self.download(images[index], image=True)
            await asyncio.to_thread(decode_images, data, self.vision_config)
            gallery["keys"].append(self.assets.put(data, owner, "image"))
            self.store.set(source_key, gallery)
        question = ("按顺序理解这条图文的图片、文字及其关联。timestamp和segment_end代表图片序号，不是秒数。"
            "结合标题：" + gallery.get("title", video.get("title", "")) + "。只依据图片和标题，不推断配乐或未出现的内容。")
        return await self.analyzer.gallery(gallery["keys"], owner, question, check_active=self.checkpoint)

    async def process_video(self, config, video, row, owner):
        account, key = config.account.douyin_id, row["media_id"]
        if not key:
            if variant := video.get("media_variant"):
                self.store.set("media_variant:" + account + ":" + video["id"], variant)
            if not (video.get("media_url") or "").startswith(("https://", "http://")):
                video["media_url"] = await self.browser.resolve_media(video)
            if not video.get("media_url"):
                self.store.save_video(account, video["id"], "unavailable", error="no_visible_media_url")
                return
            checkpoint = self.store.get("checkpoint")
            if checkpoint and (checkpoint.get("video") or {}).get("id") == video["id"]:
                checkpoint["video"] = video
                self.store.set("checkpoint", checkpoint)
            try:
                data = await self.download(video["media_url"])
            except httpx.HTTPStatusError:
                # CDN URLs can expire while QQ has priority. Refresh from the
                # same public video's normal page once, then skip only this item.
                refreshed = await self.browser.resolve_media(video, refresh=True)
                if not refreshed:
                    raise
                video["media_url"] = refreshed
                if checkpoint:
                    checkpoint["video"] = video
                    self.store.set("checkpoint", checkpoint)
                data = await self.download(refreshed)
            data, duration = await video_prefix(data, self.assets.cache, self.vision_config)
            video["duration"] = duration
            self.store.set("source_duration:" + account + ":" + video["id"], duration)
            key = self.assets.put(data, owner, "video")
            self.store.save_video(account, video["id"], "pending", media_id=key)
        source_duration = self.store.get("source_duration:" + account + ":" + video["id"], video.get("duration"))
        question = "理解视频内容、情节、表达和视听信息，为兴趣判断与公共知识记忆提供依据。"
        if source_duration and source_duration > MAX_VIDEO_SECONDS:
            question += "素材仅为原视频前180秒，后续未读取，不推断后续内容或结局。"
        analysis = json.loads(row["analysis"]) if row["analysis"] else await self.analyzer.video(
            key, owner, question, check_active=self.checkpoint)
        if source_duration and source_duration > MAX_VIDEO_SECONDS:
            analysis = dict(analysis, source_duration=source_duration, truncated=True,
                            analyzed_range=[0, min(MAX_VIDEO_SECONDS, analysis["duration"])])
            notice = "仅依据视频前180秒，后续内容未读取。"
            if notice not in analysis.get("uncertainty", []):
                analysis["uncertainty"] = [*analysis.get("uncertainty", []), notice]
                analysis["summary"] = "【仅视频前180秒】" + analysis["summary"]
        return analysis

    async def share(self, config, video, analysis, interest):
        if not config.share_enabled:
            self.store.share_decision(config.account.douyin_id, video["id"], "", "sharing_disabled")
            return
        # Candidates encountered during cooldown are discarded, never queued.
        for configured_friend in config.friends:
            self.checkpoint()
            config = self.config.read()
            friend = next((f for f in config.friends if f.douyin_id == configured_friend.douyin_id and f.opted_in), None)
            if not friend or not config.share_enabled:
                continue
            allowed, reason = self.share_eligible(config, friend.douyin_id, video)
            if not allowed:
                self.store.set("last_share_skip", reason)
                self.store.share_decision(config.account.douyin_id, video["id"], friend.douyin_id, reason)
                continue
            key = None
            def before_send():
                nonlocal key
                self.checkpoint()
                current = self.config.read()
                if current.account != config.account or not recommend(analysis, interest):
                    raise Paused("share_policy_changed")
                key, code = self.store.reserve_share(current, friend.douyin_id, video)
                if not key:
                    raise Paused(code)
            try:
                evidence = await self.feed.share(video, friend, self.checkpoint, before_send, config)
            except BaseException as exc:
                if key:
                    self.store.finish_share(key, "uncertain", "interrupted_or_unconfirmed")
                    self.store.set("halt", "share_result_uncertain")
                elif isinstance(exc, BrowserStopped) and str(exc) in {
                        "native_share_entry_unavailable", "ambiguous_share_control", "share_service_unavailable"}:
                    # This item has no usable sharing UI and no send was reserved.
                    # Keep its completed evaluation and continue browsing.
                    self.checkpoint()
                    reason = str(exc)
                    self.store.set("last_share_skip", reason)
                    self.store.share_decision(config.account.douyin_id, video["id"], friend.douyin_id, reason)
                    continue
                raise
            else:
                self.store.finish_share(key, "sent", evidence)
                self.store.set("last_share", {"id": key, "time": self.clock()})
            finally:
                await self.browser.pause()

    def share_eligible(self, config, recipient, video):
        # A read-only preview; reserve_share repeats all checks in one transaction
        # immediately before the irreversible click.
        now = self.clock()
        if config.draft_only:
            self.store.set("draft:" + video["id"], {"video": video["url"], "created": now})
            return False, "draft_only"
        for row in self.store.shares(1000):
            if row["account"] != config.account.douyin_id or row["recipient"] != recipient:
                continue
            if row["state"] in {"sending", "uncertain"}:
                return False, "unresolved_send"
            if row["state"] == "sent" and now - row["sent"] < config.share_cooldown_seconds:
                return False, "cooldown_skipped"
        from datetime import datetime
        from zoneinfo import ZoneInfo
        day = datetime.fromtimestamp(now, ZoneInfo(config.timezone)).date().isoformat()
        sent = [r for r in self.store.shares(1000) if r["account"] == config.account.douyin_id
                and r["recipient"] == recipient and r["state"] == "sent"]
        if sum(r["day"] == day for r in sent) >= config.daily_share_limit:
            return False, "daily_limit"
        if any(r["video"] == video["id"] and now - r["created"] < config.dedup_days * 86400 for r in sent):
            return False, "duplicate"
        return True, "eligible"

    async def advance_checkpoint(self, checkpoint):
        if not checkpoint.get("advanced"):
            await self.browser.pause()
            await self.feed.next()
            await self.browser.pause()
            checkpoint["advanced"] = True
            self.store.set("checkpoint", checkpoint)

    async def process_visible(self, config, video, checkpoint):
        """Advance on the viewing deadline even when background analysis takes longer."""
        async def viewing_deadline():
            await asyncio.sleep(max(0, checkpoint["watch_until"] - self.clock()))
            self.checkpoint()
            await self.advance_checkpoint(checkpoint)
        timer = asyncio.create_task(viewing_deadline()) if not checkpoint.get("advanced") else None
        processing = asyncio.create_task(self.process(config, video))
        try:
            if timer:
                await asyncio.wait({timer, processing}, return_when=asyncio.FIRST_COMPLETED)
                if timer.done():
                    await timer  # Propagate navigation errors; never lose a timer exception.
            await processing
        except (MediaError, httpx.HTTPStatusError, jsonschema.ValidationError, json.JSONDecodeError) as exc:
            self.item_failed(config, video, exc)
        finally:
            for task in (timer, processing):
                if task and not task.done():
                    task.cancel()
            await asyncio.gather(*(t for t in (timer, processing) if t), return_exceptions=True)
        await self.advance_checkpoint(checkpoint)

    def item_failed(self, config, video, exc):
        reason = ("media_http_" + str(exc.response.status_code) if isinstance(exc, httpx.HTTPStatusError) else
            "invalid_douyin_output" if isinstance(exc, (jsonschema.ValidationError, json.JSONDecodeError)) else str(exc))
        self.store.save_video(config.account.douyin_id, video["id"], "unavailable", error=reason)
        self.store.set("last_video_error", {"id": video["id"], "reason": reason, "time": self.clock()})

    async def process_retry(self, config, row):
        video = {"id": row["id"], "url": row["url"], "title": row["title"]}
        try:
            await self.process(config, video)
        except (MediaError, httpx.HTTPStatusError, jsonschema.ValidationError, json.JSONDecodeError) as exc:
            self.item_failed(config, video, exc)
        # Cancellation/QQ preemption leaves the persistent job queued.
        self.store.finish_retry(config.account.douyin_id, row["id"])

    async def step(self):
        self.running_account = None
        if reason := self.reason():
            self.state = reason
            await self.browser.pause()
            return
        config = self.config.read()
        self.running_account = config.account.model_dump()
        async with self.operation_lock:
            await self.controlled(self.browser.open(config))
            if self.browser.verified_account != config.account.model_dump():
                await self.controlled(self.browser.verify_account(config))
            # A new browser session may autoplay a fresh item while an older
            # checkpoint still owns the background analysis slot.
            await self.browser.pause()
            self.state = "browsing"
            checkpoint = self.store.get("checkpoint")
            if checkpoint and checkpoint["account"] == config.account.douyin_id:
                video = checkpoint.get("video")
                row = self.store.video(config.account.douyin_id, video["id"]) if video else None
                if row and row["status"] not in {"done", "filtered", "revoked", "unavailable"}:
                    checkpoint.setdefault("watch_until", self.clock() + MAX_VIDEO_SECONDS)
                    self.state = "analyzing"
                    await self.controlled(self.process_visible(config, video, checkpoint))
                self.checkpoint()
                await self.controlled(self.advance_checkpoint(checkpoint))
                self.store.set("checkpoint", None)
            if retry := self.store.next_retry(config.account.douyin_id):
                self.state = "recovering_failed_item"
                await self.controlled(self.process_retry(config, retry))
                self.errors = 0
                return
            try:
                video = await self.controlled(self.feed.current())
                if self.store.delay(config):
                    self.state = "rate_wait"
                    await self.browser.pause()
                    return
                await self.controlled(self.browser.resume())
                # Playback attaches the native media URL lazily on some feed items.
                video = await self.controlled(self.feed.current())
            except UnsupportedRecommendation as exc:
                self.store.skipped_encounter(config.account.douyin_id)
                self.store.set("last_skip", {"reason": str(exc), "time": self.clock()})
                await self.controlled(self.feed.next())
                await self.browser.pause()
                self.state = "unsupported_item_skipped"
                self.errors = 0
                return
            self.checkpoint()
            fresh = self.store.encounter(config.account.douyin_id, video)
            duration = video.get("duration")
            limit = min(duration, MAX_VIDEO_SECONDS) if duration and math.isfinite(duration) else MAX_VIDEO_SECONDS
            checkpoint = {"account": config.account.douyin_id, "video": video,
                          "watch_until": self.clock() + max(0, limit - video.get("position", 0))}
            self.store.set("checkpoint", checkpoint)
            if fresh:
                self.state = "analyzing"
                await self.controlled(self.process_visible(config, video, checkpoint))
            else:
                await self.controlled(self.advance_checkpoint(checkpoint))
            self.store.set("checkpoint", None)
            self.errors = 0

    async def run(self):
        self.store.recover()
        last_cleanup = 0
        while not self.closed:
            try:
                await self.step()
                if self.clock() - last_cleanup >= 3600:
                    self.assets.cleanup()
                    last_cleanup = self.clock()
            except Paused as exc:
                self.state = str(exc)
                self.running_account = None
                await self.browser.pause()
            except BrowserStopped as exc:
                self.store.set("halt", str(exc))
                self.state = "halted"
                await self.browser.pause()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.errors += 1
                self.store.set("last_error", type(exc).__name__)
                self.state = "retry_wait"
                await self.browser.pause()
                if self.errors >= 3:
                    self.store.set("halt", "repeated_processing_errors")
            await asyncio.sleep(0.25 if not self.errors else min(30, 2 ** self.errors))

    async def start(self):
        self.task = asyncio.create_task(self.run())

    async def close(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.browser.close()
