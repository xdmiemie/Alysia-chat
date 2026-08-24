from __future__ import annotations

import json
from pathlib import Path

import pytest

from runtime.qwen3 import model_registry
from training.qwen3.config import BaseModelConfig, DatasetConfig, TrainingConfig, trained_model_identity
from training.qwen3.train.checkpoint_policy import write_checkpoint_metadata
from training.qwen3.train.resume import resolve_resume_checkpoint
from training.qwen3.train import train_lora
from training.qwen3.export import export_gguf
from training.qwen3.preprocess.normalize import normalize_jsonl


def test_qwen3_registry_keeps_base_and_latest_five_trained_models(tmp_path, monkeypatch):
    registry_root = tmp_path / "registry" / "qwen3"
    active_manifest = tmp_path / "active" / "qwen3.json"
    monkeypatch.setattr(model_registry, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(model_registry, "REGISTRY_ROOT", registry_root)
    monkeypatch.setattr(model_registry, "ACTIVE_MANIFEST", active_manifest)

    base_path = registry_root / "elysia" / "base-v1" / "base.gguf"
    base_path.parent.mkdir(parents=True)
    base_path.write_text("base", encoding="utf-8")
    model_registry.register_model(
        character_id="elysia",
        model_id="base-v1",
        display_name="Base",
        kind="base",
        model_format="gguf",
        required_files=[base_path],
        activate=True,
    )

    for index in range(6):
        adapter = registry_root / "elysia" / f"trained-{index}" / "adapter.safetensors"
        adapter.parent.mkdir(parents=True)
        adapter.write_text(str(index), encoding="utf-8")
        model_registry.register_model(
            character_id="elysia",
            model_id=f"trained-{index}",
            display_name=f"trained {index}",
            kind="trained",
            model_format="lora",
            required_files=[base_path, adapter],
            base_model_id="base-v1",
            trained_limit=5,
        )
        if index == 0:
            model_registry.activate_model("elysia", "trained-0")

    manifest = json.loads((registry_root / "elysia" / "models.json").read_text(encoding="utf-8"))
    trained_ids = [model["id"] for model in manifest["models"] if model["kind"] == "trained"]
    assert trained_ids == ["trained-1", "trained-2", "trained-3", "trained-4", "trained-5"]
    assert (registry_root / "elysia" / "base-v1" / "base.gguf").exists()
    assert not (registry_root / "elysia" / "trained-0").exists()
    assert json.loads(active_manifest.read_text(encoding="utf-8"))["model_id"] == "base-v1"


def test_auto_resume_starts_fresh_when_no_compatible_checkpoint_exists(tmp_path):
    run_dir = tmp_path / "run"
    checkpoint = run_dir / "checkpoint-200"
    write_checkpoint_metadata(
        checkpoint,
        training_config={"save_steps": 200},
        dataset_hash="old-data",
        config_hash="same-config",
        base_model_id="base-v1",
    )

    assert (
        resolve_resume_checkpoint(
            run_dir,
            resume_from="auto",
            from_scratch=False,
            dataset_hash="new-data",
            config_hash="same-config",
            base_model_id="base-v1",
        )
        is None
    )


def test_manual_resume_rejects_checkpoint_from_different_dataset(tmp_path):
    run_dir = tmp_path / "run"
    checkpoint = run_dir / "checkpoint-200"
    write_checkpoint_metadata(
        checkpoint,
        training_config={"save_steps": 200},
        dataset_hash="old-data",
        config_hash="same-config",
        base_model_id="base-v1",
    )

    with pytest.raises(ValueError, match="different dataset"):
        resolve_resume_checkpoint(
            run_dir,
            resume_from="checkpoint-200",
            from_scratch=False,
            dataset_hash="new-data",
            config_hash="same-config",
            base_model_id="base-v1",
        )


def test_from_scratch_ignores_existing_checkpoint(tmp_path):
    run_dir = tmp_path / "run"
    checkpoint = run_dir / "checkpoint-200"
    write_checkpoint_metadata(
        checkpoint,
        training_config={"save_steps": 200},
        dataset_hash="old-data",
        config_hash="old-config",
        base_model_id="base-v1",
    )

    assert (
        resolve_resume_checkpoint(
            run_dir,
            resume_from="auto",
            from_scratch=True,
            dataset_hash="new-data",
            config_hash="new-config",
            base_model_id="base-v1",
        )
        is None
    )


def test_training_reads_only_conversation_payload_not_heterogeneous_metadata(tmp_path):
    source = tmp_path / "examples.jsonl"
    source.write_text(
        "\n".join([
            json.dumps({"messages": [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}], "metadata": {"source_quote_ids": []}}),
            json.dumps({"messages": [{"role": "user", "content": "c"}, {"role": "assistant", "content": "d"}], "metadata": {"source_quote_ids": ["q-1"]}}),
        ]) + "\n",
        encoding="utf-8",
    )

    assert train_lora._conversation_records(source) == [
        {"messages": [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]},
        {"messages": [{"role": "user", "content": "c"}, {"role": "assistant", "content": "d"}]},
    ]


