from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CHECKPOINT_RE = re.compile(r"^checkpoint-(\d+)$")


@dataclass(frozen=True)
class CheckpointInfo:
    path: Path
    step: int
    metadata: dict[str, Any]


def write_checkpoint_metadata(
    checkpoint_dir: Path,
    *,
    training_config: dict[str, Any],
    dataset_hash: str,
    config_hash: str,
    base_model_id: str,
) -> None:
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "dataset_hash": dataset_hash,
        "config_hash": config_hash,
        "base_model_id": base_model_id,
        "training_config": training_config,
    }
    _atomic_write_json(checkpoint_dir / "alysia_checkpoint.json", metadata)


def list_checkpoints(run_dir: Path) -> list[CheckpointInfo]:
    checkpoints: list[CheckpointInfo] = []
    if not run_dir.exists():
        return checkpoints
    for path in run_dir.iterdir():
        if not path.is_dir():
            continue
        match = CHECKPOINT_RE.match(path.name)
        if not match:
            continue
        metadata_path = path / "alysia_checkpoint.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
        checkpoints.append(CheckpointInfo(path=path, step=int(match.group(1)), metadata=metadata))
    return sorted(checkpoints, key=lambda item: item.step)


def latest_checkpoint(run_dir: Path) -> CheckpointInfo | None:
    checkpoints = list_checkpoints(run_dir)
    return checkpoints[-1] if checkpoints else None


def enforce_checkpoint_limit(run_dir: Path, *, keep: int, best_checkpoint_name: str = "best") -> list[Path]:
    if keep < 1:
        raise ValueError("keep must be at least 1")
    checkpoints = list_checkpoints(run_dir)
    removable = [checkpoint for checkpoint in checkpoints if checkpoint.path.name != best_checkpoint_name]
    to_remove = removable[:-keep]
    removed: list[Path] = []
    for checkpoint in to_remove:
        shutil.rmtree(checkpoint.path)
        removed.append(checkpoint.path)
    return removed


def assert_checkpoint_compatible(
    checkpoint: CheckpointInfo,
    *,
    dataset_hash: str,
    config_hash: str,
    base_model_id: str,
) -> None:
    metadata = checkpoint.metadata
    if metadata.get("dataset_hash") != dataset_hash:
        raise ValueError(
            f"{checkpoint.path} belongs to a different dataset. "
            "Use --from-scratch to start a new run."
        )
    if metadata.get("config_hash") != config_hash:
        raise ValueError(
            f"{checkpoint.path} belongs to a different training config. "
            "Use --from-scratch to start a new run."
        )
    if metadata.get("base_model_id") != base_model_id:
        raise ValueError(
            f"{checkpoint.path} belongs to base model {metadata.get('base_model_id')}, "
            f"not {base_model_id}."
        )


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent, suffix=".tmp") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.replace(path)
