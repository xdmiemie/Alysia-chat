from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest
import yaml

from runtime.gpt_sovits import model_registry
from training.gpt_sovits.config import (
    PROJECT_ROOT,
    BaseModelConfig,
    DatasetConfig,
    ExportConfig,
    StageConfig,
    TrainingConfig,
    file_sha256,
    load_training_config,
)
from training.gpt_sovits.preprocess.dataset_manifest import prepare_dataset, validate_dataset
from training.gpt_sovits.export.collect_weights import main as collect_weights_main
from training.gpt_sovits.train.common import run_stage
from training.gpt_sovits.train.run_recipe import ensure_language_dependencies, main as run_recipe_main


def test_gpt_sovits_dataset_reports_missing_manifest(tmp_path):
    config = _training_config(tmp_path)

    validation = validate_dataset(config)

    assert not validation.ready
    assert validation.dataset_hash == "empty"
    assert "missing processed dataset manifest" in validation.errors[0]


def test_gpt_sovits_prepare_scans_raw_wavs_and_writes_manifest(tmp_path, monkeypatch):
    config = _training_config(tmp_path)
    clips = config.dataset.raw_audio_dir / "clips"
    clips.mkdir(parents=True)
    wav_path = clips / "human-friendly-name.wav"
    _write_wav(wav_path, duration_seconds=1.25)

    class FakeRecognizer:
        @classmethod
        def from_config(cls, config_path):
            return cls()

        def transcribe(self, audio_path):
            from runtime.sensevoice.types import TranscriptionResult

            return TranscriptionResult(
                text="你好呀",
                language="zh",
                emotion="neutral",
                event="speech",
                duration_ms=1250,
                inference_ms=3,
            )

    monkeypatch.setattr("training.gpt_sovits.preprocess.dataset_manifest.SenseVoiceRecognizer", FakeRecognizer)

    prepared = prepare_dataset(config)

    assert prepared
    validation = validate_dataset(config)
    assert validation.ready
    assert validation.dataset_hash != "empty"
    assert config.dataset.raw_manifest.exists()
    assert config.dataset.normalized_manifest.exists()
    normalized = json.loads(config.dataset.normalized_manifest.read_text(encoding="utf-8"))
    assert normalized["audio_path"] == "clips/human-friendly-name.wav"
    assert normalized["text"] == "你好呀"
    assert normalized["sample_rate"] == 32000
    assert normalized["channels"] == 1


def test_gpt_sovits_prepare_skips_when_raw_wavs_are_unchanged(tmp_path, monkeypatch):
    config = _training_config(tmp_path)
    raw = config.dataset.raw_audio_dir
    raw.mkdir(parents=True)
    _write_wav(raw / "clip.wav", duration_seconds=1.1)
    calls = 0

    class FakeRecognizer:
        @classmethod
        def from_config(cls, config_path):
            return cls()

        def transcribe(self, audio_path):
            nonlocal calls
            calls += 1
            from runtime.sensevoice.types import TranscriptionResult

            return TranscriptionResult("hello", "zh", "neutral", "speech", 1100, 1)

    monkeypatch.setattr("training.gpt_sovits.preprocess.dataset_manifest.SenseVoiceRecognizer", FakeRecognizer)

    assert prepare_dataset(config)
    assert not prepare_dataset(config)
    assert calls == 1


