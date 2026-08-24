from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class DatasetConfig:
    raw: Path
    normalized: Path
    train: Path
    validation: Path
    validation_ratio: float = 0.1
    mode: str = "chat_jsonl"
    quotes: Path | None = None
    optional_profile: Path | None = None
    generated_dir: Path | None = None


@dataclass(frozen=True)
class TeacherConfig:
    """An OpenAI-compatible Teacher endpoint (local by default)."""

    provider: str = "openai_compatible"
    model: str = ""
    base_url: str = "http://127.0.0.1:8080/v1"
    api_key_env: str | None = None
    temperature: float = 0.7
    max_tokens: int = 1024


@dataclass(frozen=True)
class GenerationConfig:
    target_samples: int = 3000
    candidates_per_scene: int = 3
    max_regenerate: int = 2
    seed: int = 7
    keep_metadata: bool = True
    auto_balance: bool = True
    min_persona_score: float = 0.70
    min_relevance_score: float = 0.70
    min_naturalness_score: float = 0.65
    min_behavior_score: float = 0.65
    max_copy_score: float = 0.30
    max_assistant_like_penalty: float = 0.25
    max_catchphrase_penalty: float = 0.35
    max_near_duplicate_ratio: float = 0.92


@dataclass(frozen=True)
class BaseModelConfig:
    model_id: str
    gguf_path: Path
    hf_path: Path | None = None


@dataclass(frozen=True)
class TrainingConfig:
    character_id: str
    run_name: str
    dataset: DatasetConfig
    base_model: BaseModelConfig
    outputs_dir: Path
    # Exact user-facing name shown in the model selector after export.
    model_name: str = ""
    # Roles allowed to use the exported model.  The training character remains
    # the data/dataset owner and may differ from the selected target roles.
    assigned_character_ids: tuple[str, ...] = ()
    method: str = "qlora"
    resume_from: str = "auto"
    save_steps: int = 200
    save_total_limit: int = 5
    best_checkpoint_name: str = "best"
    max_steps: int = 1000
    learning_rate: float = 2e-4
    per_device_train_batch_size: int = 1
    gradient_accumulation_steps: int = 8
    lora_r: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.05
    epochs: float = 1.0
    sequence_length: int = 2048
    assistant_only_loss: bool = True
    seed: int = 7
    gradient_checkpointing: bool = True
    bf16: bool = False
    fp16: bool = True
    generator: TeacherConfig = TeacherConfig()
    judge: TeacherConfig = TeacherConfig()
    generation: GenerationConfig = GenerationConfig()

    @property
    def run_dir(self) -> Path:
        return self.outputs_dir / self.character_id / self.run_name


def load_training_config(path: str | Path) -> TrainingConfig:
    config_path = _project_path(path)
    raw = _load_simple_yaml(config_path)
    dataset = raw.get("dataset", {})
    data_source = raw.get("data_source", {})
    base_model = raw.get("base_model", {})
    training = raw.get("training", {})
    teacher = raw.get("teacher", {})
    generation = raw.get("generation", {})

    character_id = str(raw.get("character_id", "elysia"))
    run_name = str(raw.get("run_name", f"{character_id}-qwen3-lora"))
    assigned = _role_ids(raw.get("assigned_character_ids"), character_id)
    return TrainingConfig(
        character_id=character_id,
        run_name=run_name,
        model_name=str(raw.get("model_name") or "").strip(),
        assigned_character_ids=assigned,
        dataset=DatasetConfig(
            raw=_project_path(dataset.get("raw", data_source.get("quotes", f"training/qwen3/datasets/{character_id}/raw/{character_id}.jsonl"))),
            normalized=_project_path(dataset.get("normalized", f"training/qwen3/datasets/{character_id}/normalized.jsonl")),
            train=_project_path(dataset.get("train", f"training/qwen3/datasets/{character_id}/train.jsonl")),
            validation=_project_path(dataset.get("validation", f"training/qwen3/datasets/{character_id}/validation.jsonl")),
            validation_ratio=float(dataset.get("validation_ratio", 0.1)),
            mode=str(data_source.get("mode", dataset.get("mode", "chat_jsonl"))),
            quotes=_optional_project_path(data_source.get("quotes")),
            optional_profile=_optional_project_path(data_source.get("optional_profile")),
            generated_dir=_optional_project_path(dataset.get("generated_dir", f"training/qwen3/datasets/{character_id}/generated")),
        ),
        base_model=BaseModelConfig(
            model_id=str(base_model.get("model_id", "qwen3-8b-q4-k-m-base-v1")),
            gguf_path=_project_path(base_model.get("gguf_path", "model_store/registry/qwen3/elysia/qwen3-8b-q4-k-m-base-v1/Qwen3-8B-Q4_K_M.gguf")),
            hf_path=_optional_project_path(base_model.get("hf_path")),
        ),
        outputs_dir=_project_path(raw.get("outputs_dir", "training/qwen3/outputs")),
        method=str(training.get("method", "qlora")),
        resume_from=str(training.get("resume_from", "auto")),
        save_steps=int(training.get("save_steps", 200)),
        save_total_limit=int(training.get("save_total_limit", 5)),
        best_checkpoint_name=str(training.get("best_checkpoint_name", "best")),
        max_steps=int(training.get("max_steps", 1000)),
        learning_rate=float(training.get("learning_rate", 2e-4)),
        per_device_train_batch_size=int(training.get("per_device_train_batch_size", 1)),
        gradient_accumulation_steps=int(training.get("gradient_accumulation_steps", 8)),
        lora_r=int(training.get("lora_r", 16)),
        lora_alpha=int(training.get("lora_alpha", 32)),
        lora_dropout=float(training.get("lora_dropout", 0.05)),
        epochs=float(training.get("epochs", 1.0)),
        sequence_length=int(training.get("sequence_length", 2048)),
        assistant_only_loss=bool(training.get("assistant_only_loss", True)),
        seed=int(training.get("seed", 7)),
        gradient_checkpointing=bool(training.get("gradient_checkpointing", True)),
        bf16=bool(training.get("bf16", False)),
        fp16=bool(training.get("fp16", True)),
        generator=_teacher_config(teacher, "generator"),
        judge=_teacher_config(teacher, "judge"),
        generation=GenerationConfig(
            target_samples=int(generation.get("target_samples", 3000)),
            candidates_per_scene=int(teacher.get("candidates_per_scene", generation.get("candidates_per_scene", 3))),
            max_regenerate=int(teacher.get("max_regenerate", generation.get("max_regenerate", 2))),
            seed=int(generation.get("seed", training.get("seed", 7))),
            keep_metadata=bool(generation.get("keep_metadata", True)),
            auto_balance=bool(generation.get("auto_balance", True)),
            min_persona_score=float(generation.get("min_persona_score", 0.70)),
            min_relevance_score=float(generation.get("min_relevance_score", 0.70)),
            min_naturalness_score=float(generation.get("min_naturalness_score", 0.65)),
            min_behavior_score=float(generation.get("min_behavior_score", 0.65)),
            max_copy_score=float(generation.get("max_copy_score", 0.30)),
            max_assistant_like_penalty=float(generation.get("max_assistant_like_penalty", 0.25)),
            max_catchphrase_penalty=float(generation.get("max_catchphrase_penalty", 0.35)),
            max_near_duplicate_ratio=float(generation.get("max_near_duplicate_ratio", 0.92)),
        ),
    )


