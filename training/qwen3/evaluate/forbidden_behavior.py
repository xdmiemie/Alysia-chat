from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


FORBIDDEN_RE = re.compile(r"(<think>|system prompt|language model|AI assistant|语言模型|系统提示词)", re.IGNORECASE)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan Qwen3 acceptance answers for forbidden behavior.")
    parser.add_argument("jsonl")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    failures = scan_file(Path(args.jsonl))
    for failure in failures:
        print(failure)
    return 1 if failures else 0


def scan_file(path: Path) -> list[str]:
    failures: list[str] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        data = json.loads(line)
        text = str(data.get("assistant") or data.get("reply") or data.get("output") or "")
        if FORBIDDEN_RE.search(text):
            failures.append(f"{path}:{line_number} contains forbidden output")
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
