from __future__ import annotations

from array import array
import math
import os
from pathlib import Path
import sqlite3
import time
import wave
import urllib.request

import pytest

from runtime.audio.audio_format import inspect_wav, persist_chat_wav, write_pcm16_wav
from runtime.conversation import ConversationStore, VoiceMessage


def _tone(path: Path, *, rate: int = 16000, channels: int = 1, seconds: float = 0.4) -> Path:
    samples = array("h")
    for index in range(int(rate * seconds)):
        value = int(5000 * math.sin(2 * math.pi * 440 * index / rate))
        samples.extend([value] * channels)
    return write_pcm16_wav(path, samples.tobytes(), rate, channels)


def test_stage13_store_migrates_and_restores_voice_metadata(tmp_path: Path) -> None:
    database = tmp_path / "voice.db"
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
    store = ConversationStore(database)
    conversation = store.create_conversation("elysia")
    message = store.add_voice_message(
        conversation.id,
        "assistant",
        tmp_path / "reply.wav",
        "你好呀",
        duration_ms=1234,
        sample_rate=16000,
        emotion="happy",
        reference_audio="happy.wav",
        tts_model_version="voice-v2",
        synthesis_ms=321,
        metadata={"tts_backend_ms": 300},
    )

    restored = ConversationStore(database).get_message(message.id)
    assert restored is not None
    assert VoiceMessage.from_message(restored).transcript == "你好呀"
    assert restored.duration_ms == 1234
    assert restored.sample_rate == 16000
    assert restored.emotion == "happy"
    assert restored.tts_model_version == "voice-v2"
    assert restored.metadata == {"tts_backend_ms": 300}


def test_stage13_audio_is_saved_as_raw_and_normalized_contract(tmp_path: Path) -> None:
    source = _tone(tmp_path / "stereo-8k.wav", rate=8000, channels=2)
    raw, normalized, metrics = persist_chat_wav(source, tmp_path / "chat", "c1", "m1")
    assert raw == tmp_path / "chat" / "c1" / "m1" / "raw.wav"
    assert normalized == tmp_path / "chat" / "c1" / "m1" / "normalized.wav"
    assert raw.is_file() and normalized.is_file()
    assert normalized.with_name("metrics.json").is_file()
    assert metrics.sample_rate == 16000
    assert metrics.channels == 1
    assert metrics.sample_width_bits == 16


