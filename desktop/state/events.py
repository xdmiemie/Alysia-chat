from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StartupProgress:
    message: str


@dataclass(frozen=True)
class StartupResult:
    character_id: str
    character_name: str
    session_id: str
    workspace: str
    qwen_model: str
    tts_model: str
    log_path: Path
