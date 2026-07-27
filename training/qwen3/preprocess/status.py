from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from training.qwen3.config import config_hash, file_sha256, load_training_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Report whether Qwen3 dataset preprocessing outputs are current.")
    parser.add_argument("--config", default="training/qwen3/configs/characters/elysia.yaml")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(json.dumps(preprocess_status(args.config), ensure_ascii=False))
    return 0


def preprocess_status(config_path: str | Path) -> dict[str, object]:
    config = load_training_config(config_path)
    cfg_hash = config_hash(config_path)
    raw_hash = file_sha256(config.dataset.raw) if config.dataset.raw.exists() else None
    system_prompt_path = PROJECT_ROOT / "configs" / "characters" / config.character_id / "system_prompt.txt"
    system_prompt_hash = file_sha256(system_prompt_path) if system_prompt_path.exists() else None
    metadata_path = config.dataset.normalized.parent / ".preprocess_manifest.json"
    metadata = _load_metadata(metadata_path)

    normalized_current = (
        config.dataset.normalized.exists()
        and metadata.get("raw_hash") == raw_hash
        and metadata.get("config_hash") == cfg_hash
        and metadata.get("system_prompt_hash") == system_prompt_hash
    )
    split_current = (
        normalized_current
        and config.dataset.train.exists()
        and config.dataset.validation.exists()
        and metadata.get("normalized_hash") == file_sha256(config.dataset.normalized)
        and float(metadata.get("validation_ratio", -1)) == float(config.dataset.validation_ratio)
    )

    return {
        "metadata_path": _relative(metadata_path),
        "raw": _relative(config.dataset.raw),
        "normalized": _relative(config.dataset.normalized),
        "train": _relative(config.dataset.train),
        "validation": _relative(config.dataset.validation),
        "raw_exists": config.dataset.raw.exists(),
        "normalized_current": normalized_current,
        "split_current": split_current,
        "config_hash": cfg_hash,
        "raw_hash": raw_hash,
        "system_prompt_hash": system_prompt_hash,
    }


def write_preprocess_metadata(config_path: str | Path) -> dict[str, object]:
    config = load_training_config(config_path)
    metadata_path = config.dataset.normalized.parent / ".preprocess_manifest.json"
    metadata = {
        "config_hash": config_hash(config_path),
        "raw_hash": file_sha256(config.dataset.raw),
        "system_prompt_hash": file_sha256(PROJECT_ROOT / "configs" / "characters" / config.character_id / "system_prompt.txt"),
        "normalized_hash": file_sha256(config.dataset.normalized),
        "validation_ratio": config.dataset.validation_ratio,
        "normalized": _relative(config.dataset.normalized),
        "train": _relative(config.dataset.train),
        "validation": _relative(config.dataset.validation),
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return metadata


def _load_metadata(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
