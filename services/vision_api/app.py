"""Q7 optional CPU preprocessing/analysis API, not a model server."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hmac
import os
import time

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from integrations.qq_astrbot.plugin.inference_queue import state_directory
from integrations.qq_astrbot.plugin.vision_analysis import VisionAnalyzer
from integrations.qq_astrbot.plugin.vision_client import LocalVisionClient
from integrations.qq_astrbot.plugin.vision_config import VisionGates, load_vision_config
from integrations.qq_astrbot.plugin.vision_media import MediaError, decode_images, video_info
from integrations.qq_astrbot.plugin.vision_store import VisionStore


def authorized(authorization: str = Header(default="")):
    directory = state_directory()
    path = directory / "vision_token"
    token = os.environ.get("QQ_COMPANION_VISION_TOKEN") or (path.read_text(encoding="ascii").strip() if path.is_file() else "")
    if not token or not hmac.compare_digest(authorization, "Bearer " + token):
        raise HTTPException(401, "unauthorized")


@asynccontextmanager
async def lifespan(app):
    config = load_vision_config()
    store = VisionStore(state_directory(), config)
    client = LocalVisionClient(state_directory(), config)
    app.state.store, app.state.config = store, config
    app.state.analyzer = VisionAnalyzer(store, config, client.call, transcribe=client.media.transcribe)
    store.recover()
    async def worker():
        last_cleanup = 0
        while True:
            if time.monotonic() - last_cleanup > 3600:
                store.cleanup()
                last_cleanup = time.monotonic()
            job = store.next_job(api_only=True) if VisionGates(config=config).enabled("video") else None
            if job:
                try:
                    def check_active():
                        if store.job(job["id"], job["owner"])["status"] != "running":
                            raise MediaError("video_job_canceled")
                    result = await asyncio.wait_for(app.state.analyzer.video(job["media_id"], job["owner"], job["question"],
                        deadline=job["deadline"], check_active=check_active), max(1, job["deadline"] - time.time()))
                    store.finish_job(job["id"], result)
                except Exception as exc:
                    store.fail_job(job, type(exc).__name__)
            else:
                await asyncio.sleep(0.5)
    task = asyncio.create_task(worker())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


app = FastAPI(title="Q7 media analysis (shared Qwen3.5)", lifespan=lifespan, dependencies=[Depends(authorized)])


class AnalysisRequest(BaseModel):
    media_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    owner: str = Field(min_length=1, max_length=128)
    question: str = Field(default="", max_length=4000)


@app.get("/health")
def health():
    gates = VisionGates()
    return {"model_owner": False, "backend": "http://127.0.0.1:8080/v1",
            "capabilities": {k: gates.enabled(k) for k in ("image", "sticker", "gif", "video")}}


@app.post("/v1/vision/assets")
async def upload(request: Request, owner: str = Header(), kind: str = Header(default="image")):
    config, store = app.state.config, app.state.store
    if kind not in {"image", "sticker", "gif", "video"}:
        raise HTTPException(400, "unsupported_media_kind")
    limit = config["max_video_bytes"] if kind == "video" else config["max_image_bytes"]
    data = bytearray()
    async for block in request.stream():
        data.extend(block)
        if len(data) > limit:
            raise HTTPException(413, "media_byte_limit")
    try:
        if kind != "video":
            actual, _ = await asyncio.to_thread(decode_images, bytes(data), config)
            kind = "gif" if actual == "gif" else kind
        key = store.put(bytes(data), owner, kind)
        if kind == "video":
            path, _ = store.asset(key, owner)
            await asyncio.to_thread(video_info, path, config)
        return {"media_id": key, "source_type": kind}
    except MediaError as exc:
        raise HTTPException(400, str(exc)) from exc


async def image_request(body, kind):
    if not VisionGates().enabled(kind):
        raise HTTPException(409, "capability_not_verified")
    try:
        _, info = app.state.store.asset(body.media_id, body.owner)
        if info["kind"] != kind:
            raise HTTPException(400, "media_kind_mismatch")
        return await app.state.analyzer.image(body.media_id, body.owner, body.question, kind=kind)
    except MediaError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/v1/vision/analyze_image")
async def analyze_image(body: AnalysisRequest):
    return await image_request(body, "image")


@app.post("/v1/vision/analyze_sticker")
async def analyze_sticker(body: AnalysisRequest):
    return await image_request(body, "sticker")


@app.post("/v1/vision/analyze_gif")
async def analyze_gif(body: AnalysisRequest):
    return await image_request(body, "gif")


@app.post("/v1/vision/analyze_video", status_code=202)
def analyze_video(body: AnalysisRequest):
    if not VisionGates().enabled("video"):
        raise HTTPException(409, "capability_not_verified")
    try:
        _, info = app.state.store.asset(body.media_id, body.owner)
        if info["kind"] != "video":
            raise HTTPException(400, "media_kind_mismatch")
        job = app.state.store.submit(body.media_id, body.owner, body.question)
        return {"job_id": job}
    except MediaError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/v1/vision/jobs/{job_id}")
def job(job_id: str, owner: str = Header()):
    try:
        row = app.state.store.job(job_id, owner)
        return {k: row[k] for k in ("id", "status", "result", "error")}
    except MediaError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.delete("/v1/vision/jobs/{job_id}")
def cancel_job(job_id: str, owner: str = Header()):
    try:
        return {"canceled": app.state.store.cancel(job_id, owner)}
    except MediaError as exc:
        raise HTTPException(404, str(exc)) from exc
