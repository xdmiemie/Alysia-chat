"""Run a one-shot local Qwen3 chat/tool probe without starting QQ services."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

from scripts.qq_companion.configure_model import probe
from scripts.qq_companion.gpu_broker import GPUResourceBroker, ROOT, STATE


def run_active_probe() -> str:
    active = json.loads((ROOT / "model_store/active/qwen3.json").read_text(encoding="utf-8-sig"))
    model = ROOT / active["runtime"]["model_file"]
    adapter = active["runtime"].get("lora_adapter")
    executable = ROOT / "tools/llama.cpp/llama-server.exe"
    if not model.is_file() or not executable.is_file():
        raise RuntimeError("Active GGUF or project llama-server is missing")
    log_path = STATE / "logs/qwen3_probe.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [str(executable), "--model", str(model), "--host", "127.0.0.1",
               "--port", "8080", "--ctx-size", "8192", "--n-gpu-layers", "99", "--reasoning", "off"]
    if adapter:
        adapter_path = ROOT / adapter
        if adapter_path.suffix.lower() != ".gguf" or not adapter_path.is_file():
            raise RuntimeError("Active Qwen3 LoRA adapter must be an existing GGUF file")
        command.extend(["--lora", str(adapter_path)])
    with log_path.open("ab") as log:
        process = subprocess.Popen(command,
                                   cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"llama-server exited {process.returncode}; see {log_path}")
                with socket.socket() as sock:
                    sock.settimeout(1)
                    if sock.connect_ex(("127.0.0.1", 8080)) == 0:
                        break
                time.sleep(1)
            else:
                raise RuntimeError(f"llama-server did not listen within 180 seconds; see {log_path}")
            model_id = probe()
            print(f"Qwen3 chat/tool probe passed; model ID: {model_id}")
            return model_id
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main() -> int:
    broker = GPUResourceBroker()
    broker.acquire("qwen3_probe")
    try:
        run_active_probe()
    finally:
        broker.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
