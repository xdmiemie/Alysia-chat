from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_ROOT = PROJECT_ROOT / "model_store" / "registry" / "gpt_sovits"
ACTIVE_ROOT = PROJECT_ROOT / "model_store" / "active"
TRAINED_MODEL_LIMIT = 5


@dataclass(frozen=True)
class GPTSoVITSModelEntry:
    character_id: str
    model_id: str
    display_name: str
    kind: str
    required_files: tuple[Path, ...]
    created_at: str
    data_hash: str | None = None
    config_hash: str | None = None
    source_run: str | None = None
    export_log: str | None = None

    @property
    def gpt_weight(self) -> Path:
        for path in self.required_files:
            if path.suffix.lower() == ".ckpt":
                return path
        raise ValueError(f"GPT-SoVITS model '{self.model_id}' has no .ckpt file")

    @property
    def sovits_weight(self) -> Path:
        for path in self.required_files:
            if path.suffix.lower() == ".pth":
                return path
        raise ValueError(f"GPT-SoVITS model '{self.model_id}' has no .pth file")

    @property
    def reference_files(self) -> tuple[Path, ...]:
        return tuple(path for path in self.required_files if path.suffix.lower() in {".wav", ".flac", ".mp3", ".m4a"})

    def to_manifest(self) -> dict[str, Any]:
        manifest: dict[str, Any] = {
            "model_type": "gpt_sovits",
            "character_id": self.character_id,
            "model_id": self.model_id,
            "version": self.model_id,
            "required_files": [_relative_path(path) for path in self.required_files],
            "registry_manifest": _relative_path(character_manifest_path(self.character_id)),
            "runtime": {
                "gpt_weight": _relative_path(self.gpt_weight),
                "sovits_weight": _relative_path(self.sovits_weight),
                "reference_dir": _relative_path(self.reference_files[0].parent) if self.reference_files else None,
            },
        }
        if self.data_hash:
            manifest["data_hash"] = self.data_hash
        if self.config_hash:
            manifest["config_hash"] = self.config_hash
        if self.source_run:
            manifest["source_run"] = self.source_run
        if self.export_log:
            manifest["export_log"] = self.export_log
        return manifest


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def active_manifest_path(character_id: str) -> Path:
    return ACTIVE_ROOT / f"gpt_sovits_{character_id}.json"


def character_manifest_path(character_id: str) -> Path:
    return REGISTRY_ROOT / character_id / "models.json"


def load_character_manifest(character_id: str) -> dict[str, Any]:
    path = character_manifest_path(character_id)
    if not path.exists():
        return {"character_id": character_id, "active_model_id": None, "models": []}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    models = data.get("models")
    if not isinstance(models, list):
        raise ValueError(f"{path} field 'models' must be a list")
    return data


def save_character_manifest(character_id: str, data: dict[str, Any]) -> None:
    data["character_id"] = character_id
    data.setdefault("models", [])
    _atomic_write_json(character_manifest_path(character_id), data)


def list_models(character_id: str) -> list[GPTSoVITSModelEntry]:
    data = load_character_manifest(character_id)
    return [_entry_from_raw(character_id, raw) for raw in data.get("models", [])]


def find_model(character_id: str, model_id: str | None) -> GPTSoVITSModelEntry:
    models = list_models(character_id)
    if not model_id:
        active_id = load_character_manifest(character_id).get("active_model_id")
        model_id = str(active_id or "")
    for model in models:
        if model.model_id == model_id:
            return model
    available = ", ".join(model.model_id for model in models) or "<none>"
    raise ValueError(f"GPT-SoVITS model '{model_id}' for character '{character_id}' was not found. Available: {available}")


