from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from training.qwen3.config import load_training_config
from training.qwen3.generate.pipeline import DatasetPaused, build_quote_dataset


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build auditable Qwen3 Chat JSONL from role quotes.")
    parser.add_argument("--config", default="training/qwen3/configs/characters/elysia.yaml")
    parser.add_argument("--pause-file")
    args = parser.parse_args(argv)
    config = load_training_config(args.config)
    system_prompt = (PROJECT_ROOT / "configs" / "characters" / config.character_id / "system_prompt.txt").read_text(encoding="utf-8-sig").strip()
    def progress(value: dict) -> None:
        import json
        print("[progress] " + json.dumps(value, ensure_ascii=False), flush=True)
    try:
        report = build_quote_dataset(config, system_prompt=system_prompt, pause_file=Path(args.pause_file) if args.pause_file else None, progress=progress)
    except DatasetPaused:
        print("DATASET_PAUSED: saved candidates can be resumed from Judge", flush=True)
        return 75
    print(f"generated {report['accepted_count']} SFT examples")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
