from __future__ import annotations

import os
import sys
import time

import pytest

from desktop.state import AppSnapshot, AppState, AppStatus, Workspace


def test_stage11_status_machine_has_all_required_states() -> None:
    assert {status.value for status in AppStatus} == {
        "idle",
        "switching_character",
        "switching_model",
        "recording",
        "transcribing",
        "thinking",
        "generating_reply",
        "synthesizing",
        "playing",
        "calling",
        "training",
        "training_paused",
        "starting_service",
        "error",
    }
    assert all(status.display_name for status in AppStatus)


def test_stage11_state_is_single_source_for_control_availability() -> None:
    state = AppState()
    observed: list[AppSnapshot] = []
    unsubscribe = state.subscribe(observed.append)

    assert state.snapshot.controls_enabled
    state.set_status(AppStatus.STARTING_SERVICE, "starting")
    assert not state.snapshot.controls_enabled
    assert observed[-1].status_detail == "starting"

    state.set_error("service failed", log_path="failure.log")
    assert state.snapshot.controls_enabled
    assert state.snapshot.error_summary == "service failed"
    assert state.snapshot.log_path == "failure.log"

    unsubscribe()
    count = len(observed)
    state.update(workspace=Workspace.TRAINING)
    assert len(observed) == count


def test_stage11_main_window_switches_both_workspaces() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from desktop.controllers import AppController
    from desktop.windows import MainWindow

    app = QApplication.instance() or QApplication([])
    controller = AppController()
    window = MainWindow(controller)
    assert window.minimumWidth() == 1280
    assert window.minimumHeight() == 720
    assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert window.title_bar.minimize_button.toolTip() == "最小化"
    assert window.title_bar.maximize_button.toolTip() == "最大化"
    assert window.title_bar.close_button.toolTip() == "关闭"
    assert window.pages.currentWidget() is window.chat_page
    assert not hasattr(window.navigation, "settings_button")
    assert window.character_panel.switch_character_button.text().endswith("切换角色")
    assert window.character_panel.switch_model_button.text().endswith("切换模型")
    assert window.character_panel.model_service_button.text().endswith("启动模型")

    controller.set_workspace(Workspace.TRAINING)
    app.processEvents()
    assert window.pages.currentWidget() is window.training_page
    assert window.training_page.log_console.isReadOnly()

    window.close()


def test_stage11_character_and_model_choices_follow_registry() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from desktop.controllers import AppController

    app = QApplication.instance() or QApplication([])
    controller = AppController()
    profiles = dict(controller.available_characters())
    assert {"elysia", "fish"}.issubset(profiles)

    controller.switch_character("fish")
    app.processEvents()
    assert controller.state.snapshot.character_id == "fish"
    assert controller.state.snapshot.character_name == "qiyu"
    assert controller.available_models()

    kind, model_id, _label = controller.available_models()[0]
    controller.switch_model(kind, model_id)
    assert model_id in {controller.state.snapshot.qwen_model, controller.state.snapshot.tts_model}

    models = controller.available_models()
    qwen_id = next(model_id for kind, model_id, _label in models if kind == "qwen")
    tts_id = next(model_id for kind, model_id, _label in models if kind == "tts")
    controller.switch_models(qwen_model=qwen_id, tts_model=tts_id)
    assert controller.state.snapshot.qwen_model == qwen_id
    assert controller.state.snapshot.tts_model == tts_id
    controller.shutdown()


