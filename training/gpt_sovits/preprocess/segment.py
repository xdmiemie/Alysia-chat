from __future__ import annotations

import argparse
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Placeholder entry for manual/assisted GPT-SoVITS clip segmentation.")
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Segment source: {input_dir}")
    print(f"Segment output: {output_dir}")
    print("Stage 8 segmentation is intentionally manual-first; create short clips and manifest.jsonl after review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
