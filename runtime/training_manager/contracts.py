"""Stage 11 UI-facing training contract; implementations arrive with the training UI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol


TrainingKind = Literal["qwen3", "gpt_sovits"]
JobState = Literal["created", "running", "stopping", "stopped", "failed", "completed"]


@dataclass(frozen=True)
class TrainingJob:
    job_id: str
    kind: TrainingKind
    character_id: str
    config_path: Path
    state: JobState
    run_dir: Path
    stdout_log: Path
    stderr_log: Path
    model_id: str | None = None
    error: str | None = None


class TrainingJobController(Protocol):
    """Boundary for the desktop UI; no training-script internals are exposed."""

    def create(self, kind: TrainingKind, character_id: str, config_path: Path) -> TrainingJob: ...

    def start(self, job_id: str) -> TrainingJob: ...

    def stop(self, job_id: str) -> TrainingJob: ...

    def resume(self, job_id: str, checkpoint: Path | None = None) -> TrainingJob: ...

    def status(self, job_id: str) -> TrainingJob: ...

    def activate(self, job_id: str, model_id: str) -> TrainingJob: ...

    def rollback(self, job_id: str) -> TrainingJob: ...
