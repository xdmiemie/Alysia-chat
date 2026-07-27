from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from training.gpt_sovits.config import config_hash, load_training_config
from training.gpt_sovits.preprocess.dataset_manifest import prepare_dataset, validate_dataset
from training.gpt_sovits.train.common import run_stage


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run GPT-SoVITS S2 training with Alysia metadata/resume support.")
    parser.add_argument("--config", default="training/gpt_sovits/configs/characters/elysia.yaml")
    parser.add_argument("--resume-from", default="")
    parser.add_argument("--from-scratch", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-prepare", action="store_true")
    parser.add_argument("--force-prepare", action="store_true")
    parser.add_argument("--sensevoice-config", default="configs/runtime/sensevoice.yaml")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = PROJECT_ROOT / args.config
    config = load_training_config(config_path)
    if not args.skip_prepare:
        prepare_dataset(config, sensevoice_config=args.sensevoice_config, force=args.force_prepare)
    validation = validate_dataset(config, write_normalized=True)
    run_stage(
        config=config,
        config_path=config_path,
        validation=validation,
        cfg_hash=config_hash(config_path),
        stage_name="s2",
        stage_config=config.s2,
        resume_from=args.resume_from or config.resume_from,
        from_scratch=args.from_scratch,
        dry_run=args.dry_run,
        project_root=PROJECT_ROOT,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
