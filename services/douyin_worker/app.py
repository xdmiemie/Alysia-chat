"""Loopback-only operator API. No Agent can change policy or recipients."""
from __future__ import annotations

from contextlib import asynccontextmanager
import hmac
import os
import time

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
import psutil

from integrations.qq_astrbot.plugin.inference_queue import state_directory

from .config import ConfigStore
from .feed_driver import VERSION
from .media import MAX_VIDEO_SECONDS, SHORT_VIDEO_SECONDS
from .store import Store
from .worker import Worker


def create_app(directory=None, config=None, worker_factory=Worker):
    @asynccontextmanager
    async def lifespan(app):
        path = directory or state_directory()
        store = Store(path)
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            import json
            row = db.execute("SELECT value FROM state WHERE key='worker_owner'").fetchone()
            if row:
                previous = json.loads(row[0])
                try:
                    if abs(psutil.Process(previous["pid"]).create_time() - previous["created"]) < 1:
                        raise RuntimeError("douyin_worker_already_running")
                except psutil.NoSuchProcess:
                    pass
            db.execute("INSERT OR REPLACE INTO state VALUES('worker_owner',?)",
                       (json.dumps({"pid": os.getpid(), "created": psutil.Process().create_time()}),))
        worker = worker_factory(path, config=config or ConfigStore())
        app.state.worker = worker
        await worker.start()
        try:
            yield
        finally:
            await worker.close()
            with store.connection() as db:
                db.execute("DELETE FROM state WHERE key='worker_owner'")

    def authorized(request: Request, authorization: str = Header(default="")):
        if request.client and request.client.host not in {"127.0.0.1", "::1", "testclient"}:
            raise HTTPException(403, "loopback_required")
        path = (directory or state_directory()) / "douyin_token"
        token = os.environ.get("QQ_COMPANION_DOUYIN_TOKEN") or (path.read_text(encoding="ascii").strip() if path.exists() else "")
        if not token or not hmac.compare_digest(authorization, "Bearer " + token):
            raise HTTPException(401, "unauthorized")

    api = FastAPI(title="Q8/Q9 Douyin operator API", lifespan=lifespan, dependencies=[Depends(authorized)])

    def worker():
        return api.state.worker

    @api.get("/health")
    def health():
        from integrations.qq_astrbot.plugin.vision_config import VisionGates
        return {"state": worker().state, "halt": worker().store.get("halt"), "model_owner": False,
                "adapter": VERSION, "account_verified": bool(worker().browser.verified_account),
                "max_video_seconds": MAX_VIDEO_SECONDS, "last_skip": worker().store.get("last_skip"),
                "short_video_seconds": SHORT_VIDEO_SECONDS,
                "long_video_quota": worker().store.long_quota(worker().config.read()),
                "video_capability": VisionGates(worker().directory).status("video"),
                "last_error": worker().store.get("last_error"),
                "recovery": worker().store.retries(worker().config.read().account.douyin_id)
                    if worker().config.read().account else []}

    @api.post("/v1/douyin/retry-failed")
    def retry_failed():
        config = worker().config.read()
        if not config.account:
            raise HTTPException(409, "account_not_configured")
        queued = worker().store.queue_recoverable(config.account.douyin_id)
        return {"queued": queued, "jobs": worker().store.retries(config.account.douyin_id)}

    @api.get("/v1/douyin/config")
    def read_config():
        return worker().config.read().model_dump()

    @api.patch("/v1/douyin/config")
    def update_config(patch: dict = Body()):
        try:
            return worker().config.update(patch).model_dump()
        except (ValueError, KeyError) as exc:
            raise HTTPException(422, "invalid_douyin_config") from exc

    @api.get("/v1/douyin/frequency")
    def frequency():
        return {"videos_per_hour": worker().config.read().hourly_videos}

    @api.put("/v1/douyin/frequency")
    def set_frequency(body: Frequency):
        return {"videos_per_hour": worker().config.update({"hourly_videos": body.videos_per_hour}).hourly_videos}

    @api.post("/v1/douyin/pause")
    async def pause():
        worker().config.update({"enabled": False})
        await worker().browser.pause()
        return {"paused": True}

    @api.post("/v1/douyin/resume")
    def resume():
        config = worker().config.read()
        if not config.account or worker().browser.verified_account != config.account.model_dump():
            raise HTTPException(409, "verify_logged_in_account_first")
        if any(r["state"] in {"sending", "uncertain"} for r in worker().store.shares(1000)):
            raise HTTPException(409, "reconcile_uncertain_shares_first")
        worker().store.set("halt", None)
        worker().store.set("last_error", None)
        worker().errors = 0
        worker().config.update({"enabled": True})
        return {"resumed": True}

    @api.post("/v1/douyin/browser/open")
    async def open_browser():
        async with worker().operation_lock:
            if worker().foreground.blocked():
                raise HTTPException(409, "qq_chat_active")
            await worker().browser.open(worker().config.read())
            return {"opened": True, "login": "manual"}

    @api.post("/v1/douyin/browser/verify")
    async def verify_browser():
        async with worker().operation_lock:
            if worker().foreground.blocked():
                raise HTTPException(409, "qq_chat_active")
            config = worker().config.read()
            try:
                await worker().browser.open(config)
                result = await worker().browser.verify_account(config)
                for friend in config.friends:
                    if friend.opted_in:
                        await worker().browser.verify_friend(friend)
                worker().store.set("browser_verification", {"adapter": VERSION, "time": time.time()})
                return {**result, "friends_verified": len(worker().browser.friend_profiles)}
            except Exception as exc:
                from .browser_session import BrowserStopped
                raise HTTPException(409, str(exc) if isinstance(exc, BrowserStopped) else "browser_identity_verification_failed") from exc

    @api.get("/v1/douyin/browser/diagnostics")
    async def browser_diagnostics():
        async with worker().operation_lock:
            return await worker().browser.diagnostics()

    @api.get("/v1/douyin/shares")
    def shares():
        return {"shares": worker().store.shares()}

    @api.post("/v1/douyin/shares/{key}/reconcile")
    def reconcile(key: str, body: Reconcile):
        row = next((r for r in worker().store.shares(1000) if r["id"] == key), None)
        if not row or row["state"] != "uncertain":
            raise HTTPException(409, "share_not_uncertain")
        worker().store.finish_share(key, body.state, body.evidence)
        return {"state": body.state}

    @api.get("/v1/douyin/videos/{video_id}")
    def video(video_id: str):
        config = worker().config.read()
        row = worker().store.video(config.account.douyin_id, video_id) if config.account else None
        if not row:
            raise HTTPException(404, "unknown_video")
        return row

    @api.delete("/v1/douyin/videos/{video_id}/memory")
    def revoke_memory(video_id: str):
        config = worker().config.read()
        try:
            worker().store.revoke_memory(config.account.douyin_id, video_id)
        except (ValueError, AttributeError) as exc:
            raise HTTPException(404, "unknown_video") from exc
        return {"revoked": True}

    return api


class Frequency(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    videos_per_hour: int = Field(ge=1, le=120)


class Reconcile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: str = Field(pattern="^(sent|failed)$")
    evidence: str = Field(min_length=1, max_length=200)


app = create_app()