def test_final_adapter_is_registered_as_role_model(tmp_path, monkeypatch):
    monkeypatch.setattr(model_registry, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(model_registry, "REGISTRY_ROOT", tmp_path / "model_store" / "registry" / "qwen3")
    monkeypatch.setattr(model_registry, "ACTIVE_MANIFEST", tmp_path / "model_store" / "active" / "qwen3.json")
    monkeypatch.setattr(train_lora, "PROJECT_ROOT", tmp_path)

    base_path = tmp_path / "model_store" / "registry" / "qwen3" / "elysia" / "base-v1" / "base.gguf"
    base_path.parent.mkdir(parents=True)
    base_path.write_text("base", encoding="utf-8")
    model_registry.register_model(
        character_id="elysia",
        model_id="base-v1",
        display_name="Base",
        kind="base",
        model_format="gguf",
        required_files=[base_path],
        activate=True,
    )

    run_dir = tmp_path / "training" / "qwen3" / "outputs" / "elysia" / "elysia-qwen3-lora"
    final_dir = run_dir / "adapter-final"
    final_dir.mkdir(parents=True)
    (final_dir / "adapter_model.gguf").write_bytes(b"GGUFadapter")

    config = TrainingConfig(
        character_id="elysia",
        run_name="elysia-qwen3-lora",
        outputs_dir=tmp_path / "training" / "qwen3" / "outputs",
        base_model=BaseModelConfig(model_id="base-v1", gguf_path=base_path),
        dataset=DatasetConfig(
            raw=tmp_path / "raw.jsonl",
            normalized=tmp_path / "normalized.jsonl",
            train=tmp_path / "train.jsonl",
            validation=tmp_path / "validation.jsonl",
        ),
    )

    entry = train_lora._register_final_adapter(config, data_hash="data", cfg_hash="config")

    models = model_registry.list_models("elysia")
    assert entry.model_id in {model.model_id for model in models}
    assert entry.model_format == "lora"
    assert entry.lora_adapter is not None
    assert entry.lora_adapter.exists()
    assert entry.lora_adapter.suffix == ".gguf"
    assert entry.lora_adapter.parent.parent.name == "elysia"
    assert json.loads((run_dir / "registration_log.json").read_text(encoding="utf-8"))["model_id"] == entry.model_id


def test_custom_training_model_name_is_preserved_for_display_and_safe_for_registry():
    config = TrainingConfig(
        character_id="elysia",
        run_name="elysia-qwen3-lora",
        model_name="爱莉希雅 · 夏日语录 LoRA",
        outputs_dir=Path("outputs"),
        base_model=BaseModelConfig(model_id="base-v1", gguf_path=Path("base.gguf")),
        dataset=DatasetConfig(raw=Path("raw"), normalized=Path("normalized"), train=Path("train"), validation=Path("validation")),
    )

    model_id, display_name = trained_model_identity(config)

    assert display_name == "爱莉希雅 · 夏日语录 LoRA"
    assert model_id.startswith("elysia-lora-")
    assert model_id.endswith("-final")


def test_registering_peft_safetensors_as_llama_lora_is_rejected(tmp_path):
    adapter = tmp_path / "adapter_model.safetensors"
    adapter.write_bytes(b"\x28\x08not gguf")

    assert not export_gguf._is_gguf_file(adapter)


def test_normalize_accepts_utf8_bom_jsonl(tmp_path):
    source = tmp_path / "raw.jsonl"
    output = tmp_path / "normalized.jsonl"
    source.write_text(
        '\ufeff{"instruction":"hello","output":"hi"}\n',
        encoding="utf-8",
    )

    count = normalize_jsonl(source, output, "system")

    assert count == 1
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["messages"][1]["content"] == "hello"
    assert record["messages"][2]["content"] == "hi"
