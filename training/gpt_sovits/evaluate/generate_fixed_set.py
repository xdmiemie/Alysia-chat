from __future__ import annotations

import argparse
import json
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a fixed GPT-SoVITS evaluation text set.")
    parser.add_argument("--inputs", default="training/gpt_sovits/evaluate/fixed_texts.txt")
    parser.add_argument("--output", default="training/gpt_sovits/evaluate/fixed_set.jsonl")
    parser.add_argument("--emotion", default="gentle")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    lines = [line.strip() for line in Path(args.inputs).read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "\n".join(json.dumps({"text": line, "emotion": args.emotion}, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(lines)} prompts to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
