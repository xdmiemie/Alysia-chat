from __future__ import annotations

import argparse
import json
import shutil
import time
import sys
from dataclasses import asdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from training.qwen3.config import TrainingConfig, config_hash, dataset_hash, load_training_config, trained_model_identity
from training.qwen3.preprocess.validate import validate_files
from training.qwen3.train.checkpoint_policy import enforce_checkpoint_limit, write_checkpoint_metadata
from training.qwen3.train.resume import resolve_resume_checkpoint
from runtime.qwen3 import model_registry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Train a Qwen3 role LoRA/QLoRA adapter.")
    parser.add_argument("--config", default="training/qwen3/configs/characters/elysia.yaml")
    parser.add_argument("--resume-from", help="Override config resume_from. Use 'auto', 'none', or a checkpoint path.")
    parser.add_argument("--from-scratch", action="store_true", help="Ignore any existing checkpoint and start from step 0.")
    parser.add_argument("--dry-run", action="store_true", help="Create metadata/checkpoint scaffolding without loading ML libraries.")
    parser.add_argument("--skip-register", action="store_true", help="Do not register adapter-final into the role model list.")
    parser.add_argument("--pause-file", help="A file created by the UI requests a safe checkpoint-and-pause.")
    parser.add_argument("--finish-file", help="A file created by the UI requests a safe checkpoint then exportable final adapter.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = PROJECT_ROOT / args.config
    config = load_training_config(config_path)
    resume_from = args.resume_from or config.resume_from

    dataset_manifest = validate_files(
        [config.dataset.train, config.dataset.validation],
        max_near_duplicate_ratio=config.generation.max_near_duplicate_ratio,
    )
    data_hash = str(dataset_manifest["dataset_hash"])
    cfg_hash = config_hash(config_path)
    run_dir = config.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)

    checkpoint = resolve_resume_checkpoint(
        run_dir,
        resume_from=resume_from,
        from_scratch=args.from_scratch,
        dataset_hash=data_hash,
        config_hash=cfg_hash,
        base_model_id=config.base_model.model_id,
    )
    if checkpoint is None:
        print("resume: no compatible checkpoint found; starting from scratch")
    else:
        print(f"resume: continuing from {checkpoint.path.relative_to(PROJECT_ROOT)}")

    _write_run_manifest(config, config_path, dataset_manifest, cfg_hash, checkpoint.path if checkpoint else None)

    if args.dry_run:
        next_step = (checkpoint.step if checkpoint else 0) + config.save_steps
        checkpoint_dir = run_dir / f"checkpoint-{next_step}"
        write_checkpoint_metadata(
            checkpoint_dir,
            training_config=_jsonable_config(config),
            dataset_hash=data_hash,
            config_hash=cfg_hash,
            base_model_id=config.base_model.model_id,
            lineage=_lineage(config),
        )
        adapter_path = checkpoint_dir / "adapter_model.safetensors"
        adapter_path.write_text("dry-run adapter placeholder\n", encoding="utf-8")
        enforce_checkpoint_limit(run_dir, keep=config.save_total_limit, best_checkpoint_name=config.best_checkpoint_name)
        print(f"dry-run checkpoint saved: {checkpoint_dir.relative_to(PROJECT_ROOT)}")
        return 0

    stopped_for = run_qlora_training(
        config, checkpoint.path if checkpoint else None, data_hash=data_hash, cfg_hash=cfg_hash,
        pause_file=Path(args.pause_file) if args.pause_file else None,
        finish_file=Path(args.finish_file) if args.finish_file else None,
    )
    _stamp_trainer_checkpoints(config, data_hash=data_hash, cfg_hash=cfg_hash)
    enforce_checkpoint_limit(run_dir, keep=config.save_total_limit, best_checkpoint_name=config.best_checkpoint_name)
    if stopped_for == "pause":
        print("TRAINING_PAUSED: checkpoint saved; adapter export was intentionally skipped")
        return 75
    if not args.skip_register:
        entry = _register_final_adapter(config, data_hash=data_hash, cfg_hash=cfg_hash)
        if entry is not None:
            print(f"registered trained model: {config.character_id}/{entry.model_id}")
    print(f"training complete: {run_dir.relative_to(PROJECT_ROOT)}")
    return 0


