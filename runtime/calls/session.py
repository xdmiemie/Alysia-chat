from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import inspect
from pathlib import Path
from threading import Event, Lock
import time
from typing import Callable, Protocol
from uuid import uuid4

from runtime.audio.vad_recorder import RecordingCancelled
from runtime.conversation import Message
from runtime.calls.store import CallRecord, CallStore


class CallRecorder(Protocol):
    def listen_once(self, recordings_dir: str | Path, **kwargs): ...


class CallPipelineResult(Protocol):
    transcript: str
    reply_text: str
    emotion: str
    audio_path: str
    tts_model_version: str
    synthesis_ms: int
    metadata: dict[str, object]


CallPipeline = Callable[..., CallPipelineResult]
CallPlayer = Callable[..., None]


@dataclass(frozen=True)
class CallTurn:
    turn_id: str
    index: int
    created_at: str
    user_transcript: str
    assistant_reply: str
    emotion: str
    asr_ms: int
    llm_ms: int
    tts_ms: int
    total_ms: int
    qwen_model_version: str
    tts_model_version: str
    user_audio: str
    assistant_audio: str
    error: str = ""
    failed_stage: str = ""


@dataclass(frozen=True)
class CallSessionSummary:
    call_id: str
    path: str
    status: str
    turn_count: int
    error: str = ""


