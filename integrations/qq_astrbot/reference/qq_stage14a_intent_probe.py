"""Check the local model's structured singing decision without connecting to QQ."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime.qq_adapter.agent import AgentSession
from runtime.qq_adapter.chat import ChatPolicy, MessageStore, QQChatBridge
from runtime.qwen3.config import load_qwen3_config


def run() -> list[dict]:
    policy = ChatPolicy.load(ROOT / "configs/qq_companion/local.json")
    if not policy.song.enabled:
        raise RuntimeError("song feature is disabled in the local QQ policy")
    with tempfile.TemporaryDirectory(dir=ROOT / "storage/temporary") as directory:
        store = MessageStore(Path(directory) / "messages.sqlite3")
        try:
            bridge = QQChatBridge(1, policy, store, load_qwen3_config(), "intent-probe")
            schemas = tuple({"name": item.name, "arguments": item.args_schema}
                            for item in bridge.registry.definitions())
            results = []
            for text in ("大肥鱼，我还想听你唱年轮", "我要听你唱雨爱",
                         "点一首雨爱", "聊聊《雨爱》", "播放《雨爱》原唱"):
                action = AgentSession(load_qwen3_config()).decide(
                    text, (), 250, tool_schemas=schemas, song_enabled=True)
                results.append({"text": text, "action": action.action,
                                "sing": action.sing, "song_query": action.song_query,
                                "content": action.content})
        finally:
            store.db.close()
    return results


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