def run_qlora_training(
    config: TrainingConfig,
    resume_checkpoint: Path | None,
    *,
    data_hash: str,
    cfg_hash: str,
    pause_file: Path | None = None,
    finish_file: Path | None = None,
) -> str | None:
    """Backward-compatible entry point for both LoRA and QLoRA runs."""
    if config.base_model.hf_path is None or not config.base_model.hf_path.exists():
        raise RuntimeError(
            "Qwen3 LoRA/QLoRA training requires base_model.hf_path to point at a Hugging Face format Qwen3 model. "
            "Run quickstart/download_qwen3_training_model.ps1 after bootstrap and training dependency installation. "
            "The runtime GGUF is used for inference/export, but cannot be fine-tuned directly."
        )
    try:
        import transformers
        import torch
        from datasets import Dataset, DatasetDict
        from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
        from transformers import (
            AutoModelForCausalLM,
            AutoTokenizer,
            BitsAndBytesConfig,
            Trainer,
            TrainerCallback,
            TrainingArguments,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Missing QLoRA training dependencies. Run quickstart/install_qwen3_training_deps.ps1 after bootstrap."
        ) from exc
    if _version_tuple(transformers.__version__) < (4, 51, 0):
        raise RuntimeError(
            f"Qwen3 training requires transformers>=4.51.0, but found {transformers.__version__}. "
            "Run quickstart/install_qwen3_training_deps.ps1 to upgrade the training-only dependencies."
        )

    # Training consumes conversations only. Quote-to-SFT metadata is retained
    # in JSONL for auditing, but an empty source_quote_ids list in one split
    # and non-empty list in another makes Arrow infer incompatible schemas.
    # Construct datasets from the validated messages field before model load.
    dataset = DatasetDict({
        "train": Dataset.from_list(_conversation_records(config.dataset.train)),
        "validation": Dataset.from_list(_conversation_records(config.dataset.validation)),
    })

    tokenizer = AutoTokenizer.from_pretrained(config.base_model.hf_path, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    if config.method not in {"lora", "qlora"}:
        raise ValueError("training.method must be 'lora' or 'qlora'")
    load_options: dict[str, object] = {"trust_remote_code": True}
    if config.method == "qlora":
        load_options["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=torch.bfloat16 if config.bf16 else torch.float16,
        )
        load_options["device_map"] = "auto"
    elif config.bf16:
        load_options["torch_dtype"] = torch.bfloat16
    elif config.fp16:
        load_options["torch_dtype"] = torch.float16
    model = AutoModelForCausalLM.from_pretrained(config.base_model.hf_path, **load_options)
    model.config.use_cache = False
    if config.method == "qlora":
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=config.gradient_checkpointing)
    elif config.gradient_checkpointing:
        model.gradient_checkpointing_enable()
    model = get_peft_model(
        model,
        LoraConfig(
            r=config.lora_r,
            lora_alpha=config.lora_alpha,
            lora_dropout=config.lora_dropout,
            bias="none",
            task_type="CAUSAL_LM",
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        ),
    )

    def tokenize(example: dict[str, object]) -> dict[str, object]:
        messages = example["messages"]
        if not isinstance(messages, list):
            raise ValueError("dataset record messages must be a list")
        return tokenize_chat_messages(tokenizer, messages, config.sequence_length, config.assistant_only_loss)

    tokenized = dataset.map(tokenize, remove_columns=dataset["train"].column_names)
    data_collator = AssistantOnlyDataCollator(tokenizer)

    class CheckpointMetadataCallback(TrainerCallback):
        stop_reason: str | None = None
        started_at: float | None = None

        def on_train_begin(self, args, state, control, **kwargs):
            self.started_at = time.monotonic()
            return control

        def on_log(self, args, state, control, logs=None, **kwargs):
            elapsed = max(0.001, time.monotonic() - (self.started_at or time.monotonic()))
            payload = {
                "phase": "training", "completed": state.global_step, "total": state.max_steps,
                "percent": round(100 * state.global_step / state.max_steps, 1) if state.max_steps else 0.0,
                "metrics": logs or {}, "steps_per_second": round(state.global_step / elapsed, 3),
            }
            print("[training_progress] " + json.dumps(payload, ensure_ascii=False), flush=True)
            return control

        def on_step_end(self, args, state, control, **kwargs):
            if pause_file is not None and pause_file.exists():
                self.stop_reason = "pause"
            elif finish_file is not None and finish_file.exists():
                self.stop_reason = "finish"
            if self.stop_reason:
                # Trainer finishes the current safe step, writes its regular
                # optimizer/scheduler/RNG checkpoint, then exits normally.
                control.should_save = True
                control.should_training_stop = True
            return control

        def on_save(self, args, state, control, **kwargs):
            checkpoint_dir = Path(args.output_dir) / f"checkpoint-{state.global_step}"
            if checkpoint_dir.exists():
                write_checkpoint_metadata(
                    checkpoint_dir,
                    training_config=_jsonable_config(config),
                    dataset_hash=data_hash,
                    config_hash=cfg_hash,
                    base_model_id=config.base_model.model_id,
                    lineage=_lineage(config),
                )
            return control

    training_args = TrainingArguments(
        output_dir=str(config.run_dir),
        max_steps=config.max_steps,
        save_steps=config.save_steps,
        save_total_limit=config.save_total_limit,
        learning_rate=config.learning_rate,
        per_device_train_batch_size=config.per_device_train_batch_size,
        gradient_accumulation_steps=config.gradient_accumulation_steps,
        num_train_epochs=config.epochs,
        seed=config.seed,
        gradient_checkpointing=config.gradient_checkpointing,
        logging_steps=10,
        eval_strategy="steps",
        eval_steps=config.save_steps,
        fp16=config.fp16 and not config.bf16,
        bf16=config.bf16,
        report_to=[],
    )
    checkpoint_callback = CheckpointMetadataCallback()
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=tokenized["train"],
        eval_dataset=tokenized["validation"],
        data_collator=data_collator,
        callbacks=[checkpoint_callback],
    )
    trainer.train(resume_from_checkpoint=str(resume_checkpoint) if resume_checkpoint else None)
    model.save_pretrained(config.run_dir / "adapter-final")
    tokenizer.save_pretrained(config.run_dir / "adapter-final")
    return checkpoint_callback.stop_reason


