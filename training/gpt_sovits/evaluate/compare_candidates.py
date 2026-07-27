from __future__ import annotations

import argparse
import json
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Write a manual comparison sheet for GPT-SoVITS candidate outputs.")
    parser.add_argument("--candidate-dir", required=True)
    parser.add_argument("--output", default="training/gpt_sovits/evaluate/candidate_comparison.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    candidate_dir = Path(args.candidate_dir)
    files = sorted(str(path) for path in candidate_dir.rglob("*") if path.suffix.lower() in {".wav", ".flac", ".mp3"})
    report = {
        "candidate_dir": str(candidate_dir),
        "audio_files": files,
        "manual_fields": ["naturalness", "similarity", "noise", "prosody", "notes"],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote comparison scaffold: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
