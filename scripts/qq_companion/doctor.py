"""Read-only Q0/Q1 checks. Never emit local tokens, UINs or chat content."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ASTRBOT = ROOT / "external" / "AstrBot"
SNOWLUMA = ROOT / "external" / "SnowLuma" / "v2.1.1"


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'OK' if ok else 'WAIT'}] {name}" + (f": {detail}" if detail else ""))
    return ok


def listening(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def main() -> int:
    all_ok = True
    git = subprocess.run(["git", "merge-base", "HEAD", "aca495e801f061210ce4961da8df2f63ab3f3c44"],
                         cwd=ROOT, capture_output=True, text=True, check=False)
    all_ok &= check("Stage 10 ancestor", git.returncode == 0 and git.stdout.strip() == "aca495e801f061210ce4961da8df2f63ab3f3c44")
    all_ok &= check("AstrBot pinned source", (ASTRBOT / "pyproject.toml").exists())
    astr_python = ROOT / "envs" / "astrbot" / "Scripts" / "python.exe"
    all_ok &= check("isolated AstrBot Python 3.12", astr_python.exists())
    all_ok &= check("SnowLuma v2.1.1", (SNOWLUMA / "release.json").exists())
    all_ok &= check("FFmpeg", (ROOT / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe").exists())
    selected = os.environ.get("QQ_COMPANION_SNOWLUMA_CONFIG")
    configs = [Path(selected).resolve()] if selected else list((SNOWLUMA / "config").glob("onebot_*.json"))
    if selected and (configs[0].parent != (SNOWLUMA / "config").resolve() or not configs[0].name.startswith("onebot_")):
        configs = []
    astr_config = ASTRBOT / "data" / "cmd_config.json"
    if len(configs) == 1 and configs[0].exists() and astr_config.exists():
        snow = json.loads(configs[0].read_text(encoding="utf-8-sig"))
        astr = json.loads(astr_config.read_text(encoding="utf-8-sig"))
        clients = [c for c in snow.get("networks", {}).get("wsClients", []) if c.get("name") == "astrbot-chat"]
        platforms = [p for p in astr.get("platform", []) if p.get("id") == "snowluma"]
        paired = len(clients) == len(platforms) == 1 and clients[0].get("url") == "ws://127.0.0.1:6199/ws"
        if paired:
            paired &= bool(clients[0].get("accessToken")) and clients[0].get("accessToken") == platforms[0].get("ws_reverse_token")
            paired &= platforms[0].get("ws_reverse_host") == "127.0.0.1"
        paired &= bool(astr.get("platform_settings", {}).get("ignore_bot_self_message"))
        all_ok &= check("OneBot reverse WS pairing", paired)
    else:
        all_ok &= check("OneBot reverse WS pairing", False, "initialize and configure both services")
    all_ok &= check("Q1 inbound guard installed", (ASTRBOT / "data" / "plugins" / "astrbot_chat_q1" / "main.py").exists())
    for label, port in (("AstrBot dashboard", 6185), ("AstrBot OneBot", 6199), ("SnowLuma WebUI", 5099), ("SnowLuma HTTP", 3000), ("Qwen3", 8080)):
        check(label, listening(port), f"127.0.0.1:{port}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
