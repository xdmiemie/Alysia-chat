"""Configure the pinned AstrBot and SnowLuma OneBot pair without printing secrets."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ASTRBOT = ROOT / "external" / "AstrBot"
SNOWLUMA = ROOT / "external" / "SnowLuma" / "v2.1.1"
BACKUPS = ROOT / "storage" / "qq_companion" / "config_backups"
WS_URL = "ws://127.0.0.1:6199/ws"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_json(path: Path, value: dict) -> None:
    if path.exists() and read_json(path) == value:
        return
    BACKUPS.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        shutil.copy2(path, BACKUPS / f"{path.name}.{stamp}.bak")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def locate_snowluma_config(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).resolve()
        if path.parent != (SNOWLUMA / "config").resolve() or not path.name.startswith("onebot_"):
            raise ValueError("SnowLuma config must be an existing onebot_*.json in the pinned release")
        return path
    matches = list((SNOWLUMA / "config").glob("onebot_*.json"))
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one SnowLuma account config; found {len(matches)}. Pass --snowluma-config.")
    return matches[0]


def configure(astrbot: dict, snowluma: dict, *, token_override: str | None = None) -> tuple[dict, dict]:
    networks = snowluma.setdefault("networks", {})
    servers = networks.get("httpServers", [])
    listeners = servers + networks.get("wsServers", [])
    if not all(s.get("host") in ("127.0.0.1", "localhost") for s in listeners):
        raise ValueError("SnowLuma HTTP/WS servers must bind only to loopback")
    token = token_override or next((s.get("accessToken") for s in servers if s.get("accessToken")), None)
    if not token:
        token = secrets.token_urlsafe(32)
        for server in servers:
            server["accessToken"] = token
    clients = [c for c in networks.get("wsClients", []) if c.get("name") != "astrbot-chat"]
    clients.append({
        "name": "astrbot-chat",
        "url": WS_URL,
        "role": "Universal",
        "accessToken": token,
        "messageFormat": "array",
        "reportSelfMessage": False,
        "reconnectIntervalMs": 5000,
    })
    networks["wsClients"] = clients
    platforms = [p for p in astrbot.get("platform", []) if p.get("id") != "snowluma"]
    platforms.append({
        "id": "snowluma",
        "type": "aiocqhttp",
        "enable": True,
        "ws_reverse_host": "127.0.0.1",
        "ws_reverse_port": 6199,
        "ws_reverse_token": token,
    })
    astrbot["platform"] = platforms
    astrbot.setdefault("platform_settings", {})["ignore_bot_self_message"] = True
    astrbot.setdefault("dashboard", {})["port"] = 6185
    astrbot["dashboard"]["host"] = "127.0.0.1"
    return astrbot, snowluma


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Back up then write both local configs")
    parser.add_argument("--snowluma-config")
    args = parser.parse_args()
    snow_path = locate_snowluma_config(args.snowluma_config)
    astr_path = ASTRBOT / "data" / "cmd_config.json"
    if not astr_path.exists():
        raise SystemExit("AstrBot config is absent. Initialize AstrBot first, then rerun configure.py.")
    runtime_path = SNOWLUMA / "config" / "runtime.json"
    if runtime_path.exists() and read_json(runtime_path).get("webuiHost") not in ("127.0.0.1", "localhost"):
        raise SystemExit("SnowLuma WebUI must bind to loopback before configuring the pair")
    astr, snow = configure(read_json(astr_path), read_json(snow_path), token_override=os.getenv("SNOWLUMA_ONEBOT_TOKEN"))
    if args.apply:
        save_json(astr_path, astr)
        save_json(snow_path, snow)
        plugin_src = ROOT / "integrations" / "qq_astrbot" / "plugin"
        plugin_dest = ASTRBOT / "data" / "plugins" / "astrbot_chat_q1"
        plugin_dest.mkdir(parents=True, exist_ok=True)
        for name in ("main.py", "guard.py", "metadata.yaml"):
            source = plugin_src / name
            target = plugin_dest / name
            if not target.exists() or source.read_bytes() != target.read_bytes():
                shutil.copy2(source, target)
        print("Configured OneBot reverse WS on loopback. Backups are in ignored storage/qq_companion/config_backups.")
    else:
        print("Configuration is valid in memory. Use --apply while both services are stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