def register_model(
    *,
    character_id: str,
    model_id: str,
    display_name: str,
    kind: str,
    required_files: list[str | Path],
    data_hash: str | None = None,
    config_hash: str | None = None,
    source_run: str | None = None,
    export_log: str | None = None,
    activate: bool = False,
    trained_limit: int = TRAINED_MODEL_LIMIT,
) -> GPTSoVITSModelEntry:
    if kind not in {"base", "trained"}:
        raise ValueError("kind must be 'base' or 'trained'")

    resolved_files = tuple(_project_path(path) for path in required_files)
    missing = [path for path in resolved_files if not path.exists()]
    if missing:
        raise FileNotFoundError("GPT-SoVITS model file(s) missing: " + ", ".join(str(path) for path in missing))
    suffixes = {path.suffix.lower() for path in resolved_files}
    if ".ckpt" not in suffixes or ".pth" not in suffixes:
        raise ValueError("GPT-SoVITS model registration requires one .ckpt and one .pth file")

    data = load_character_manifest(character_id)
    models = [raw for raw in data.get("models", []) if raw.get("id") != model_id]
    raw_entry = {
        "id": model_id,
        "display_name": display_name,
        "kind": kind,
        "required_files": [_relative_path(path) for path in resolved_files],
        "created_at": utc_now(),
    }
    if data_hash:
        raw_entry["data_hash"] = data_hash
    if config_hash:
        raw_entry["config_hash"] = config_hash
    if source_run:
        raw_entry["source_run"] = source_run
    if export_log:
        raw_entry["export_log"] = export_log

    models.append(raw_entry)
    data["models"] = sorted(models, key=lambda item: (str(item.get("kind")) != "base", str(item.get("created_at", ""))))
    if activate or not data.get("active_model_id"):
        data["active_model_id"] = model_id
    save_character_manifest(character_id, data)
    prune_trained_models(character_id, keep=trained_limit)
    if activate:
        return activate_model(character_id, model_id)
    return find_model(character_id, model_id)


def activate_model(character_id: str, model_id: str, active_manifest: Path | None = None) -> GPTSoVITSModelEntry:
    model = find_model(character_id, model_id)
    missing = [path for path in model.required_files if not path.exists()]
    if missing:
        raise FileNotFoundError("GPT-SoVITS model file(s) missing: " + ", ".join(str(path) for path in missing))

    data = load_character_manifest(character_id)
    data["active_model_id"] = model_id
    data["activated_at"] = utc_now()
    save_character_manifest(character_id, data)

    manifest = model.to_manifest()
    manifest["activated_at"] = data["activated_at"]
    _atomic_write_json(active_manifest or active_manifest_path(character_id), manifest)
    return model


def prune_trained_models(character_id: str, keep: int = TRAINED_MODEL_LIMIT) -> None:
    data = load_character_manifest(character_id)
    trained = [raw for raw in data.get("models", []) if raw.get("kind") == "trained"]
    trained.sort(key=lambda raw: str(raw.get("created_at", "")), reverse=True)
    to_remove = trained[keep:]
    if not to_remove:
        return

    remove_ids = {str(raw.get("id")) for raw in to_remove}
    data["models"] = [raw for raw in data.get("models", []) if str(raw.get("id")) not in remove_ids]
    active_was_pruned = data.get("active_model_id") in remove_ids
    if active_was_pruned:
        data["active_model_id"] = next(
            (raw.get("id") for raw in data["models"] if raw.get("kind") == "base"),
            data["models"][0].get("id") if data["models"] else None,
        )
    save_character_manifest(character_id, data)
    if active_was_pruned and data.get("active_model_id"):
        activate_model(character_id, str(data["active_model_id"]))

    character_root = (REGISTRY_ROOT / character_id).resolve()
    for raw in to_remove:
        for required in raw.get("required_files", []):
            model_dir = _project_path(required).resolve().parent
            if character_root in model_dir.parents and model_dir.name == str(raw.get("id")):
                shutil.rmtree(model_dir, ignore_errors=True)


def _entry_from_raw(character_id: str, raw: dict[str, Any]) -> GPTSoVITSModelEntry:
    required_files = tuple(_project_path(path) for path in raw.get("required_files", []))
    return GPTSoVITSModelEntry(
        character_id=character_id,
        model_id=str(raw["id"]),
        display_name=str(raw.get("display_name") or raw["id"]),
        kind=str(raw.get("kind") or "trained"),
        required_files=required_files,
        created_at=str(raw.get("created_at") or ""),
        data_hash=str(raw["data_hash"]) if raw.get("data_hash") else None,
        config_hash=str(raw["config_hash"]) if raw.get("config_hash") else None,
        source_run=str(raw["source_run"]) if raw.get("source_run") else None,
        export_log=str(raw["export_log"]) if raw.get("export_log") else None,
    )


def _project_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return PROJECT_ROOT / candidate


def _relative_path(path: str | Path) -> str:
    resolved = _project_path(path).resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent, suffix=".tmp") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.replace(path)