def test_gpt_sovits_prepare_unifies_language_to_majority(tmp_path, monkeypatch):
    config = _training_config(tmp_path)
    raw = config.dataset.raw_audio_dir
    raw.mkdir(parents=True)
    for name in ("a.wav", "b.wav", "c.wav"):
        _write_wav(raw / name, duration_seconds=1.1)

    class FakeRecognizer:
        @classmethod
        def from_config(cls, config_path):
            return cls()

        def transcribe(self, audio_path):
            from runtime.sensevoice.types import TranscriptionResult

            language = "ja" if Path(audio_path).name == "b.wav" else "zh"
            return TranscriptionResult(Path(audio_path).stem, language, "neutral", "speech", 1100, 1)

    monkeypatch.setattr("training.gpt_sovits.preprocess.dataset_manifest.SenseVoiceRecognizer", FakeRecognizer)

    assert prepare_dataset(config)

    records = [
        json.loads(line)
        for line in config.dataset.raw_manifest.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert {record["language"] for record in records} == {"zh"}


def test_gpt_sovits_prepare_normalizes_current_manifest_language_without_retranscribing(tmp_path, monkeypatch):
    config = _training_config(tmp_path)
    raw = config.dataset.raw_audio_dir
    raw.mkdir(parents=True)
    for name in ("a.wav", "b.wav", "c.wav"):
        _write_wav(raw / name, duration_seconds=1.1)
    audio_index = {path.name: path for path in sorted(raw.glob("*.wav"))}
    config.dataset.raw_manifest.parent.mkdir(parents=True, exist_ok=True)
    config.dataset.raw_manifest.write_text(
        "\n".join(
            json.dumps(
                {
                    "audio_path": name,
                    "text": Path(name).stem,
                    "speaker": "elysia",
                    "emotion": "neutral",
                    "language": "ja" if name == "b.wav" else "zh",
                    "source": "sensevoice",
                    "audio_hash": file_sha256(audio_index[name]),
                },
                ensure_ascii=False,
            )
            for name in ("a.wav", "b.wav", "c.wav")
        )
        + "\n",
        encoding="utf-8",
    )

    class FailingRecognizer:
        @classmethod
        def from_config(cls, config_path):
            raise AssertionError("unchanged datasets should not be retranscribed")

    monkeypatch.setattr("training.gpt_sovits.preprocess.dataset_manifest.SenseVoiceRecognizer", FailingRecognizer)

    assert prepare_dataset(config)
    assert not prepare_dataset(config)
    records = [
        json.loads(line)
        for line in config.dataset.raw_manifest.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert {record["language"] for record in records} == {"zh"}


def test_gpt_sovits_validation_skips_out_of_range_audio(tmp_path):
    config = _training_config(tmp_path)
    raw = config.dataset.raw_audio_dir
    raw.mkdir(parents=True)
    _write_wav(raw / "ok.wav", duration_seconds=1.1)
    _write_wav(raw / "short.wav", duration_seconds=0.5)
    _write_wav(raw / "long.wav", duration_seconds=13.0)
    config.dataset.raw_manifest.parent.mkdir(parents=True, exist_ok=True)
    config.dataset.raw_manifest.write_text(
        "\n".join(
            [
                '{"audio_path":"ok.wav","text":"ok","speaker":"elysia","emotion":"neutral","language":"zh","source":"manual"}',
                '{"audio_path":"short.wav","text":"short","speaker":"elysia","emotion":"neutral","language":"zh","source":"manual"}',
                '{"audio_path":"long.wav","text":"long","speaker":"elysia","emotion":"neutral","language":"zh","source":"manual"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    validation = validate_dataset(config)

    assert validation.ready
    assert [item.audio_path for item in validation.items] == ["ok.wav"]
    assert any("skipped audio shorter" in warning for warning in validation.warnings)
    assert any("skipped audio longer" in warning for warning in validation.warnings)


def test_gpt_sovits_dry_run_checkpoint_uses_resume_metadata(tmp_path):
    config = _training_config(tmp_path)
    clips = config.dataset.raw_audio_dir / "clips"
    clips.mkdir(parents=True)
    _write_wav(clips / "000001.wav", duration_seconds=1.1)
    config.dataset.raw_manifest.parent.mkdir(parents=True, exist_ok=True)
    config.dataset.raw_manifest.write_text(
        '{"audio_path":"clips/000001.wav","text":"hello","speaker":"elysia","emotion":"neutral","language":"zh","source":"manual"}\n',
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("character_id: elysia\n", encoding="utf-8")
    validation = validate_dataset(config)

    checkpoint = run_stage(
        config=config,
        config_path=config_path,
        validation=validation,
        cfg_hash="config",
        stage_name="s1",
        stage_config=config.s1,
        resume_from="auto",
        from_scratch=False,
        dry_run=True,
        project_root=tmp_path,
    )

    assert checkpoint is not None
    metadata = json.loads((checkpoint / "alysia_checkpoint.json").read_text(encoding="utf-8"))
    assert metadata["dataset_hash"] == validation.dataset_hash
    assert (checkpoint / "s1.ckpt").exists()


def test_gpt_sovits_default_config_uses_fixed_recipe_commands():
    config = load_training_config(PROJECT_ROOT / "training/gpt_sovits/configs/characters/elysia.yaml")

    assert config.s1.command == "{python} -m training.gpt_sovits.train.run_recipe --stage s1 --config {config}"
    assert config.s2.command == "{python} -m training.gpt_sovits.train.run_recipe --stage s2 --config {config}"


def test_gpt_sovits_recipe_dry_config_generates_upstream_files(tmp_path):
    config = _training_config(tmp_path)
    clips = config.dataset.raw_audio_dir / "nested"
    clips.mkdir(parents=True)
    _write_wav(clips / "human-name.wav", duration_seconds=1.2)
    config.dataset.raw_manifest.parent.mkdir(parents=True, exist_ok=True)
    config.dataset.raw_manifest.write_text(
        '{"audio_path":"nested/human-name.wav","text":"hello world","speaker":"elysia","emotion":"neutral","language":"zh","source":"manual"}\n',
        encoding="utf-8",
    )
    config_path = _write_training_yaml(tmp_path, config)

    assert run_recipe_main(["--stage", "s1", "--config", str(config_path), "--dry-config"]) == 0

    recipe_dir = config.run_dir / "recipe"
    filelist = (recipe_dir / "0-filelist.list").read_text(encoding="utf-8")
    generated_name = filelist.split("|", 1)[0]
    assert generated_name.startswith("000001_")
    assert generated_name.endswith(".wav")
    assert "human-name.wav" not in filelist
    assert (recipe_dir / "audio" / generated_name).exists()

    s1_config = yaml.full_load((recipe_dir / "tmp_s1.yaml").read_text(encoding="utf-8"))
    s2_config = json.loads((recipe_dir / "tmp_s2.json").read_text(encoding="utf-8"))
    s2_prepare_config = json.loads((recipe_dir / "tmp_s2_prepare.json").read_text(encoding="utf-8"))
    assert s1_config["train_phoneme_path"] == str(recipe_dir / "2-name2text.txt")
    assert s1_config["train_semantic_path"] == str(recipe_dir / "6-name2semantic.tsv")
    assert isinstance(s1_config["optimizer"]["lr_init"], float)
    assert s1_config["data"]["num_workers"] == 0
    assert s2_config["data"]["exp_dir"] == str(recipe_dir)
    assert s2_config["s2_ckpt_dir"] == str(recipe_dir / "logs_s2_v1")
    assert s2_config["model"]["version"] == "v1"
    assert "version" not in s2_prepare_config["model"]


def test_gpt_sovits_recipe_preflights_japanese_dependency(tmp_path, monkeypatch):
    config = _training_config(tmp_path)
    raw = config.dataset.raw_audio_dir
    raw.mkdir(parents=True)
    _write_wav(raw / "clip.wav", duration_seconds=1.2)
    config.dataset.raw_manifest.parent.mkdir(parents=True, exist_ok=True)
    config.dataset.raw_manifest.write_text(
        '{"audio_path":"clip.wav","text":"Oh。","speaker":"elysia","emotion":"neutral","language":"ja","source":"manual"}\n',
        encoding="utf-8",
    )
    validation = validate_dataset(config)
    monkeypatch.setattr("training.gpt_sovits.train.run_recipe.importlib.util.find_spec", lambda module: None)

    with pytest.raises(RuntimeError, match="pyopenjtalk"):
        ensure_language_dependencies(validation)


def test_gpt_sovits_registry_keeps_base_and_latest_five_trained_models(tmp_path, monkeypatch):
    monkeypatch.setattr(model_registry, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(model_registry, "REGISTRY_ROOT", tmp_path / "model_store" / "registry" / "gpt_sovits")
    monkeypatch.setattr(model_registry, "ACTIVE_ROOT", tmp_path / "model_store" / "active")

    base_dir = tmp_path / "model_store" / "registry" / "gpt_sovits" / "elysia" / "base-v1"
    base_files = _write_model_files(base_dir)
    model_registry.register_model(
        character_id="elysia",
        model_id="base-v1",
        display_name="Base",
        kind="base",
        required_files=base_files,
        activate=True,
    )

    for index in range(6):
        model_dir = tmp_path / "model_store" / "registry" / "gpt_sovits" / "elysia" / f"trained-{index}"
        files = _write_model_files(model_dir)
        model_registry.register_model(
            character_id="elysia",
            model_id=f"trained-{index}",
            display_name=f"Trained {index}",
            kind="trained",
            required_files=files,
            trained_limit=5,
        )
        if index == 0:
            model_registry.activate_model("elysia", "trained-0")

    manifest = json.loads((model_registry.REGISTRY_ROOT / "elysia" / "models.json").read_text(encoding="utf-8"))
    trained_ids = [item["id"] for item in manifest["models"] if item["kind"] == "trained"]
    assert trained_ids == ["trained-1", "trained-2", "trained-3", "trained-4", "trained-5"]
    assert json.loads((model_registry.ACTIVE_ROOT / "gpt_sovits_elysia.json").read_text(encoding="utf-8"))[
        "model_id"
    ] == "base-v1"


def test_gpt_sovits_registry_backfills_base_when_trained_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(model_registry, "REGISTRY_ROOT", tmp_path / "model_store" / "registry" / "gpt_sovits")
    monkeypatch.setattr(model_registry, "ACTIVE_ROOT", tmp_path / "model_store" / "active")
    base_dir = model_registry.REGISTRY_ROOT / "elysia" / "base-v1"
    _write_model_files(base_dir)
    trained_dir = model_registry.REGISTRY_ROOT / "elysia" / "trained"
    trained_files = _write_model_files(trained_dir)
    manifest_path = model_registry.REGISTRY_ROOT / "elysia" / "models.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "character_id": "elysia",
                "active_model_id": "trained",
                "models": [
                    {
                        "id": "trained",
                        "display_name": "Trained",
                        "kind": "trained",
                        "required_files": [path.relative_to(tmp_path).as_posix() for path in trained_files],
                        "created_at": "2026-07-27T00:00:00+00:00",
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    models = model_registry.list_models("elysia")

    assert [model.model_id for model in models] == ["base-v1", "trained"]
    assert model_registry.load_character_manifest("elysia")["active_model_id"] == "trained"


def test_gpt_sovits_collect_weights_auto_discovers_run_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(model_registry, "REGISTRY_ROOT", tmp_path / "model_store" / "registry" / "gpt_sovits")
    monkeypatch.setattr(model_registry, "ACTIVE_ROOT", tmp_path / "model_store" / "active")
    config = _training_config(tmp_path)
    clips = config.dataset.raw_audio_dir
    clips.mkdir(parents=True)
    _write_wav(clips / "clip.wav", duration_seconds=1.1)
    config.dataset.raw_manifest.parent.mkdir(parents=True, exist_ok=True)
    config.dataset.raw_manifest.write_text(
        '{"audio_path":"clip.wav","text":"hello","speaker":"elysia","emotion":"neutral","language":"zh","source":"manual"}\n',
        encoding="utf-8",
    )
    config_path = _write_training_yaml(tmp_path, config)
    gpt_weight = config.run_dir / "weights" / "GPT_weights" / "elysia-e1.ckpt"
    sovits_weight = config.run_dir / "weights" / "SoVITS_weights" / "elysia_e1_s10.pth"
    references = config.run_dir / "references"
    gpt_weight.parent.mkdir(parents=True)
    sovits_weight.parent.mkdir(parents=True)
    references.mkdir(parents=True)
    gpt_weight.write_text("gpt", encoding="utf-8")
    sovits_weight.write_text("sovits", encoding="utf-8")
    _write_wav(references / "neutral_01.wav", duration_seconds=1.0)
    (references / "neutral_01.txt").write_text("hello\n", encoding="utf-8")

    assert collect_weights_main(["--config", str(config_path), "--model-id", "auto-collected"]) == 0

    registry_dir = config.export.registry_root / config.character_id / "auto-collected"
    manifest = json.loads((model_registry.REGISTRY_ROOT / config.character_id / "models.json").read_text(encoding="utf-8"))
    trained = [item for item in manifest["models"] if item["id"] == "auto-collected"][0]
    assert (registry_dir / gpt_weight.name).exists()
    assert (registry_dir / sovits_weight.name).exists()
    assert (registry_dir / "references" / "neutral_01.wav").exists()
    assert any(path.endswith("elysia-e1.ckpt") for path in trained["required_files"])


def _training_config(tmp_path: Path) -> TrainingConfig:
    dataset_root = tmp_path / "training" / "gpt_sovits" / "datasets" / "elysia"
    raw_manifest = dataset_root / "work" / "manifest.jsonl"
    return TrainingConfig(
        character_id="elysia",
        run_name="elysia-gpt-sovits",
        speaker="elysia",
        language="zh",
        outputs_dir=tmp_path / "training" / "gpt_sovits" / "outputs",
        resume_from="auto",
        seed=20260727,
        dataset=DatasetConfig(
            root=dataset_root,
            raw_audio_dir=dataset_root / "raw",
            raw_manifest=raw_manifest,
            normalized_manifest=dataset_root / "work" / "manifest.normalized.jsonl",
            validation_report=dataset_root / "work" / "validation_report.json",
            min_duration_seconds=1.0,
            max_duration_seconds=12.0,
            target_sample_rate=32000,
        ),
        base_model=BaseModelConfig(
            model_id="base",
            pretrained_root=tmp_path / "pretrained",
            s1_checkpoint=tmp_path / "pretrained" / "s1.ckpt",
            s2_checkpoint=tmp_path / "pretrained" / "s2.pth",
        ),
        s1=StageConfig(True, "", 1000, 200, 5, "best"),
        s2=StageConfig(True, "", 1000, 200, 5, "best"),
        export=ExportConfig(tmp_path / "model_store" / "registry" / "gpt_sovits", "{character}-{timestamp}"),
    )


def _write_training_yaml(tmp_path: Path, config: TrainingConfig) -> Path:
    config_path = tmp_path / "elysia.yaml"
    config_path.write_text(
        f"""
character_id: {config.character_id}
run_name: {config.run_name}
speaker: {config.speaker}
language: {config.language}
outputs_dir: {config.outputs_dir.as_posix()}
resume_from: auto
seed: {config.seed}

base_model:
  model_id: {config.base_model.model_id}
  pretrained_root: {config.base_model.pretrained_root.as_posix()}
  s1_checkpoint: {config.base_model.s1_checkpoint.as_posix()}
  s2_checkpoint: {config.base_model.s2_checkpoint.as_posix()}

dataset:
  root: {config.dataset.root.as_posix()}
  raw_audio_dir: {config.dataset.raw_audio_dir.as_posix()}
  raw_manifest: {config.dataset.raw_manifest.as_posix()}
  normalized_manifest: {config.dataset.normalized_manifest.as_posix()}
  validation_report: {config.dataset.validation_report.as_posix()}
  min_duration_seconds: {config.dataset.min_duration_seconds}
  max_duration_seconds: {config.dataset.max_duration_seconds}
  target_sample_rate: {config.dataset.target_sample_rate}
  allowed_extensions: .wav,.flac,.mp3,.m4a

s1:
  enabled: true
  command: "{{python}} -m training.gpt_sovits.train.run_recipe --stage s1 --config {{config}}"
  max_steps: {config.s1.max_steps}
  save_steps: {config.s1.save_steps}
  save_total_limit: {config.s1.save_total_limit}
  best_checkpoint_name: {config.s1.best_checkpoint_name}

s2:
  enabled: true
  command: "{{python}} -m training.gpt_sovits.train.run_recipe --stage s2 --config {{config}}"
  max_steps: {config.s2.max_steps}
  save_steps: {config.s2.save_steps}
  save_total_limit: {config.s2.save_total_limit}
  best_checkpoint_name: {config.s2.best_checkpoint_name}

export:
  registry_root: {config.export.registry_root.as_posix()}
  model_id_template: "{config.export.model_id_template}"
  references_emotion: {config.export.references_emotion}
""".lstrip(),
        encoding="utf-8",
    )
    return config_path


def _write_wav(path: Path, *, duration_seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sample_rate = 32000
    frames = int(sample_rate * duration_seconds)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\0\0" * frames)


def _write_model_files(model_dir: Path) -> list[Path]:
    model_dir.mkdir(parents=True, exist_ok=True)
    gpt = model_dir / "model.ckpt"
    sovits = model_dir / "model.pth"
    reference = model_dir / "references" / "neutral.wav"
    reference.parent.mkdir(parents=True, exist_ok=True)
    gpt.write_text("gpt", encoding="utf-8")
    sovits.write_text("sovits", encoding="utf-8")
    _write_wav(reference, duration_seconds=1.0)
    return [gpt, sovits, reference]
