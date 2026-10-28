"""Fault-inject only the registered TTS wrapper; verify text stack survives."""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

import psutil

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "storage/qq_companion/supervisor.json"
OPENER = build_opener(ProxyHandler({}))


def main() -> int:
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    entry = next(item for item in data["children"] if item["name"] == "tts_wrapper")
    process = psutil.Process(entry["pid"])
    if abs(process.create_time() - entry["created_at"]) > 2:
        raise RuntimeError("Registered TTS PID was reused; aborting fault injection")
    process.terminate()
    process.wait(timeout=10)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        current = json.loads(REGISTRY.read_text(encoding="utf-8"))
        if any(item["name"] == "tts_wrapper" and item["pid"] != entry["pid"] for item in current["children"]):
            break
        time.sleep(1)
    else:
        raise RuntimeError("TTS wrapper was not restarted")
    with OPENER.open("http://127.0.0.1:8080/health", timeout=5) as response:
        if response.status != 200:
            raise RuntimeError("Qwen3 text service is unavailable")
    with socket.create_connection(("127.0.0.1", 6199), timeout=5):
        pass
    print("TTS wrapper restarted; Qwen3 and AstrBot text listener remained available")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
