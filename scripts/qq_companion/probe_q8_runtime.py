"""Isolated real-model Q8/Q9 probe; never writes production memories or sends."""
from __future__ import annotations
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


async def main():
    import httpx
    import psutil
    from services.douyin_worker.config import ConfigStore
    from services.douyin_worker.worker import Worker, Paused
    from services.douyin_worker.interest_scorer import score, recommend
    directory = ROOT / "storage/temporary/q8-runtime-probe"
    directory.mkdir(parents=True, exist_ok=True)
    config = ConfigStore(directory / "config.json")
    production = ConfigStore().read()
    config.update({**production.model_dump(), "enabled": True, "share_enabled": False,
                   "friends": [], "idle_resume_seconds": 0})
    awake = datetime(2026, 11, 2, 12, tzinfo=ZoneInfo(production.timezone)).timestamp()
    worker = Worker(directory, config=config, clock=lambda: awake)
    report = {"production_memory_writes": 0, "external_sends": 0}
    pid = int((ROOT / "storage/temporary/qwen3-llama-server.pid").read_text().strip())
    created = psutil.Process(pid).create_time()
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8080", trust_env=False, timeout=10) as client:
        # Observe actual backend work, then interrupt through the same foreground lease.
        task = asyncio.create_task(worker.controlled(worker.model.complete([
            {"role": "user", "content": "请写一篇完整的叙事短文，至少八百字。"}],
            {"type": "json_schema", "json_schema": {"name": "cancel_probe", "strict": True,
                "schema": {"type": "object", "properties": {"text": {"type": "string"}},
                           "required": ["text"], "additionalProperties": False}}})))
        observed = False
        for _ in range(200):
            if task.done():
                break
            slots = (await client.get("/slots")).json()
            if any(s.get("is_processing") for s in slots):
                observed = True
                break
            await asyncio.sleep(.1)
        started = time.monotonic()
        lease = worker.foreground.begin()
        try:
            await task
            report["cancel_result"] = "completed_before_interruption"
        except Paused:
            report["cancel_result"] = "foreground_interrupted"
        finally:
            worker.foreground.end(lease)
        report["backend_work_observed"] = observed
        report["cancel_seconds"] = round(time.monotonic() - started, 3)
        for _ in range(50):
            if not any(s.get("is_processing") for s in (await client.get("/slots")).json()):
                break
            await asyncio.sleep(.1)
        else:
            raise RuntimeError("backend_slot_not_released")
        with worker.model.media.state.connection() as db:
            report["gpu_leases_after_cancel"] = db.execute("SELECT count(*) FROM activity").fetchone()[0]
        with worker.store.connection() as db:
            report["shares"] = db.execute("SELECT count(*) FROM share_ledger").fetchone()[0]
    video = json.loads((ROOT / "storage/temporary/q8-browser-probe/report.json").read_text(encoding="utf-8"))["current"]
    if not video.get("duration") or video["duration"] > 30:
        raise RuntimeError("probe_requires_visible_video_up_to_30_seconds")
    media = await worker.download(video["media_url"])
    key = worker.assets.put(media, "q8-isolated-probe", "video")
    analysis = await worker.analyzer.video(key, "q8-isolated-probe", "理解视频内容、动作和表达。", check_active=worker.checkpoint)
    persona = (ROOT / "configs/characters/elysia/system_prompt.txt").read_text(encoding="utf-8-sig")
    interest = await score(worker.model, analysis, video["title"], persona)
    report.update(analysis=analysis, interest=interest, recommend=recommend(analysis, interest),
        same_model_process=psutil.Process(pid).create_time() == created,
        video_id=video["id"], media_bytes=len(media))
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"success": True, "same_model_process": report["same_model_process"],
        "cancel_result": report["cancel_result"], "cancel_seconds": report["cancel_seconds"],
        "report": str(directory / "report.json")}, ensure_ascii=False))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
