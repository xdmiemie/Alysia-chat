"""Real Qwen Q5 acceptance with synthetic facts and zero QQ connections.

Without --existing this owns one temporary model under the GPU broker and stops
only that model. The production plugin/config and memory database are untouched.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "external/AstrBot")]
OUTPUT = ROOT / "storage/temporary/q5-acceptance"


async def cases(base, model_id):
    from astrbot.core.agent.message import Message
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
    from integrations.qq_astrbot.plugin.agent_runtime import parse_final_reply
    from integrations.qq_astrbot.plugin.persistent_memory import PersistentMemory, text_payload
    from integrations.qq_astrbot.plugin.prompt_loader import load_prompts
    from integrations.qq_astrbot.plugin.request_provider import request_chat
    from integrations.qq_astrbot.plugin.context_budget import RequestBudget
    from scripts.qq_companion.configure_model import configure, PROVIDER_ID

    config = configure(json.loads((ROOT / "external/AstrBot/data/cmd_config.json").read_text(encoding="utf-8-sig")), model_id, "elysia")
    model = next(p for p in config["provider"] if p["id"] == PROVIDER_ID)
    source = next(p for p in config["provider_sources"] if p["id"] == model["provider_source_id"])
    provider = ProviderOpenAIOfficial({**source, **model, "api_base": base + "/v1", "timeout": 90}, {})
    directory = OUTPUT / ("run-" + str(time.time_ns()))
    service = PersistentMemory(directory, timezone_name="Asia/Hong_Kong")
    prompts = load_prompts()
    persona = (ROOT / "configs/characters/elysia/system_prompt.txt").read_text(encoding="utf-8-sig")
    async def persona_factory(): return persona
    service.persona_factory = persona_factory
    now = datetime.now(service.zone).replace(hour=12, minute=0, second=0, microsecond=0)
    report = {"qq_connections": 0, "qq_sends": 0, "inference": "real local Qwen", "cases": {}}

    def note(name, passed, **details):
        report["cases"][name] = {"passed": bool(passed), **details}
        (OUTPUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Q5 {name}: {'PASS' if passed else 'FAIL'}", flush=True)
        if not passed:
            raise RuntimeError("Q5 model acceptance failed; inspect storage/temporary/q5-acceptance/report.json")

    def request(scope, text):
        return [Message(role="system", content=persona + "\n" + prompts["memory_use"] + "\n" + prompts["reply_output"]),
                Message(role="user", content=json.dumps({"session_id": scope, "current_message": {"text": text}}, ensure_ascii=False))]

    try:
        for i, (sender, text) in enumerate([
            ("101", "我最喜欢的歌手是周杰伦，我尤其喜欢他的晴天。"),
            ("102", "我最喜欢的歌手是孙燕姿。"),
            ("101", "我有偏头痛，这属于我的健康隐私。"),
            ("101", "我现在最喜欢的歌手改成林俊杰了，之前说周杰伦是过去的偏好。"),
        ]):
            service.repository.enqueue(f"qq:999:dm:{sender}", sender, f"probe-{i}", text,
                now=now.timestamp() + i, character="elysia")
            assert await service.extract_one(provider)
        own = service.repository.retrieve("qq:999:dm:101", "歌手 偏头痛")
        foreign = service.repository.retrieve("qq:999:dm:102", "偏头痛")
        note("automatic_extraction_and_privacy", any("偏头痛" in r["summary"] and r["sensitive"] for r in own)
             and not any("偏头痛" in r["summary"] for r in foreign), private_fact_count=len(own))
        with service.repository.connection() as db:
            revisions = db.execute("SELECT count(*) FROM memory_revisions").fetchone()[0]
        note("correction", revisions >= 1, revisions=revisions)

        messages = await service.prepare(request("qq:999:dm:101", "你还记得我现在最喜欢哪位歌手吗？"), provider, "qq:999:dm:101")
        response = await request_chat(provider, phase="memory", contexts=messages, func_tool=None,
            max_tokens=512, request_max_retries=1)
        reply = parse_final_reply(response.completion_text or "")
        note("prompt_recall", bool(reply and "林俊杰" in reply["reply"]) and not response.tools_call_name,
             reply=reply["reply"] if reply else response.completion_text)

        # Lots of durable synthetic facts exercise the real tokenizer and model
        # compactor. They never overwrite the four extraction fixtures above.
        for i in range(64):
            text = f"101对音乐话题的长期偏好记录{i}：" + "喜欢旋律清晰而且节奏自然的歌曲，听歌时会注意歌词表达，也愿意讨论编曲带来的情绪感受。" * 7
            service.repository.enqueue("qq:999:dm:101", "101", f"bulk-{i}", text, now=now.timestamp() + 10 + i)
            pending = service.repository.claim_source(character="elysia")
            service.repository.save_extraction(pending, [{"key": f"test_music_{i}", "kind": "fact", "summary": text,
                "tags": ["音乐"], "salience": 0.8, "confidence": 0.9, "sensitive": True, "shared_topic": None}], {})
        audit = []
        messages = await service.prepare(request("qq:999:dm:101", "我以前聊过什么音乐偏好？"), provider,
            "qq:999:dm:101", record=audit.append)
        injected = json.loads(text_payload(messages[-1])[0])["relevant_memories"]
        measured = await RequestBudget(provider, "agent").count(messages)
        note("real_token_budget_and_compaction", audit and audit[-1]["compressed"]
             and measured < model["max_context_tokens"] - 1536 - 256,
             audit=audit, total_input_tokens=measured, injected_records=len(injected))

        day = now.date()
        note("private_and_public_diary", await service.generate_diary(provider, "999", day))
        diary = service.repository.diary("999", "elysia", day.isoformat())
        note("diary_idempotency", not await service.generate_diary(provider, "999", day),
             private_chars=len(diary["draft"].removeprefix("[私人]\n")),
             public_chars=len(diary["public_candidate"] or ""))
        public = service.repository.retrieve("qq:999:group:201", "日记 音乐")
        note("diary_sharing_boundary", not any("偏头痛" in r["summary"] or "林俊杰" in r["summary"] for r in public))
    finally:
        await service.close()
        await provider.terminate()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--existing", action="store_true")
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    os.environ["ASTRBOT_ROOT"] = str(OUTPUT)
    os.environ["ASTRBOT_CHAT_MODEL_DEBUG_DIR"] = str(OUTPUT / "model_debug")
    opener = build_opener(ProxyHandler({}))
    if args.existing:
        from integrations.qq_astrbot.plugin.media_state import MediaState
        state = MediaState(ROOT / "storage/qq_companion")
        lease = state.enter()
        if not lease:
            raise RuntimeError("Managed GPU admission is closed")
        try:
            with opener.open("http://127.0.0.1:8080/v1/models", timeout=3) as response:
                model_id = json.load(response)["data"][0]["id"]
            asyncio.run(cases("http://127.0.0.1:8080", model_id))
        finally:
            state.leave(lease)
    else:
        from runtime.qwen3.config import load_qwen3_config
        from scripts.qq_companion.gpu_broker import GPUResourceBroker
        broker, process = GPUResourceBroker(), None
        broker.acquire("q5_acceptance")
        try:
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", 18080)) == 0:
                    raise RuntimeError("Acceptance port 18080 is occupied")
            active = json.loads((ROOT / "model_store/active/qwen3.json").read_text(encoding="utf-8-sig"))
            with (OUTPUT / "llama.log").open("wb") as log:
                process = subprocess.Popen([str(ROOT / "tools/llama.cpp/llama-server.exe"), "--model",
                    str(ROOT / active["runtime"]["model_file"]), "--jinja"] + load_qwen3_config().server_args(port=18080),
                    cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                deadline = time.monotonic() + 120
                while True:
                    try:
                        with opener.open("http://127.0.0.1:18080/health", timeout=3):
                            break
                    except Exception:
                        if process.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("Isolated Q5 model did not become ready")
                        time.sleep(0.5)
                with opener.open("http://127.0.0.1:18080/v1/models", timeout=3) as response:
                    model_id = json.load(response)["data"][0]["id"]
                asyncio.run(cases("http://127.0.0.1:18080", model_id))
        finally:
            if process:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            broker.release()
    print("Q5_MODEL_PASS; QQ connections: 0; QQ sends: 0", flush=True)


if __name__ == "__main__":
    main()
