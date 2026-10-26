"""Standalone SnowLuma / OneBot v11 text transport acceptance tool."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from websockets.exceptions import ConnectionClosed, InvalidHandshake

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.qq_adapter import ConnectionState, OneBotActionError, OneBotClient, OneBotConfig, probe_connection


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check SnowLuma OneBot v11 without starting AI models")
    parser.add_argument("--ws-url", default="ws://127.0.0.1:3001/")
    parser.add_argument("--http-port", type=int, default=3000)
    parser.add_argument("--token-env", default="SNOWLUMA_WS_TOKEN", help="environment variable holding the WS token")
    parser.add_argument("--prompt-token", action="store_true", help="read the WS token without echoing it")
    parser.add_argument("--receive", type=int, default=0, metavar="N", help="wait for N real private/group messages")
    parser.add_argument("--receive-timeout", type=float, default=120.0)
    parser.add_argument("--retries", type=int, default=2, help="extra diagnostic attempts after a failed check")
    parser.add_argument("--retry-delay", type=float, default=1.0)
    parser.add_argument("--send-to", type=int, metavar="QQ_ID", help="friend QQ number for a test message")
    parser.add_argument("--send-text", default="Alysia QQ Stage 12 文字收发测试")
    parser.add_argument("--log", type=Path, default=Path("storage/logs/qq-stage12-probe.json"))
    args = parser.parse_args()
    if args.receive < 0 or args.receive_timeout <= 0 or args.retries < 0 or args.retry_delay < 0:
        parser.error("receive/retries must be nonnegative and receive-timeout must be positive")
    if args.send_to is not None and args.send_to <= 0:
        parser.error("--send-to must be a positive QQ number")
    return args


async def run(args: argparse.Namespace) -> int:
    token = getpass.getpass("SnowLuma WebSocket Token: ") if args.prompt_token else os.environ.get(args.token_env, "")
    config = OneBotConfig(
        ws_url=args.ws_url,
        token=token,
        http_port=args.http_port,
    )
    for attempt in range(args.retries + 1):
        result = await probe_connection(config)
        if result.state is ConnectionState.READY or attempt == args.retries:
            break
        await asyncio.sleep(args.retry_delay)
    report: dict[str, object] = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "ws_url": config.ws_url,
        "state": result.state.value,
        "detail": result.detail,
        "hint": result.hint,
        "bot_user_id": result.user_id,
        "diagnostic_attempts": attempt + 1,
        "received_count": 0,
        "sent": False,
    }
    print(f"{result.state.value}: {result.hint}")
    try:
        if result.state is not ConnectionState.READY:
            return 1
        if args.send_to is None and args.receive == 0:
            return 0
        async with OneBotClient(config) as client:
            if args.send_to is not None:
                await client.send_private_message(args.send_to, args.send_text)
                report["sent"] = True
                print("Test private message sent")
            deadline = asyncio.get_running_loop().time() + args.receive_timeout
            for index in range(args.receive):
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise asyncio.TimeoutError("Timed out waiting for messages")
                message = await client.next_message(timeout=remaining)
                report["received_count"] = index + 1
                print(
                    f"{index + 1}/{args.receive}: {message.message_type} "
                    f"user={message.user_id} group={message.group_id} "
                    f"message_id={message.message_id} segments={len(message.segments)}"
                )
        return 0
    except (ConnectionError, asyncio.TimeoutError, OneBotActionError, ConnectionClosed, InvalidHandshake, OSError, ValueError) as exc:
        report["error"] = type(exc).__name__
        print(f"Transport check failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        args.log.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
