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
    parser = argparse.ArgumentParser(description="Report basic duration/audio metadata for GPT-SoVITS clips.")
    parser.add_argument("--config", default="training/gpt_sovits/configs/characters/elysia.yaml")
    return parser


def main(argv: list[str] | None = None) -> int:
    config = load_training_config(build_parser().parse_args(argv).config)
    validation = validate_dataset(config, write_normalized=False)
    for item in validation.items:
        print(
            f"{item.audio_path}\t{item.duration:.3f}s\t"
            f"{item.sample_rate or 'unknown'} Hz\t{item.channels or 'unknown'} ch"
        )
    return 0 if validation.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
