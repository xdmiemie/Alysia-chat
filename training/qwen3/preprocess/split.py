from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from training.qwen3.config import load_training_config
from training.qwen3.preprocess.status import write_preprocess_metadata


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Split normalized Qwen3 JSONL into train and validation sets.")
    parser.add_argument("--config", default="training/qwen3/configs/characters/elysia.yaml")
    parser.add_argument("--source")
    parser.add_argument("--train")
    parser.add_argument("--validation")
    parser.add_argument("--validation-ratio", type=float)
    parser.add_argument("--seed", type=int, default=7)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_training_config(args.config)
    source = Path(args.source) if args.source else config.dataset.normalized
    train = Path(args.train) if args.train else config.dataset.train
    validation = Path(args.validation) if args.validation else config.dataset.validation
    ratio = args.validation_ratio if args.validation_ratio is not None else config.dataset.validation_ratio
    train_count, validation_count = split_jsonl(source, train, validation, ratio, args.seed)
    if not args.source and not args.train and not args.validation and args.validation_ratio is None:
        write_preprocess_metadata(args.config)
    print(f"split train={train_count}, validation={validation_count}")
    return 0


def split_jsonl(source: Path, train: Path, validation: Path, validation_ratio: float, seed: int) -> tuple[int, int]:
    if not 0 < validation_ratio < 1:
        raise ValueError("validation_ratio must be between 0 and 1")
    lines = [line for line in source.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if len(lines) < 2:
        raise ValueError("need at least two examples to create train and validation splits")
    rng = random.Random(seed)
    rng.shuffle(lines)
    validation_count = max(1, int(round(len(lines) * validation_ratio)))
    validation_lines = lines[:validation_count]
    train_lines = lines[validation_count:]
    if not train_lines:
        train_lines, validation_lines = lines[:-1], lines[-1:]

    train.parent.mkdir(parents=True, exist_ok=True)
    validation.parent.mkdir(parents=True, exist_ok=True)
    train.write_text("\n".join(train_lines) + "\n", encoding="utf-8")
    validation.write_text("\n".join(validation_lines) + "\n", encoding="utf-8")
    return len(train_lines), len(validation_lines)


if __name__ == "__main__":
    raise SystemExit(main())
