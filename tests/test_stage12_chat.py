from __future__ import annotations

import os
import sqlite3
import subprocess
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

from runtime.conversation import ConversationStore
from runtime.qwen3.types import CharacterReply
from desktop.state import AppSnapshot


def test_stage12_store_persists_and_filters_character_history(tmp_path: Path) -> None:
    database = tmp_path / "alysia.db"
    store = ConversationStore(database)
    elysia = store.create_conversation("elysia")
    fish = store.create_conversation("fish", "Fish chat")
    user = store.add_message(
        elysia.id,
        "user",
        "hello",
        character_id="elysia",
        request_id="request-1",
    )
    store.add_message(
        elysia.id,
        "assistant",
        "hi",
        character_id="elysia",
        model_version="qwen-v1",
        request_id="request-1",
        parent_message_id=user.id,
    )

    reopened = ConversationStore(database)
    assert [item.id for item in reopened.list_conversations("elysia")] == [elysia.id]
    assert [item.id for item in reopened.list_conversations("fish")] == [fish.id]
    messages = reopened.list_messages(elysia.id)
    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[-1].model_version == "qwen-v1"
    assert messages[-1].request_id == "request-1"
    assert reopened.get_conversation(elysia.id).title == "hello"


def test_stage12_store_migrates_document_minimum_schema(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY, character_id TEXT NOT NULL, title TEXT,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL,
                message_type TEXT NOT NULL, text TEXT, audio_path TEXT,
                model_version TEXT, created_at INTEGER NOT NULL, status TEXT NOT NULL
            );
            """
        )
    ConversationStore(database)
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
    assert {"character_id", "request_id", "parent_message_id"}.issubset(columns)


def test_stage12_delete_cascades_and_context_is_limited(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "alysia.db")
    conversation = store.create_conversation("elysia")
    for index in range(12):
        store.add_message(conversation.id, "user", f"u{index}")
        store.add_message(conversation.id, "assistant", f"a{index}")
    context = store.context_messages(conversation.id, max_turns=8)
    assert len(context) <= 16
    assert context[-1].text == "a11"
    store.delete_conversation(conversation.id)
    assert store.list_messages(conversation.id) == []


def test_stage12_controller_generates_without_blocking_and_traces_reply(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from desktop.controllers import AppController
    from desktop.state import AppStatus

    app = QApplication.instance() or QApplication([])
    store = ConversationStore(tmp_path / "alysia.db")

    def fake_reply(character_id, model_id, text, history):
        time.sleep(0.03)
        assert character_id == "elysia"
        return CharacterReply(f"reply:{text}", "happy", 0.8, 1.0)

    controller = AppController(conversation_store=store, reply_factory=fake_reply)
    models = controller.available_models()
    qwen = next(model_id for kind, model_id, _ in models if kind == "qwen")
    controller.state.update(qwen_model=qwen, model_service_running=True)
    started = time.monotonic()
    controller.send_chat("hello")
    assert time.monotonic() - started < 0.5
    assert controller.state.snapshot.status in {AppStatus.THINKING, AppStatus.GENERATING_REPLY}

    deadline = time.monotonic() + 3
    while controller._chat_thread is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    messages = controller.current_messages()
    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[-1].text == "reply:hello"
    assert messages[-1].character_id == "elysia"
    assert messages[-1].model_version == qwen
    assert messages[-1].request_id == messages[0].request_id
    assert controller.state.snapshot.status is AppStatus.IDLE
    controller.shutdown()


def test_stage12_character_switch_uses_independent_conversation(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from desktop.controllers import AppController

    app = QApplication.instance() or QApplication([])
    controller = AppController(conversation_store=ConversationStore(tmp_path / "alysia.db"))
    elysia_session = controller.state.snapshot.session_id
    controller.switch_character("fish")
    app.processEvents()
    assert controller.state.snapshot.character_id == "fish"
    assert controller.state.snapshot.session_id != elysia_session
    assert controller.current_messages() == []
    controller.switch_character("elysia")
    assert controller.state.snapshot.session_id == elysia_session
    controller.shutdown()


def test_stage12_fifty_text_turns_complete_without_blocking_ui(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from desktop.controllers import AppController

    app = QApplication.instance() or QApplication([])
    controller = AppController(
        conversation_store=ConversationStore(tmp_path / "alysia.db"),
        reply_factory=lambda _character, _model, text, _history: CharacterReply(
            f"reply:{text}", "neutral", 0.5, 1.0
        ),
    )
    controller.state.update(qwen_model="test-qwen", model_service_running=True)
    for index in range(50):
        controller.send_chat(f"turn-{index}")
        deadline = time.monotonic() + 2
        while controller._chat_thread is not None and time.monotonic() < deadline:
            app.processEvents()
        assert controller._chat_thread is None

    messages = controller.current_messages()
    assert len(messages) == 100
    assert messages[-1].text == "reply:turn-49"
    controller.shutdown()


def test_stage12_chat_page_has_multiline_input_and_history_controls() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QTextEdit

    from desktop.pages import ChatPage

    app = QApplication.instance() or QApplication([])
    page = ChatPage()
    assert page.voice_button.text() == ""
    assert not page.voice_button.icon().isNull()
    assert page.voice_button.accessibleName() == "发送语音消息"
    assert isinstance(page.input, QTextEdit)
    assert page.new_button.text().endswith("新建")
    assert page.rename_button.text() == "重命名"
    assert page.delete_button.text() == "删除"
    assert page.clear_button.text() == "清空上下文"
    assert not page.voice_button.isEnabled()
    assert not page.input.isEnabled()
    page.apply_state(AppSnapshot(model_service_running=True))
    assert page.input.isEnabled()
    page.close()


def test_model_launcher_does_not_pipe_background_process_output(tmp_path: Path, monkeypatch) -> None:
    from runtime.qwen3 import server_manager
    from runtime.qwen3.model_registry import Qwen3ModelEntry

    model_file = tmp_path / "model.gguf"
    model_file.write_bytes(b"model")
    model = Qwen3ModelEntry(
        character_id="elysia",
        model_id="test-model",
        display_name="Test model",
        kind="base",
        model_format="gguf",
        required_files=(model_file,),
        created_at="",
    )
    responses = iter([None, {"model_path": str(model_file)}])
    monkeypatch.setattr(server_manager, "_get_json", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(server_manager, "_load_selection", lambda: {})
    observed = {}

    def fake_run(*args, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(server_manager.subprocess, "run", fake_run)
    server_manager.start_qwen_server(model, timeout_seconds=1)

    assert observed["stdout"] is subprocess.DEVNULL
    assert observed["stderr"] is subprocess.DEVNULL
    assert "capture_output" not in observed


def test_controller_shutdown_stops_model_service(tmp_path: Path, monkeypatch) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    import desktop.controllers.app_controller as app_controller_module
    from desktop.controllers import AppController

    app = QApplication.instance() or QApplication([])
    stopped = []
    monkeypatch.setattr(app_controller_module, "DEFAULT_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(app_controller_module, "stop_qwen_server", lambda: stopped.append(True))
    controller = AppController(conversation_store=ConversationStore(tmp_path / "alysia.db"))
    controller.state.update(model_service_running=True)
    controller.shutdown()

    assert stopped == [True]
    assert not controller.state.snapshot.model_service_running
