"""Real Q7 pixel/sequence/capacity acceptance. Does not connect to QQ."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
import socket
import secrets
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "external/AstrBot")]

import httpx
import psutil

from runtime.qwen3.config import load_qwen3_config
from scripts.qq_companion.gpu_broker import GPUResourceBroker
from scripts.qq_companion.owned_process import OwnedProcess
from scripts.qq_companion.probe_model_switch import Monitor, wait_ready
from scripts.qq_companion.q7_fixtures import generate
from integrations.qq_astrbot.plugin.vision_analysis import VisionAnalyzer
from integrations.qq_astrbot.plugin.vision_client import LocalVisionClient
from integrations.qq_astrbot.plugin.vision_config import load_vision_config, VisionGates, fingerprint
from integrations.qq_astrbot.plugin.vision_media import decode_images
from integrations.qq_astrbot.plugin.vision_store import VisionStore
from integrations.qq_astrbot.plugin.inference_queue import InferenceQueue


def save(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def quality(kind, fixture, value):
    text = json.dumps(value, ensure_ascii=False).lower()
    expected = fixture["expected"]
    if kind == "sticker":
        return expected.lower() in text
    if kind == "image":
        card = int(Path(fixture["path"]).stem.rsplit("-", 1)[-1]) + 1
        expected_ocr = f"CARD{card:02d}"
        count = fixture["count"]
        count_patterns = {1: r"(?:一|1)个", 2: r"(?:两|二|2)个", 3: r"(?:三|3)个"}
        narrative = value.get("summary", "") + " ".join(value.get("actions", []))
        return expected[:1] in text and expected_ocr.lower() in re.sub(r"\s", "", text) and bool(re.search(count_patterns[count], narrative))
    if isinstance(expected, str):
        expected = [expected]
    if kind in {"gif", "video"}:
        ordered = " ".join(row["description"] for row in sorted(value.get("timestamps", []), key=lambda row: row["timestamp"]))
        narrative = ordered or value.get("summary", "") + " " + " ".join(value.get("actions", []))
        position = 0
        for item in expected:
            found = narrative.find(item[:1], position)
            if found < 0:
                return False
            position = found + 1
        return True
    return all(item[:1] in text for item in expected)


async def exercises(args, output, report, monitor):
    config = load_vision_config()
    client = LocalVisionClient(output, config)
    store = VisionStore(output, config)
    analyzer = VisionAnalyzer(store, config, client.call, transcribe=client.media.transcribe)
    fixtures = generate(args.fixtures or output / "fixtures")
    owner = "qq:999:dm:101"
    for kind in args.kinds:
        rows = []
        monitor.phase = "q7-" + kind
        for index, fixture in enumerate(fixtures[kind]):
            if args.quick and index >= 2:
                break
            key = store.put(Path(fixture["path"]).read_bytes(), owner, kind)
            started = time.monotonic()
            try:
                value = await (analyzer.video(key, owner, fixture["question"]) if kind == "video" else
                    analyzer.image(key, owner, fixture["question"], kind=kind))
                row = {"index": index, "passed": quality(kind, fixture, value),
                    "result": value, "seconds": time.monotonic() - started}
            except Exception as exc:
                row = {"index": index, "passed": False, "error": repr(exc), "seconds": time.monotonic() - started}
            rows.append(row)
            report["capabilities"][kind] = {"samples": rows}
            report["receipts"] = client.receipts
            save(output / "report.json", report)
            print(f"Q7 {kind} {index + 1}/{len(fixtures[kind])}: {'PASS' if row['passed'] else 'FAIL'}", flush=True)
        score = sum(row["passed"] for row in rows) / len(rows)
        expected_count = {"image": 20, "sticker": 20, "gif": 10, "video": 20}[kind]
        passed = len(rows) == expected_count and score >= .85 and not any("error" in row for row in rows)
        report["capabilities"][kind].update(passed=passed, quality_rate=score, expected_count=expected_count)
        save(output / "report.json", report)
    # Real image swaps / question swaps / comparison; no filename in model input.
    if "image" in args.kinds:
        monitor.phase = "q7-alternation"
        _, red = decode_images(Path(fixtures["image"][0]["path"]).read_bytes(), config)
        _, blue = decode_images(Path(fixtures["image"][1]["path"]).read_bytes(), config)
        checks = []
        for frames, question, expected in [(red, "主要图形是什么颜色？", "红"), (blue, "主要图形是什么颜色？", "蓝"),
                (red, "图中可见的英文文字是什么？", "CARD"), (red + blue, "比较两张图形的颜色差异。", "蓝")]:
            text = await client.complete([{"role": "user", "content": [{"type": "text", "text": question}] +
                [{"type": "image_url", "image_url": {"url": f.url}} for f in frames]}])
            checks.append({"question": question, "passed": expected.lower() in text.lower(), "answer": text})
        report["input_truth"] = checks
        alternating = []
        for i in range(2 if args.quick else 30):
            text = await client.complete([{"role": "user", "content": "只回复数字7。"}], stream=True)
            picture = red if i % 2 == 0 else blue
            answer = await client.complete([{"role": "user", "content": [{"type": "text", "text": "图形是什么颜色？"},
                {"type": "image_url", "image_url": {"url": picture[0].url}}]}], stream=True)
            expected = "红" if i % 2 == 0 else "蓝"
            alternating.append({"round": i + 1, "text_ok": "7" in text, "image_ok": expected in answer})
            report["alternation"] = alternating
            report["receipts"] = client.receipts
            save(output / "report.json", report)
            print(f"Q7 text/image alternation {i + 1}/30", flush=True)
        if not all(c["passed"] for c in checks) or len(alternating) != 30 or not all(r["text_ok"] and r["image_ok"] for r in alternating):
            report["capabilities"]["image"]["passed"] = False
        monitor.phase = "q7-foreground-priority"
        async def background():
            for index in range(3):
                await client.complete([{"role": "user", "content": [{"type": "text", "text": "描述图形颜色。"},
                    {"type": "image_url", "image_url": {"url": red[0].url}}]}], priority=10, request_id=f"q7-qos-bg-{index}")
        task = asyncio.create_task(background())
        queue = InferenceQueue(output)
        for _ in range(200):
            with queue.connection() as db:
                running = db.execute("SELECT 1 FROM inference_requests WHERE id='q7-qos-bg-0' AND status='running'").fetchone()
            if running:
                break
            await asyncio.sleep(.025)
        answer = await client.complete([{"role": "user", "content": "只回复数字7。"}], request_id="q7-qos-fg", stream=True)
        await task
        with queue.connection() as db:
            order = [r[0] for r in db.execute("SELECT id FROM inference_requests WHERE id LIKE 'q7-qos-%' ORDER BY started")]
        report["foreground_priority"] = {"order": order, "passed": "7" in answer and order.index("q7-qos-fg") < order.index("q7-qos-bg-1")}
        report["receipts"] = client.receipts
        if not report["foreground_priority"]["passed"]:
            report["capabilities"]["image"]["passed"] = False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kinds", nargs="+", choices=["image", "sticker", "gif", "video"], default=["image", "sticker", "gif", "video"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--enable-passed", action="store_true")
    parser.add_argument("--use-running", action="store_true")
    parser.add_argument("--fixtures", type=Path, help="Reuse the generated original acceptance fixtures")
    parser.add_argument("--audio-services", action="store_true", help="Keep production ASR/TTS/media services resident during acceptance")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    os.environ["ASTRBOT_CHAT_STATE_DIR"] = str(output)
    report = {"started": time.time(), "fingerprint": fingerprint(), "capabilities": {}, "qq_connections": 0,
        "fixture_scope": "original generated geometric/OCR/expression/motion clips, not a natural-photo benchmark"}
    active = json.loads((ROOT / "model_store/active/qwen3.json").read_text(encoding="utf-8-sig"))
    runtime = active["runtime"]
    projector = ROOT / runtime["mmproj_file"]
    with projector.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if digest != runtime["mmproj_sha256"]:
        raise RuntimeError("Projector checksum mismatch")
    monitor = Monitor(output / "gpu_samples.jsonl")
    broker, owner, log = None, None, None
    auxiliary, auxiliary_logs = [], []
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    try:
        if not args.use_running:
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", 8080)) == 0:
                    raise RuntimeError("Port 8080 already owned; stop normally or use --use-running")
            broker = GPUResourceBroker()
            broker.acquire("q7_acceptance")
            config = load_qwen3_config()
            command = [str(ROOT / "tools/llama.cpp/llama-server.exe"), "--model", str(ROOT / runtime["model_file"]),
                "--mmproj", str(projector), "--slots"] + config.server_args()
            if not config.mmproj_offload:
                command.append("--no-mmproj-offload")
            log = (output / "llama.log").open("wb")
            owner = OwnedProcess(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                startupinfo=startup)
            monitor.owners.append(owner)
            monitor.model_pid = owner.process.pid
            with httpx.Client(trust_env=False) as http:
                wait_ready(http, owner, "http://127.0.0.1:8080/health")
        else:
            candidates = [p for p in psutil.process_iter(["name", "cmdline"]) if (p.info["name"] or "").lower() == "llama-server.exe"
                and str(projector).lower() in " ".join(p.info["cmdline"] or []).lower()]
            if len(candidates) != 1:
                raise RuntimeError("Cannot identify exactly one current multimodal model process")
            monitor.model_pid = candidates[0].pid
        if args.audio_services:
            token_path = output / "media_token"
            token_path.write_text(secrets.token_urlsafe(32), encoding="ascii")
            environment = {**os.environ, "QQ_COMPANION_MEDIA_TOKEN": token_path.read_text(), "ASTRBOT_CHAT_STATE_DIR": str(output)}
            commands = [
                ("asr", [str(ROOT / "runtime_env/python.exe"), "-m", "uvicorn", "runtime.sensevoice.api:app", "--host", "127.0.0.1", "--port", "5001"], 5001, "/health"),
                ("tts", ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts/start_gpt_sovits_upstream.ps1")], 9880, "/docs"),
                ("tts-wrapper", [str(ROOT / "runtime_env/python.exe"), "-m", "uvicorn", "runtime.gpt_sovits.api:app", "--host", "127.0.0.1", "--port", "5002"], 5002, "/health"),
                ("media", [str(ROOT / "runtime_env/python.exe"), "-m", "uvicorn", "services.media_api.app:app", "--host", "127.0.0.1", "--port", "8765"], 8765, "/docs")]
            with httpx.Client(trust_env=False) as http:
                for name, command, port, route in commands:
                    with socket.socket() as sock:
                        if sock.connect_ex(("127.0.0.1", port)) == 0:
                            raise RuntimeError(f"Audio port {port} is already occupied")
                    handle = (output / (name + ".log")).open("wb")
                    auxiliary_logs.append(handle)
                    process = OwnedProcess(command, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                        stdout=handle, stderr=subprocess.STDOUT, startupinfo=startup)
                    auxiliary.append(process)
                    monitor.owners.append(process)
                    wait_ready(http, process, f"http://127.0.0.1:{port}{route}", timeout=240)
                warmed = http.post("http://127.0.0.1:5002/v1/audio/speech", json={"text": "今天的事情做完了。", "character_id": "elysia"}, timeout=180)
                warmed.raise_for_status()
            report["audio_services_resident"] = True
        report["pid_before"] = monitor.model_pid
        report["ram_before"] = dict(psutil.virtual_memory()._asdict())
        monitor.phase = "q7-baseline"
        monitor.thread.start()
        time.sleep(1.2)
        report["baseline"] = monitor.samples[-1] if monitor.samples else None
        asyncio.run(exercises(args, output, report, monitor))
        report["pid_after"] = monitor.model_pid if psutil.pid_exists(monitor.model_pid) else None
        report["gpu"] = monitor.summary("q7-")
        report["ram_after"] = dict(psutil.virtual_memory()._asdict())
        report["single_instance_passed"] = report["pid_after"] == report["pid_before"]
        # Do not claim zero OOM when any request failed or telemetry is missing.
        report["normal_requests_passed"] = all(not any("error" in row for row in cap["samples"]) for cap in report["capabilities"].values())
        report["finished"] = time.time()
        report["gate_fingerprint"] = fingerprint()
        save(output / "report.json", report)
        if args.enable_passed and not args.quick and report["single_instance_passed"]:
            gates = VisionGates(ROOT / "storage/qq_companion")
            for kind, cap in report["capabilities"].items():
                if cap["passed"]:
                    gates.record(kind, {"passed": True, "report": str(output / "report.json"),
                        "samples": len(cap["samples"]), "quality_rate": cap["quality_rate"], "time": time.time(),
                        "scope": report["fixture_scope"]})
        return 0 if all(cap.get("passed") for cap in report["capabilities"].values()) else 1
    finally:
        monitor.stop_event.set()
        if monitor.thread.is_alive():
            monitor.thread.join(timeout=5)
        if owner:
            owner.stop()
        for process in reversed(auxiliary):
            process.stop()
        for handle in auxiliary_logs:
            handle.close()
        if log:
            log.close()
        if broker:
            broker.release()


if __name__ == "__main__":
    raise SystemExit(main())
