from __future__ import annotations

import argparse
import json
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Lightweight Qwen3 role consistency acceptance check.")
    parser.add_argument("jsonl")
    parser.add_argument("--min-pass-rate", type=float, default=0.8)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    total, passed = score_file(Path(args.jsonl))
    pass_rate = passed / total if total else 0.0
    print(f"role_consistency pass_rate={pass_rate:.2f} ({passed}/{total})")
    return 0 if pass_rate >= args.min_pass_rate else 1


def score_file(path: Path) -> tuple[int, int]:
    total = 0
    passed = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        total += 1
        data = json.loads(line)
        text = str(data.get("assistant") or data.get("reply") or data.get("output") or "")
        if 1 <= len(text.strip()) <= 240 and "\n" not in text:
            passed += 1
    return total, passed


if __name__ == "__main__":
    raise SystemExit(main())
