from __future__ import annotations

import json
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from runtime.core.model_versions import activate_version, deletable_model_files, protected_ids, validate_files, validate_identity


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_ROOT = PROJECT_ROOT / "model_store" / "registry" / "qwen3"
ACTIVE_MANIFEST = PROJECT_ROOT / "model_store" / "active" / "qwen3.json"
TRAINED_MODEL_LIMIT = 5


@dataclass(frozen=True)
class Qwen3ModelEntry:
    character_id: str
    model_id: str
    display_name: str
    kind: str
    model_format: str
    required_files: tuple[Path, ...]
    created_at: str
    base_model_id: str | None = None
    data_hash: str | None = None
    config_hash: str | None = None
    source_run: str | None = None
    export_log: str | None = None
    pinned: bool = False
    status: str = "registered"

    @property
    def model_file(self) -> Path:
        if self.model_format == "lora":
            base = find_model(self.character_id, self.base_model_id or "")
            return base.model_file
        return self.required_files[0]

    @property
    def lora_adapter(self) -> Path | None:
        if self.model_format != "lora":
            return None
        for path in self.required_files:
            if path.suffix.lower() in {".bin", ".safetensors"}:
                return path
        return self.required_files[-1]

    def to_manifest(self) -> dict[str, Any]:
        manifest: dict[str, Any] = {
            "model_type": "qwen3",
            "character_id": self.character_id,
            "model_id": self.model_id,
            "version": self.model_id,
            "model_format": self.model_format,
            "required_files": [_relative_path(path) for path in self.required_files],
            "registry_manifest": _relative_path(character_manifest_path(self.character_id)),
            "runtime": {
                "model_file": _relative_path(self.model_file),
            },
        }
        adapter = self.lora_adapter
        if adapter is not None:
            manifest["runtime"]["lora_adapter"] = _relative_path(adapter)
            manifest["base_model_id"] = self.base_model_id
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


def list_characters() -> list[str]:
    ids: set[str] = set()
    character_config_root = PROJECT_ROOT / "configs" / "characters"
    if character_config_root.exists():
        ids.update(path.name for path in character_config_root.iterdir() if path.is_dir())
    if REGISTRY_ROOT.exists():
        ids.update(path.name for path in REGISTRY_ROOT.iterdir() if path.is_dir())
    return sorted(ids)


def list_models(character_id: str) -> list[Qwen3ModelEntry]:
    data = load_character_manifest(character_id)
    return [_entry_from_raw(character_id, raw) for raw in data.get("models", [])]


def find_model(character_id: str, model_id: str | None) -> Qwen3ModelEntry:
    models = list_models(character_id)
    if not model_id:
        active_id = load_character_manifest(character_id).get("active_model_id")
        model_id = str(active_id or "")
    for model in models:
        if model.model_id == model_id:
            return model
    available = ", ".join(model.model_id for model in models) or "<none>"
    raise ValueError(f"Qwen3 model '{model_id}' for character '{character_id}' was not found. Available: {available}")


def register_model(
    *,
    character_id: str,
    model_id: str,
    display_name: str,
    kind: str,
    model_format: str,
    required_files: list[str | Path],
    base_model_id: str | None = None,
    data_hash: str | None = None,
    config_hash: str | None = None,
    source_run: str | None = None,
    export_log: str | None = None,
    activate: bool = False,
    trained_limit: int = TRAINED_MODEL_LIMIT,
) -> Qwen3ModelEntry:
    if model_format not in {"gguf", "lora"}:
        raise ValueError("model_format must be 'gguf' or 'lora'")
    if kind not in {"base", "trained"}:
        raise ValueError("kind must be 'base' or 'trained'")

    resolved_files = tuple(_project_path(path) for path in required_files)
    missing = [path for path in resolved_files if not path.exists()]
    if missing:
        raise FileNotFoundError("Qwen3 model file(s) missing: " + ", ".join(str(path) for path in missing))

    data = load_character_manifest(character_id)
    models = [raw for raw in data.get("models", []) if raw.get("id") != model_id]
    raw_entry = {
        "id": model_id,
        "display_name": display_name,
        "kind": kind,
        "format": model_format,
        "required_files": [_relative_path(path) for path in resolved_files],
        "created_at": utc_now(),
        "status": "registered",
    }
    if base_model_id:
        raw_entry["base_model_id"] = base_model_id
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
    if not activate and not data.get("active_model_id"):
        data["active_model_id"] = model_id
    save_character_manifest(character_id, data)
    # Cleanup is deferred until the desktop can show the exact deletion plan.
    if activate:
        return activate_model(character_id, model_id)
    return find_model(character_id, model_id)


def activate_model(character_id: str, model_id: str, active_manifest: Path | None = None,
                   smoke_test: Callable[[Qwen3ModelEntry], None] | None = None) -> Qwen3ModelEntry:
    validate_identity(character_id, model_id)
    active_manifest = active_manifest or ACTIVE_MANIFEST
    model = find_model(character_id, model_id)
    validate_files(model.required_files)

    data = load_character_manifest(character_id)
    def check() -> None:
        validate_files(model.required_files)
        if smoke_test is not None:
            smoke_test(model)
    activate_version(character_manifest_path(character_id), active_manifest, data,
                     model_id, model.to_manifest(), utc_now(), check, validated=smoke_test is not None)
    return model


def rollback_model(character_id: str, *, smoke_test: Callable[[Qwen3ModelEntry], None] | None = None) -> Qwen3ModelEntry:
    previous = str(load_character_manifest(character_id).get("previous_model_id") or "")
    if not previous:
        raise ValueError("没有可回滚的上一版本")
    return activate_model(character_id, previous, smoke_test=smoke_test)


