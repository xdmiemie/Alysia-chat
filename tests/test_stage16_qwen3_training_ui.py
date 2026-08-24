from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from desktop.controllers.training_worker import (
    Qwen3Job,
    Qwen3JobState,
    Qwen3TrainingWorker,
    _training_powershell_args,
    load_incomplete_jobs,
)
from training.qwen3.train.train_lora import build_parser


def test_stage16_job_store_restores_paused_jobs(tmp_path: Path) -> None:
    payload = {
        "job_id": "paused-job", "kind": "training", "character_id": "elysia", "config_path": "job.yaml",
        "state": "PAUSED", "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:01+00:00", "error": "",
    }
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "paused-job.json").write_text(json.dumps(payload), encoding="utf-8")
    jobs = load_incomplete_jobs(tmp_path)
    assert jobs == [Qwen3Job(**{**payload, "state": Qwen3JobState.PAUSED})]


def test_stage16_job_store_recovers_interrupted_dataset_as_resumable_pause(tmp_path: Path) -> None:
    payload = {
        "job_id": "interrupted-job", "kind": "dataset", "character_id": "elysia", "config_path": "job.yaml",
        "state": "PAUSING", "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:01+00:00", "error": "",
    }
    (tmp_path / "interrupted-job.json").write_text(json.dumps(payload), encoding="utf-8")

    jobs = load_incomplete_jobs(tmp_path)

    assert jobs[0].state is Qwen3JobState.PAUSED
    assert json.loads((tmp_path / "interrupted-job.json").read_text(encoding="utf-8"))["state"] == "PAUSED"


def test_stage16_training_cli_exposes_cooperative_pause_and_finish_flags() -> None:
    args = build_parser().parse_args(["--pause-file", "pause.request", "--finish-file", "finish.request"])
    assert args.pause_file == "pause.request"
    assert args.finish_file == "finish.request"


def test_stage16_new_training_explicitly_ignores_old_checkpoints() -> None:
    fresh = _training_powershell_args(
        "storage/training_jobs/configs/new.yaml",
        pause_file=Path("pause.request"),
        finish_file=Path("finish.request"),
        resume=False,
    )
    resumed = _training_powershell_args(
        "storage/training_jobs/configs/new.yaml",
        pause_file=Path("pause.request"),
        finish_file=Path("finish.request"),
        resume=True,
        resume_from=r"D:\Alysia-chat\training\qwen3\outputs\elysia\run-a\checkpoint-20",
    )

    assert "-FromScratch" in fresh
    assert "-ResumeFrom" not in fresh
    assert resumed[resumed.index("-ResumeFrom") + 1].endswith("run-a\\checkpoint-20")
    assert "-FromScratch" not in resumed


def test_stage16_each_new_ui_training_run_uses_a_distinct_output_directory() -> None:
    from desktop.pages.training_page import _new_training_run_name

    first = _new_training_run_name("elysia-qwen3-lora")
    second = _new_training_run_name("elysia-qwen3-lora")

    assert first.startswith("elysia-qwen3-lora-")
    assert second.startswith("elysia-qwen3-lora-")
    assert first != second


def test_stage16_paused_training_remembers_its_exact_checkpoint(tmp_path: Path) -> None:
    outputs = tmp_path / "outputs"
    config = tmp_path / "job.yaml"
    config.write_text(
        f"character_id: violet\nrun_name: run-a\noutputs_dir: {json.dumps(str(outputs))}\n",
        encoding="utf-8",
    )
    older = outputs / "violet" / "run-a" / "checkpoint-813"
    newer = outputs / "violet" / "run-a" / "checkpoint-20"
    for checkpoint in (older, newer):
        checkpoint.mkdir(parents=True)
        (checkpoint / "alysia_checkpoint.json").write_text("{}", encoding="utf-8")
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))

    worker = Qwen3TrainingWorker(project_root=tmp_path)
    worker.job = Qwen3Job("paused", "training", "violet", str(config), Qwen3JobState.PAUSING, "now", "now")
    worker._remember_pause_checkpoint()

    assert worker.job is not None
    assert worker.job.metrics["resume_checkpoint"] == str(newer.resolve())


def test_stage16_legacy_paused_job_recovers_checkpoint_from_its_log(tmp_path: Path) -> None:
    outputs = tmp_path / "outputs"
    config = tmp_path / "job.yaml"
    config.write_text(
        f"character_id: violet\nrun_name: run-a\noutputs_dir: {json.dumps(str(outputs))}\n",
        encoding="utf-8",
    )
    checkpoint = outputs / "violet" / "run-a" / "checkpoint-20"
    checkpoint.mkdir(parents=True)
    (checkpoint / "alysia_checkpoint.json").write_text("{}", encoding="utf-8")
    worker = Qwen3TrainingWorker(project_root=tmp_path)
    job = Qwen3Job("legacy", "training", "violet", str(config), Qwen3JobState.PAUSED, "now", "now")
    (worker.job_root / "legacy.log").parent.mkdir(parents=True)
    (worker.job_root / "legacy.log").write_text(
        '[training_progress] {"completed":20}\nTRAINING_PAUSED: checkpoint saved\n', encoding="utf-8"
    )

    assert worker._resume_checkpoint_for(job) == str(checkpoint.resolve())


