"""Start the Stage 13 QQ text loop using an existing SnowLuma installation."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime.qq_adapter import OneBotClient, OneBotConfig
from runtime.qq_adapter.chat import ChatPolicy, MessageStore, QQChatBridge
from runtime.qwen3.config import load_qwen3_config

LOG = logging.getLogger(__name__)


def local_onebot_config(install: Path, bot_id: int | None = None) -> OneBotConfig:
    configs = sorted((install / "config").glob("onebot_*.json"))
    if bot_id is not None:
        configs = [path for path in configs if path.stem == f"onebot_{bot_id}"]
    if len(configs) != 1:
        raise RuntimeError("Expected exactly one local SnowLuma OneBot account config")
    data = json.loads(configs[0].read_text(encoding="utf-8-sig"))
    servers = (data.get("networks") or {}).get("wsServers") or []
    servers = [server for server in servers if server.get("role") == "Universal"]
    if len(servers) != 1:
        raise RuntimeError("Expected one Universal forward WebSocket server in SnowLuma config")
    server = servers[0]
    if server.get("host") not in ("127.0.0.1", "localhost", "::1"):
        raise RuntimeError("SnowLuma OneBot WebSocket must listen on localhost")
    token = server.get("accessToken")
    if not isinstance(token, str) or not token:
        raise RuntimeError("SnowLuma OneBot token is missing")
    port = int(server["port"])
    path = str(server.get("path") or "/")
    if not path.startswith("/") or "?" in path or "#" in path:
        raise RuntimeError("Invalid local OneBot path")
    return OneBotConfig(ws_url=f"ws://127.0.0.1:{port}{path}", token=token)


async def run(args: argparse.Namespace) -> None:
    policy = ChatPolicy.load(Path(args.policy))
    store = MessageStore(Path(args.database))
    qwen = load_qwen3_config()
    active = json.loads(Path("model_store/active/qwen3.json").read_text(encoding="utf-8-sig"))
    model_version = str(active.get("version") or active.get("model_id") or "unknown")
    delay = 1.0
    while True:
        try:
            config = local_onebot_config(Path(args.snowluma), args.bot_id)
            async with OneBotClient(config) as client:
                status = await client.call_api("get_status")
                login = await client.call_api("get_login_info")
                if not isinstance(status, dict) or not status.get("online") or not status.get("good"):
                    raise RuntimeError("QQ account or OneBot is not ready")
                bot_id = int(login["user_id"])
                bridge = QQChatBridge(bot_id, policy, store, qwen, model_version)
                await bridge.load_friends(client)
                LOG.info("QQ text chat ready for account %s", bot_id)
                delay = 1.0
                worker = asyncio.create_task(bridge.worker(client))
                try:
                    while True:
                        event = await client.next_message()
                        bridge.accept(event)
                finally:
                    worker.cancel()
                    try:
                        await worker
                    except asyncio.CancelledError:
                        pass
        except Exception as exc:
            store.recover_incomplete()
            LOG.warning("QQ bridge unavailable (%s); retrying in %.0fs", type(exc).__name__, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snowluma", default=str(ROOT / "third_party/SnowLuma/v2.1.1"))
    parser.add_argument("--bot-id", type=int)
    parser.add_argument("--policy", default=str(ROOT / "configs/qq_agent/local.json"))
    parser.add_argument("--database", default=str(ROOT / "storage/qq/messages.sqlite3"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