def resolve_model(character_id: str | None = None, model_id: str | None = None) -> Qwen3ModelEntry:
    if not character_id:
        active = _load_active_manifest()
        character_id = str(active.get("character_id") or "elysia")
        if not model_id:
            model_id = str(active.get("model_id") or "")
    return find_model(character_id, model_id)


def ensure_default_elysia_base() -> Qwen3ModelEntry:
    character_id = "elysia"
    model_id = "qwen3-8b-q4-k-m-base-v1"
    new_path = REGISTRY_ROOT / character_id / model_id / "Qwen3-8B-Q4_K_M.gguf"
    legacy_path = REGISTRY_ROOT / "qwen3-8b" / "q4-k-m-base-v1" / "Qwen3-8B-Q4_K_M.gguf"
    if not new_path.exists() and legacy_path.exists():
        new_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy_path), str(new_path))
    return register_model(
        character_id=character_id,
        model_id=model_id,
        display_name="Qwen3-8B Q4_K_M base",
        kind="base",
        model_format="gguf",
        required_files=[new_path],
        activate=True,
    )


def plan_prune_trained_models(character_id: str, keep: int = TRAINED_MODEL_LIMIT) -> list[Qwen3ModelEntry]:
    data = load_character_manifest(character_id)
    trained = [raw for raw in data.get("models", []) if raw.get("kind") == "trained"]
    trained.sort(key=lambda raw: str(raw.get("created_at", "")), reverse=True)
    retained = {str(raw.get("id")) for raw in trained[:keep]} | protected_ids(data)
    return [_entry_from_raw(character_id, raw) for raw in trained if str(raw.get("id")) not in retained]


def plan_prune_files(character_id: str, keep: int = TRAINED_MODEL_LIMIT) -> list[Path]:
    candidates = plan_prune_trained_models(character_id, keep)
    removed = {entry.model_id for entry in candidates}
    referenced = {path.resolve() for other in list_characters() for entry in list_models(other)
                  if other != character_id or entry.model_id not in removed
                  for path in entry.required_files}
    return deletable_model_files(REGISTRY_ROOT, character_id, candidates, referenced)


def prune_trained_models(character_id: str, keep: int = TRAINED_MODEL_LIMIT) -> None:
    data = load_character_manifest(character_id)
    to_remove = plan_prune_trained_models(character_id, keep)
    if not to_remove:
        return
    files_to_delete = plan_prune_files(character_id, keep)

    remove_ids = {entry.model_id for entry in to_remove}
    data["models"] = [raw for raw in data.get("models", []) if str(raw.get("id")) not in remove_ids]
    save_character_manifest(character_id, data)
    for path in files_to_delete:
        path.unlink(missing_ok=True)
    for directory in sorted({path.parent for path in files_to_delete}, key=lambda item: len(item.parts), reverse=True):
        while directory != REGISTRY_ROOT and directory.exists():
            try:
                directory.rmdir()
            except OSError:
                break
            directory = directory.parent


def choose_interactively(character_id: str | None = None) -> Qwen3ModelEntry:
    if not character_id:
        characters = list_characters()
        if not characters:
            raise ValueError("No Qwen3 characters are available.")
        character_id = _prompt_choice("Character", characters, [item[:1].upper() + item[1:] for item in characters])
    models = list_models(character_id)
    if not models:
        raise ValueError(f"No Qwen3 models are registered for character '{character_id}'.")
    model_id = _prompt_choice("Model", [model.model_id for model in models], [_model_choice_label(model) for model in models])
    return find_model(character_id, model_id)


def _prompt_choice(label: str, choices: list[str], display_choices: list[str] | None = None) -> str:
    display_label = {"Character": "角色", "Model": "模型"}.get(label, label)
    display_choices = display_choices or choices
    print(f"选择{display_label}:", file=sys.stderr)
    for index, choice in enumerate(display_choices, start=1):
        print(f"  {index}. {choice}", file=sys.stderr)
    while True:
        print(f"{display_label} [1-{len(choices)}]> ", end="", file=sys.stderr, flush=True)
        raw = input().strip()
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return choices[int(raw) - 1]
        if raw in choices:
            return raw
        print("输入无效，请输入编号。", file=sys.stderr)


def _model_choice_label(model: Qwen3ModelEntry) -> str:
    if model.model_format == "lora":
        return f"lora - {model.display_name} ({model.model_id})"
    return f"base - {model.display_name} ({model.model_id})"


def _entry_from_raw(character_id: str, raw: dict[str, Any]) -> Qwen3ModelEntry:
    required_files = tuple(_project_path(path) for path in raw.get("required_files", []))
    if not required_files:
        path = raw.get("path")
        if path:
            required_files = (_project_path(path),)
    return Qwen3ModelEntry(
        character_id=character_id,
        model_id=str(raw["id"]),
        display_name=str(raw.get("display_name") or raw["id"]),
        kind=str(raw.get("kind") or "trained"),
        model_format=str(raw.get("format") or "gguf"),
        required_files=required_files,
        created_at=str(raw.get("created_at") or ""),
        base_model_id=str(raw["base_model_id"]) if raw.get("base_model_id") else None,
        data_hash=str(raw["data_hash"]) if raw.get("data_hash") else None,
        config_hash=str(raw["config_hash"]) if raw.get("config_hash") else None,
        source_run=str(raw["source_run"]) if raw.get("source_run") else None,
        export_log=str(raw["export_log"]) if raw.get("export_log") else None,
        pinned=bool(raw.get("pinned", False)),
        status=str(raw.get("status") or "registered"),
    )


def _load_active_manifest() -> dict[str, Any]:
    if not ACTIVE_MANIFEST.exists():
        return {}
    return json.loads(ACTIVE_MANIFEST.read_text(encoding="utf-8-sig"))


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
