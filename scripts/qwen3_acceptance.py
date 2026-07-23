from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.qwen3.service import Qwen3ChatSession
from runtime.qwen3.service import FORBIDDEN_REPLY_RE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the stage 3 Qwen3 30-turn text acceptance check.")
    parser.add_argument("--config", default="configs/runtime/qwen3.yaml")
    parser.add_argument("--inputs", default="tests/fixtures/qwen3_stage3_inputs.txt")
    parser.add_argument("--rounds", type=int, default=30)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    inputs = _load_inputs(Path(args.inputs))
    if not inputs:
        print("Error: no acceptance inputs found.", file=sys.stderr)
        return 2

    session = Qwen3ChatSession.from_config(args.config)
    failures = 0
    for index in range(args.rounds):
        user_text = inputs[index % len(inputs)]
        try:
            reply = session.chat(user_text)
            data = reply.to_dict()
            has_think = "<think" in reply.reply.lower()
            forbidden = bool(FORBIDDEN_REPLY_RE.search(reply.reply))
            if has_think or forbidden:
                failures += 1
            print(
                json.dumps(
                    {"round": index + 1, "user": user_text, "forbidden": forbidden, **data},
                    ensure_ascii=False,
                )
            )
        except Exception as exc:
            failures += 1
            print(json.dumps({"round": index + 1, "user": user_text, "error": str(exc)}, ensure_ascii=False))

    print(json.dumps({"rounds": args.rounds, "failures": failures}, ensure_ascii=False))
    return 1 if failures else 0


def _load_inputs(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


if __name__ == "__main__":
    raise SystemExit(main())
