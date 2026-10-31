"""Real Q6 model formatting probe; no QQ transport, history or memory inserts."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "external/AstrBot")]
OUTPUT = ROOT / "storage/temporary/q6/model"


async def main():
    import httpx
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
    from integrations.qq_astrbot.plugin.api_client import MediaClient
    from integrations.qq_astrbot.plugin.persistent_memory import PersistentMemory
    from integrations.qq_astrbot.plugin.qzone_config import DEFAULTS
    from integrations.qq_astrbot.plugin.qzone_runtime import DiaryPublisher
    from scripts.qq_companion.configure_model import configure, PROVIDER_ID

    async with httpx.AsyncClient(trust_env=False, timeout=5) as http:
        model_id = (await http.get("http://127.0.0.1:8080/v1/models")).json()["data"][0]["id"]
    config = configure(json.loads((ROOT / "external/AstrBot/data/cmd_config.json").read_text(encoding="utf-8-sig")), model_id, "elysia")
    model = next(p for p in config["provider"] if p["id"] == PROVIDER_ID)
    source = next(p for p in config["provider_sources"] if p["id"] == model["provider_source_id"])
    provider = ProviderOpenAIOfficial({**source, **model, "timeout": 90}, {})
    async def provider_factory(): return provider
    async def persona():
        return (ROOT / "configs/characters/elysia/system_prompt.txt").read_text(encoding="utf-8-sig")
    # Reuse only the stateless utility request method, not the Q5 repository.
    utility = PersistentMemory.__new__(PersistentMemory)
    media = MediaClient(ROOT / "storage/qq_companion")
    memory = SimpleNamespace(character="elysia", admission=media.admission,
        provider_factory=provider_factory, persona_factory=persona, _utility=utility._utility)
    publisher = DiaryPublisher(OUTPUT, memory, None)
    report = {"qq_sends": 0, "memory_inserts": 0, "chat_history_writes": 0, "cases": []}
    for key, materials in [("qzone_daily", ["在线交流中聊过音乐，留下了一点关于旋律和表达的感受。"]),
                           ("qzone_daily", []), ("qzone_event", ["今天是元旦，记录对新一年的期待。"])]:
        text = await publisher.generate(key, materials, DEFAULTS)
        assert text.count("。") == 2 and len(text) <= DEFAULTS["daily_max_chars"]
        report["cases"].append({"prompt": key, "text": text, "characters": len(text)})
    (OUTPUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    assert not (OUTPUT / "memories.sqlite3").exists()
    print("Q6_MODEL_PASS: daily summary, quiet day and important event; no QQ/history/memory writes.")


if __name__ == "__main__":
    OUTPUT.mkdir(parents=True, exist_ok=True)
    os.environ["ASTRBOT_ROOT"] = str(OUTPUT)
    os.environ["ASTRBOT_CHAT_MODEL_DEBUG_DIR"] = str(OUTPUT / "model_debug")
    asyncio.run(main())
