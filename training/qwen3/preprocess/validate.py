from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from training.qwen3.config import dataset_hash, file_sha256, load_training_config


VALID_ROLES = {"system", "user", "assistant"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate Qwen3 chat JSONL datasets.")
    parser.add_argument("--config", default="training/qwen3/configs/characters/elysia.yaml")
    parser.add_argument("--paths", nargs="*")
    parser.add_argument("--manifest")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_training_config(args.config)
    paths = [Path(path) for path in args.paths] if args.paths else [config.dataset.train, config.dataset.validation]
    result = validate_files(paths)
    manifest_path = Path(args.manifest) if args.manifest else config.run_dir / "dataset_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"validated {result['total_examples']} examples -> {manifest_path.relative_to(PROJECT_ROOT)}")
    return 0


def validate_files(paths: list[Path]) -> dict[str, object]:
    files = []
    total = 0
    for path in paths:
        count = validate_file(path)
        total += count
        files.append(
            {
                "path": path.relative_to(PROJECT_ROOT).as_posix() if path.is_relative_to(PROJECT_ROOT) else str(path),
                "sha256": file_sha256(path),
                "examples": count,
            }
        )
    return {
        "total_examples": total,
        "dataset_hash": dataset_hash(paths),
        "files": files,
    }


def validate_file(path: Path) -> int:
    count = 0
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            messages = record.get("messages")
            if not isinstance(messages, list) or len(messages) < 2:
                raise ValueError(f"{path}:{line_number} must include at least two messages")
            for message in messages:
                if not isinstance(message, dict):
                    raise ValueError(f"{path}:{line_number} message must be an object")
                role = message.get("role")
                content = message.get("content")
                if role not in VALID_ROLES:
                    raise ValueError(f"{path}:{line_number} invalid role: {role}")
                if not isinstance(content, str) or not content.strip():
                    raise ValueError(f"{path}:{line_number} message content must not be empty")
            if messages[-1].get("role") != "assistant":
                raise ValueError(f"{path}:{line_number} final message must be assistant")
            count += 1
    if count == 0:
        raise ValueError(f"{path} did not contain any examples")
    return count


if __name__ == "__main__":
    raise SystemExit(main())
