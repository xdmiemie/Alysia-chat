"""Local operator CLI. Credentials are loaded from a private file, never printed."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import sys
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["configure", "status", "frequency", "open", "verify", "pause", "resume", "shares", "diagnostics", "retry-failed"])
    parser.add_argument("value", nargs="?", type=int)
    parser.add_argument("--account")
    parser.add_argument("--name")
    parser.add_argument("--friend")
    parser.add_argument("--friend-name")
    args = parser.parse_args()
    from integrations.qq_astrbot.plugin.inference_queue import state_directory
    from services.douyin_worker.config import ConfigStore
    directory = state_directory()
    directory.mkdir(parents=True, exist_ok=True)
    token = directory / "douyin_token"
    if args.action == "configure":
        if not all((args.account, args.name, args.friend, args.friend_name)):
            parser.error("configure requires account/name/friend/friend-name")
        ConfigStore().update({"account": {"douyin_id": args.account, "name": args.name},
            "friends": [{"douyin_id": args.friend, "name": args.friend_name, "opted_in": True}],
            "enabled": False, "share_enabled": True, "draft_only": False})
        if not token.exists():
            token.write_text(secrets.token_urlsafe(32), encoding="ascii")
        from services.douyin_worker.security import protect
        protect(token)
        print("Configured. Start the worker, open its browser, log in manually, then verify/resume.")
        return 0
    routes = {"status": ("GET", "/health"), "open": ("POST", "/v1/douyin/browser/open"),
        "verify": ("POST", "/v1/douyin/browser/verify"), "pause": ("POST", "/v1/douyin/pause"),
        "resume": ("POST", "/v1/douyin/resume"), "shares": ("GET", "/v1/douyin/shares"),
        "retry-failed": ("POST", "/v1/douyin/retry-failed"),
        "frequency": ("PUT" if args.value is not None else "GET", "/v1/douyin/frequency"),
        "diagnostics": ("GET", "/v1/douyin/browser/diagnostics")}
    method, route = routes[args.action]
    body = {"videos_per_hour": args.value} if args.action == "frequency" and args.value is not None else {}
    request = Request("http://127.0.0.1:8767" + route,
        data=json.dumps(body).encode() if method != "GET" else None, method=method,
        headers={"Authorization": "Bearer " + token.read_text(encoding="ascii").strip(), "Content-Type": "application/json"})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=120) as response:
            print(json.dumps(json.load(response), ensure_ascii=False, indent=2))
    except HTTPError as exc:
        print(json.dumps({"status": exc.code, "error": json.loads(exc.read(4096))}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
