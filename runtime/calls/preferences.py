from __future__ import annotations

import json
from pathlib import Path


DEFAULT_PATH = Path("storage/desktop/settings.json")


def load_call_preferences(path: str | Path = DEFAULT_PATH) -> dict[str, object]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"save_call_audio": True}
    return {
        "save_call_audio": bool(data.get("save_call_audio", True)),
    }


def save_call_preferences(
    *, save_call_audio: bool, path: str | Path = DEFAULT_PATH
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = load_call_preferences(target)
    data["save_call_audio"] = bool(save_call_audio)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
