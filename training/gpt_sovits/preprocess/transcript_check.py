from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from training.gpt_sovits.config import load_training_config
from training.gpt_sovits.preprocess.dataset_manifest import validate_dataset


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check transcript fields for GPT-SoVITS training clips.")
    parser.add_argument("--config", default="training/gpt_sovits/configs/characters/elysia.yaml")
    parser.add_argument("--min-text-chars", type=int, default=1)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_training_config(args.config)
    validation = validate_dataset(config, write_normalized=False)
    failed = False
    for item in validation.items:
        if len(item.text.strip()) < args.min_text_chars:
            print(f"{item.audio_path}: transcript too short", file=sys.stderr)
            failed = True
    return 1 if failed or not validation.ready else 0


if __name__ == "__main__":
    raise SystemExit(main())
