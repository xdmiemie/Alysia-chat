from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from training.gpt_sovits.config import load_training_config
from training.gpt_sovits.preprocess.dataset_manifest import validate_dataset


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Select reference audio clips for a trained GPT-SoVITS model.")
    parser.add_argument("--config", default="training/gpt_sovits/configs/characters/elysia.yaml")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--emotion", default="")
    parser.add_argument("--limit", type=int, default=6)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_training_config(args.config)
    validation = validate_dataset(config, write_normalized=False)
    if not validation.ready:
        return 1
    emotion = (args.emotion or config.export.references_emotion).lower()
    selected = [item for item in validation.items if item.emotion == emotion] or list(validation.items)
    selected = selected[: args.limit]
    output_dir = Path(args.output_dir) if args.output_dir else config.run_dir / "references"
    if not output_dir.is_absolute():
        output_dir = PROJECT_ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    for index, item in enumerate(selected, start=1):
        target = output_dir / f"{item.emotion}_{index:02d}{item.resolved_audio_path.suffix.lower()}"
        shutil.copy2(item.resolved_audio_path, target)
        target.with_suffix(".txt").write_text(item.text.strip() + "\n", encoding="utf-8")
        print(target.relative_to(PROJECT_ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