def test_stage16_dataset_completion_persists_a_ready_progress_state(tmp_path: Path) -> None:
    worker = Qwen3TrainingWorker(project_root=tmp_path)
    worker.job = Qwen3Job(
        "dataset-ready", "dataset", "elysia", "job.yaml", Qwen3JobState.BUILDING_DATASET,
        "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
    )

    worker._set_state(Qwen3JobState.DATASET_READY, progress=100.0, phase="complete", detail="训练集已就绪，可开始训练。")

    assert worker.job is not None
    assert worker.job.progress == 100.0
    assert worker.job.detail == "训练集已就绪，可开始训练。"


def test_stage16_training_ignores_internal_dataset_100_percent_progress(tmp_path: Path) -> None:
    worker = Qwen3TrainingWorker(project_root=tmp_path)
    worker.job = Qwen3Job("training-progress", "training", "elysia", "job.yaml", Qwen3JobState.RUNNING, "now", "now")

    worker._handle_output_line('[progress] {"phase":"complete","percent":100,"message":"dataset ready"}')
    assert worker.job.progress == 0.0
    worker._handle_output_line('[training_progress] {"completed":10,"total":1000,"percent":1.0,"metrics":{}}')

    assert worker.job.progress == 1.0
    assert worker.job.phase == "training"


def test_stage16_stopping_a_paused_dataset_clears_only_derived_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from desktop.pages import training_page

    dataset_root = tmp_path / "training" / "qwen3" / "datasets" / "violet"
    generated = dataset_root / "generated"; final = dataset_root / "final"; raw = dataset_root / "raw"
    generated.mkdir(parents=True); final.mkdir(); raw.mkdir()
    (generated / "persona.json").write_text("{}", encoding="utf-8")
    (final / "train.jsonl").write_text("{}\n", encoding="utf-8")
    (raw / "quotes.txt").write_text("keep me", encoding="utf-8")
    config = tmp_path / "job.yaml"
    config.write_text(
        f"character_id: violet\ndataset:\n  generated_dir: {generated}\n  train: {final / 'train.jsonl'}\n  validation: {final / 'validation.jsonl'}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(training_page, "_project_root", lambda: tmp_path)
    job = Qwen3Job("clear-results", "dataset", "violet", str(config), Qwen3JobState.PAUSED, "now", "now")

    removed = training_page.TrainingPage._clear_dataset_results(object(), job)

    assert removed == 2
    assert not generated.exists()
    assert not final.exists()
    assert (raw / "quotes.txt").read_text(encoding="utf-8") == "keep me"


def test_stage16_training_page_exposes_qwen3_sources_and_controls() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from desktop.pages import TrainingPage

    app = QApplication.instance() or QApplication([])
    page = TrainingPage()
    assert page.data_mode_combo.itemData(0) == "quotes"
    assert page.data_mode_combo.itemData(1) == "chat_jsonl"
    page.data_mode_combo.setCurrentIndex(0)
    assert page.build_dataset_button.isEnabled()
    assert "准备模型并构造" in page.build_dataset_button.text()
    assert "Qwen3-8B-Instruct" in page.generator_model_label.text()
    assert "跟随 Generator" in page.judge_model_label.text()
    assert page.assistant_loss_check.isChecked()
    assert page.pause_training_button.isEnabled() is False
    assert page.finish_training_button.isEnabled() is False
    assert page.start_training_button.isEnabled() is False
    page._set_task_buttons(Qwen3JobState.DATASET_READY)
    assert page.start_training_button.isEnabled() is True
    page.task_progress.setValue(100)
    page._show_training_model_loading(resume=True)
    assert page.task_progress.value() == 0
    assert "加载训练模型" in page.task_detail_label.text()
    assert page.start_training_button.isEnabled() is False
    page.training_worker.job = Qwen3Job("clear", "training", "elysia", "job.yaml", Qwen3JobState.RUNNING, "now", "now")
    page._set_task_buttons(Qwen3JobState.RUNNING)
    assert page.clear_training_data_button.isEnabled() is True
    assert page.page_scroll.widget().minimumHeight() >= 1120
    assert all(
        page.training_tabs.widget(index).widget().minimumHeight() >= 360
        for index in range(page.training_tabs.count())
    )
    page.close()
