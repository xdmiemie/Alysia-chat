"""Real thinking/Agent selection against saved weapi results; never sends QQ."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "external/AstrBot")]


async def run():
    output = ROOT / "storage/temporary/q4-selection-acceptance"
    output.mkdir(parents=True, exist_ok=True)
    os.environ["ASTRBOT_ROOT"] = str(output)
    from astrbot.core.star.context import Context as AstrContext
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
    from integrations.qq_astrbot.plugin.api_client import MediaClient
    from integrations.qq_astrbot.plugin.media_router import MediaRouter
    from integrations.qq_astrbot.plugin.media_decision import parse_media
    from integrations.qq_astrbot.plugin.prompt_loader import load_prompts
    from integrations.qq_astrbot.plugin.reply_pacing import parse_reply
    from scripts.qq_companion.configure_model import configure

    config = json.loads((ROOT / "external/AstrBot/data/cmd_config.json").read_text(encoding="utf-8-sig"))
    model = next(p for p in config["provider"] if p["id"] == "astrbot_chat_qwen3")
    provider_config = configure({}, model["model"], "elysia")["provider"][0]
    provider = ProviderOpenAIOfficial(provider_config, config["provider_settings"])
    thinking_sizes = []

    class Manager:
        async def get_provider_by_id(self, provider_id):
            assert provider_id == "astrbot_chat_qwen3"
            return provider

    class Context:
        provider_manager = Manager()

        async def llm_generate(self, **kwargs):
            response = await AstrContext.llm_generate(self, **kwargs)
            thinking_sizes.append(len(response.reasoning_content or ""))
            return response

        async def send_message(self, *args, **kwargs):
            raise AssertionError("Acceptance must not send QQ messages")

    context = Context()
    router, active = MediaRouter(context, output), MediaClient(ROOT / "storage/qq_companion")
    persona = (ROOT / "configs/characters/elysia/system_prompt.txt").read_text(encoding="utf-8-sig")
    prompts = load_prompts()
    saved = json.loads((ROOT / "storage/temporary/song-search-results/results.json").read_text(encoding="utf-8"))
    results = {row["query"]: row["results"] for row in saved["searches"]}
    fixtures = [{"song_id": 1, "title": "雨爱", "artists": "杨丞琳", "available": True},
                {"song_id": 2, "title": "雨爱DJ版", "artists": "DJ歌手", "available": True},
                {"song_id": 3, "title": "雨爱司凤版", "artists": "司凤", "available": True}]
    cases = [("翻唱雨爱的司凤版", "雨爱", fixtures, 3),
             ("翻唱雨爱的DJ版", "雨爱", fixtures, 2),
             ("翻唱雨爱的周星星版", "雨爱", results["雨爱"], 1445761206),
             ("翻唱雨爱的秦始皇DJ版", "雨爱", results["雨爱"], 3343290282),
             ("翻唱雨爱的司凤版", "雨爱", results["雨爱"], [3442281398, 3444876644, 3445953575]),
             ("翻唱雨爱的火星风铃版", "雨爱", results["雨爱"], None),
             ("听鞠婧祎的叹云兮原唱", "叹云兮", results["叹云兮"], 863481092)]
    rows = []
    try:
        for index, (request, query, candidates, expected) in enumerate(cases):
            payload = {"persona": persona, "user_request": request, "request_context": [],
                       "music": {"mode": "cover", "query": query, "artist": ""}}
            async with active.admission(timeout=180):
                chosen = await router.select_song(f"selection-{os.getpid()}-{index}", payload, candidates)
            matched = chosen in expected if isinstance(expected, list) else chosen == expected
            rows.append({"request": request, "selected": chosen, "expected": expected,
                         "thinking_chars": thinking_sizes[-1], "passed": matched and thinking_sizes[-1] > 0})
            print(f"Thinking selection {index + 1}/{len(cases)}: {'PASS' if rows[-1]['passed'] else 'FAIL'}", flush=True)

        for request, query in [("翻唱雨爱的司凤版", "雨爱"), ("唱叹云兮，不要DJ版", "叹云兮")]:
            async with active.admission(timeout=180):
                result = await context.llm_generate(chat_provider_id="astrbot_chat_qwen3",
                    system_prompt="\n".join((persona, prompts["qq_context"], prompts["reply_output"], prompts["media_decisions"])),
                    prompt=json.dumps({"session_id": "qq:999:dm:101", "current_message": {"text": request},
                        "input_modality": "text", "image_understanding_available": False, "recent_messages": []}, ensure_ascii=False),
                    request_max_retries=1)
            _, _, valid = parse_reply(result.completion_text)
            music = parse_media(result.completion_text).music or {}
            passed = valid and music.get("mode") == "cover" and music.get("query") == query and thinking_sizes[-1] > 0
            rows.append({"request": request, "parsed_music": music, "thinking_chars": thinking_sizes[-1], "passed": passed})
            print(f"Thinking intent extraction: {'PASS' if passed else 'FAIL'}", flush=True)

        payload = {"persona": persona, "user_request": "翻唱雨爱的火星风铃版", "music": {"mode": "cover", "query": "雨爱"}}
        async with active.admission(timeout=180):
            line = await router.result_line(payload, {"status": "needs_clarification", "has_audio": False,
                "error": "no_matching_candidate", "song": {}}, key=f"clarification-{os.getpid()}")
        rows.append({"clarification_reply": line, "thinking_chars": thinking_sizes[-1], "passed": bool(line) and thinking_sizes[-1] > 0})
        (output / "report.json").write_text(json.dumps({"cases": rows, "qq_connections": 0,
            "thinking_enabled": True, "reasoning_budget_tokens": 1024}, ensure_ascii=False, indent=2), encoding="utf-8")
        passed = all(row["passed"] for row in rows)
        print("Q4_SELECTION_" + ("PASS" if passed else "FAIL") + ": real local thinking; QQ connections: 0.", flush=True)
        return 0 if passed else 1
    finally:
        await provider.terminate()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