def config_hash(path: str | Path) -> str:
    return file_sha256(_project_path(path))


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with _project_path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dataset_hash(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.as_posix()):
        digest.update(path.as_posix().encode("utf-8"))
        digest.update(file_sha256(path).encode("ascii"))
    return digest.hexdigest()


def trained_model_identity(config: TrainingConfig) -> tuple[str, str]:
    """Return the stable registry id and visible name for a trained adapter."""
    custom_name = config.model_name.strip()
    if not custom_name:
        model_id = f"{config.character_id}-{config.run_name}-final"
        return model_id, f"{config.character_id} Qwen3 LoRA final"

    # The visible name may be Chinese or contain punctuation; the registry
    # folder/id must remain filesystem-safe. The digest keeps such names unique.
    slug = re.sub(r"[^a-z0-9]+", "-", custom_name.lower()).strip("-") or "custom-model"
    digest = hashlib.sha256(custom_name.encode("utf-8")).hexdigest()[:8]
    return f"{config.character_id}-{slug}-{digest}-final", custom_name


def _project_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return PROJECT_ROOT / candidate


def _optional_project_path(value: Any) -> Path | None:
    if value in {None, "", "null", "~"}:
        return None
    return _project_path(str(value))


def _teacher_config(raw: dict[str, Any], name: str) -> TeacherConfig:
    """Read a named Teacher while retaining the concise legacy config form."""
    value = raw.get(name, {})
    values = value if isinstance(value, dict) else {"model": value}
    return TeacherConfig(
        provider=str(values.get("provider", raw.get("provider", "openai_compatible"))),
        model=str(values.get("model", "")),
        base_url=str(values.get("base_url", raw.get("base_url", "http://127.0.0.1:8080/v1"))).rstrip("/"),
        api_key_env=_none_or_string(values.get("api_key_env", raw.get("api_key_env"))),
        temperature=float(values.get("temperature", raw.get("temperature", 0.7))),
        max_tokens=int(values.get("max_tokens", raw.get("max_tokens", 1024))),
    )


def _none_or_string(value: Any) -> str | None:
    return None if value in {None, "", "null", "~"} else str(value)


def _role_ids(value: Any, default: str) -> tuple[str, ...]:
    """Read the compact comma-separated YAML scalar written by the desktop UI."""
    if isinstance(value, (list, tuple)):
        values = value
    else:
        values = str(value or "").replace("，", ",").split(",")
    ids = tuple(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))
    return ids or (default,)


def _load_simple_yaml(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, data)]
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        line = raw_line.split("#", 1)[0].rstrip()
        indent = len(line) - len(line.lstrip(" "))
        if ":" not in line:
            continue
        key, raw_value = line.strip().split(":", 1)
        stripped_value = raw_value.strip()
        value = _parse_scalar(stripped_value)
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if stripped_value == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = value
    return data


def load_raw_config(path: str | Path) -> dict[str, Any]:
    """Load the project-supported YAML subset for generation-only settings.

    Character configs intentionally remain plain YAML so they can be edited by
    hand and consumed by both the command line and the desktop application.
    """
    return _load_simple_yaml(_project_path(path))


def _parse_scalar(value: str) -> Any:
    if value in {"", "null", "None", "~"}:
        return None
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.startswith(("'", '"')) and value.endswith(("'", '"')):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value