def test_stage11_changing_qwen_does_not_revalidate_unchanged_tts(monkeypatch) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    import desktop.controllers.app_controller as controller_module
    from desktop.controllers import AppController

    app = QApplication.instance() or QApplication([])
    state = AppState(AppSnapshot(character_id="elysia", qwen_model="qwen-old", tts_model="tts-current"))
    controller = AppController(state=state)
    monkeypatch.setattr(controller, "available_models", lambda: [
        ("qwen", "qwen-new", "Qwen3 · New"), ("tts", "tts-current", "GPT-SoVITS · Current"),
    ])
    monkeypatch.setattr(controller_module, "_manifest_selects", lambda _path, _character, model_id: model_id == "tts-current")
    monkeypatch.setattr(controller_module.qwen_registry, "find_model", lambda *_args: object())
    monkeypatch.setattr(controller_module.qwen_registry, "activate_model", lambda *_args: object())
    verified: list[tuple[str | None, str | None]] = []
    monkeypatch.setattr(controller_module, "_verify_active_models", lambda _character, qwen, tts: verified.append((qwen, tts)))

    controller.switch_models(qwen_model="qwen-new", tts_model="tts-current")

    assert verified == [("qwen-new", None)]
    assert controller.state.snapshot.qwen_model == "qwen-new"
    assert controller.state.snapshot.tts_model == "tts-current"
    controller.shutdown()


def test_stage11_selection_dialogs_expose_separate_model_controls() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from desktop.windows.selection_dialogs import CharacterSelectionDialog, ModelSelectionDialog

    app = QApplication.instance() or QApplication([])
    character_dialog = CharacterSelectionDialog(
        [("elysia", "Elysia"), ("fish", "qiyu")],
        "fish",
    )
    assert character_dialog.selected_character_id == "fish"

    model_dialog = ModelSelectionDialog(
        [
            ("qwen", "qwen-a", "Qwen3 · Qwen A"),
            ("qwen", "qwen-b", "Qwen3 · Qwen B"),
            ("tts", "tts-a", "GPT-SoVITS · TTS A"),
        ],
        "qwen-b",
        "tts-a",
    )
    assert model_dialog.qwen_combo.count() == 2
    assert model_dialog.tts_combo.count() == 1
    assert model_dialog.selected_qwen_model == "qwen-b"
    assert model_dialog.selected_tts_model == "tts-a"
    character_dialog.close()
    model_dialog.close()


def test_secondary_windows_share_frameless_title_bar_and_dark_theme(tmp_path, monkeypatch) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from desktop.app import THEME_PATH
    import desktop.windows.settings_dialog as settings_module
    from desktop.windows.selection_dialogs import ModelSelectionDialog
    from desktop.windows.voice_call_dialog import VoiceCallDialog

    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(THEME_PATH.read_text(encoding="utf-8"))
    monkeypatch.setattr(settings_module, "SETTINGS_PATH", tmp_path / "settings.json")
    settings = settings_module.SettingsDialog(tmp_path / "missing.log")
    model = ModelSelectionDialog(
        [("qwen", "q1", "Qwen3 · Base"), ("tts", "t1", "GPT-SoVITS · Voice")],
        "q1",
        "t1",
    )
    call = VoiceCallDialog("爱莉希雅")
    call._ending = True

    assert all(
        dialog.windowFlags() & Qt.WindowType.FramelessWindowHint
        for dialog in (settings, model, call)
    )
    assert settings.title_bar.title_label.text() == "设置与日志"
    assert model.title_bar.title_label.text() == "切换模型"
    assert call.title_bar.title_label.text() == "与爱莉希雅语音通话"
    settings.title_bar.ensurePolished()
    assert settings.title_bar.close_button.font().pixelSize() >= 24
    settings.reject()
    model.reject()
    call.reject()


def test_stage11_managed_service_does_not_block_and_is_stopped() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QProcess
    from PySide6.QtWidgets import QApplication

    from desktop.controllers import AppController

    app = QApplication.instance() or QApplication([])
    controller = AppController()
    started_at = time.monotonic()
    process = controller.start_managed_process(
        sys.executable,
        ["-c", "import time; time.sleep(30)"],
        label="测试服务",
    )
    assert time.monotonic() - started_at < 1.0
    assert process.waitForStarted(3000)
    app.processEvents()
    assert process.state() is QProcess.ProcessState.Running

    controller.shutdown()
    assert process.state() is QProcess.ProcessState.NotRunning
