from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from desktop.controllers.voice_worker import VoicePipelineFailure, VoicePipelineResult
from runtime.calls import CallSession, CallStore
from runtime.audio.vad_recorder import RecordingCancelled


class FakeRecorder:
    def __init__(self) -> None:
        self.calls = 0

    def listen_once(self, recordings_dir, *, stop_event=None, mute_event=None, level_callback=None):
        assert stop_event is not None
        assert mute_event is not None
        self.calls += 1
        target = Path(recordings_dir) / f"turn-{self.calls:04d}.wav"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"user audio")
        if level_callback:
            level_callback(0.5)
        return SimpleNamespace(wav_path=target)


class FakePipeline:
    def __init__(self, fail_at: set[int] | None = None) -> None:
        self.calls = 0
        self.fail_at = fail_at or set()
        self.history_lengths: list[int] = []

    def __call__(self, **kwargs):
        self.calls += 1
        self.history_lengths.append(len(kwargs["history"]))
        kwargs["progress"]("transcribing")
        kwargs["progress"]("generating")
        if self.calls in self.fail_at:
            raise VoicePipelineFailure(
                "temporary inference failure",
                stage="generating",
                transcript=f"用户 {self.calls}",
            )
        kwargs["progress"]("synthesizing")
        output = Path(kwargs["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"assistant audio")
        return VoicePipelineResult(
            transcript=f"用户 {self.calls}",
            reply_text=f"回复 {self.calls}",
            emotion="happy",
            audio_path=str(output),
            duration_ms=500,
            sample_rate=32000,
            reference_audio="reference.wav",
            tts_model_version=kwargs["tts_model_id"],
            synthesis_ms=4,
            metadata={"asr_inference_ms": 3},
        )


def _session(tmp_path: Path, *, pipeline=None, save_audio=True, callbacks=None):
    callbacks = callbacks or {}
    recorder = FakeRecorder()
    pipeline = pipeline or FakePipeline()
    played: list[tuple[Path, float]] = []

    def player(path, *, output_device=None, volume=1.0, stop_event=None):
        assert output_device is None
        assert stop_event is not None
        played.append((Path(path), volume))

    session = CallSession(
        character_id="elysia",
        conversation_id="conversation-1",
        qwen_model_id="qwen-v3",
        tts_model_id="voice-v2",
        history=[],
        recorder=recorder,
        pipeline=pipeline,
        player=player,
        store=CallStore(tmp_path / "calls"),
        save_audio=save_audio,
        echo_guard_seconds=0,
        **callbacks,
    )
    return session, recorder, pipeline, played


def test_stage14_completes_30_half_duplex_turns_and_persists_models(tmp_path: Path) -> None:
    states = []
    session, recorder, pipeline, played = _session(
        tmp_path,
        callbacks={"state_callback": lambda state, _detail: states.append(state)},
    )
    summary = session.run(max_turns=30)

    assert summary.status == "completed"
    assert summary.turn_count == recorder.calls == pipeline.calls == len(played) == 30
    assert pipeline.history_lengths[:9] == list(range(0, 18, 2))
    assert pipeline.history_lengths[9:] == [16] * 21
    assert states.count("listening") == 30
    assert states.count("speaking") == 30
    call = json.loads((Path(summary.path) / "call.json").read_text(encoding="utf-8"))
    assert call["character_id"] == "elysia"
    assert call["conversation_id"] == "conversation-1"
    assert call["models"] == {"qwen3": "qwen-v3", "gpt_sovits": "voice-v2"}
    turns = [json.loads(line) for line in (Path(summary.path) / "turns.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(turns) == 30
    assert all(turn["qwen_model_version"] == "qwen-v3" for turn in turns)
    assert all(turn["tts_model_version"] == "voice-v2" for turn in turns)


def test_stage14_single_turn_failure_is_recorded_and_next_turn_continues(tmp_path: Path) -> None:
    errors = []
    pipeline = FakePipeline({2})
    session, _recorder, _pipeline, played = _session(
        tmp_path,
        pipeline=pipeline,
        callbacks={"error_callback": errors.append},
    )
    summary = session.run(max_turns=3)

    assert summary.status == "completed"
    assert summary.turn_count == 3
    assert len(played) == 2
    turns = [json.loads(line) for line in (Path(summary.path) / "turns.jsonl").read_text(encoding="utf-8").splitlines()]
    assert turns[1]["error"] == "temporary inference failure"
    assert turns[1]["failed_stage"] == "generating"
    assert errors and "可继续通话" in errors[0]


def test_stage14_audio_opt_out_keeps_text_history_but_removes_audio(tmp_path: Path) -> None:
    session, _recorder, _pipeline, _played = _session(tmp_path, save_audio=False)
    summary = session.run(max_turns=1)
    turn = json.loads((Path(summary.path) / "turns.jsonl").read_text(encoding="utf-8"))
    assert turn["user_transcript"] == "用户 1"
    assert turn["assistant_reply"] == "回复 1"
    assert turn["user_audio"] == turn["assistant_audio"] == ""
    assert not list((Path(summary.path) / "user").glob("*.wav"))
    assert not list((Path(summary.path) / "assistant").glob("*.wav"))


def test_stage14_repeated_calls_have_distinct_released_sessions(tmp_path: Path) -> None:
    ids = []
    for _ in range(10):
        session, recorder, _pipeline, _played = _session(tmp_path)
        summary = session.run(max_turns=1)
        ids.append(summary.call_id)
        assert recorder.calls == 1
        assert json.loads((Path(summary.path) / "call.json").read_text(encoding="utf-8"))["status"] == "completed"
    assert len(set(ids)) == 10


def test_stage14_call_dialog_exposes_required_controls() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from desktop.windows.voice_call_dialog import VoiceCallDialog

    app = QApplication.instance() or QApplication([])
    dialog = VoiceCallDialog("爱莉希雅")
    dialog.set_call_state("listening", "正在聆听")
    dialog.set_level(0.42)
    dialog.set_transcript("你好")
    dialog.set_reply("嗨，好久不见")
    assert dialog.status_label.text() == "正在聆听"
    assert dialog.level.value() == 42
    assert dialog.transcript_label.text() == "你好"
    assert dialog.reply_label.text() == "嗨，好久不见"
    assert dialog.mute_button.isCheckable()
    assert dialog.mute_button.text() == ""
    assert not dialog.mute_button.icon().isNull()
    normal_icon = dialog.mute_button.icon().cacheKey()
    dialog.mute_button.setChecked(True)
    assert dialog.mute_button.icon().cacheKey() != normal_icon
    assert dialog.mute_button.toolTip() == "恢复麦克风"
    assert dialog.end_button.text() == ""
    assert not dialog.end_button.icon().isNull()
    assert dialog.volume_slider.maximum() == 100
    dialog._ending = True
    dialog.close()


def test_stage14_end_button_closes_call_dialog_immediately() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from desktop.windows.voice_call_dialog import VoiceCallDialog

    app = QApplication.instance() or QApplication([])
    dialog = VoiceCallDialog("爱莉希雅")
    ended = []
    dialog.end_requested.connect(lambda: ended.append(True))
    dialog.show()
    app.processEvents()
    dialog.end_button.click()
    app.processEvents()

    assert ended == [True]
    assert not dialog.isVisible()


def test_stage14_controller_ends_call_and_allows_another(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from desktop.controllers import AppController
    from desktop.state import AppStatus
    from runtime.conversation import ConversationStore

    app = QApplication.instance() or QApplication([])
    created = []

    class WaitingRecorder(FakeRecorder):
        def listen_once(self, recordings_dir, *, stop_event=None, **kwargs):
            if self.calls == 0:
                return super().listen_once(recordings_dir, stop_event=stop_event, **kwargs)
            while not stop_event.wait(0.005):
                pass
            raise RecordingCancelled("ended")

    def recorder_factory():
        recorder = WaitingRecorder()
        created.append(recorder)
        return recorder

    pipeline = FakePipeline()
    controller = AppController(
        conversation_store=ConversationStore(tmp_path / "calls.db"),
        voice_pipeline=pipeline,
        call_recorder_factory=recorder_factory,
        call_player=lambda *_args, **_kwargs: None,
        calls_path=tmp_path / "calls",
    )
    controller.state.update(
        model_service_running=True,
        qwen_model="qwen-v3",
        tts_model="voice-v2",
    )
    for expected in (1, 2):
        controller.start_voice_call()
        deadline = time.monotonic() + 2
        while pipeline.calls < expected and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.001)
        controller.end_voice_call()
        while controller._call_thread is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.001)
        assert controller._call_thread is None
        assert controller.state.snapshot.status is AppStatus.IDLE
    assert len(created) == 2
    controller.shutdown()
