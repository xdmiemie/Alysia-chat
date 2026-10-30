"""Real local Qwen + AstrBot Runner probe; mocked media, zero QQ connections."""
from __future__ import annotations

import asyncio
import argparse
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "external/AstrBot")]
from runtime.qwen3.config import load_qwen3_config
OUTPUT = ROOT / "storage/temporary/native-agent-acceptance"
BASE = "http://127.0.0.1:18080"


async def cases(model_id, *, base=BASE, replay=False, controls_only=False, recent_failures=False, latest_failure=False, recovery_only=False, quick=False, invalid_reply=False):
    from astrbot.core.agent.hooks import BaseAgentRunHooks
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    from astrbot.core.agent.tool import ToolSet
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
    from astrbot.core.provider.entities import LLMResponse, ProviderRequest
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
    from integrations.qq_astrbot.plugin.agent_runtime import NativeAgentLifecycle, parse_final_reply
    from integrations.qq_astrbot.plugin.agent_state import AgentStore
    from integrations.qq_astrbot.plugin.agent_tools import AgentTools
    from integrations.qq_astrbot.plugin.prompt_loader import load_prompts
    from integrations.qq_astrbot.plugin.reply_content import chat_history
    from integrations.qq_astrbot.plugin.request_provider import request_chat
    from scripts.qq_companion.configure_model import configure, PROVIDER_ID

    config = configure(json.loads((ROOT / "external/AstrBot/data/cmd_config.json").read_text(encoding="utf-8-sig")), model_id, "elysia")
    model = next(p for p in config["provider"] if p["id"] == PROVIDER_ID)
    source = next(p for p in config["provider_sources"] if p["id"] == model["provider_source_id"])
    provider = ProviderOpenAIOfficial({**source, **model, "api_base": base + "/v1", "timeout": 90}, {})
    store = AgentStore(OUTPUT / "agent.sqlite3")
    prompts = load_prompts()
    persona = (ROOT / "configs/characters/elysia/system_prompt.txt").read_text(encoding="utf-8-sig")
    rows = []
    class Event:
        def __init__(self): self.extra = {}
        def get_extra(self, name, default=None): return self.extra.get(name, default)
        def set_extra(self, name, value): self.extra[name] = value
        async def send(self, chain): raise AssertionError("QQ sending is forbidden in this probe")
        def clear_result(self): pass

    try:
        specs = [("今天有点累，陪我聊两句吧", "晴天", "周杰伦", [], False, True),
                 ("我想听你唱周杰伦的晴天", "晴天", "周杰伦", [], False, True)]
        if replay:
            path = ROOT / "external/AstrBot/data/data_v4.db"
            with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as db:
                row = db.execute("SELECT content FROM conversations ORDER BY updated_at DESC LIMIT 1").fetchone()
            history = json.loads(row[0]) if row else []
            specs.append(("吃了，今天想听你唱鞠婧祎版本的叹云兮", "叹云兮", "鞠婧祎", chat_history(history)[-16:], True, True))
            specs.append(("想听你唱鞠婧祎版本的叹云兮，请保留这个指定版本", "叹云兮", "鞠婧祎", [], False, False))
            specs.append(("今天想听你唱鞠婧祎版本的叹云兮", "叹云兮", "鞠婧祎", chat_history(history)[-16:], False, True))
        specs += [("先别唱了，取消刚才的翻唱", "叹云兮", "鞠婧祎", [], False, True, 'cancel'),
                  ("刚才那首不要了，改唱周杰伦的晴天", "晴天", "周杰伦", [], False, True, 'replace'),
                  ("我给这大肥鱼登了我的网易VIP账号，现在那些歌他也能唱了", "叹云兮", "鞠婧祎", [], False, True, 'keep')]
        if quick:
            specs = specs[:2]
        if controls_only:
            specs = [spec for spec in specs if len(spec) > 6]
        if recent_failures or latest_failure:
            with sqlite3.connect(f"file:{(ROOT / 'storage/qq_companion/agent.sqlite3').as_posix()}?mode=ro", uri=True) as db:
                failed = db.execute("SELECT a.payload FROM audit a JOIN runs r ON r.id=a.run_id " + (
                    "WHERE a.kind='request' ORDER BY r.started DESC LIMIT 1" if latest_failure else
                    "WHERE a.kind='request' AND r.requests=16 AND r.tool_calls=0 ORDER BY r.started DESC LIMIT 2")).fetchall()
            with sqlite3.connect(f"file:{(ROOT / 'external/AstrBot/data/data_v4.db').as_posix()}?mode=ro", uri=True) as db:
                row = db.execute("SELECT content FROM conversations ORDER BY updated_at DESC LIMIT 1").fetchone()
            history = json.loads(row[0]) if row else []
            specs = []
            for (raw,) in reversed(failed):
                current = json.loads(raw)['current_request']
                cutoff = None
                for pos, message in enumerate(history):
                    if message.get('role') != 'user':
                        continue
                    text = message.get('content', '')
                    parts = [text] if isinstance(text, str) else [p.get('text', '') for p in text if isinstance(p, dict)]
                    for part in parts:
                        try:
                            data = json.loads(part)
                        except (ValueError, TypeError):
                            continue
                        if isinstance(data, dict) and str(data.get('current_message', {}).get('message_id')) == current['message_id']:
                            cutoff = pos
                            break
                    if cutoff is not None:
                        break
                if cutoff is None:
                    raise RuntimeError('Cannot locate the failed request in its real conversation history')
                specs.append((current['text'], '雨爱', '杨丞琳', chat_history(history[:cutoff]), False, True))
            if len(specs) != (1 if latest_failure else 2):
                raise RuntimeError('Expected failed request history was not found')
            if latest_failure:
                # Same real failure once normally and once with the exact
                # out-of-order call injected, to prove recovery after rejection.
                specs.append((*specs[0], None, True))
                if recovery_only:
                    specs = specs[-1:]
        for index, spec in enumerate(specs):
            text, title, artist, history, inject_false_claim, available = spec[:6]
            control = spec[6] if len(spec) > 6 else None
            inject_unregistered = bool(len(spec) > 7 and spec[7])
            injected = False
            async def chat(**kwargs):
                nonlocal injected
                if invalid_reply and index == 1 and not injected and kwargs.get("func_tool"):
                    injected = True
                    return LLMResponse("assistant", completion_text=json.dumps({"name": "weapi",
                        "arguments": {"action": "search", "query": title, "page": 1}}, ensure_ascii=False))
                if inject_unregistered and not injected and kwargs.get("func_tool"):
                    injected = True
                    return LLMResponse("tool", tools_call_name=["musiccover"],
                        tools_call_args=[{"candidate": 1, "action": "start"}], tools_call_ids=["premature"])
                return await request_chat(provider, **kwargs)
            class ProbedProvider:
                def __getattr__(self, name): return getattr(provider, name)
                async def text_chat(self, **kwargs): return await chat(**kwargs)
            case_provider = ProbedProvider()
            started = time.monotonic()
            event = Event()
            event.set_extra("qq_persona_prompt", persona)
            scope = f"qq:999:dm:{101 + index}"
            version = store.observe(scope, f"probe-{os.getpid()}-{index}")
            run = store.begin(scope, version, f"probe-{os.getpid()}-{index}")
            event.set_extra("qq_agent_run", run)
            owner = SimpleNamespace(agent_store=store, _event_active=lambda event: store.current(scope, version))
            tools = AgentTools(owner)
            called = []
            async def dispatch(event, name, args, key):
                called.append({"tool": name, "arguments": args})
                if name == "weapi":
                    if latest_failure:
                        result = json.loads((ROOT / 'storage/temporary/rain-love-token-baseline-20261029.json').read_text(encoding='utf-8'))['source_result']
                        result['songs'] = result['songs'][:10]
                        for song in result['songs']:
                            run.remember_song(song)
                        return result
                    if replay and index == 4:
                        with sqlite3.connect(f"file:{(ROOT / 'storage/qq_companion/agent.sqlite3').as_posix()}?mode=ro", uri=True) as db:
                            results = [json.loads(row[0]) for row in db.execute("SELECT payload FROM audit WHERE kind='tool_result' AND payload LIKE '%\"songs\"%' ORDER BY id DESC LIMIT 30")]
                        result = next(value for value in results if any(title in song.get('title', '') and artist in song.get('artists', '')
                            for song in value.get('songs', [])))
                        result["songs"] = result["songs"][:10]
                        for item in result["songs"]:
                            run.remember_song(item)
                        return result
                    song = {"song_id": 1001, "title": title, "artists": artist, "album": title,
                            "aliases": [], "available": available, "link": "https://music.163.com/song?id=1001"}
                    run.remember_song(song)
                    return {"ok": True, "status": "completed", "songs": [song]}
                if name in {"music", "musiccover"}:
                    if args.get('action') == 'cancel':
                        return ({'ok': True, 'status': 'canceled', 'tasks': [{'job_id': 'old-task', 'status': 'canceled'}]}
                            if name == 'musiccover' else {'ok': False, 'status': 'not_found', 'tasks': []})
                    assert run.song_id(args["candidate"]) in run.candidates
                    if name == "musiccover" and not available:
                        return {"ok": False, "status": "unavailable", "error": "selected_audio_unavailable",
                                "song": run.candidates[run.song_id(args["candidate"])], "can_cover": False, "sent": False}
                    return {"ok": True, "status": "queued", "job_id": "mock-job", "song": run.candidates[run.song_id(args['candidate'])],
                            "mode": "cover" if name == "musiccover" else "original", "completed": False, "sent": False,
                            'replaced': [{'job_id': 'old-task', 'status': 'canceled'}] if args.get('action') == 'replace' else []}
                return {"ok": True, "status": "prepared", "sent": False}
            tools.dispatch = dispatch
            runner = ToolLoopAgentRunner()
            class Hooks(BaseAgentRunHooks):
                async def on_agent_begin(self, context):
                    NativeAgentLifecycle(runner, store, run, event).install()
                async def on_agent_done(self, context, response):
                    candidate = parse_final_reply(response.completion_text or "")
                    if candidate: response.completion_text = candidate["reply"]
            system = "\n".join((prompts["astrbot_safety"], persona, prompts["qq_context"],
                                  prompts["reply_output"], prompts["agent_policy"]))
            await runner.reset(provider=case_provider, request=ProviderRequest(system_prompt=system,
                contexts=history, prompt=json.dumps({"session_id": scope, "current_message": {"sender_uin": "101", "text": text},
                    "input_modality": "text", "recent_messages": [],
                    "active_music_tasks": [{'id': 'old-task', 'mode': 'cover', 'title': '叹云兮', 'state': 'accepted'}] if control else []},
                    ensure_ascii=False), func_tool=ToolSet(tools.registered())),
                run_context=ContextWrapper(context=SimpleNamespace(event=event), tool_call_timeout=60),
                tool_executor=FunctionToolExecutor(), agent_hooks=Hooks(), streaming=False)
            visible = []
            async for response in runner.step_until_done(32):
                if response.type == "llm_result":
                    value = response.data["chain"].get_plain_text()
                    if value: visible.append(value)
            main_requests = run.requests
            if invalid_reply and index == 1:
                assert run.reply_retries == 1
            additions = []
            if quick and index == 0:
                # Exercise the actual post-Agent path with real inference and
                # a local fake sender. No OneBot client is constructed.
                from integrations.qq_astrbot.plugin.main import Q1Guard
                from integrations.qq_astrbot.plugin.message_router import Incoming
                followup_owner = object.__new__(Q1Guard)
                followup_owner.agent_store = store
                event.get_group_id = lambda: ""
                event.is_stopped = lambda: False
                event.set_extra("qq_scope", scope)
                event.set_extra("qq_inbound_sequence", version)
                event.set_extra("qq_incoming", Incoming("999", str(101 + index), "probe", text, ""))
                event.set_extra("qq_primary_reply", visible[0])
                event.get_extra("qq_agent_lifecycle").sent(visible[0])
                async def local_send(chain):
                    additions.append(chain.get_plain_text())
                event.send = local_send
                await followup_owner._maybe_followup(event)
                assert 1 <= run.followups <= 3 and not called
                with store.connection() as db:
                    assert not db.execute("SELECT 1 FROM audit WHERE run_id=? AND kind IN "
                        "('followup_failed','followup_rejected')", (run.run_id,)).fetchone()
            passed = bool(len(visible) == 1 and event.get_extra("qq_agent_completed"))
            if control == 'keep':
                passed = passed and not any(call['tool'] in {'music', 'musiccover'} for call in called)
            elif control:
                passed = passed and any(call['tool'] == 'musiccover' and call['arguments'].get('action') == control for call in called)
            elif index >= 1 or recent_failures or latest_failure:
                passed = passed and any(call["tool"] == "weapi" for call in called)
                passed = passed and (not available or any(call["tool"] == "musiccover" for call in called))
            passed = passed and prompts['agent_budget_reply'] not in visible
            if recent_failures or latest_failure:
                music_calls = [call for call in called if call['tool'] in {'music', 'musiccover'}]
                passed = passed and len(music_calls) == 1 and music_calls[0]['tool'] == 'musiccover'
                passed = passed and music_calls[0]['arguments'].get('action', 'start') == 'start'
                # Tool rounds and a direct final response share the request budget.
                passed = passed and run.requests <= (12 if inject_unregistered else 10)
                passed = passed and not any(claim in text for text in visible for claim in (
                    '链接也发给你', '已经发给', '已发送', '唱好了', '唱好啦', '已经唱好', '点开链接听'))
            if latest_failure:
                selected = run.candidates[run.song_id(music_calls[0]['arguments']['candidate'])] if music_calls else {}
                # Preserve contextual version choice; never hard-code rank 1.
                passed = passed and title in selected.get('title', '') and selected.get('available') is True
            rows.append({"input": text, "tools": called, "main_requests": main_requests,
                         "reply_retries": run.reply_retries, "followups": run.followups,
                         "followup_messages": additions, "requests": run.requests,
                         "visible": visible, "seconds": round(time.monotonic() - started, 3), "passed": passed})
            (OUTPUT / "report.json").write_text(json.dumps({"cases": rows, "qq_connections": 0,
                "media": "mocked", "inference": "real Qwen and native AstrBot Runner"}, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"NATIVE_AGENT case {index + 1}: {'PASS' if passed else 'FAIL'}, requests={run.requests}, tools={[c['tool'] for c in called]}", flush=True)
        if not all(row["passed"] for row in rows):
            raise RuntimeError("Native model acceptance failed; inspect the report and audit database")
    finally:
        await provider.terminate()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--existing", action="store_true", help="Use the managed model on 8080 under an admission lease")
    parser.add_argument("--replay", action="store_true", help="Include current conversation and available/unavailable song fixtures")
    parser.add_argument("--controls-only", action="store_true", help="Only test cancel, replace and unrelated chatter")
    parser.add_argument("--recent-failures", action="store_true", help="Replay the two 16-request failures with their real preceding history")
    parser.add_argument("--latest-failure", action="store_true", help="Replay latest real request and force an unregistered-candidate recovery; frozen real ten-song results")
    parser.add_argument("--recovery-only", action="store_true", help="Only force the preflight-recovery variant of --latest-failure")
    parser.add_argument("--quick", action="store_true", help="Only check direct chat and the native song tool roundtrip")
    parser.add_argument("--invalid-reply", action="store_true", help="Inject one plain-body tool JSON and verify the single regeneration")
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    os.environ["ASTRBOT_ROOT"] = str(OUTPUT)
    if args.existing:
        from integrations.qq_astrbot.plugin.media_state import MediaState
        state = MediaState(ROOT / "storage/qq_companion")
        lease = state.enter()
        if lease is None:
            raise RuntimeError("Managed model admission is closed; refusing to interfere with GPU switching")
        try:
            opener = build_opener(ProxyHandler({}))
            with opener.open("http://127.0.0.1:8080/v1/models", timeout=3) as response:
                model_id = json.load(response)["data"][0]["id"]
            asyncio.run(cases(model_id, base="http://127.0.0.1:8080", replay=args.replay,
                controls_only=args.controls_only, recent_failures=args.recent_failures, latest_failure=args.latest_failure,
                recovery_only=args.recovery_only, quick=args.quick, invalid_reply=args.invalid_reply))
            print("NATIVE_AGENT_MODEL_PASS; existing model preserved; QQ connections: 0", flush=True)
        finally:
            state.leave(lease)
        return
    from scripts.qq_companion.gpu_broker import GPUResourceBroker
    broker = GPUResourceBroker()
    broker.acquire("native_agent_acceptance")
    process = None
    OUTPUT.mkdir(parents=True, exist_ok=True)
    os.environ["ASTRBOT_ROOT"] = str(OUTPUT)
    active = json.loads((ROOT / "model_store/active/qwen3.json").read_text(encoding="utf-8-sig"))
    model = ROOT / active["runtime"]["model_file"]
    opener = build_opener(ProxyHandler({}))
    try:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", 18080)) == 0:
                raise RuntimeError("Acceptance port is occupied; refusing to attach or stop another service")
        with (OUTPUT / "llama.log").open("wb") as log:
            process = subprocess.Popen([str(ROOT / "tools/llama.cpp/llama-server.exe"), "--model", str(model),
                "--jinja"] + load_qwen3_config().server_args(port=18080),
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW)
            deadline = time.monotonic() + 120
            while True:
                try:
                    with opener.open(BASE + "/health", timeout=3): break
                except Exception:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Isolated local model did not become ready")
                    time.sleep(0.5)
            with opener.open(BASE + "/v1/models", timeout=3) as response:
                model_id = json.load(response)["data"][0]["id"]
            asyncio.run(cases(model_id, replay=args.replay, controls_only=args.controls_only,
                recent_failures=args.recent_failures, latest_failure=args.latest_failure, recovery_only=args.recovery_only,
                quick=args.quick, invalid_reply=args.invalid_reply))
            print("NATIVE_AGENT_MODEL_PASS; QQ connections: 0", flush=True)
    finally:
        if process:
            process.terminate()
            try: process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        broker.release()


if __name__ == "__main__":
    main()
