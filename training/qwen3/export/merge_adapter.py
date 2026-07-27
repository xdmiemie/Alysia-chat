from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from training.qwen3.config import load_training_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Merge a Qwen3 LoRA adapter into a HF-format base model.")
    parser.add_argument("--config", default="training/qwen3/configs/characters/elysia.yaml")
    parser.add_argument("--adapter-dir", required=True)
    parser.add_argument("--output-dir")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_training_config(args.config)
    output_dir = Path(args.output_dir) if args.output_dir else config.run_dir / "merged"
    if config.base_model.hf_path is None:
        raise RuntimeError("base_model.hf_path is required to merge an adapter.")
    try:
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        raise RuntimeError("Missing merge dependencies: transformers and peft.") from exc

    model = AutoModelForCausalLM.from_pretrained(config.base_model.hf_path, device_map="auto", trust_remote_code=True)
    model = PeftModel.from_pretrained(model, args.adapter_dir)
    merged = model.merge_and_unload()
    output_dir.mkdir(parents=True, exist_ok=True)
    merged.save_pretrained(output_dir)
    AutoTokenizer.from_pretrained(config.base_model.hf_path, trust_remote_code=True).save_pretrained(output_dir)
    print(output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
