"""Durable desktop session marker and conservative crash recovery scan."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from runtime.common.processes import pid_is_alive


@dataclass(frozen=True)
class RecoveryReport:
    interrupted: bool
    unfinished_jobs: tuple[str, ...]
    invalid_manifests: tuple[str, ...]
    stale_pids: tuple[str, ...]
    repaired_manifests: tuple[str, ...] = ()


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def begin_session(root: Path) -> RecoveryReport:
    marker = root / "storage" / "state" / "last_session.json"
    try:
        previous = json.loads(marker.read_text(encoding="utf-8"))
        interrupted = isinstance(previous, dict) and previous.get("clean_exit") is False
        if interrupted:
            try:
                previous_pid = int(previous.get("pid", 0))
                if previous_pid != os.getpid() and pid_is_alive(previous_pid):
                    raise RuntimeError("Alysia Studio 已在另一个进程中运行")
            except (OSError, ValueError):
                pass
    except (OSError, ValueError):
        interrupted = False

    unfinished: list[str] = []
    jobs = root / "storage" / "training_jobs"
    for path in sorted(jobs.glob("*.json")) if jobs.exists() else ():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("state") in {"RUNNING", "PAUSING", "FINISHING", "EXPORTING", "BUILDING_DATASET", "PAUSED"}:
                unfinished.append(str(value.get("job_id") or path.stem))
        except (OSError, ValueError, AttributeError):
            continue

    invalid: list[str] = []
    repaired: list[str] = []
    active = root / "model_store" / "active"
    for path in sorted(active.glob("*.json")) if active.exists() else ():
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            if not _manifest_valid(root, value):
                if _try_rollback(root, value):
                    repaired.append(path.name)
                else:
                    invalid.append(path.name)
        except (OSError, ValueError):
            invalid.append(path.name)

    stale: list[str] = []
    temporary = root / "storage" / "temporary"
    for name in ("qwen3-llama-server.pid", "gpt-sovits-desktop.pid"):
        path = temporary / name
        try:
            pid = int(path.read_text(encoding="utf-8-sig").strip())
            if not pid_is_alive(pid):
                raise ValueError("stale PID")
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            companions = (
                ("qwen3-selection.json",) if name.startswith("qwen3-")
                else ("gpt-sovits-upstream.character", "gpt-sovits-upstream.model")
            )
            for companion in companions:
                (temporary / companion).unlink(missing_ok=True)
            stale.append(name)

    _write_json(marker, {"pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat(), "clean_exit": False})
    return RecoveryReport(interrupted, tuple(unfinished), tuple(invalid), tuple(stale), tuple(repaired))


def _manifest_valid(root: Path, value: object) -> bool:
    if not isinstance(value, dict) or not value.get("model_type"):
        return False
    files = value.get("required_files")
    if not isinstance(files, list) or not files:
        return False
    for raw in files:
        if not isinstance(raw, str) or not raw:
            return False
        path = (root / raw).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            return False
    return True


def _try_rollback(root: Path, value: object) -> bool:
    # Registry APIs use project-relative storage, so only invoke them for the
    # actual repository. Temporary test roots must never mutate live models.
    if root.resolve() != Path(__file__).resolve().parents[2] or not isinstance(value, dict):
        return False
    character_id = str(value.get("character_id") or "")
    try:
        if value.get("model_type") == "qwen3" and character_id:
            from runtime.qwen3.model_registry import rollback_model

            rollback_model(character_id)
        elif value.get("model_type") == "gpt_sovits" and character_id:
            from runtime.gpt_sovits.model_registry import rollback_model

            rollback_model(character_id)
        else:
            return False
        return True
    except (OSError, ValueError, KeyError, RuntimeError):
        return False


def end_session(root: Path) -> None:
    marker = root / "storage" / "state" / "last_session.json"
    _write_json(marker, {"pid": os.getpid(), "ended_at": datetime.now(timezone.utc).isoformat(), "clean_exit": True})