def test_stage13_silent_audio_is_not_sent_to_pipeline(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from desktop.controllers import AppController

    app = QApplication.instance() or QApplication([])
    called = []
    silent = write_pcm16_wav(tmp_path / "silent.wav", b"\0\0" * 8000, 16000, 1)
    controller = AppController(
        conversation_store=ConversationStore(tmp_path / "silent.db"),
        voice_pipeline=lambda **kwargs: called.append(kwargs),
        chat_recordings_path=tmp_path / "recordings",
    )
    controller.state.update(model_service_running=True)
    controller.send_voice_file(silent)
    app.processEvents()
    assert called == []
    assert controller.current_messages() == []
    assert "没有检测到声音" in controller.state.snapshot.error_summary
    controller.shutdown()


def test_stage13_thirty_voice_turns_persist_and_remain_playable(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from desktop.controllers import AppController
    from desktop.controllers.voice_worker import VoicePipelineResult

    app = QApplication.instance() or QApplication([])
    database = tmp_path / "voice.db"
    source = _tone(tmp_path / "input.wav")
    counter = 0

    def fake_pipeline(**kwargs):
        nonlocal counter
        counter += 1
        output = _tone(Path(kwargs["output_path"]), seconds=0.35)
        metrics = inspect_wav(output)
        return VoicePipelineResult(
            transcript=f"第{counter}条语音",
            reply_text=f"第{counter}条回复",
            emotion="happy",
            audio_path=str(output),
            duration_ms=int(metrics.duration_seconds * 1000),
            sample_rate=metrics.sample_rate,
            reference_audio="reference-happy.wav",
            tts_model_version="voice-v1",
            synthesis_ms=12,
            metadata={"tts_backend_ms": 10},
        )

    controller = AppController(
        conversation_store=ConversationStore(database),
        voice_pipeline=fake_pipeline,
        chat_recordings_path=tmp_path / "recordings",
    )
    controller.state.update(
        model_service_running=True,
        qwen_model="qwen-test",
        tts_model="voice-v1",
    )
    conversation_id = controller.state.snapshot.session_id
    for _index in range(30):
        controller.send_voice_file(source)
        deadline = time.monotonic() + 2
        while controller._voice_thread is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.001)
        assert controller._voice_thread is None

    messages = controller.current_messages()
    assert len(messages) == 60
    assert all(message.conversation_id == conversation_id for message in messages)
    assert all(message.message_type == "voice" for message in messages)
    assert all(message.status == "completed" for message in messages)
    users = messages[::2]
    assistants = messages[1::2]
    assert [message.text for message in users] == [f"第{i}条语音" for i in range(1, 31)]
    assert [message.text for message in assistants] == [f"第{i}条回复" for i in range(1, 31)]
    assert all(Path(message.audio_path).is_file() for message in messages)
    assert all(message.parent_message_id == user.id for user, message in zip(users, assistants))
    assert all(message.reference_audio == "reference-happy.wav" for message in assistants)

    reopened = ConversationStore(database).list_messages(conversation_id)
    assert len(reopened) == 60
    assert all(Path(message.audio_path).is_file() for message in reopened)
    controller.shutdown()


def test_stage13_voice_bubble_reports_missing_file_without_crashing(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from desktop.widgets import VoiceMessageBubble

    app = QApplication.instance() or QApplication([])
    store = ConversationStore(tmp_path / "bubble.db")
    conversation = store.create_conversation("elysia")
    message = store.add_voice_message(
        conversation.id,
        "assistant",
        tmp_path / "missing.wav",
        "仍然可以查看这段文字",
        duration_ms=1000,
    )
    bubble = VoiceMessageBubble("爱莉希雅", message)
    assert not bubble.play_button.isEnabled()
    assert bubble.error_label.text() == "音频文件不存在"
    bubble.close()
    app.processEvents()


def test_stage13_loopback_http_bypasses_system_proxy(monkeypatch) -> None:
    from runtime.common import http

    direct_calls = []
    proxied_calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class DirectOpener:
        def open(self, request, timeout):
            direct_calls.append((request, timeout))
            return Response()

    monkeypatch.setattr(http, "_DIRECT_OPENER", DirectOpener())
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout: proxied_calls.append((request, timeout)) or Response(),
    )
    with http.urlopen("http://127.0.0.1:9880/docs", timeout=1):
        pass
    with http.urlopen("https://example.com", timeout=2):
        pass

    assert direct_calls == [("http://127.0.0.1:9880/docs", 1)]
    assert proxied_calls == [("https://example.com", 2)]


def test_stage13_model_worker_starts_qwen_and_tts(monkeypatch) -> None:
    import desktop.controllers.model_service_worker as worker_module

    events = []
    monkeypatch.setattr(worker_module, "qwen_server_running", lambda: False)
    monkeypatch.setattr(worker_module, "find_model", lambda character, model: (character, model))
    monkeypatch.setattr(worker_module, "find_tts_model", lambda character, model: (character, model))
    monkeypatch.setattr(
        worker_module,
        "start_qwen_server",
        lambda model, cancelled: events.append(("qwen", model)),
    )
    monkeypatch.setattr(
        worker_module,
        "start_gpt_sovits_server",
        lambda model, cancelled: events.append(("tts", model)),
    )
    worker = worker_module.ModelServiceWorker("start", "elysia", "qwen-v1", "tts-v1")
    succeeded = []
    worker.succeeded.connect(succeeded.append)
    worker.run()

    assert events == [
        ("qwen", ("elysia", "qwen-v1")),
        ("tts", ("elysia", "tts-v1")),
    ]
    assert succeeded == [True]


def test_stage13_tts_failure_preserves_transcript_and_retryable_reply(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from desktop.controllers import AppController
    from desktop.controllers.voice_worker import VoicePipelineFailure

    app = QApplication.instance() or QApplication([])
    source = _tone(tmp_path / "input.wav")

    def fail_tts(**_kwargs):
        raise VoicePipelineFailure(
            "TTS unavailable",
            stage="synthesizing",
            transcript="一二三二一",
            reply_text="我听到啦。",
            emotion="happy",
        )

    controller = AppController(
        conversation_store=ConversationStore(tmp_path / "failure.db"),
        voice_pipeline=fail_tts,
        chat_recordings_path=tmp_path / "recordings",
    )
    controller.state.update(model_service_running=True, tts_model="voice-v1")
    controller.send_voice_file(source)
    deadline = time.monotonic() + 2
    while controller._voice_thread is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.001)

    user, assistant = controller.current_messages()
    assert user.text == "一二三二一"
    assert user.status == "completed"
    assert assistant.text == "我听到啦。"
    assert assistant.status == "failed"
    assert assistant.metadata["failed_stage"] == "synthesizing"
    controller.shutdown()


def test_stage13_restart_marks_interrupted_voice_as_failed(tmp_path: Path) -> None:
    database = tmp_path / "restart.db"
    store = ConversationStore(database)
    conversation = store.create_conversation("elysia")
    pending = store.add_voice_message(
        conversation.id,
        "user",
        "recording.wav",
        status="pending",
    )
    restored = ConversationStore(database).get_message(pending.id)
    assert restored is not None
    assert restored.status == "failed"
    assert restored.text == "上次语音处理未完成"
