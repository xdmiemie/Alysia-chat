from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from training.gpt_sovits.config import StageConfig, TrainingConfig, jsonable_config
from training.gpt_sovits.preprocess.dataset_manifest import DatasetValidation
from training.gpt_sovits.train.checkpoint_policy import enforce_checkpoint_limit, write_checkpoint_metadata
from training.gpt_sovits.train.resume import resolve_resume_checkpoint


def run_stage(
    *,
    config: TrainingConfig,
    config_path: Path,
    validation: DatasetValidation,
    cfg_hash: str,
    stage_name: str,
    stage_config: StageConfig,
    resume_from: str,
    from_scratch: bool,
    dry_run: bool,
    project_root: Path,
) -> Path | None:
    if not stage_config.enabled:
        print(f"{stage_name}: disabled in config")
        return None
    if not validation.ready and not dry_run:
        raise RuntimeError("GPT-SoVITS dataset is not ready. Run dataset_manifest.py and fix the reported issues.")

    config.run_dir.mkdir(parents=True, exist_ok=True)
    _write_run_manifest(config, config_path, validation, cfg_hash)
    checkpoint = resolve_resume_checkpoint(
        config.run_dir,
        stage=stage_name,
        resume_from=resume_from,
        from_scratch=from_scratch,
        dataset_hash=validation.dataset_hash,
        config_hash=cfg_hash,
        base_model_id=config.base_model.model_id,
    )
    if checkpoint is None:
        print(f"{stage_name}: no compatible checkpoint found; starting from scratch")
    else:
        print(f"{stage_name}: continuing from {checkpoint.path.relative_to(project_root)}")

    stage_dir = config.run_dir / stage_name
    if dry_run:
        next_step = (checkpoint.step if checkpoint else 0) + stage_config.save_steps
        checkpoint_dir = stage_dir / f"checkpoint-{next_step}"
        write_checkpoint_metadata(
            checkpoint_dir,
            training_config=jsonable_config(config),
            dataset_hash=validation.dataset_hash,
            config_hash=cfg_hash,
            base_model_id=config.base_model.model_id,
        )
        artifact_name = "s1.ckpt" if stage_name == "s1" else "s2.pth"
        checkpoint_dir.joinpath(artifact_name).write_text("dry-run placeholder\n", encoding="utf-8")
        enforce_checkpoint_limit(
            stage_dir,
            keep=stage_config.save_total_limit,
            best_checkpoint_name=stage_config.best_checkpoint_name,
        )
        print(f"{stage_name}: dry-run checkpoint saved: {checkpoint_dir.relative_to(project_root)}")
        return checkpoint_dir

    if not stage_config.command.strip():
        raise RuntimeError(
            f"{stage_name}: no real GPT-SoVITS training command is configured. "
            f"Edit {config_path} field {stage_name}.command after preparing the upstream training recipe, "
            "or run with --dry-run for pipeline validation."
        )

    env = {
        "ALYSIA_PROJECT_ROOT": str(project_root),
        "ALYSIA_GPT_SOVITS_CONFIG": str(config_path),
        "ALYSIA_GPT_SOVITS_RUN_DIR": str(config.run_dir),
        "ALYSIA_GPT_SOVITS_DATASET": str(config.dataset.normalized_manifest),
        "ALYSIA_GPT_SOVITS_RESUME": str(checkpoint.path) if checkpoint else "",
    }
    import os

    command = _format_stage_command(stage_config.command, config_path=config_path, project_root=project_root)
    completed = subprocess.run(
        command,
        cwd=project_root,
        shell=True,
        text=True,
        env={**os.environ, **env},
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{stage_name}: GPT-SoVITS training command failed with exit code {completed.returncode}")
    _stamp_existing_checkpoints(config, stage_name, stage_config, validation.dataset_hash, cfg_hash)
    return None


def _format_stage_command(command: str, *, config_path: Path, project_root: Path) -> str:
    return command.format(
        python=_shell_quote(sys.executable),
        config=_shell_quote(str(config_path)),
        project_root=_shell_quote(str(project_root)),
    )


def _shell_quote(value: str) -> str:
    if sys.platform == "win32":
        return subprocess.list2cmdline([value])
    return shlex.quote(value)


def _write_run_manifest(
    config: TrainingConfig,
    config_path: Path,
    validation: DatasetValidation,
    cfg_hash: str,
) -> None:
    shutil.copy2(config_path, config.run_dir / "training_config.yaml")
    manifest = {
        "character_id": config.character_id,
        "run_name": config.run_name,
        "speaker": config.speaker,
        "language": config.language,
        "base_model_id": config.base_model.model_id,
        "dataset_hash": validation.dataset_hash,
        "config_hash": cfg_hash,
        "dataset_report": validation.to_report(),
    }
    (config.run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _stamp_existing_checkpoints(
    config: TrainingConfig,
    stage_name: str,
    stage_config: StageConfig,
    dataset_hash: str,
    cfg_hash: str,
) -> None:
    stage_dir = config.run_dir / stage_name
    for checkpoint_dir in stage_dir.glob("checkpoint-*"):
        if checkpoint_dir.is_dir():
            write_checkpoint_metadata(
                checkpoint_dir,
                training_config=jsonable_config(config),
                dataset_hash=dataset_hash,
                config_hash=cfg_hash,
                base_model_id=config.base_model.model_id,
            )
    enforce_checkpoint_limit(
        stage_dir,
        keep=stage_config.save_total_limit,
        best_checkpoint_name=stage_config.best_checkpoint_name,
    )
