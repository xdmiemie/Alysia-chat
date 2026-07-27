from __future__ import annotations

from pathlib import Path

from training.gpt_sovits.train.checkpoint_policy import (
    CheckpointInfo,
    assert_checkpoint_compatible,
    list_checkpoints,
)


def resolve_resume_checkpoint(
    run_dir: Path,
    *,
    stage: str,
    resume_from: str,
    from_scratch: bool,
    dataset_hash: str,
    config_hash: str,
    base_model_id: str,
) -> CheckpointInfo | None:
    stage_dir = run_dir / stage
    if from_scratch or resume_from.lower() in {"none", "false", "no"}:
        return None

    if resume_from.lower() == "auto":
        for candidate in reversed(list_checkpoints(stage_dir)):
            try:
                assert_checkpoint_compatible(
                    candidate,
                    dataset_hash=dataset_hash,
                    config_hash=config_hash,
                    base_model_id=base_model_id,
                )
            except ValueError:
                continue
            return candidate
        return None

    checkpoint_path = Path(resume_from)
    if not checkpoint_path.is_absolute():
        checkpoint_path = stage_dir / checkpoint_path
    if not checkpoint_path.exists():
        raise FileNotFoundError(f"resume checkpoint does not exist: {checkpoint_path}")

    import json

    metadata_path = checkpoint_path / "alysia_checkpoint.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
    step = int(checkpoint_path.name.split("-", 1)[1]) if checkpoint_path.name.startswith("checkpoint-") else 0
    checkpoint = CheckpointInfo(path=checkpoint_path, step=step, metadata=metadata)
    assert_checkpoint_compatible(
        checkpoint,
        dataset_hash=dataset_hash,
        config_hash=config_hash,
        base_model_id=base_model_id,
    )
    return checkpoint
