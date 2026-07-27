from __future__ import annotations

import hashlib
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class DatasetConfig:
    root: Path
    raw_audio_dir: Path
    raw_manifest: Path
    normalized_manifest: Path
    validation_report: Path
    min_duration_seconds: float = 1.0
    max_duration_seconds: float = 12.0
    target_sample_rate: int = 32000
    allowed_extensions: tuple[str, ...] = (".wav", ".flac", ".mp3", ".m4a")


@dataclass(frozen=True)
class BaseModelConfig:
    model_id: str
    pretrained_root: Path
    s1_checkpoint: Path
    s2_checkpoint: Path


@dataclass(frozen=True)
class StageConfig:
    enabled: bool
    command: str
    max_steps: int
    save_steps: int
    save_total_limit: int
    best_checkpoint_name: str


@dataclass(frozen=True)
class ExportConfig:
    registry_root: Path
    model_id_template: str
    references_emotion: str = "neutral"


@dataclass(frozen=True)
class TrainingConfig:
    character_id: str
    run_name: str
    speaker: str
    language: str
    outputs_dir: Path
    resume_from: str
    seed: int
    dataset: DatasetConfig
    base_model: BaseModelConfig
    s1: StageConfig
    s2: StageConfig
    export: ExportConfig

    @property
    def run_dir(self) -> Path:
        return self.outputs_dir / self.character_id / self.run_name


def load_training_config(path: str | Path) -> TrainingConfig:
    config_path = _project_path(path)
    raw = _load_simple_yaml(config_path)
    character_id = str(raw.get("character_id", "elysia"))
    dataset_raw = raw.get("dataset", {})
    base_raw = raw.get("base_model", {})
    s1_raw = raw.get("s1", {})
    s2_raw = raw.get("s2", {})
    export_raw = raw.get("export", {})

    dataset_root = _project_path(dataset_raw.get("root", f"training/gpt_sovits/datasets/{character_id}"))
    run_name = str(raw.get("run_name", f"{character_id}-gpt-sovits"))
    return TrainingConfig(
        character_id=character_id,
        run_name=run_name,
        speaker=str(raw.get("speaker", character_id)),
        language=str(raw.get("language", "zh")),
        outputs_dir=_project_path(raw.get("outputs_dir", "training/gpt_sovits/outputs")),
        resume_from=str(raw.get("resume_from", "auto")),
        seed=int(raw.get("seed", 20260727)),
        dataset=DatasetConfig(
            root=dataset_root,
            raw_audio_dir=_project_path(dataset_raw.get("raw_audio_dir", dataset_root / "raw")),
            raw_manifest=_project_path(dataset_raw.get("raw_manifest", dataset_root / "work" / "manifest.jsonl")),
            normalized_manifest=_project_path(
                dataset_raw.get("normalized_manifest", dataset_root / "work" / "manifest.normalized.jsonl")
            ),
            validation_report=_project_path(
                dataset_raw.get("validation_report", dataset_root / "work" / "validation_report.json")
            ),
            min_duration_seconds=float(dataset_raw.get("min_duration_seconds", 1.0)),
            max_duration_seconds=float(dataset_raw.get("max_duration_seconds", 12.0)),
            target_sample_rate=int(dataset_raw.get("target_sample_rate", 32000)),
            allowed_extensions=_split_extensions(str(dataset_raw.get("allowed_extensions", ".wav,.flac,.mp3,.m4a"))),
        ),
        base_model=BaseModelConfig(
            model_id=str(base_raw.get("model_id", "gpt-sovits-pretrained-v2")),
            pretrained_root=_project_path(base_raw.get("pretrained_root", "third_party/GPT-SoVITS/GPT_SoVITS/pretrained_models")),
            s1_checkpoint=_project_path(
                base_raw.get("s1_checkpoint", "third_party/GPT-SoVITS/GPT_SoVITS/pretrained_models/s1bert25hz-2kh-longer-epoch=68e-step=50232.ckpt")
            ),
            s2_checkpoint=_project_path(
                base_raw.get("s2_checkpoint", "third_party/GPT-SoVITS/GPT_SoVITS/pretrained_models/s2G488k.pth")
            ),
        ),
        s1=_stage_config(s1_raw, default_command=""),
        s2=_stage_config(s2_raw, default_command=""),
        export=ExportConfig(
            registry_root=_project_path(export_raw.get("registry_root", "model_store/registry/gpt_sovits")),
            model_id_template=str(export_raw.get("model_id_template", "{character}-gpt-sovits-{timestamp}")),
            references_emotion=str(export_raw.get("references_emotion", "neutral")),
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


def stable_hash(values: list[str]) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def jsonable_config(config: TrainingConfig) -> dict[str, Any]:
    return _stringify_paths(asdict(config))


def _stage_config(raw: dict[str, Any], *, default_command: str) -> StageConfig:
    return StageConfig(
        enabled=bool(raw.get("enabled", True)),
        command=str(raw.get("command", default_command)),
        max_steps=int(raw.get("max_steps", 1000)),
        save_steps=int(raw.get("save_steps", 200)),
        save_total_limit=int(raw.get("save_total_limit", 5)),
        best_checkpoint_name=str(raw.get("best_checkpoint_name", "best")),
    )


def _project_path(path: str | Path) -> Path:
    candidate = path if isinstance(path, Path) else Path(str(path))
    if candidate.is_absolute():
        return candidate
    return PROJECT_ROOT / candidate


def _split_extensions(value: str) -> tuple[str, ...]:
    return tuple(item.strip().lower() for item in value.split(",") if item.strip())


def _stringify_paths(value: Any) -> Any:
    if isinstance(value, Path):
        return _relative(value)
    if isinstance(value, dict):
        return {key: _stringify_paths(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_stringify_paths(item) for item in value]
    if isinstance(value, tuple):
        return [_stringify_paths(item) for item in value]
    return value


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


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
