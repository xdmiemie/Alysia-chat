"""Real model + pinned AstrBot provider result replies, with no QQ transport.

Use envs/astrbot/Scripts/python.exe while the managed local model is ready.
Reads the latest song's stored persona; writes only disposable acceptance state.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "external/AstrBot")]


async def run():
    output = ROOT / "storage/temporary/q4-result-acceptance"
    output.mkdir(parents=True, exist_ok=True)
    os.environ["ASTRBOT_ROOT"] = str(output)
    from astrbot.core.star.context import Context as AstrContext
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
    from integrations.qq_astrbot.plugin.media_router import MediaRouter
    from integrations.qq_astrbot.plugin.media_state import MediaState
    from services.media_api.songs import SongStore

    active = MediaState(ROOT / "storage/qq_companion")
    with active.connection() as db:
        key, raw = db.execute("SELECT id,payload FROM delivery ORDER BY updated DESC LIMIT 1").fetchone()
    payload = json.loads(raw)
    job = SongStore(ROOT / "storage/qq_companion").public(key, payload["scope"])
    config = json.loads((ROOT / "external/AstrBot/data/cmd_config.json").read_text(encoding="utf-8-sig"))
    model = next(p for p in config["provider"] if p["id"] == "astrbot_chat_qwen3")
    source = next((p for p in config.get("provider_sources", [])
                   if p["id"] == model.get("provider_source_id")), {})
    provider_config = {**source, **model}
    if provider_config.get("api_base") != "http://127.0.0.1:8080/v1":
        raise RuntimeError("Acceptance only permits the local managed Qwen provider")
    provider = ProviderOpenAIOfficial(provider_config, config["provider_settings"])

    class Manager:
        async def get_provider_by_id(self, provider_id):
            assert provider_id == "astrbot_chat_qwen3"
            return provider

    class Context:
        provider_manager = Manager()

        async def llm_generate(self, **kwargs):
            return await AstrContext.llm_generate(self, **kwargs)

        async def send_message(self, *args, **kwargs):
            raise AssertionError("Result acceptance must never send QQ messages")

    router = MediaRouter(Context(), output)
    rows = []
    lease = active.enter()
    if not lease:
        await provider.terminate()
        raise RuntimeError("Models are reserved; rerun only when ready")
    try:
        for index, (mode, status, has_audio) in enumerate([
            ("cover", "completed", True), ("cover", "completed", True),
            ("original", "completed", True), ("cover", "completed", False),
            ("cover", "failed", False),
        ]):
            current_payload, current_job = deepcopy(payload), deepcopy(job)
            current_payload["music"]["mode"] = mode
            current_job.update(mode=mode, status=status, has_audio=has_audio,
                               error="test_source_unavailable" if status == "failed" else "")
            probe_key = f"probe-{index}-{os.getpid()}"
            line = await router.result_line(current_payload, current_job, key=probe_key)
            with router.client.state.connection() as db:
                audit = db.execute("SELECT status,raw,reply,error FROM song_reply_attempts WHERE job_id=? ORDER BY id",
                                   (probe_key,)).fetchall()
            assert audit[-1][0] == "generated" and audit[-1][2] == line
            rows.append({"mode": mode, "status": status, "has_audio": has_audio,
                         "reply": line, "attempts": audit})
            print(f"Result reply {index + 1}/5: generated and validated", flush=True)
        (output / "report.json").write_text(json.dumps({"cases": rows, "qq_connections": 0,
            "inference": "AstrBot Context.llm_generate and real local Qwen"}, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Q4_RESULT_PASS: 5 real model result replies; QQ connections: 0.", flush=True)
    finally:
        active.leave(lease)
        await provider.terminate()


if __name__ == "__main__":
    asyncio.run(run())
