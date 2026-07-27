from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from training.gpt_sovits.config import load_training_config
from training.gpt_sovits.preprocess.dataset_manifest import validate_dataset


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convert GPT-SoVITS dataset clips to training WAV format.")
    parser.add_argument("--config", default="training/gpt_sovits/configs/characters/elysia.yaml")
    parser.add_argument("--ffmpeg", default="tools/ffmpeg/bin/ffmpeg.exe")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_training_config(args.config)
    validation = validate_dataset(config, write_normalized=False)
    if not validation.ready:
        return 1

    ffmpeg = PROJECT_ROOT / args.ffmpeg
    output_dir = config.dataset.root / "work" / "wav32k"
    output_dir.mkdir(parents=True, exist_ok=True)
    for item in validation.items:
        output = output_dir / (Path(item.audio_path).stem + ".wav")
        command = [
            str(ffmpeg),
            "-y",
            "-i",
            str(item.resolved_audio_path),
            "-ac",
            "1",
            "-ar",
            str(config.dataset.target_sample_rate),
            str(output),
        ]
        print(" ".join(command))
        if not args.dry_run:
            completed = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
            if completed.returncode != 0:
                return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
