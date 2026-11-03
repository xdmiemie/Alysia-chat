from __future__ import annotations

import json
import os
from pathlib import Path
import threading
from typing import Annotated
import uuid
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, field_validator

from integrations.qq_astrbot.plugin.vision_config import project_root

DouyinID = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]


class Account(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    douyin_id: DouyinID
    name: str = Field(min_length=1, max_length=80)


class Friend(Account):
    opted_in: StrictBool = True


class DouyinConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: StrictBool = False
    share_enabled: StrictBool = False
    draft_only: StrictBool = False
    hourly_videos: int = Field(default=20, ge=1, le=120)
    timezone: str = "Asia/Hong_Kong"
    start_hour: int = Field(default=10, ge=0, le=23)
    end_hour: int = Field(default=24, ge=1, le=24)
    idle_resume_seconds: int = Field(default=60, ge=0, le=600)
    daily_share_limit: int = Field(default=5, ge=1, le=5)
    share_cooldown_seconds: int = Field(default=3600, ge=3600, le=86400)
    dedup_days: int = Field(default=30, ge=1, le=365)
    headless: StrictBool = False
    account: Account | None = None
    friends: list[Friend] = Field(default_factory=list, max_length=20)

    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls, value):
        ZoneInfo(value)
        return value

    def awake(self, timestamp):
        from datetime import datetime
        hour = datetime.fromtimestamp(timestamp, ZoneInfo(self.timezone)).hour
        return self.start_hour <= hour < self.end_hour


class ConfigStore:
    """Atomic, validated, hot-read local config. IDs never come from the LLM."""
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get("ASTRBOT_CHAT_DOUYIN_CONFIG",
            project_root() / "configs/qq_companion/douyin.local.json"))
        self.lock = threading.RLock()

    def read(self):
        if not self.path.exists():
            return DouyinConfig()
        value = DouyinConfig.model_validate_json(self.path.read_bytes())
        if value.start_hour >= value.end_hour:
            raise ValueError("invalid_awake_window")
        ids = [f.douyin_id for f in value.friends]
        if len(set(ids)) != len(ids) or value.account and value.account.douyin_id in ids:
            raise ValueError("invalid_friend_allowlist")
        return value

    def update(self, patch):
        with self.lock:
            if not isinstance(patch, dict):
                raise ValueError("invalid_config_patch")
            data = {**self.read().model_dump(), **patch}
            value = DouyinConfig.model_validate(data)
            if value.start_hour >= value.end_hour:
                raise ValueError("invalid_awake_window")
            ids = [f.douyin_id for f in value.friends]
            if len(set(ids)) != len(ids) or value.account and value.account.douyin_id in ids:
                raise ValueError("invalid_friend_allowlist")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix("." + uuid.uuid4().hex + ".tmp")
            temporary.write_text(json.dumps(value.model_dump(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.path)
            return value
