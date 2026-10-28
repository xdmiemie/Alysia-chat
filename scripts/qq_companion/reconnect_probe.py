"""Exercise three bounded AstrBot failures while SnowLuma stays attached to NTQQ."""

from __future__ import annotations

import json
import time

import psutil

from supervisor import LOGS, REGISTRY, port_open, stop_tree


def registry() -> dict:
    for _ in range(10):
        try:
            return json.loads(REGISTRY.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.2)
    raise RuntimeError("Q1 supervisor registry is unavailable")


def child(name: str) -> dict | None:
    return next((item for item in registry()["children"] if item["name"] == name), None)


def live(item: dict) -> bool:
    try:
        return abs(psutil.Process(item["pid"]).create_time() - item["created_at"]) <= 2
    except psutil.NoSuchProcess:
        return False


def main() -> int:
    snow = child("snowluma")
    if snow is None or not live(snow) or not port_open(5099) or not port_open(6199):
        raise RuntimeError("Q1 services are not ready")
    log = LOGS / "snowluma.log"
    for attempt in range(1, 4):
        astr = child("astrbot")
        if astr is None or not live(astr):
            raise RuntimeError("AstrBot is not owned by the Q1 supervisor")
        offset = log.stat().st_size
        start = time.monotonic()
        stop_tree(astr["pid"], astr["created_at"])
        deadline = start + 90
        while time.monotonic() < deadline:
            current = child("astrbot")
            new_astr = current is not None and current["pid"] != astr["pid"] and live(current)
            current_snow = child("snowluma")
            same_snow = current_snow is not None and live(snow) and current_snow["pid"] == snow["pid"]
            with log.open("rb") as stream:
                stream.seek(offset)
                new_log = stream.read().decode("utf-8", errors="replace")
            connected = "[astrbot-chat] connected ws://127.0.0.1:6199/ws" in new_log
            if new_astr and same_snow and port_open(6199) and connected:
                print(f"reconnect_{attempt}=pass, seconds={time.monotonic() - start:.1f}", flush=True)
                break
            time.sleep(1)
        else:
            raise RuntimeError(f"Reconnect attempt {attempt} did not recover within 90 seconds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
