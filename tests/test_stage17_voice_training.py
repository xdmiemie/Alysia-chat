from __future__ import annotations

import json
import os
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from training.gpt_sovits import desktop_job
from training.gpt_sovits.config import load_training_config
from training.gpt_sovits.preprocess.dataset_manifest import split_items, validate_dataset
from runtime.common.python_executable import resolve_runtime_python


def _wav(path: Path, *, frames: int = 32000, sample: bytes = b"\0\0") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(32000)
        stream.writeframes(sample * frames)


def _config(tmp_path: Path, *, samples: int = 1) -> Path:
    raw = tmp_path / "data" / "raw"
    manifest = tmp_path / "data" / "work" / "manifest.jsonl"
    manifest.parent.mkdir(parents=True)
    records = []
    for number in range(samples):
        filename = f"{number}.wav"
        _wav(raw / filename, sample=(number + 1).to_bytes(2, "little", signed=True))
        records.append({"audio_path": filename, "text": f"sentence {number}", "speaker": "test",
                        "emotion": "neutral", "language": "zh", "source": "manual"})
    manifest.write_text("\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8")
    config = {
        "character_id": "test", "run_name": "stage17", "speaker": "test", "language": "zh",
        "outputs_dir": str(tmp_path / "outputs"), "resume_from": "auto", "seed": 1,
        "dataset": {"root": str(tmp_path / "data"), "raw_audio_dir": str(raw),
                    "raw_manifest": str(manifest),
                    "normalized_manifest": str(tmp_path / "data" / "work" / "manifest.normalized.jsonl"),
                    "validation_report": str(tmp_path / "data" / "work" / "validation_report.json"),
                    "validation_ratio": 0.25},
        "s1": {"enabled": True, "command": "echo s1", "max_steps": 2, "save_steps": 1,
               "save_total_limit": 2, "batch_size": 1},
        "s2": {"enabled": True, "command": "echo s2", "max_steps": 2, "save_steps": 1,
               "save_total_limit": 2, "batch_size": 1},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def test_voice_validation_reports_sample_issues_and_stable_holdout(tmp_path):
    config_path = _config(tmp_path, samples=4)
    config = load_training_config(config_path)
    validation = validate_dataset(config)
    assert validation.ready
    assert validation.stats["train_samples"] == 3
    assert validation.stats["validation_samples"] == 1
    train, holdout = split_items(validation.items, .25)
    assert len(train) == 3 and len(holdout) == 1
    assert (config.dataset.normalized_manifest.parent / "manifest.train.jsonl").exists()
    assert (config.dataset.normalized_manifest.parent / "manifest.validation.jsonl").exists()

    records = [json.loads(line) for line in config.dataset.raw_manifest.read_text(encoding="utf-8").splitlines()]
    records[1]["speaker"] = "wrong-character"
    records[2]["text"] = ""
    records[3]["audio_path"] = "0.wav"
    config.dataset.raw_manifest.write_text("\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8")
    validation = validate_dataset(config)
    assert not validation.ready
    assert validation.stats["empty_text"] == 1
    assert validation.stats["duplicates"] == 1
    assert any("1.wav" in issue and "speaker" in issue for issue in validation.errors)
    assert any("2.wav" in issue and "text" in issue for issue in validation.errors)


def test_voice_job_pauses_at_checkpoint_then_activates_after_preview(tmp_path, monkeypatch):
    path = _config(tmp_path)
    config = load_training_config(path)
    pause = tmp_path / "pause"
    events = []
    call_count = 0

    def fake_run(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            pause.write_text("pause", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(desktop_job.subprocess, "run", fake_run)
    def fake_weights(_run_dir, stage):
        paths = [tmp_path / f"{stage}-{name}.pth" for name in (("export", "G", "D") if stage == "s2" else ("export", "lightning"))]
        for item in paths:
            item.write_bytes(b"checkpoint")
        return paths

    monkeypatch.setattr(desktop_job, "_require_weight", fake_weights)
    monkeypatch.setattr(desktop_job, "_run_module", lambda module, *_: events.append("registered") if module.endswith("collect_weights") else None)
    from runtime.gpt_sovits import model_registry
    candidate = SimpleNamespace(model_id="candidate", created_at="now", source_run=str(config.run_dir))
    monkeypatch.setattr(model_registry, "list_models", lambda _character: [candidate])
    monkeypatch.setattr(model_registry, "activate_model", lambda *_, **_kwargs: events.append("activated"))
    from training.gpt_sovits import preview
    monkeypatch.setattr(preview, "synthesize_fixed_set", lambda *_args, **_kwargs: events.append("previewed"))

    assert desktop_job.main(["train", "--config", str(path), "--pause-file", str(pause)]) == desktop_job.PAUSED_EXIT
    state = json.loads((config.run_dir / "desktop_progress.json").read_text(encoding="utf-8"))
    assert state["s2"] == 1 and state["s1"] == 0
    assert (config.run_dir / "s2" / "checkpoint-000001" / "alysia_checkpoint.json").exists()
    assert events == []

    pause.unlink()
    assert desktop_job.main(["train", "--config", str(path), "--resume"]) == 0
    assert events == ["registered", "previewed", "activated"]


def test_voice_job_keeps_active_model_when_preview_fails(tmp_path, monkeypatch):
    path = _config(tmp_path)
    config = load_training_config(path)
    monkeypatch.setattr(desktop_job.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(desktop_job, "_require_weight", lambda *_: None)
    monkeypatch.setattr(desktop_job, "_run_module", lambda *_: None)
    from runtime.gpt_sovits import model_registry
    candidate = SimpleNamespace(model_id="candidate", created_at="now", source_run=str(config.run_dir))
    monkeypatch.setattr(model_registry, "list_models", lambda _character: [candidate])
    activated = []
    monkeypatch.setattr(model_registry, "activate_model", lambda *_: activated.append(True))
    from training.gpt_sovits import preview
    def fail_preview(*_args, **_kwargs):
        raise RuntimeError("invalid candidate audio")
    monkeypatch.setattr(preview, "synthesize_fixed_set", fail_preview)

    with pytest.raises(RuntimeError, match="invalid candidate audio"):
        desktop_job.main(["train", "--config", str(path)])
    assert activated == []


def test_candidate_retention_never_prunes_the_active_voice(tmp_path, monkeypatch):
    from runtime.gpt_sovits import model_registry
    monkeypatch.setattr(model_registry, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(model_registry, "REGISTRY_ROOT", tmp_path / "registry")
    monkeypatch.setattr(model_registry, "ACTIVE_ROOT", tmp_path / "active")
    entries = []
    for number in range(7):
        model_id = f"trained-{number}"
        directory = model_registry.REGISTRY_ROOT / "test" / model_id
        directory.mkdir(parents=True)
        entries.append({"id": model_id, "kind": "trained", "created_at": f"2026-01-{number + 1:02d}",
                        "required_files": []})
    model_registry.save_character_manifest("test", {"character_id": "test",
                                                    "active_model_id": "trained-0", "models": entries})
    model_registry.prune_trained_models("test", keep=5)
    result = model_registry.load_character_manifest("test")
    assert result["active_model_id"] == "trained-0"
    assert {item["id"] for item in result["models"]} == {"trained-0", "trained-2", "trained-3", "trained-4", "trained-5", "trained-6"}


def test_stage17_desktop_page_exposes_voice_workflow():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from desktop.pages.voice_training_page import VoiceTrainingPage

    app = QApplication.instance() or QApplication([])
    page = VoiceTrainingPage()
    page.set_character_id("elysia")
    assert page.tabs.count() == 4
    assert page.dataset_root.text()
    assert page.manifest.text().endswith("manifest.jsonl")
    assert page.s1_epochs.value() > 0 and page.s2_epochs.value() > 0
    assert page.pause_button.isEnabled() is False
    page.set_runtime_busy(True)
    page._start("train")
    assert "关闭运行模型" in page.phase.text()
    assert page.worker.is_running is False
    page.close()


def test_voice_tasks_choose_project_runtime_over_desktop_python(tmp_path, monkeypatch):
    project_python = tmp_path / "runtime_env" / "python.exe"
    project_python.parent.mkdir()
    project_python.write_bytes(b"stub")
    override = tmp_path / "override.exe"
    override.write_bytes(b"stub")
    monkeypatch.setenv("ALYSIA_PYTHON", str(override))
    assert resolve_runtime_python(tmp_path) == project_python.resolve()
    project_python.unlink()
    assert resolve_runtime_python(tmp_path) == override.resolve()
    override.unlink()
    with pytest.raises(FileNotFoundError, match="ALYSIA_PYTHON"):
        resolve_runtime_python(tmp_path)


def test_desktop_preprocess_uses_version_free_semantic_config(tmp_path, monkeypatch):
    path = _config(tmp_path)
    from training.gpt_sovits.train import run_recipe
    expected = tmp_path / "tmp_s2_prepare.json"
    recipe = SimpleNamespace(s2_prepare_config=expected)
    used = []
    monkeypatch.setattr(desktop_job, "prepare_dataset", lambda _config: None)
    monkeypatch.setattr(run_recipe, "ensure_language_dependencies", lambda _validation: None)
    monkeypatch.setattr(run_recipe, "build_recipe", lambda _config, _validation: recipe)
    monkeypatch.setattr(run_recipe, "write_s1_config", lambda *_: None)
    monkeypatch.setattr(run_recipe, "write_s2_config", lambda *_: tmp_path / "tmp_s2.json")
    monkeypatch.setattr(run_recipe, "ensure_upstream_prepared",
                        lambda _config, _recipe, s2_config, **_kwargs: used.append(s2_config))

    assert desktop_job.main(["prepare", "--config", str(path)]) == 0
    assert used == [expected]


def test_voice_resume_rejects_missing_native_checkpoint(tmp_path):
    path = _config(tmp_path)
    config = load_training_config(path)
    from training.gpt_sovits.config import config_hash
    validation = validate_dataset(config)
    state_path = config.run_dir / "desktop_progress.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"dataset_hash": validation.dataset_hash,
                                      "config_hash": config_hash(path), "s1": 0,
                                      "s2": 1, "status": "paused"}), encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="检查点不完整"):
        desktop_job.main(["train", "--config", str(path), "--resume"])


def test_voice_task_controls_show_action_and_resume_on_click(monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt, QProcess
    from PySide6.QtWidgets import QApplication
    from desktop.pages.voice_training_page import VoiceTrainingPage

    app = QApplication.instance() or QApplication([])
    page = VoiceTrainingPage()
    page.worker.job = {"job_id": "job", "action": "train"}
    page.worker.process = SimpleNamespace(state=lambda: QProcess.ProcessState.Running)
    page._buttons("RUNNING")
    assert page.pause_button.isEnabled()
    assert page.pause_button.objectName() == "secondaryButton"
    assert page.pause_button.cursor().shape() == Qt.CursorShape.PointingHandCursor
    page._buttons("PAUSING")
    assert not page.pause_button.isEnabled()
    assert "保存检查点" in page.pause_button.text()
    assert page.pause_button.cursor().shape() == Qt.CursorShape.ArrowCursor

    page.worker.process = None
    page._paused_job = {"job_id": "job", "config_path": "saved.yaml", "character_id": "test"}
    page._buttons("PAUSED")
    assert page.pause_button.isEnabled()
    assert page.pause_button.text() == "继续训练"
    calls = []
    monkeypatch.setattr(page.worker, "start", lambda *args, **kwargs: calls.append((args, kwargs)))
    page.pause_button.click()
    assert calls[0][0] == ("train", Path("saved.yaml"))
    assert calls[0][1]["resume"]["job_id"] == "job"
    page._buttons("FAILED")
    assert page.pause_button.isEnabled()
    page.close()


def test_voice_clear_removes_only_current_run(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from desktop.pages.voice_training_page import VoiceTrainingPage

    app = QApplication.instance() or QApplication([])
    path = _config(tmp_path)
    config = load_training_config(path)
    config.run_dir.mkdir(parents=True)
    (config.run_dir / "checkpoint.bin").write_bytes(b"checkpoint")
    job = {"job_id": "clear-voice", "action": "train", "character_id": "test", "config_path": str(path), "state": "PAUSED"}
    page = VoiceTrainingPage()
    page.worker.root = tmp_path
    jobs = tmp_path / "storage" / "training_jobs"
    jobs.mkdir(parents=True)
    (jobs / "clear-voice.voice.json").write_text(json.dumps(job), encoding="utf-8")
    page._paused_job = job
    page._discard_training_results(job)

    assert not config.run_dir.exists()
    assert config.dataset.raw_manifest.exists()
    assert not (jobs / "clear-voice.voice.json").exists()
    assert page._paused_job is None
    assert page.start_button.isEnabled()
    page.close()
