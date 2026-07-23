from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.qwen3.service import Qwen3ChatSession


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 3 Qwen3 character chat client.")
    parser.add_argument("message", nargs="?", help="Single user message. Omit to start an interactive session.")
    parser.add_argument("--config", default="configs/runtime/qwen3.yaml")
    parser.add_argument("--json", action="store_true", help="Print structured reply JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        session = Qwen3ChatSession.from_config(args.config)
        if args.message:
            reply = session.chat(args.message)
            print(json.dumps(reply.to_dict(), ensure_ascii=False) if args.json else reply.reply)
            return 0

        print("Stage 3 Qwen3 chat. Press Ctrl+C or enter an empty line to exit.")
        while True:
            user_text = input("You> ").strip()
            if not user_text:
                return 0
            reply = session.chat(user_text)
            print("Elysia> " + (json.dumps(reply.to_dict(), ensure_ascii=False) if args.json else reply.reply))
    except KeyboardInterrupt:
        print("")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
