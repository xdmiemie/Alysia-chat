from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from math import ceil
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from training.gpt_sovits.config import TrainingConfig, file_sha256, load_training_config
from training.gpt_sovits.preprocess.dataset_manifest import DatasetValidation, validate_dataset


UPSTREAM_ROOT = PROJECT_ROOT / "third_party" / "GPT-SoVITS"
GPT_SOVITS_ROOT = UPSTREAM_ROOT / "GPT_SoVITS"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate and run the Alysia GPT-SoVITS upstream recipe.")
    parser.add_argument("--stage", choices=("s1", "s2"), required=True)
    parser.add_argument("--config", default="")
    parser.add_argument("--dry-config", action="store_true", help="Generate recipe files without upstream execution.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config or os.environ.get("ALYSIA_GPT_SOVITS_CONFIG", ""))
    if not str(config_path):
        config_path = PROJECT_ROOT / "training/gpt_sovits/configs/characters/elysia.yaml"
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path

    config = load_training_config(config_path)
    validation = validate_dataset(config, write_normalized=True)
    if not validation.ready:
        raise RuntimeError("GPT-SoVITS dataset is not ready. Run quickstart/train_gpt_sovits.ps1 first.")

    recipe = build_recipe(config, validation)
    s2_config_path = write_s2_config(config, validation, recipe)
    s1_config_path = write_s1_config(config, validation, recipe)

    if args.dry_config:
        print(f"recipe filelist: {_relative(recipe.input_text)}")
        print(f"s1 config: {_relative(s1_config_path)}")
        print(f"s2 config: {_relative(s2_config_path)}")
        print(f"s2 prepare config: {_relative(recipe.s2_prepare_config)}")
        return 0

    ensure_language_dependencies(validation)
    ensure_upstream_prepared(config, recipe, recipe.s2_prepare_config, include_semantic=args.stage == "s1")
    if args.stage == "s1":
        run_upstream(["GPT_SoVITS/s1_train.py", "--config_file", str(s1_config_path)])
    else:
        run_upstream(["GPT_SoVITS/s2_train.py", "--config", str(s2_config_path)])
    return 0


class RecipePaths:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.audio_dir = root / "audio"
        self.input_text = root / "0-filelist.list"
        self.manifest = root / "recipe_manifest.json"
        self.s1_config = root / "tmp_s1.yaml"
        self.s2_config = root / "tmp_s2.json"
        self.s2_prepare_config = root / "tmp_s2_prepare.json"

    @property
    def text_path(self) -> Path:
        return self.root / "2-name2text.txt"

    @property
    def semantic_path(self) -> Path:
        return self.root / "6-name2semantic.tsv"


def build_recipe(config: TrainingConfig, validation: DatasetValidation) -> RecipePaths:
    recipe = RecipePaths(config.run_dir / "recipe")
    recipe.root.mkdir(parents=True, exist_ok=True)
    recipe.audio_dir.mkdir(parents=True, exist_ok=True)

    previous_hash = ""
    if recipe.manifest.exists():
        try:
            previous_hash = str(json.loads(recipe.manifest.read_text(encoding="utf-8")).get("dataset_hash", ""))
        except json.JSONDecodeError:
            previous_hash = ""
    if previous_hash and previous_hash != validation.dataset_hash:
        _remove_generated_outputs(recipe)

    lines: list[str] = []
    mapping: list[dict[str, str]] = []
    for index, item in enumerate(validation.items, start=1):
        generated_name = f"{index:06d}_{item.audio_hash[:12]}.wav"
        destination = recipe.audio_dir / generated_name
        if not destination.exists() or file_sha256(destination) != item.audio_hash:
            shutil.copy2(item.resolved_audio_path, destination)
        text = _clean_field(item.text)
        speaker = _clean_field(item.speaker or config.speaker)
        language = _gpt_sovits_language(item.language or config.language)
        lines.append(f"{generated_name}|{speaker}|{language}|{text}")
        mapping.append(
            {
                "recipe_audio": generated_name,
                "source_audio": item.audio_path,
                "audio_hash": item.audio_hash,
                "text": text,
            }
        )

    recipe.input_text.write_text("\n".join(lines) + "\n", encoding="utf-8")
    recipe.manifest.write_text(
        json.dumps(
            {
                "dataset_hash": validation.dataset_hash,
                "recipe_audio_dir": _relative(recipe.audio_dir),
                "input_text": _relative(recipe.input_text),
                "items": mapping,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return recipe


def ensure_upstream_prepared(
    config: TrainingConfig,
    recipe: RecipePaths,
    s2_config_path: Path,
    *,
    include_semantic: bool,
) -> None:
    item_count = _recipe_item_count(recipe.input_text)
    common_env = {
        "inp_text": str(recipe.input_text),
        "inp_wav_dir": str(recipe.audio_dir),
        "exp_name": config.run_name,
        "opt_dir": str(recipe.root),
        "i_part": "0",
        "all_parts": "1",
        "_CUDA_VISIBLE_DEVICES": os.environ.get("ALYSIA_GPT_SOVITS_GPUS", "0"),
        "is_half": os.environ.get("ALYSIA_GPT_SOVITS_HALF", "True"),
    }

    if not _line_count_at_least(recipe.text_path, item_count):
        _remove_part_files(recipe.root, "2-name2text-*.txt")
        run_upstream(
            ["GPT_SoVITS/prepare_datasets/1-get-text.py"],
            {
                **common_env,
                "bert_pretrained_dir": str(GPT_SOVITS_ROOT / "pretrained_models/chinese-roberta-wwm-ext-large"),
                "version": detect_version(config),
            },
        )
        _merge_part_files(recipe.root, "2-name2text-*.txt", recipe.text_path, header=None)

    if not _feature_count_at_least(recipe.root / "4-cnhubert", item_count):
        run_upstream(
            ["GPT_SoVITS/prepare_datasets/2-get-hubert-wav32k.py"],
            {
                **common_env,
                "cnhubert_base_dir": str(GPT_SOVITS_ROOT / "pretrained_models/chinese-hubert-base"),
            },
        )

    if include_semantic and not _line_count_at_least(recipe.semantic_path, item_count + 1):
        _remove_part_files(recipe.root, "6-name2semantic-*.tsv")
        run_upstream(
            ["GPT_SoVITS/prepare_datasets/3-get-semantic.py"],
            {
                **common_env,
                "pretrained_s2G": str(config.base_model.s2_checkpoint),
                "s2config_path": str(s2_config_path),
            },
        )
        _merge_part_files(recipe.root, "6-name2semantic-*.tsv", recipe.semantic_path, header="item_name\tsemantic_audio")


def write_s1_config(config: TrainingConfig, validation: DatasetValidation, recipe: RecipePaths) -> Path:
    version = detect_version(config)
    template_name = "s1longer.yaml" if version == "v1" else "s1longer-v2.yaml"
    data = _load_simple_yaml(GPT_SOVITS_ROOT / "configs" / template_name)
    batch_size = _env_int("ALYSIA_GPT_SOVITS_S1_BATCH_SIZE", 8)
    steps_per_epoch = max(1, ceil(len(validation.items) / batch_size))
    data.setdefault("train", {})
    data["train"]["seed"] = config.seed
    data["train"]["batch_size"] = batch_size
    data["train"]["epochs"] = _epochs_from_steps(config.s1.max_steps, steps_per_epoch)
    data["train"]["save_every_n_epoch"] = _epochs_from_steps(config.s1.save_steps, steps_per_epoch)
    data["train"]["pretrained_s1"] = str(config.base_model.s1_checkpoint)
    data["pretrained_s1"] = str(config.base_model.s1_checkpoint)
    data["train"]["if_save_every_weights"] = True
    data["train"]["if_save_latest"] = False
    data["train"]["if_dpo"] = False
    data.setdefault("data", {})
    default_num_workers = 0 if os.name == "nt" else int(data["data"].get("num_workers", 4))
    data["data"]["num_workers"] = _env_int_allow_zero("ALYSIA_GPT_SOVITS_S1_NUM_WORKERS", default_num_workers)
    half_weights_save_dir = config.run_dir / "weights" / _gpt_weight_dir(version)
    output_dir = config.run_dir / "s1"
    half_weights_save_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "ckpt").mkdir(parents=True, exist_ok=True)
    data["train"]["half_weights_save_dir"] = str(half_weights_save_dir)
    data["train"]["exp_name"] = config.run_name
    data["train_semantic_path"] = str(recipe.semantic_path)
    data["train_phoneme_path"] = str(recipe.text_path)
    data["output_dir"] = str(output_dir)
    recipe.s1_config.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return recipe.s1_config


def write_s2_config(config: TrainingConfig, validation: DatasetValidation, recipe: RecipePaths) -> Path:
    version = detect_version(config)
    template = "s2.json"
    if version in {"v2Pro", "v2ProPlus"}:
        template = f"s2{version}.json"
    data = json.loads((GPT_SOVITS_ROOT / "configs" / template).read_text(encoding="utf-8"))
    batch_size = _env_int("ALYSIA_GPT_SOVITS_S2_BATCH_SIZE", 4)
    steps_per_epoch = max(1, ceil(len(validation.items) / batch_size))
    data["train"]["seed"] = config.seed
    data["train"]["batch_size"] = batch_size
    data["train"]["epochs"] = _epochs_from_steps(config.s2.max_steps, steps_per_epoch)
    data["train"]["pretrained_s2G"] = str(config.base_model.s2_checkpoint)
    data["train"]["pretrained_s2D"] = str(_default_s2d_checkpoint(config, version))
    data["train"]["if_save_latest"] = False
    data["train"]["if_save_every_weights"] = True
    data["train"]["save_every_epoch"] = _epochs_from_steps(config.s2.save_steps, steps_per_epoch)
    data["train"]["gpu_numbers"] = os.environ.get("ALYSIA_GPT_SOVITS_GPUS", "0")
    data["train"]["grad_ckpt"] = os.environ.get("ALYSIA_GPT_SOVITS_GRAD_CKPT", "false").lower() == "true"
    data["model"]["version"] = version
    s2_exp_dir = recipe.root
    s2_ckpt_dir = s2_exp_dir / f"logs_s2_{version}"
    save_weight_dir = config.run_dir / "weights" / _sovits_weight_dir(version)
    s2_ckpt_dir.mkdir(parents=True, exist_ok=True)
    save_weight_dir.mkdir(parents=True, exist_ok=True)
    data["data"]["exp_dir"] = str(s2_exp_dir)
    data["s2_ckpt_dir"] = str(s2_ckpt_dir)
    data["save_weight_dir"] = str(save_weight_dir)
    data["name"] = config.run_name
    data["version"] = version
    prepare_data = json.loads(json.dumps(data))
    prepare_data.get("model", {}).pop("version", None)
    recipe.s2_config.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    recipe.s2_prepare_config.write_text(json.dumps(prepare_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return recipe.s2_config


def run_upstream(args: list[str], extra_env: dict[str, str] | None = None) -> None:
    upstream_paths = [str(GPT_SOVITS_ROOT), str(UPSTREAM_ROOT / "tools"), str(UPSTREAM_ROOT)]
    existing_pythonpath = os.environ.get("PYTHONPATH", "")
    pythonpath = os.pathsep.join([*upstream_paths, existing_pythonpath]) if existing_pythonpath else os.pathsep.join(upstream_paths)
    completed = subprocess.run(
        [sys.executable, "-s", *args],
        cwd=UPSTREAM_ROOT,
        env={**os.environ, "PYTHONPATH": pythonpath, **(extra_env or {})},
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"GPT-SoVITS upstream command failed with exit code {completed.returncode}: {' '.join(args)}")


def ensure_language_dependencies(validation: DatasetValidation) -> None:
    languages = {_gpt_sovits_language(item.language) for item in validation.items}
    required_modules = {
        "ja": ("pyopenjtalk", "Japanese text cleaning"),
    }
    missing = [
        f"{module} ({reason})"
        for language, (module, reason) in required_modules.items()
        if language in languages and importlib.util.find_spec(module) is None
    ]
    if missing:
        raise RuntimeError(
            "GPT-SoVITS training data uses language(s) that need missing Python module(s): "
            + ", ".join(missing)
            + ". Install them with: powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/install_gpt_sovits_training_deps.ps1"
        )


def detect_version(config: TrainingConfig) -> str:
    probe = f"{config.base_model.model_id} {config.base_model.s1_checkpoint} {config.base_model.s2_checkpoint}".lower()
    if "v2proplus" in probe:
        return "v2ProPlus"
    if "v2pro" in probe:
        return "v2Pro"
    if "s2g488k" in probe or "s1bert25hz-2kh" in probe:
        return "v1"
    if "v4" in probe:
        return "v4"
    if "v3" in probe:
        return "v3"
    if "gsv-v2final" in probe or "s2g2333k" in probe or "s1bert25hz-5kh" in probe or "v2" in probe:
        return "v2"
    return "v1"


def _default_s2d_checkpoint(config: TrainingConfig, version: str) -> Path:
    candidates = {
        "v1": GPT_SOVITS_ROOT / "pretrained_models/s2D488k.pth",
        "v2": GPT_SOVITS_ROOT / "pretrained_models/gsv-v2final-pretrained/s2D2333k.pth",
        "v2Pro": GPT_SOVITS_ROOT / "pretrained_models/v2Pro/s2Dv2Pro.pth",
        "v2ProPlus": GPT_SOVITS_ROOT / "pretrained_models/v2Pro/s2Dv2ProPlus.pth",
    }
    return candidates.get(version, config.base_model.pretrained_root / "s2D488k.pth")


def _remove_generated_outputs(recipe: RecipePaths) -> None:
    for path in (recipe.text_path, recipe.semantic_path):
        if path.exists():
            path.unlink()
    _remove_part_files(recipe.root, "2-name2text-*.txt")
    _remove_part_files(recipe.root, "6-name2semantic-*.tsv")


def _remove_part_files(root: Path, pattern: str) -> None:
    for path in root.glob(pattern):
        if path.is_file():
            path.unlink()


def _merge_part_files(root: Path, pattern: str, destination: Path, *, header: str | None) -> None:
    lines: list[str] = [header] if header else []
    for path in sorted(root.glob(pattern)):
        content = path.read_text(encoding="utf-8").strip("\n")
        if content:
            lines.extend(content.splitlines())
        path.unlink()
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _line_count_at_least(path: Path, expected: int) -> bool:
    if not path.exists():
        return False
    return len([line for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]) >= expected


def _feature_count_at_least(path: Path, expected: int) -> bool:
    return path.exists() and len(list(path.glob("*.pt"))) >= expected


def _recipe_item_count(path: Path) -> int:
    return len([line for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()])


def _epochs_from_steps(max_steps: int, steps_per_epoch: int) -> int:
    return max(1, ceil(max_steps / max(1, steps_per_epoch)))


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


def _env_int_allow_zero(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


def _clean_field(value: str) -> str:
    return str(value).replace("\r", " ").replace("\n", " ").replace("|", " ").strip()


def _gpt_sovits_language(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"zh", "zho", "chinese", "cmn"}:
        return "zh"
    if normalized in {"ja", "jp", "jpn", "japanese"}:
        return "ja"
    if normalized in {"en", "eng", "english"}:
        return "en"
    if normalized in {"ko", "kor", "korean"}:
        return "ko"
    if normalized in {"yue", "cantonese"}:
        return "yue"
    return "zh"


def _gpt_weight_dir(version: str) -> str:
    return "GPT_weights" if version == "v1" else f"GPT_weights_{version}"


def _sovits_weight_dir(version: str) -> str:
    return "SoVITS_weights" if version == "v1" else f"SoVITS_weights_{version}"


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
        value = raw_value.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _parse_scalar(value)
    return data


def _parse_scalar(value: str) -> Any:
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value in {"null", "None", "~"}:
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value.strip("'\"")


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