class CallSession:
    """A cancellable half-duplex VAD → ASR → LLM → TTS → speaker loop."""

    def __init__(
        self,
        *,
        character_id: str,
        conversation_id: str,
        qwen_model_id: str,
        tts_model_id: str,
        history: list[Message],
        recorder: CallRecorder,
        pipeline: CallPipeline,
        player: CallPlayer,
        store: CallStore,
        save_audio: bool = True,
        echo_guard_seconds: float = 0.18,
        output_device: int | None = None,
        state_callback: Callable[[str, str], None] | None = None,
        level_callback: Callable[[float], None] | None = None,
        transcript_callback: Callable[[str], None] | None = None,
        reply_callback: Callable[[str], None] | None = None,
        error_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.character_id = character_id
        self.conversation_id = conversation_id
        self.qwen_model_id = qwen_model_id
        self.tts_model_id = tts_model_id
        self.history = list(history)
        self.recorder = recorder
        self.pipeline = pipeline
        self.player = player
        self.store = store
        self.save_audio = bool(save_audio)
        self.echo_guard_seconds = max(0.0, float(echo_guard_seconds))
        self.output_device = output_device
        self.state_callback = state_callback or (lambda _state, _detail: None)
        self.level_callback = level_callback or (lambda _level: None)
        self.transcript_callback = transcript_callback or (lambda _text: None)
        self.reply_callback = reply_callback or (lambda _text: None)
        self.error_callback = error_callback or (lambda _error: None)
        self._stop = Event()
        self._muted = Event()
        self._settings_lock = Lock()
        self._volume = 1.0

    def stop(self) -> None:
        self._stop.set()

    def set_muted(self, muted: bool) -> None:
        if muted:
            self._muted.set()
            self.level_callback(0.0)
        else:
            self._muted.clear()

    def set_volume(self, volume: float) -> None:
        with self._settings_lock:
            self._volume = max(0.0, min(1.0, float(volume)))

    def run(self, max_turns: int | None = None) -> CallSessionSummary:
        record: CallRecord | None = None
        completed = 0
        fatal_error = ""
        status = "completed"
        try:
            self.state_callback("connecting", "正在连接音频设备…")
            record = self.store.start(
                character_id=self.character_id,
                conversation_id=self.conversation_id,
                qwen_model=self.qwen_model_id,
                tts_model=self.tts_model_id,
                save_audio=self.save_audio,
            )
            while not self._stop.is_set() and (max_turns is None or completed < max_turns):
                if self._muted.is_set():
                    self.state_callback("muted", "麦克风已静音")
                    self._stop.wait(0.05)
                    continue
                index = completed + 1
                self.state_callback("listening", "正在聆听")
                try:
                    recording = self._listen(record.path / "user")
                except RecordingCancelled:
                    break
                except Exception as exc:
                    fatal_error = str(exc) or exc.__class__.__name__
                    status = "failed"
                    self.error_callback(f"麦克风不可用：{fatal_error}")
                    break
                if self._stop.is_set():
                    self._remove_audio(recording.wav_path)
                    break
                started = time.perf_counter()
                output_path = record.path / "assistant" / f"turn-{index:04d}.wav"
                progress_stage = "transcribing"

                def progress(stage: str) -> None:
                    nonlocal progress_stage
                    progress_stage = stage
                    labels = {
                        "transcribing": ("transcribing", "正在识别"),
                        "generating": ("thinking", "思考中"),
                        "synthesizing": ("synthesizing", "正在合成语音"),
                    }
                    state, detail = labels.get(stage, (stage, stage))
                    self.state_callback(state, detail)

                try:
                    result = self.pipeline(
                        character_id=self.character_id,
                        qwen_model_id=self.qwen_model_id,
                        tts_model_id=self.tts_model_id,
                        audio_path=str(recording.wav_path),
                        output_path=str(output_path),
                        history=list(self.history),
                        progress=progress,
                    )
                    total_ms = int((time.perf_counter() - started) * 1000)
                    asr_ms = int(result.metadata.get("asr_inference_ms") or 0)
                    tts_ms = max(0, int(result.synthesis_ms))
                    llm_ms = max(0, total_ms - asr_ms - tts_ms)
                    turn = CallTurn(
                        turn_id=uuid4().hex,
                        index=index,
                        created_at=datetime.now(timezone.utc).isoformat(),
                        user_transcript=result.transcript,
                        assistant_reply=result.reply_text,
                        emotion=result.emotion,
                        asr_ms=asr_ms,
                        llm_ms=llm_ms,
                        tts_ms=tts_ms,
                        total_ms=total_ms,
                        qwen_model_version=self.qwen_model_id,
                        tts_model_version=result.tts_model_version or self.tts_model_id,
                        user_audio=self._relative_audio(record, recording.wav_path),
                        assistant_audio=self._relative_audio(record, result.audio_path),
                    )
                    self.transcript_callback(turn.user_transcript)
                    self.reply_callback(turn.assistant_reply)
                    self.state_callback("speaking", "角色正在说话")
                    try:
                        self._play(result.audio_path)
                    except Exception as exc:
                        playback_error = str(exc) or exc.__class__.__name__
                        turn = replace(
                            turn,
                            error=playback_error,
                            failed_stage="playback",
                        )
                        self.error_callback(f"本轮播放失败，可继续通话：{playback_error}")
                    self.store.append_turn(record, turn)
                    completed += 1
                    self._append_history(turn)
                    if not self.save_audio:
                        self._remove_audio(recording.wav_path)
                        self._remove_audio(result.audio_path)
                    if not self._stop.is_set() and self.echo_guard_seconds:
                        self.state_callback("echo_guard", "正在恢复麦克风…")
                        self._stop.wait(self.echo_guard_seconds)
                except RecordingCancelled:
                    break
                except Exception as exc:
                    error = str(exc) or exc.__class__.__name__
                    turn = CallTurn(
                        turn_id=uuid4().hex,
                        index=index,
                        created_at=datetime.now(timezone.utc).isoformat(),
                        user_transcript=str(getattr(exc, "transcript", "")),
                        assistant_reply=str(getattr(exc, "reply_text", "")),
                        emotion=str(getattr(exc, "emotion", "")),
                        asr_ms=0,
                        llm_ms=0,
                        tts_ms=0,
                        total_ms=int((time.perf_counter() - started) * 1000),
                        qwen_model_version=self.qwen_model_id,
                        tts_model_version=self.tts_model_id,
                        user_audio=self._relative_audio(record, recording.wav_path),
                        assistant_audio="",
                        error=error,
                        failed_stage=str(getattr(exc, "stage", progress_stage)),
                    )
                    self.store.append_turn(record, turn)
                    completed += 1
                    if turn.user_transcript:
                        self.transcript_callback(turn.user_transcript)
                    if turn.assistant_reply:
                        self.reply_callback(turn.assistant_reply)
                    self.error_callback(f"本轮失败，可继续通话：{error}")
                    if not self.save_audio:
                        self._remove_audio(recording.wav_path)
                        self._remove_audio(output_path)
                    if not self._stop.is_set():
                        self._stop.wait(0.1)
        except Exception as exc:
            fatal_error = str(exc) or exc.__class__.__name__
            status = "failed"
            self.error_callback(fatal_error)
        finally:
            self.level_callback(0.0)
            if self._stop.is_set() and status != "failed":
                status = "ended"
            if record is not None:
                self.store.finish(record, status=status, turn_count=completed, error=fatal_error)
        return CallSessionSummary(
            call_id=record.call_id if record else "",
            path=str(record.path) if record else "",
            status=status,
            turn_count=completed,
            error=fatal_error,
        )

    def _listen(self, path: Path):
        parameters = inspect.signature(self.recorder.listen_once).parameters
        kwargs: dict[str, object] = {}
        if "stop_event" in parameters or any(p.kind is p.VAR_KEYWORD for p in parameters.values()):
            kwargs["stop_event"] = self._stop
        if "mute_event" in parameters or any(p.kind is p.VAR_KEYWORD for p in parameters.values()):
            kwargs["mute_event"] = self._muted
        if "level_callback" in parameters or any(p.kind is p.VAR_KEYWORD for p in parameters.values()):
            kwargs["level_callback"] = self.level_callback
        return self.recorder.listen_once(path, **kwargs)

    def _play(self, audio_path: str | Path) -> None:
        with self._settings_lock:
            volume = self._volume
        parameters = inspect.signature(self.player).parameters
        kwargs: dict[str, object] = {}
        if "output_device" in parameters:
            kwargs["output_device"] = self.output_device
        if "volume" in parameters:
            kwargs["volume"] = volume
        if "stop_event" in parameters:
            kwargs["stop_event"] = self._stop
        self.player(audio_path, **kwargs)

    def _relative_audio(self, record: CallRecord, path: str | Path) -> str:
        if not self.save_audio:
            return ""
        try:
            return str(Path(path).resolve().relative_to(record.path.resolve())).replace("\\", "/")
        except ValueError:
            return str(path)

    def _append_history(self, turn: CallTurn) -> None:
        now = int(time.time() * 1000)
        common = {
            "conversation_id": self.conversation_id,
            "message_type": "voice",
            "audio_path": "",
            "character_id": self.character_id,
            "request_id": turn.turn_id,
            "parent_message_id": "",
            "created_at": now,
            "status": "completed",
        }
        self.history.append(Message(id=f"{turn.turn_id}-u", role="user", text=turn.user_transcript, model_version="", **common))
        self.history.append(Message(id=f"{turn.turn_id}-a", role="assistant", text=turn.assistant_reply, model_version=self.qwen_model_id, **common))
        self.history = self.history[-16:]

    @staticmethod
    def _remove_audio(path: str | Path) -> None:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
