"""Stage 19 operation deadlines shared by desktop and runtime workers."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from runtime.common.config import load_flat_yaml


@dataclass(frozen=True)
class TimeoutConfig:
    asr_seconds: float = 30
    llm_seconds: float = 90
    tts_seconds: float = 120
    model_start_seconds: float = 180
    model_switch_seconds: float = 240
    training_stop_seconds: float = 300


def load_timeouts(path: Path | None = None) -> TimeoutConfig:
    path = path or Path(__file__).resolve().parents[2] / "configs" / "runtime" / "timeouts.yaml"
    raw = load_flat_yaml(path)
    values = {key: float(raw.get(key, getattr(TimeoutConfig, key))) for key in TimeoutConfig.__dataclass_fields__}
    if any(value <= 0 for value in values.values()):
        raise ValueError("all timeouts must be positive")
    return TimeoutConfig(**values)
