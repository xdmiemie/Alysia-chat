"""Offline registered-model switching acceptance with real GPU telemetry.

Owns temporary model/audio processes under the GPU broker, restores manifests
on exit, and never starts QQ. Agent media actions are mocked; speech/ASR, native
tool inference, memory, and long-context inference are real.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import psutil

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "external/AstrBot")]

import httpx

from runtime.qwen3.config import load_qwen3_config
from runtime.qwen3 import model_registry
from scripts.qq_companion.gpu_broker import GPUResourceBroker
from scripts.qq_companion.gpu_memory import GPUMemoryCounters
from scripts.qq_companion.owned_process import OwnedProcess
from scripts.qq_companion.supervisor import port_open

BASE = "http://127.0.0.1:18080"


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


class Monitor:
    def __init__(self, output):
        self.output, self.phase, self.model_pid = output, "baseline", None
        self.owners, self.samples, self.errors = [], [], []
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        counters = None
        try:
            counters = GPUMemoryCounters()
            with self.output.open("w", encoding="utf-8") as log:
                while not self.stop_event.is_set():
                    readings = counters.sample()
                    owned = {}
                    with self.lock:
                        for owner in self.owners:
                            if owner.job and owner.job.handle:
                                for process in owner.job.processes():
                                    if process.pid in readings:
                                        owned[str(process.pid)] = readings[process.pid]
                    gpu = subprocess.check_output(["nvidia-smi", "--query-gpu=memory.total,memory.used,memory.free",
                        "--format=csv,noheader,nounits"], text=True,
                        creationflags=subprocess.CREATE_NO_WINDOW).strip().splitlines()[0]
                    total, used, free = [int(value.strip()) for value in gpu.split(",")]
                    row = {"time": time.time(), "phase": self.phase, "model_pid": self.model_pid,
                        "ram_used_bytes": psutil.virtual_memory().used,
                        "model_rss_bytes": psutil.Process(self.model_pid).memory_info().rss if self.model_pid and psutil.pid_exists(self.model_pid) else None,
                        "gpu_total_mib": total, "gpu_used_mib": used, "gpu_free_mib": free,
                        "owned_processes": owned, "model": readings.get(self.model_pid)}
                    self.samples.append(row)
                    log.write(json.dumps(row) + "\n")
                    log.flush()
                    self.stop_event.wait(0.5)
        except BaseException as exc:
            self.errors.append(repr(exc))
        finally:
            if counters:
                counters.close()

    def summary(self, prefix):
        samples = [r for r in self.samples if r["phase"].startswith(prefix) and r["model"]]
        if self.errors or not samples:
            raise RuntimeError(f"Missing GPU memory telemetry: {self.errors}")
        return {"samples": len(samples), "peak_gpu_used_mib": max(r["gpu_used_mib"] for r in samples),
            "peak_ram_used_mib": round(max(r["ram_used_bytes"] for r in samples) / 2**20, 3),
            "peak_model_rss_mib": round(max(r["model_rss_bytes"] or 0 for r in samples) / 2**20, 3),
            "min_gpu_free_mib": min(r["gpu_free_mib"] for r in samples),
            "peak_model_dedicated_mib": round(max(r["model"]["dedicated_bytes"] for r in samples) / 2**20, 3),
            "peak_model_shared_mib": round(max(r["model"]["shared_bytes"] for r in samples) / 2**20, 3),
            "peak_owned_shared_mib": round(max(sum(p["shared_bytes"] for p in r["owned_processes"].values())
                for r in samples) / 2**20, 3)}


def wait_ready(client, owner, url, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if owner.process.poll() is not None:
            raise RuntimeError(f"Owned service exited {owner.process.returncode}: {url}")
        try:
            if client.get(url, timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Service startup timed out: {url}")


async def long_context(base, model_id, profile):
    from astrbot.core.agent.message import Message
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
    from integrations.qq_astrbot.plugin.context_budget import RequestBudget
    from integrations.qq_astrbot.plugin.request_provider import request_chat
    from integrations.qq_astrbot.plugin.prompt_loader import load_prompts
    from integrations.qq_astrbot.plugin.agent_runtime import parse_final_reply
    from scripts.qq_companion.configure_model import configure
    config = configure({}, model_id, "elysia", prompt_profile=profile)
    provider = ProviderOpenAIOfficial({**config["provider_sources"][0], **config["provider"][0],
        "api_base": base + "/v1", "timeout": 180}, {})
    try:
        prompts = load_prompts()
        budget = RequestBudget(provider, "followup")
        def messages(n):
            return [Message(role="system", content=prompts["qq_context"]),
                Message(role="user", content=json.dumps({"session_id": "local-load-test",
                    "recent_messages": [{"sender_uin": "101", "text": "今天听了音乐，也整理了书架。"} for _ in range(n)],
                    "current_message": {"sender_uin": "101", "text": "今天的事情做完了，晚安。"}}, ensure_ascii=False)),
                Message(role="system", content=prompts["reply_output"])]
        low, high = 0, 3000
        target = load_qwen3_config().context_size - load_qwen3_config().max_tokens - 512
        while low + 1 < high:
            middle = (low + high) // 2
            if await budget.count(messages(middle)) <= target:
                low = middle
            else:
                high = middle
        context = messages(low)
        count = await budget.count(context)
        start = time.monotonic()
        response = await request_chat(provider, phase="followup", contexts=context,
            max_tokens=256, temperature=0, request_max_retries=1)
        result = parse_final_reply(response.completion_text or "")
        if not result:
            raise RuntimeError("Long-context reply did not satisfy the shared output contract")
        return {"input_tokens": count, "context_size": budget.window,
            "seconds": round(time.monotonic() - start, 3), "reply": result, "passed": True}
    finally:
        await provider.terminate()


def run(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    os.environ["ASTRBOT_ROOT"] = str(output / "astrbot")
    os.environ["ASTRBOT_CHAT_MODEL_DEBUG_DIR"] = str(output / "model_debug")
    broker = GPUResourceBroker()
    broker.acquire("model_switch_acceptance")
    active_path = model_registry.ACTIVE_MANIFEST
    registry_path = model_registry.character_manifest_path(args.character)
    snapshots = {path: path.read_bytes() for path in (active_path, registry_path)}
    owners, logs = [], []
    monitor = Monitor(output / "gpu_samples.jsonl")
    report = {"models": [], "qq_connections": 0, "media_actions": "mocked",
        "audio_services": not args.no_audio, "restored_original_manifest": False}
    client = httpx.Client(trust_env=False, timeout=180)

    def start(name, command):
        log = (output / (name + ".log")).open("wb")
        logs.append(log)
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        owner = OwnedProcess(command, cwd=ROOT, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, startupinfo=startup,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        owners.append(owner)
        with monitor.lock:
            monitor.owners.append(owner)
        return owner

    def stop(owner):
        with monitor.lock:
            monitor.owners.remove(owner)
        owner.stop()
        owners.remove(owner)

    try:
        for port in (8080, 18080, 5001, 5002, 9880):
            if port_open(port):
                raise RuntimeError(f"Port {port} occupied; stop the managed stack before testing")
        for model_id in args.models:
            model_registry.find_model(args.character, model_id)
        monitor.thread.start()
        if not args.no_audio:
            python = str(ROOT / "runtime_env/python.exe")
            monitor.phase = "audio_warmup"
            asr = start("sensevoice", [python, "-m", "uvicorn", "runtime.sensevoice.api:app", "--host", "127.0.0.1", "--port", "5001"])
            tts = start("gpt_sovits", ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                str(ROOT / "scripts/start_gpt_sovits_upstream.ps1")])
            wrapper = start("tts_wrapper", [python, "-m", "uvicorn", "runtime.gpt_sovits.api:app", "--host", "127.0.0.1", "--port", "5002"])
            wait_ready(client, asr, "http://127.0.0.1:5001/health")
            wait_ready(client, wrapper, "http://127.0.0.1:5002/health")
            wait_ready(client, tts, "http://127.0.0.1:9880/docs", timeout=240)
            speech = client.post("http://127.0.0.1:5002/v1/audio/speech", json={"text": "今天的事情做完了，晚安。", "character_id": args.character})
            speech.raise_for_status()
            if not speech.content.startswith(b"RIFF"):
                raise RuntimeError("Speech service did not return WAV")
            (output / "speech.wav").write_bytes(speech.content)
            transcript = client.post("http://127.0.0.1:5001/v1/audio/transcriptions",
                files={"file": ("speech.wav", speech.content, "audio/wav")})
            transcript.raise_for_status()
            report["audio_warmup"] = {"wav_bytes": len(speech.content), "asr": transcript.json()}
        seen = set()
        for index, model_id in enumerate(args.models):
            entry = model_registry.activate_model(args.character, model_id)
            prefix = f"{index + 1}-{model_id}"
            directory = output / prefix
            directory.mkdir()
            monitor.phase = prefix + ":load"
            argv = [str(ROOT / "tools/llama.cpp/llama-server.exe"), "--model", str(entry.model_file),
                "--jinja", "--slots", "--verbosity", "5"] + load_qwen3_config().server_args(port=18080)
            if entry.lora_adapter:
                argv += ["--lora", str(entry.lora_adapter)]
            if entry.mmproj_file:
                argv += ["--mmproj", str(entry.mmproj_file)]
                if not load_qwen3_config().mmproj_offload:
                    argv.append("--no-mmproj-offload")
            model = start(prefix, argv)
            monitor.model_pid = model.process.pid
            row = {"model_id": model_id, "prompt_profile": entry.prompt_profile, "pid": model.process.pid,
                "command": argv, "passed": False, "check_errors": {}}
            report["models"].append(row)
            def check(name, operation):
                try:
                    return operation()
                except Exception as exc:
                    row["check_errors"][name] = repr(exc)
                    print(f"CHECK {model_id}/{name}: FAIL {exc}", flush=True)
                    if model.process.poll() is not None:
                        raise RuntimeError("Model process exited during acceptance") from exc
            try:
                wait_ready(client, model, BASE + "/health")
                served_id = client.get(BASE + "/v1/models").json()["data"][0]["id"]
                save(directory / "props.json", client.get(BASE + "/props").json())
                from scripts.qq_companion import probe_agent_native, probe_q5_local
                probe_agent_native.OUTPUT = directory / "agent"
                probe_agent_native.OUTPUT.mkdir()
                monitor.phase = prefix + ":agent"
                check("agent", lambda: asyncio.run(probe_agent_native.cases(served_id, base=BASE,
                    prompt_profile=entry.prompt_profile, quick=True)))
                row["agent"] = json.loads((probe_agent_native.OUTPUT / "report.json").read_text(encoding="utf-8"))
                if model_id not in seen:
                    probe_agent_native.OUTPUT = directory / "controls"
                    probe_agent_native.OUTPUT.mkdir()
                    monitor.phase = prefix + ":controls"
                    check("controls", lambda: asyncio.run(probe_agent_native.cases(served_id, base=BASE,
                        prompt_profile=entry.prompt_profile, controls_only=True)))
                    row["controls"] = json.loads((probe_agent_native.OUTPUT / "report.json").read_text(encoding="utf-8"))
                    probe_q5_local.OUTPUT = directory / "memory"
                    probe_q5_local.OUTPUT.mkdir()
                    monitor.phase = prefix + ":memory"
                    check("memory", lambda: asyncio.run(probe_q5_local.cases(BASE, served_id, prompt_profile=entry.prompt_profile)))
                    row["memory"] = json.loads((probe_q5_local.OUTPUT / "report.json").read_text(encoding="utf-8"))
                    monitor.phase = prefix + ":long_context"
                    row["long_context"] = check("long_context", lambda: asyncio.run(long_context(BASE, served_id, entry.prompt_profile)))
                if not args.no_audio:
                    monitor.phase = prefix + ":speech"
                    speech = client.post("http://127.0.0.1:5002/v1/audio/speech",
                        json={"text": "今天辛苦了，早点休息。", "character_id": args.character})
                    speech.raise_for_status()
                    row["coexisting_speech_wav_bytes"] = len(speech.content)
                row["passed"] = not row["check_errors"]
                seen.add(model_id)
            finally:
                row["gpu"] = monitor.summary(prefix)
                stop(model)
                monitor.model_pid = None
                text = (output / (prefix + ".log")).read_text(encoding="utf-8", errors="replace")
                row["gpu_log_evidence"] = [line for line in text.splitlines() if re.search(
                    r"offloaded \d+/\d+ layers|buffer size|n_ctx_slot|n_ctx_seq|CUDA error|out of memory", line)]
                row["all_layers_offloaded"] = any((m := re.search(r"offloaded (\d+)/(\d+) layers", line))
                    and m[1] == m[2] for line in row["gpu_log_evidence"])
                row["zero_shared_gpu_memory"] = row["gpu"]["peak_model_shared_mib"] == 0
                save(output / "report.json", report)
            print(f"SWITCH {model_id}: functional {'PASS' if row['passed'] else 'FAIL'}; GPU {row['gpu']}", flush=True)
    except BaseException as exc:
        report["error"] = repr(exc)
        raise
    finally:
        monitor.stop_event.set()
        if monitor.thread.is_alive():
            monitor.thread.join(timeout=10)
        for owner in reversed(owners):
            owner.stop()
        for log in logs:
            log.close()
        for path, content in snapshots.items():
            path.write_bytes(content)
        report["restored_original_manifest"] = True
        report["telemetry_errors"] = monitor.errors
        report["functional_pass"] = bool(report["models"]) and all(row["passed"] for row in report["models"]) and "error" not in report
        report["all_layers_gpu"] = bool(report["models"]) and all(row.get("all_layers_offloaded") for row in report["models"])
        report["gpu_acceptance_pass"] = report["all_layers_gpu"] and not monitor.errors and "error" not in report and all(
            row.get("long_context", {"passed": True}) and row.get("long_context", {"passed": True}).get("passed")
            and any("CUDA0 KV buffer size" in line for line in row.get("gpu_log_evidence", []))
            and not any(re.search(r"CUDA error|out of memory", line, re.IGNORECASE)
                        for line in row.get("gpu_log_evidence", [])) for row in report["models"])
        report["shared_memory_requirement"] = "Nonzero shared GPU buffers are allowed; full layer/cache GPU offload and no OOM are required."
        save(output / "report.json", report)
        client.close()
        broker.release()
    return report["functional_pass"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--character", default="elysia")
    parser.add_argument("--models", nargs="+", default=["qwen3-14b-q4-k-m-base-v1", "qwen3.5-9b-q8-0-base-v1", "qwen3-14b-q4-k-m-base-v1"])
    parser.add_argument("--no-audio", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "storage/temporary" / ("model-switch-" + time.strftime("%Y%m%d-%H%M%S")))
    if not run(parser.parse_args()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
