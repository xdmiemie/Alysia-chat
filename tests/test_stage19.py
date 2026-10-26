from __future__ import annotations

import json
import os
import time
from array import array
from pathlib import Path


def test_recovery_finds_interrupted_jobs_and_cleans_stale_pid(tmp_path: Path) -> None:
    from runtime.common.recovery import begin_session, end_session

    state = tmp_path / "storage" / "state" / "last_session.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"clean_exit": false}', encoding="utf-8")
    job = tmp_path / "storage" / "training_jobs" / "job.voice.json"
    job.parent.mkdir(parents=True)
    job.write_text('{"job_id": "job", "state": "RUNNING"}', encoding="utf-8")
    manifest = tmp_path / "model_store" / "active" / "broken.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{broken", encoding="utf-8")
    pid = tmp_path / "storage" / "temporary" / "qwen3-llama-server.pid"
    pid.parent.mkdir(parents=True)
    pid.write_text("0", encoding="utf-8")
    selection = pid.parent / "qwen3-selection.json"
    selection.write_text('{"pid": 0}', encoding="utf-8")

    report = begin_session(tmp_path)
    assert report.interrupted
    assert report.unfinished_jobs == ("job",)
    assert report.invalid_manifests == ("broken.json",)
    assert report.stale_pids == (pid.name,)
    assert not pid.exists()
    assert not selection.exists()
    assert json.loads(state.read_text(encoding="utf-8"))["pid"] == os.getpid()
    end_session(tmp_path)
    assert not begin_session(tmp_path).interrupted
    end_session(tmp_path)


def test_pid_probe_is_read_only() -> None:
    from runtime.common.processes import pid_is_alive

    assert pid_is_alive(os.getpid())
    assert not pid_is_alive(0)


def test_timeouts_load_nested_config(tmp_path: Path) -> None:
    from runtime.common.timeouts import load_timeouts

    path = tmp_path / "timeouts.yaml"
    path.write_text("timeouts:\n  asr_seconds: 12\n  model_switch_seconds: 250\n", encoding="utf-8")
    config = load_timeouts(path)
    assert config.asr_seconds == 12
    assert config.model_switch_seconds == 250
    assert config.training_stop_seconds == 300


def test_asr_reports_elapsed_timeout(tmp_path: Path, monkeypatch) -> None:
    import pytest

    from runtime.audio.audio_format import write_pcm16_wav
    from runtime.common.timeouts import TimeoutConfig
    from runtime.sensevoice.config import SenseVoiceConfig
    from runtime.sensevoice.service import SenseVoiceRecognizer
    from runtime.sensevoice.types import RawTranscription
    import runtime.sensevoice.service as service

    wav = write_pcm16_wav(tmp_path / "speech.wav", array("h", [0, 1200, -1200, 800] * 2000).tobytes(), sample_rate=16000, channels=1)

    class SlowBackend:
        def transcribe(self, _audio_path):
            time.sleep(0.02)
            return RawTranscription(text="hello", inference_ms=20)

    monkeypatch.setattr(service, "load_timeouts", lambda: TimeoutConfig(asr_seconds=0.001))
    with pytest.raises(TimeoutError):
        SenseVoiceRecognizer(SenseVoiceConfig(), SlowBackend()).transcribe(wav)


def test_training_waits_for_inference_shutdown_and_blocks_restart(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from desktop.controllers.app_controller import AppController
    from desktop.state import AppSnapshot, AppState
    from runtime.conversation import ConversationStore

    app = QApplication.instance() or QApplication([])
    controller = AppController(
        state=AppState(AppSnapshot(model_service_running=True)),
        conversation_store=ConversationStore(tmp_path / "conversations.db"),
    )
    controller._health_timer.stop()
    actions: list[str] = []
    controller._start_service_worker = lambda action, *_args: actions.append(action)
    ready: list[bool] = []

    assert controller.request_training_mode(lambda: ready.append(True))
    assert actions == ["stop"]
    assert ready == []
    assert not controller.state.snapshot.chat_enabled
    controller._service_action = "stop"
    controller._on_service_succeeded(False)
    assert ready == [True]
    assert controller._resource_mode == "TRAIN_MODE"
    controller.start_model_service()
    assert actions == ["stop"]
    controller.training_finished()
    assert controller._resource_mode == "USE_MODE"

    class FinishingThread:
        def deleteLater(self) -> None:
            pass

    controller._service_thread = FinishingThread()
    controller._service_action = "start"
    controller._restore_pending = True
    assert controller.request_training_mode(lambda: ready.append(True))
    assert actions == ["stop"]
    assert not controller._restore_pending
    controller._on_service_succeeded(True)
    assert ready == [True]
    controller._service_thread_finished()
    assert actions == ["stop", "stop"]
    controller._service_action = "stop"
    controller._on_service_succeeded(False)
    assert ready == [True, True]
    assert controller._resource_mode == "TRAIN_MODE"
    controller.training_finished()
    failures: list[str] = []
    assert controller.request_training_mode(lambda: ready.append(True), on_failed=failures.append)
    controller._service_action = "stop"
    controller._on_service_failed("unmanaged server")
    assert ready == [True, True]
    assert failures == ["unmanaged server"]
    assert controller._resource_mode == "USE_MODE"


def test_health_restart_is_bounded(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from desktop.controllers.app_controller import AppController
    from desktop.state import AppSnapshot, AppState, AppStatus
    from runtime.conversation import ConversationStore

    app = QApplication.instance() or QApplication([])
    controller = AppController(
        state=AppState(AppSnapshot(model_service_running=True)),
        conversation_store=ConversationStore(tmp_path / "conversations.db"),
    )
    controller._health_timer.stop()
    for attempt in range(4):
        controller._service_action = "health"
        controller.state.update(model_service_running=True)
        controller._on_service_succeeded(False)
        if attempt < 3:
            assert controller._restart_pending
            controller._restart_pending = False
    assert controller.state.snapshot.status is AppStatus.ERROR
    assert not controller._restart_pending
