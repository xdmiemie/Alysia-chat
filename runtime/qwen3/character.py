from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from runtime.qwen3.types import CharacterReply


@dataclass(frozen=True)
class CharacterProfile:
    character_id: str
    name: str
    personality: tuple[str, ...]
    max_sentences: int
    forbidden: tuple[str, ...]


def load_character_profile(path: str | Path) -> CharacterProfile:
    data = _load_yaml_subset(Path(path))
    speaking_style = data.get("speaking_style", {})
    return CharacterProfile(
        character_id=str(data.get("character_id", "elysia")),
        name=str(data.get("name", "Elysia")),
        personality=tuple(str(item) for item in data.get("personality", [])),
        max_sentences=int(speaking_style.get("max_sentences", 3)),
        forbidden=tuple(str(item) for item in data.get("forbidden", [])),
    )


def load_examples(path: str | Path) -> list[tuple[str, CharacterReply]]:
    examples_path = Path(path)
    if not examples_path.exists():
        return []

    examples = []
    for line in examples_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        data = json.loads(stripped)
        assistant = data.get("assistant", {})
        examples.append(
            (
                str(data.get("user", "")),
                CharacterReply(
                    reply=str(assistant.get("reply", "")),
                    emotion=str(assistant.get("emotion", "neutral")),
                    intensity=float(assistant.get("intensity", 0.5)),
                    speed=float(assistant.get("speed", 1.0)),
                ),
            )
        )
    return examples


def _load_yaml_subset(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}

    try:
        import yaml
    except ImportError:
        return _parse_yaml_subset(path.read_text(encoding="utf-8"))

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return {}
    return data


def _parse_yaml_subset(text: str) -> dict[str, object]:
    data: dict[str, object] = {}
    current_key: str | None = None
    current_map: dict[str, object] | None = None
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.startswith("  - ") and current_key is not None:
            values = data.setdefault(current_key, [])
            if isinstance(values, list):
                values.append(line[4:].strip())
            continue
        if line.startswith("  ") and current_map is not None and ":" in line:
            key, value = line.strip().split(":", 1)
            current_map[key.strip()] = _parse_scalar(value.strip())
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        current_key = key
        current_map = None
        if value:
            data[key] = _parse_scalar(value)
        else:
            if key == "speaking_style":
                current_map = {}
                data[key] = current_map
            else:
                data[key] = []
    return data


def _parse_scalar(value: str) -> object:
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        return value.strip("'\"")