def tokenize_chat_messages(tokenizer, messages: list[dict[str, object]], sequence_length: int, assistant_only_loss: bool) -> dict[str, list[int]]:
    """Apply Qwen's chat template and mask all non-assistant tokens from loss."""
    full_ids = list(tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=False))
    labels = list(full_ids) if not assistant_only_loss else [-100] * len(full_ids)
    if assistant_only_loss:
        for index, message in enumerate(messages):
            if message.get("role") != "assistant":
                continue
            # The generated assistant prefix includes the role marker.  Its
            # length is the first assistant content token in the full record.
            prefix = list(tokenizer.apply_chat_template(messages[:index], tokenize=True, add_generation_prompt=True))
            through_assistant = list(tokenizer.apply_chat_template(messages[: index + 1], tokenize=True, add_generation_prompt=False))
            start, end = min(len(prefix), len(full_ids)), min(len(through_assistant), len(full_ids))
            labels[start:end] = full_ids[start:end]
    return {"input_ids": full_ids[:sequence_length], "attention_mask": [1] * min(len(full_ids), sequence_length), "labels": labels[:sequence_length]}


class AssistantOnlyDataCollator:
    def __init__(self, tokenizer) -> None:
        self.tokenizer = tokenizer

    def __call__(self, features):
        import torch

        max_length = max(len(item["input_ids"]) for item in features)
        pad_id = self.tokenizer.pad_token_id
        input_ids, attention_mask, labels = [], [], []
        for item in features:
            size = len(item["input_ids"])
            padding = max_length - size
            input_ids.append(item["input_ids"] + [pad_id] * padding)
            attention_mask.append(item["attention_mask"] + [0] * padding)
            labels.append(item["labels"] + [-100] * padding)
        return {"input_ids": torch.tensor(input_ids), "attention_mask": torch.tensor(attention_mask), "labels": torch.tensor(labels)}


def _stamp_trainer_checkpoints(config: TrainingConfig, *, data_hash: str, cfg_hash: str) -> None:
    for checkpoint_dir in config.run_dir.glob("checkpoint-*"):
        if checkpoint_dir.is_dir():
            write_checkpoint_metadata(
                checkpoint_dir,
                training_config=_jsonable_config(config),
                dataset_hash=data_hash,
                config_hash=cfg_hash,
                base_model_id=config.base_model.model_id,
                lineage=_lineage(config),
            )


