from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from training.qwen3.config import load_training_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Normalize Qwen3 role JSONL into chat messages JSONL.")
    parser.add_argument("--config", default="training/qwen3/configs/characters/elysia.yaml")
    parser.add_argument("--source")
    parser.add_argument("--output")
    parser.add_argument("--system-prompt")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_training_config(args.config)
    source = Path(args.source) if args.source else config.dataset.raw
    output = Path(args.output) if args.output else config.dataset.normalized
    system_prompt_path = PROJECT_ROOT / args.system_prompt if args.system_prompt else PROJECT_ROOT / "configs" / "characters" / config.character_id / "system_prompt.txt"
    system_prompt = system_prompt_path.read_text(encoding="utf-8").strip()
    count = normalize_jsonl(source, output, system_prompt)
    print(f"normalized {count} examples -> {output.relative_to(PROJECT_ROOT)}")
    return 0


def normalize_jsonl(source: Path, output: Path, system_prompt: str) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with source.open("r", encoding="utf-8-sig") as input_handle, output.open("w", encoding="utf-8") as output_handle:
        for line_number, line in enumerate(input_handle, start=1):
            if not line.strip():
                continue
            raw = json.loads(line)
            existing_messages = raw.get("messages")
            if isinstance(existing_messages, list):
                # Chat JSONL is already the canonical format.  Preserve its
                # author-supplied system context and auditable metadata rather
                # than rebuilding a second, divergent training representation.
                record = {"messages": existing_messages}
                if isinstance(raw.get("metadata"), dict):
                    record["metadata"] = raw["metadata"]
                output_handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                count += 1
                continue
            user_text = _user_text(raw)
            assistant_text = _assistant_text(raw)
            if not user_text or not assistant_text:
                raise ValueError(f"{source}:{line_number} missing user or assistant text")
            record = {
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                    {"role": "assistant", "content": assistant_text},
                ]
            }
            output_handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1
    if count == 0:
        raise ValueError(f"{source} did not contain any examples")
    return count


def _user_text(raw: dict[str, object]) -> str:
    instruction = str(raw.get("instruction", "")).strip()
    input_text = str(raw.get("input", "")).strip()
    if instruction and input_text:
        return instruction + "\n" + input_text
    return instruction or input_text or str(raw.get("user", "")).strip()


def _assistant_text(raw: dict[str, object]) -> str:
    output = raw.get("output")
    if isinstance(output, str):
        return output.strip()
    assistant = raw.get("assistant")
    if isinstance(assistant, dict):
        return str(assistant.get("reply", "")).strip()
    if isinstance(assistant, str):
        return assistant.strip()
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
