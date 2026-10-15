from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.qwen3 import model_registry
from training.qwen3.config import file_sha256, load_training_config, trained_model_identity


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export or register a Qwen3 trained model for llama.cpp.")
    parser.add_argument("--config", default="training/qwen3/configs/characters/elysia.yaml")
    parser.add_argument("--run-dir")
    parser.add_argument("--model-id")
    parser.add_argument("--display-name")
    parser.add_argument("--gguf-path", help="Register an already exported GGUF file.")
    parser.add_argument("--adapter-path", help="Register a LoRA adapter file for llama.cpp --lora.")
    parser.add_argument("--merged-model-dir", help="HF merged model directory to convert to GGUF.")
    parser.add_argument("--converter-script", help="llama.cpp convert_hf_to_gguf.py path.")
    parser.add_argument("--activate", action="store_true", help="Activate after registration.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_training_config(args.config)
    run_dir = Path(args.run_dir) if args.run_dir else config.run_dir
    default_model_id, default_display_name = trained_model_identity(config)
    model_id = args.model_id or default_model_id
    display_name = args.display_name or default_display_name

    gguf_path = Path(args.gguf_path) if args.gguf_path else None
    adapter_path = Path(args.adapter_path) if args.adapter_path else None
    export_log = run_dir / "export_log.json"

    if gguf_path is None and adapter_path is None:
        gguf_path = _convert_to_gguf(args.merged_model_dir, args.converter_script, run_dir, model_id)

    if gguf_path is not None:
        gguf_path = _install_artifact(gguf_path, config.character_id, model_id)
        entry = model_registry.register_model(
            character_id=config.character_id,
            model_id=model_id,
            display_name=display_name,
            kind="trained",
            model_format="gguf",
            required_files=[gguf_path],
            base_model_id=config.base_model.model_id,
            data_hash=_run_manifest_value(run_dir, "dataset_hash"),
            config_hash=_run_manifest_value(run_dir, "config_hash"),
            source_run=_relative(run_dir),
            export_log=_relative(export_log),
            activate=False,
        )
        _associate_with_selected_roles(config, entry, activate=False)
        _write_export_log(export_log, entry, gguf_path)
    else:
        if not _is_gguf_file(adapter_path):
            raise ValueError(
                "llama.cpp LoRA adapters must be GGUF files. "
                "Run quickstart/convert_qwen3_lora_to_gguf.ps1 to convert adapter_model.safetensors first."
            )
        adapter_path = _install_artifact(adapter_path, config.character_id, model_id)
        entry = model_registry.register_model(
            character_id=config.character_id,
            model_id=model_id,
            display_name=display_name,
            kind="trained",
            model_format="lora",
            required_files=[config.base_model.gguf_path, adapter_path],
            base_model_id=config.base_model.model_id,
            data_hash=_run_manifest_value(run_dir, "dataset_hash"),
            config_hash=_run_manifest_value(run_dir, "config_hash"),
            source_run=_relative(run_dir),
            export_log=_relative(export_log),
            activate=False,
        )
        _associate_with_selected_roles(config, entry, activate=False)
        _write_export_log(export_log, entry, adapter_path)

    if args.activate:
        from runtime.core.model_smoke import smoke_test_qwen
        smoke_test_qwen(entry)
        for character_id in dict.fromkeys((config.character_id, *config.assigned_character_ids)):
            model_registry.activate_model(character_id, entry.model_id, smoke_test=lambda _model: None)
    print(json.dumps(entry.to_manifest(), ensure_ascii=False, indent=2))
    return 0


def _convert_to_gguf(merged_model_dir: str | None, converter_script: str | None, run_dir: Path, model_id: str) -> Path:
    if not merged_model_dir:
        raise ValueError("Provide --gguf-path, --adapter-path, or --merged-model-dir.")
    if not converter_script:
        raise ValueError("Provide --converter-script when converting a merged HF model to GGUF.")
    merged = Path(merged_model_dir)
    converter = Path(converter_script)
    if not converter.exists():
        raise FileNotFoundError(f"converter script does not exist: {converter}")
    output = run_dir / f"{model_id}.gguf"
    command = [sys.executable, str(converter), str(merged), "--outfile", str(output), "--outtype", "q8_0"]
    completed = subprocess.run(command, cwd=PROJECT_ROOT, text=True, capture_output=True, check=False)
    (run_dir / "convert_gguf.stdout.log").write_text(completed.stdout, encoding="utf-8")
    (run_dir / "convert_gguf.stderr.log").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(f"GGUF conversion failed with exit code {completed.returncode}")
    return output


def _install_artifact(path: Path | None, character_id: str, model_id: str) -> Path:
    if path is None:
        raise ValueError("artifact path is required")
    source = path if path.is_absolute() else PROJECT_ROOT / path
    if not source.exists():
        raise FileNotFoundError(f"artifact does not exist: {source}")
    target_dir = PROJECT_ROOT / "model_store" / "registry" / "qwen3" / character_id / model_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    if source.resolve() != target.resolve():
        if target.exists():
            raise FileExistsError(f"模型版本已存在，拒绝覆盖权重：{target}")
        target.write_bytes(source.read_bytes())
    return target


def _is_gguf_file(path: Path | None) -> bool:
    if path is None:
        return False
    source = path if path.is_absolute() else PROJECT_ROOT / path
    if not source.exists() or source.is_dir():
        return False
    with source.open("rb") as handle:
        return handle.read(4) == b"GGUF"


def _write_export_log(path: Path, entry: model_registry.Qwen3ModelEntry, artifact: Path) -> None:
    payload = {
        "character_id": entry.character_id,
        "model_id": entry.model_id,
        "format": entry.model_format,
        "artifact": _relative(artifact),
        "artifact_sha256": file_sha256(artifact),
        "registered_manifest": _relative(model_registry.character_manifest_path(entry.character_id)),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _associate_with_selected_roles(config, entry: model_registry.Qwen3ModelEntry, *, activate: bool) -> None:
    """Register the same artifact paths for every role selected in training.

    Runtime lookups remain per-character, while each registry entry references
    the single exported artifact.  A LoRA target also receives the required
    base entry so ``find_model`` can resolve its runtime base model.
    """
    targets = tuple(dict.fromkeys((config.character_id, *config.assigned_character_ids)))
    for character_id in targets:
        if character_id == config.character_id:
            continue
        if entry.model_format == "lora":
            model_registry.register_model(
                character_id=character_id,
                model_id=config.base_model.model_id,
                display_name=config.base_model.model_id,
                kind="base",
                model_format="gguf",
                required_files=[config.base_model.gguf_path],
                activate=False,
            )
        model_registry.register_model(
            character_id=character_id,
            model_id=entry.model_id,
            display_name=entry.display_name,
            kind=entry.kind,
            model_format=entry.model_format,
            required_files=list(entry.required_files),
            base_model_id=entry.base_model_id,
            data_hash=entry.data_hash,
            config_hash=entry.config_hash,
            source_run=entry.source_run,
            export_log=entry.export_log,
            activate=activate,
        )


def _run_manifest_value(run_dir: Path, key: str) -> str | None:
    manifest_path = run_dir / "run_manifest.json"
    if not manifest_path.exists():
        return None
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if key == "dataset_hash":
        dataset = data.get("dataset_manifest")
        if isinstance(dataset, dict):
            value = dataset.get("dataset_hash")
            return str(value) if value else None
    value = data.get(key)
    return str(value) if value else None


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
