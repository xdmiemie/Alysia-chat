"""Shared, filesystem-only helpers for versioned model manifests."""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any, Callable


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent, suffix=".tmp") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def restore_json(path: Path, original: bytes | None) -> None:
    if original is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", delete=False, dir=path.parent, suffix=".tmp") as handle:
        handle.write(original)
        temporary = Path(handle.name)
    temporary.replace(path)


def validate_files(paths: tuple[Path, ...]) -> None:
    if not paths:
        raise ValueError("模型没有必需文件")
    for path in paths:
        if not path.is_file() or path.stat().st_size == 0:
            raise FileNotFoundError(f"模型文件缺失或为空：{path}")


def validate_identity(character_id: str, model_id: str) -> None:
    pattern = r"[a-z][a-z0-9_-]{0,127}"
    if not re.fullmatch(pattern, character_id) or not re.fullmatch(pattern, model_id):
        raise ValueError("模型 ID 或角色 ID 不合法")


def protected_ids(manifest: dict[str, Any]) -> set[str]:
    ids = {str(manifest.get("active_model_id") or ""), str(manifest.get("previous_model_id") or "")}
    ids.update(str(item.get("id")) for item in manifest.get("models", []) if item.get("pinned"))
    return ids - {""}


def deletable_model_files(registry_root: Path, character_id: str, candidates: list[Any],
                          referenced: set[Path]) -> list[Path]:
    character_root = (registry_root / character_id).resolve()
    directories: set[Path] = set()
    for entry in candidates:
        for required in entry.required_files:
            directory = required.resolve().parent
            if character_root in directory.parents and directory.name == entry.model_id:
                directories.add(directory)
    files: set[Path] = set()
    for directory in directories:
        if any(path == directory or directory in path.parents for path in referenced):
            continue
        files.update(path for path in directory.rglob("*") if path.is_file())
    return sorted(files)


def activate_version(
    registry_path: Path,
    active_path: Path,
    registry: dict[str, Any],
    model_id: str,
    active_payload: dict[str, Any],
    activated_at: str,
    check: Callable[[], None],
    *,
    validated: bool = False,
) -> None:
    """Validate before writing, then restore both manifests on a write failure."""
    check()
    previous = str(registry.get("active_model_id") or "")
    updated = dict(registry)
    updated["active_model_id"] = model_id
    updated["activated_at"] = activated_at
    updated["models"] = [
        {**raw, "status": "validated" if validated or raw.get("status") == "validated" else "active"}
        if str(raw.get("id")) == model_id else raw
        for raw in registry.get("models", [])
    ]
    if previous and previous != model_id:
        updated["previous_model_id"] = previous
        history = list(updated.get("activation_history") or [])
        history.append({"from": previous, "to": model_id, "at": activated_at})
        updated["activation_history"] = history[-100:]
    payload = {**active_payload, "activated_at": activated_at}
    old_registry = registry_path.read_bytes() if registry_path.exists() else None
    old_active = active_path.read_bytes() if active_path.exists() else None
    try:
        atomic_json(active_path, payload)
        atomic_json(registry_path, updated)
    except Exception:
        restore_json(active_path, old_active)
        restore_json(registry_path, old_registry)
        raise