def _write_run_manifest(
    config: TrainingConfig,
    config_path: Path,
    dataset_manifest: dict[str, object],
    cfg_hash: str,
    resume_checkpoint: Path | None,
) -> None:
    shutil.copy2(config_path, config.run_dir / "training_config.yaml")
    manifest = {
        "character_id": config.character_id,
        "run_name": config.run_name,
        "method": config.method,
        "base_model_id": config.base_model.model_id,
        "base_gguf_path": _relative(config.base_model.gguf_path),
        "base_hf_path": _relative(config.base_model.hf_path) if config.base_model.hf_path else None,
        "dataset_manifest": dataset_manifest,
        "config_hash": cfg_hash,
        "resume_checkpoint": _relative(resume_checkpoint) if resume_checkpoint else None,
        "lineage": _lineage(config),
    }
    (config.run_dir / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _register_final_adapter(config: TrainingConfig, *, data_hash: str, cfg_hash: str) -> model_registry.Qwen3ModelEntry | None:
    adapter_path = config.run_dir / "adapter-final" / "adapter_model.gguf"
    if not adapter_path.exists():
        peft_path = config.run_dir / "adapter-final" / "adapter_model.safetensors"
        if peft_path.exists():
            print(
                "final PEFT LoRA adapter saved, but llama.cpp needs LoRA GGUF. "
                "Run quickstart/convert_qwen3_lora_to_gguf.ps1 to register it for runtime loading."
            )
            return None
        raise FileNotFoundError(f"final LoRA adapter was not saved: {adapter_path}")

    model_id, display_name = trained_model_identity(config)
    registry_dir = PROJECT_ROOT / "model_store" / "registry" / "qwen3" / config.character_id / model_id
    registry_dir.mkdir(parents=True, exist_ok=True)
    registered_adapter = registry_dir / adapter_path.name
    if adapter_path.resolve() != registered_adapter.resolve():
        shutil.copy2(adapter_path, registered_adapter)

    registration_log = config.run_dir / "registration_log.json"
    entry = model_registry.register_model(
        character_id=config.character_id,
        model_id=model_id,
        display_name=display_name,
        kind="trained",
        model_format="lora",
        required_files=[config.base_model.gguf_path, registered_adapter],
        base_model_id=config.base_model.model_id,
        data_hash=data_hash,
        config_hash=cfg_hash,
        source_run=_relative(config.run_dir),
        export_log=_relative(registration_log),
    )
    registration_log.write_text(json.dumps(entry.to_manifest(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return entry


def _conversation_records(path: Path) -> list[dict[str, object]]:
    """Read only the stable chat payload needed by the Trainer."""
    records: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            raw = json.loads(line)
            messages = raw.get("messages")
            if not isinstance(messages, list):
                raise ValueError(f"{path}:{line_number} must include messages")
            conversation: list[dict[str, str]] = []
            for message in messages:
                if not isinstance(message, dict) or not isinstance(message.get("role"), str) or not isinstance(message.get("content"), str):
                    raise ValueError(f"{path}:{line_number} contains an invalid message")
                conversation.append({"role": message["role"], "content": message["content"]})
            records.append({"messages": conversation})
    if not records:
        raise ValueError(f"{path} did not contain any examples")
    return records


def _jsonable_config(config: TrainingConfig) -> dict[str, object]:
    data = asdict(config)
    return _stringify_paths(data)


def _stringify_paths(value):
    if isinstance(value, Path):
        return _relative(value)
    if isinstance(value, dict):
        return {key: _stringify_paths(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_stringify_paths(item) for item in value]
    return value


def _lineage(config: TrainingConfig) -> dict[str, str | None]:
    generated = config.dataset.generated_dir
    report = generated / "generation_report.json" if generated else None
    persona = generated / "persona.json" if generated else None
    raw = config.dataset.quotes or config.dataset.raw
    return {
        "source_mode": config.dataset.mode,
        "raw_source_sha256": config_hash(raw) if raw.exists() else None,
        "generation_report_sha256": config_hash(report) if report and report.exists() else None,
        "persona_sha256": config_hash(persona) if persona and persona.exists() else None,
    }


def _relative(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def _version_tuple(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for part in value.replace("-", ".").replace("+", ".").split("."):
        if part.isdigit():
            parts.append(int(part))
        else:
            break
    return tuple(parts)


if __name__ == "__main__":
    raise SystemExit(main())
