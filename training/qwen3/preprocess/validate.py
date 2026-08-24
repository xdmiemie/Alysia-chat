from __future__ import annotations

import argparse
import json
import re
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
    result = validate_files(paths, max_near_duplicate_ratio=config.generation.max_near_duplicate_ratio)
    manifest_path = Path(args.manifest) if args.manifest else config.run_dir / "dataset_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"validated {result['total_examples']} examples -> {manifest_path.relative_to(PROJECT_ROOT)}")
    return 0


def validate_files(paths: list[Path], *, max_near_duplicate_ratio: float = 0.92) -> dict[str, object]:
    files = []
    total = 0
    signatures: list[tuple[Path, int, str]] = []
    for path in paths:
        count, file_signatures = _validate_file_with_signatures(path)
        total += count
        signatures.extend((path, line, text) for line, text in file_signatures)
        files.append(
            {
                "path": path.relative_to(PROJECT_ROOT).as_posix() if path.is_relative_to(PROJECT_ROOT) else str(path),
                "sha256": file_sha256(path),
                "examples": count,
            }
        )
    _assert_not_duplicate_or_near_duplicate(signatures, max_near_duplicate_ratio)
    return {
        "total_examples": total,
        "dataset_hash": dataset_hash(paths),
        "files": files,
    }


def validate_file(path: Path) -> int:
    return _validate_file_with_signatures(path)[0]


def _validate_file_with_signatures(path: Path) -> tuple[int, list[tuple[int, str]]]:
    count = 0
    signatures: list[tuple[int, str]] = []
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
            _validate_role_order(path, line_number, messages)
            metadata = record.get("metadata")
            if isinstance(metadata, dict) and metadata.get("source_type") == "quote_to_sft":
                source_ids = metadata.get("source_quote_ids")
                if not isinstance(source_ids, list) or not all(isinstance(item, str) and item for item in source_ids):
                    raise ValueError(f"{path}:{line_number} quote_to_sft record must retain source_quote_ids")
            signatures.append((line_number, _signature(messages)))
            count += 1
    if count == 0:
        raise ValueError(f"{path} did not contain any examples")
    return count, signatures


def _validate_role_order(path: Path, line_number: int, messages: list[dict[str, object]]) -> None:
    conversation = messages[1:] if messages[0].get("role") == "system" else messages
    expected = "user"
    for message in conversation:
        if message.get("role") != expected:
            raise ValueError(f"{path}:{line_number} roles must alternate user/assistant after optional system message")
        expected = "assistant" if expected == "user" else "user"


def _signature(messages: list[dict[str, object]]) -> str:
    assistant = " ".join(str(item["content"]) for item in messages if item.get("role") == "assistant")
    return re.sub(r"[\W_]", "", assistant).casefold()


def _assert_not_duplicate_or_near_duplicate(signatures: list[tuple[Path, int, str]], threshold: float) -> None:
    if not 0 < threshold <= 1:
        raise ValueError("max_near_duplicate_ratio must be between 0 and 1")
    seen: dict[str, tuple[Path, int]] = {}
    token_sets: list[tuple[Path, int, set[str]]] = []
    for path, line, signature in signatures:
        if signature in seen:
            previous_path, previous_line = seen[signature]
            raise ValueError(f"duplicate SFT example: {previous_path}:{previous_line} and {path}:{line}")
        seen[signature] = (path, line)
        tokens = set(re.findall(r"[\u4e00-\u9fff]|[A-Za-z0-9]+", signature))
        for previous_path, previous_line, previous_tokens in token_sets:
            overlap = len(tokens & previous_tokens) / len(tokens | previous_tokens) if tokens or previous_tokens else 1.0
            if overlap >= threshold:
                raise ValueError(f"near-duplicate SFT example: {previous_path}:{previous_line} and {path}:{line}")
        token_sets.append((path, line, tokens))


if __name__ == "__main__":
    raise SystemExit(main())
