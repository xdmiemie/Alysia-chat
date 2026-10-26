"""Append compact structured operational events to bounded local JSONL files."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

_LOCK = RLock()
_ROOT = Path(__file__).resolve().parents[2]
_LIMIT = 10 * 1024 * 1024


def append_event(category: str, event: dict[str, object], *, root: Path = _ROOT) -> None:
    if not category or any(part in {"", ".", ".."} for part in category.split("/")):
        raise ValueError("invalid event category")
    if any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_/" for char in category):
        raise ValueError("invalid event category")
    target = root / "storage" / "logs" / category / "events.jsonl"
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), **event}
    line = json.dumps(record, ensure_ascii=False, default=str, separators=(",", ":")) + "\n"
    try:
        with _LOCK:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.stat().st_size + len(line.encode("utf-8")) > _LIMIT:
                target.replace(target.with_suffix(".jsonl.1"))
            with target.open("a", encoding="utf-8") as handle:
                handle.write(line)
    except OSError:
        logging.getLogger(__name__).exception("Could not write %s event", category)
