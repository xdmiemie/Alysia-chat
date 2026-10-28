from __future__ import annotations

import hashlib
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

    @property
    def run_dir(self) -> Path:
        return self.outputs_dir / self.character_id / self.run_name


def load_training_config(path: str | Path) -> TrainingConfig:
    config_path = _project_path(path)
    raw = _load_simple_yaml(config_path)
    dataset = raw.get("dataset", {})
    base_model = raw.get("base_model", {})
    training = raw.get("training", {})

    character_id = str(raw.get("character_id", "elysia"))
    run_name = str(raw.get("run_name", f"{character_id}-qwen3-lora"))
    return TrainingConfig(
        character_id=character_id,
        run_name=run_name,
        dataset=DatasetConfig(
            raw=_project_path(dataset.get("raw", f"training/qwen3/datasets/{character_id}/raw/{character_id}.jsonl")),
            normalized=_project_path(dataset.get("normalized", f"training/qwen3/datasets/{character_id}/normalized.jsonl")),
            train=_project_path(dataset.get("train", f"training/qwen3/datasets/{character_id}/train.jsonl")),
            validation=_project_path(dataset.get("validation", f"training/qwen3/datasets/{character_id}/validation.jsonl")),
            validation_ratio=float(dataset.get("validation_ratio", 0.1)),
        ),
        base_model=BaseModelConfig(
            model_id=str(base_model.get("model_id", "qwen3-14b-q4-k-m-base-v1")),
            gguf_path=_project_path(base_model.get("gguf_path", "model_store/registry/qwen3/elysia/qwen3-14b-q4-k-m-base-v1/Qwen3-14B-Q4_K_M.gguf")),
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


def _project_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return PROJECT_ROOT / candidate


def _optional_project_path(value: Any) -> Path | None:
    if value in {None, "", "null", "~"}:
        return None
    return _project_path(str(value))


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
