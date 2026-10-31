"""Local, reloadable Q6 policy. Account IDs never belong in tracked defaults."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import os
import re

import yaml

from .diary_jobs import resolve_timezone


DEFAULTS = {
    "enabled": False, "account_id": "", "timezone": "Asia/Hong_Kong",
    "daily_time": "23:40", "daily_deadline": "23:59", "daily_max_chars": 100,
    "visibility": "allowlist", "friend_allowlist": [],
    "interactions_enabled": False, "interaction_time": "19:30", "event_time": "10:00",
    "comment_daily_limit": 2, "like_daily_limit": 3,
    "events": [],
}


def config_path():
    if explicit := os.environ.get("ASTRBOT_CHAT_QZONE_CONFIG"):
        return Path(explicit).resolve()
    from .prompt_loader import prompt_path
    return prompt_path().with_name("qzone.local.yaml")


def load_config(path=None):
    path = Path(path) if path else config_path()
    value = yaml.safe_load(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("invalid_qzone_config_keys")
    result = {**deepcopy(DEFAULTS), **value}
    for key in ("enabled", "interactions_enabled"):
        if type(result[key]) is not bool:
            raise ValueError("invalid_qzone_boolean")
    result["account_id"] = str(result["account_id"])
    if result["account_id"] and not re.fullmatch(r"[1-9][0-9]{4,19}", result["account_id"]):
        raise ValueError("invalid_qzone_account")
    if result["enabled"] and not result["account_id"]:
        raise ValueError("qzone_account_required")
    for key in ("daily_time", "daily_deadline", "interaction_time", "event_time"):
        if not isinstance(result[key], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", result[key]):
            raise ValueError("invalid_qzone_time")
    if result["daily_time"] > result["daily_deadline"]:
        raise ValueError("invalid_qzone_window")
    resolve_timezone(result["timezone"])
    if result["visibility"] not in {"self", "allowlist"}:
        raise ValueError("invalid_qzone_visibility")
    if not isinstance(result["friend_allowlist"], list):
        raise ValueError("invalid_qzone_allowlist")
    result["friend_allowlist"] = list(dict.fromkeys(str(x) for x in result["friend_allowlist"]))
    if len(result["friend_allowlist"]) > 20 or any(
            not re.fullmatch(r"[1-9][0-9]{4,19}", x) for x in result["friend_allowlist"]):
        raise ValueError("invalid_qzone_allowlist")
    for key, minimum, maximum in (("daily_max_chars", 20, 200),
                                  ("comment_daily_limit", 0, 2), ("like_daily_limit", 0, 3)):
        if type(result[key]) is not int or not minimum <= result[key] <= maximum:
            raise ValueError("invalid_qzone_limit")
    if not isinstance(result["events"], list) or len(result["events"]) > 100:
        raise ValueError("invalid_qzone_events")
    event_ids = set()
    for event in result["events"]:
        if (not isinstance(event, dict) or set(event) - {"id", "date", "annual", "text"}
                or not re.fullmatch(r"[\w-]{1,64}", str(event.get("id", "")))
                or event["id"] in event_ids or type(event.get("annual", False)) is not bool
                or not isinstance(event.get("text"), str) or not 1 <= len(event["text"]) <= 400):
            raise ValueError("invalid_qzone_event")
        from datetime import date
        date.fromisoformat(event["date"])
        event_ids.add(event["id"])
    return result


def save_config(value, path=None):
    path = Path(path) if path else config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    load_config(temporary)  # Validate before replacing a working configuration.
    temporary.replace(path)
