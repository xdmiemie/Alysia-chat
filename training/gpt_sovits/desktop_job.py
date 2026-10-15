"""Stage 17 subprocess entry point.  The desktop never runs model work on its UI thread."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from math import ceil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from training.gpt_sovits.config import config_hash, load_training_config
from training.gpt_sovits.preprocess.dataset_manifest import prepare_dataset, split_items, validate_dataset
from training.gpt_sovits.train.checkpoint_policy import write_checkpoint_metadata
from training.gpt_sovits.config import jsonable_config

PAUSED_EXIT = 20


def emit(phase: str, percent: float, message: str, **extra: object) -> None:
    print("[voice_progress] " + json.dumps({"phase": phase, "percent": round(percent, 1),
                                              "message": message, **extra}, ensure_ascii=False), flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("scan", "validate", "prepare", "train"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--pause-file", default="")
    parser.add_argument("--finish-file", default="")
    parser.add_argument("--resume", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config).resolve()
    config = load_training_config(config_path)
    if args.action == "scan":
        files = [path for path in config.dataset.raw_audio_dir.rglob("*.wav") if path.is_file()] if config.dataset.raw_audio_dir.exists() else []
        emit("scan", 100, f"找到 {len(files)} 个 WAV 文件；manifest {'存在' if config.dataset.raw_manifest.exists() else '尚未生成'}",
             files=len(files), manifest=str(config.dataset.raw_manifest))
        return 0
    if args.action == "prepare":
        emit("preprocessing", 0, "正在扫描音频并准备转写")
        prepare_dataset(config)
    validation = validate_dataset(config)
    if args.action == "prepare" and validation.ready:
        emit("preprocessing", 65, "正在标准化音频并构造 GPT-SoVITS 特征")
        from training.gpt_sovits.train.run_recipe import (build_recipe, ensure_language_dependencies,
                                                          ensure_upstream_prepared, write_s1_config, write_s2_config)
        ensure_language_dependencies(validation)
        recipe = build_recipe(config, validation)
        write_s1_config(config, validation, recipe)
        write_s2_config(config, validation, recipe)
        ensure_upstream_prepared(config, recipe, recipe.s2_prepare_config, include_semantic=True)
    emit("validation" if args.action != "prepare" else "preprocessing", 100 if validation.ready else 0,
         "数据集校验通过" if validation.ready else "数据集校验失败",
         report_path=str(config.dataset.validation_report))
    if args.action in {"validate", "prepare"}:
        return 0 if validation.ready else 1
    if not validation.ready:
        return 1

    state_path = config.run_dir / "desktop_progress.json"
    cfg_hash = config_hash(config_path)
    if args.resume:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("dataset_hash") != validation.dataset_hash or state.get("config_hash") != cfg_hash:
            raise ValueError("训练配置或数据集已变化，无法从原检查点恢复")
        _verify_resume_checkpoints(config, state, validation.dataset_hash, cfg_hash)
    else:
        if state_path.exists():
            raise ValueError("当前 run_name 已有训练记录；请使用新的实验名称")
        config.run_dir.mkdir(parents=True, exist_ok=True)
        state = {"dataset_hash": validation.dataset_hash, "config_hash": cfg_hash,
                 "s1": 0, "s2": 0, "status": "running"}
        _save(state_path, state)

    pause_file = Path(args.pause_file) if args.pause_file else None
    finish_file = Path(args.finish_file) if args.finish_file else None
    train_count = len(split_items(validation.items, config.dataset.validation_ratio)[0])
    for stage_name in ("s2", "s1"):
        stage = getattr(config, stage_name)
        if not stage.enabled:
            continue
        steps_per_epoch = max(1, ceil(train_count / stage.batch_size))
        total_epochs = stage.epochs or max(1, ceil(stage.max_steps / steps_per_epoch))
        interval_epochs = max(1, ceil(stage.save_steps / steps_per_epoch))
        while int(state[stage_name]) < total_epochs:
            if pause_file and pause_file.exists() and (int(state["s1"]) or int(state["s2"])):
                state["status"] = "paused"
                _save(state_path, state)
                emit("paused", _percent(state, config, train_count), "已在可恢复检查点暂停",
                     resume_checkpoint=str(_resume_checkpoint(config, state)))
                return PAUSED_EXIT
            previous = int(state[stage_name])
            target = min(total_epochs, previous + interval_epochs)
            emit(stage_name, _percent(state, config, train_count),
                 f"{stage_name.upper()} 第 {previous + 1}–{target}/{total_epochs} epoch；完成后保存检查点")
            env = {**os.environ, "ALYSIA_GPT_SOVITS_EPOCH_LIMIT": str(target),
                   "ALYSIA_GPT_SOVITS_SAVE_EPOCH_INTERVAL": str(interval_epochs if target % interval_epochs == 0 else 1),
                   "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
            script = PROJECT_ROOT / "training" / "gpt_sovits" / "train" / f"train_{stage_name}.py"
            completed = subprocess.run([sys.executable, str(script), "--config", str(config_path), "--skip-prepare"],
                                       cwd=PROJECT_ROOT, env=env, check=False)
            if completed.returncode:
                raise RuntimeError(f"{stage_name.upper()} 训练失败，退出码 {completed.returncode}")
            native_files = _require_weight(config.run_dir, stage_name)
            checkpoint = config.run_dir / stage_name / f"checkpoint-{target:06d}"
            write_checkpoint_metadata(checkpoint, training_config=jsonable_config(config),
                                      dataset_hash=validation.dataset_hash, config_hash=cfg_hash,
                                      base_model_id=config.base_model.model_id)
            (checkpoint / "native_files.json").write_text(json.dumps(
                [str(path) for path in (native_files or [])], ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            state[stage_name] = target
            _save(state_path, state)
            emit(stage_name, _percent(state, config, train_count),
                 f"{stage_name.upper()} 已保存第 {target} epoch 检查点",
                 resume_checkpoint=str(checkpoint))
            if pause_file and pause_file.exists():
                state["status"] = "paused"
                _save(state_path, state)
                emit("paused", _percent(state, config, train_count),
                     f"已在 {stage_name.upper()} 第 {target} epoch 检查点暂停",
                     resume_checkpoint=str(checkpoint))
                return PAUSED_EXIT
            # Finish after both S1 and S2 have a real trained weight.  If the
            # request arrived during the first stage, the other gets one chunk.
            if finish_file and finish_file.exists() and int(state["s1"]) and int(state["s2"]):
                break
            if finish_file and finish_file.exists() and stage_name == "s2" and int(state["s2"]):
                break
        if finish_file and finish_file.exists() and int(state["s1"]) and int(state["s2"]):
            break

    emit("references", 91, "正在选择角色参考音频")
    _run_module("training.gpt_sovits.export.select_references", config_path)
    emit("export", 94, "正在收集 S1/S2 权重并注册候选")
    _run_module("training.gpt_sovits.export.collect_weights", config_path)
    # The candidate is registered without --activate.  A separate fixed-text
    # synthesis check protects the previous active voice on any failure.
    from runtime.gpt_sovits import model_registry
    models = model_registry.list_models(config.character_id)
    candidate = max((item for item in models if item.source_run == _relative(config.run_dir)),
                    key=lambda item: item.created_at)
    emit("validation", 97, "正在生成固定测试句并验证候选音色", candidate_model_id=candidate.model_id)
    from training.gpt_sovits.preview import synthesize_fixed_set
    synthesize_fixed_set(candidate, emotion="neutral", output_dir=config.run_dir / "fixed_preview")
    model_registry.activate_model(config.character_id, candidate.model_id, smoke_test=lambda _model: None)
    state["status"] = "completed"
    state["model_id"] = candidate.model_id
    _save(state_path, state)
    emit("completed", 100, "候选已通过试听验证并自动应用", model_id=candidate.model_id)
    return 0


def _require_weight(run_dir: Path, stage: str) -> list[Path]:
    from training.gpt_sovits.export.collect_weights import discover_latest_gpt_weight, discover_latest_sovits_weight
    exported = (discover_latest_gpt_weight if stage == "s1" else discover_latest_sovits_weight)(run_dir)
    if stage == "s1":
        native = list((run_dir / "s1" / "ckpt").glob("*.ckpt"))
        if not native:
            raise FileNotFoundError("S1 没有生成可恢复的 Lightning 检查点")
        return [exported, max(native, key=lambda path: path.stat().st_mtime_ns)]
    native_root = run_dir / "recipe"
    generators = list(native_root.glob("logs_s2_*/G_*.pth"))
    discriminators = list(native_root.glob("logs_s2_*/D_*.pth"))
    if not generators or not discriminators:
        raise FileNotFoundError("S2 没有生成完整的 G/D 可恢复检查点")
    return [exported, max(generators, key=lambda path: path.stat().st_mtime_ns),
            max(discriminators, key=lambda path: path.stat().st_mtime_ns)]


def _resume_checkpoint(config, state: dict[str, object]) -> Path:
    stage = "s1" if int(state["s1"]) else "s2"
    return config.run_dir / stage / f"checkpoint-{int(state[stage]):06d}"


def _verify_resume_checkpoints(config, state: dict[str, object], dataset_hash: str, cfg_hash: str) -> None:
    if not (int(state.get("s1", 0)) or int(state.get("s2", 0))):
        raise ValueError("没有已保存的训练检查点，无法恢复")
    for stage in ("s2", "s1"):
        epoch = int(state.get(stage, 0))
        if not epoch:
            continue
        checkpoint = config.run_dir / stage / f"checkpoint-{epoch:06d}"
        metadata_path = checkpoint / "alysia_checkpoint.json"
        native_path = checkpoint / "native_files.json"
        if not metadata_path.is_file() or not native_path.is_file():
            raise FileNotFoundError(f"{stage.upper()} 检查点不完整：{checkpoint}")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if (metadata.get("dataset_hash") != dataset_hash or metadata.get("config_hash") != cfg_hash
                or metadata.get("base_model_id") != config.base_model.model_id):
            raise ValueError(f"{stage.upper()} 检查点与当前配置或数据不匹配：{checkpoint}")
        native_files = json.loads(native_path.read_text(encoding="utf-8"))
        if len(native_files) < (3 if stage == "s2" else 2):
            raise FileNotFoundError(f"{stage.upper()} 缺少原生可恢复权重：{checkpoint}")
        for native_file in native_files:
            path = Path(native_file)
            if not path.is_file() or path.stat().st_size == 0:
                raise FileNotFoundError(f"检查点权重不存在或为空：{path}")


def _run_module(module: str, config_path: Path) -> None:
    completed = subprocess.run([sys.executable, "-m", module, "--config", str(config_path)], cwd=PROJECT_ROOT, check=False)
    if completed.returncode:
        raise RuntimeError(f"{module} failed with exit code {completed.returncode}")


def _save(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _percent(state: dict[str, object], config, count: int) -> float:
    totals = [stage.epochs or max(1, ceil(stage.max_steps / max(1, ceil(count / stage.batch_size))))
              for stage in (config.s1, config.s2)]
    return 5 + 85 * (min(int(state["s1"]), totals[0]) + min(int(state["s2"]), totals[1])) / sum(totals)


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[voice_error] {exc}", file=sys.stderr, flush=True)
        raise
