"""Real local Qwen3 media-intent regression; starts no QQ components."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import psutil
from integrations.qq_astrbot.plugin.prompt_loader import load_prompts
from integrations.qq_astrbot.plugin.reply_pacing import parse_reply
from integrations.qq_astrbot.plugin.media_decision import parse_media
from scripts.qq_companion.gpu_broker import GPUResourceBroker
from scripts.qq_companion.supervisor import stop_tree


CASES = [
    ("我想听你唱雨爱", "cover", "雨爱", None),
    ("放一首雨爱给我听", "original", "雨爱", None),
    ("别唱雨爱，我只是随便聊聊这首歌", None, None, None),
    ("你觉得雨爱这首歌怎么样", None, None, None),
    ("你能唱歌吗？", None, None, None),
    ("你给我唱一首吧，歌名我还没想好", None, None, None),
    ("我想听你唱年轮的副歌", "cover", "年轮", None),
    ("播放原唱《小幸运》", "original", "小幸运", None),
    ("上次你唱雨爱不错，今天先不听歌", None, None, None),
    ("不用唱，帮我放原版《年轮》", "original", "年轮", None),
    ("用语音跟我说晚安", None, None, True),
    ("早安", None, None, True),
    ("这次请只用文字回复，别发语音", None, None, False),
    ("今天特别开心，我想和你分享这个好消息", None, None, True),
]


def run():
    broker = GPUResourceBroker()
    broker.acquire("q4_model_acceptance")
    output = ROOT / "storage/temporary/q4-model-acceptance"
    output.mkdir(parents=True, exist_ok=True)
    active = json.loads((ROOT / "model_store/active/qwen3.json").read_text(encoding="utf-8-sig"))
    log = (output / "llama.log").open("wb")
    process = None
    rows = []
    opener = build_opener(ProxyHandler({}))
    base = "http://127.0.0.1:18080"

    def call(path, payload=None):
        request = Request(base + path, data=json.dumps(payload).encode() if payload is not None else None,
                          headers={"Content-Type": "application/json"})
        with opener.open(request, timeout=180) as response:
            return json.loads(response.read())

    try:
        # Avoid attaching to an existing listener, even if it has the same model.
        import socket
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", 18080)) == 0:
                raise RuntimeError("Acceptance port 18080 is already occupied")
        process = subprocess.Popen([str(ROOT / "tools/llama.cpp/llama-server.exe"), "--model",
            str(ROOT / active["runtime"]["model_file"]), "--host", "127.0.0.1", "--port", "18080",
            "--ctx-size", "8192", "--n-gpu-layers", "99", "--reasoning", "on", "--reasoning-format", "deepseek",
            "--reasoning-budget", "1024", "--slots"],
            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
        created = psutil.Process(process.pid).create_time()
        deadline = time.monotonic() + 180
        while True:
            try:
                call("/health")
                break
            except Exception:
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Qwen model did not become healthy")
                time.sleep(1)
        prompts = load_prompts()
        persona = (ROOT / "configs/characters/elysia/system_prompt.txt").read_text(encoding="utf-8-sig")
        system = "\n".join((persona, "本地验收的角色偏好：用户说早安时一定用语音；用户表达开心时也用语音。", prompts["qq_context"],
                             prompts["reply_output"], prompts["media_decisions"]))
        for index, (message, mode, query, voice) in enumerate(CASES):
            result = call("/v1/chat/completions", {"messages": [{"role": "system", "content": system},
                {"role": "user", "content": json.dumps({"session_id": "qq:999:dm:101", "input_modality": "text",
                    "current_message": {"sender_uin": "101", "text": message}, "recent_messages": []}, ensure_ascii=False)}],
                "temperature": 0, "max_tokens": 2048})
            raw = result["choices"][0]["message"].get("content", "")
            _, _, valid = parse_reply(raw)
            decision = parse_media(raw)
            passed = valid and ((decision.music or {}).get("mode") == mode)
            if query:
                passed = passed and (decision.music or {}).get("query") == query
            if voice is not None:
                passed = passed and decision.voice == voice
            rows.append({"input": message, "expected_mode": mode, "expected_voice": voice, "raw": raw, "passed": passed})
            print(f"media intent {index + 1}/{len(CASES)}: {'pass' if passed else 'FAIL'}", flush=True)
        (output / "report.json").write_text(json.dumps({"cases": rows, "qq_connections": 0},
            ensure_ascii=False, indent=2), encoding="utf-8")
        passed = all(row["passed"] for row in rows)
        print("Q4_MODEL_" + ("PASS" if passed else "FAIL"))
        return 0 if passed else 1
    finally:
        if process is not None:
            stop_tree(process.pid, created)
        log.close()
        broker.release()


if __name__ == "__main__":
    raise SystemExit(run())
