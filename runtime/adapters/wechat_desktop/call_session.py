from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from runtime.adapters.wechat_desktop.call_audio import CallAudioInput
from runtime.adapters.wechat_desktop.call_controller import CallStateTracker
from runtime.adapters.wechat_desktop.call_models import CallContact
from runtime.adapters.wechat_desktop.config import CallsConfig, SchedulerConfig
from runtime.core.models import AlysiaReply, UnifiedMessage, new_request_id
from runtime.scheduler.inference_queue import SingleGPUQueue


class CallInputRecorder(Protocol):
    def listen_once(self, recordings_dir: str | Path) -> CallAudioInput | None: ...
    def stop(self) -> None: ...


class CallOutputSpeaker(Protocol):
    def play(self, audio_path: str | Path) -> None: ...
    def stop(self) -> None: ...


@dataclass(frozen=True)
class CallTurnResult:
    call_id: str
    turn_index: int
    ok: bool
    state: str
    input_audio: str = ""
    output_audio: str = ""
    transcript: str = ""
    reply_text: str = ""
    error: str = ""
    metadata: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class HalfDuplexCallSession:
    """One Stage 17 call: listen, infer, speak, then listen again."""

    def __init__(
        self,
        *,
        call_id: str,
        contact: CallContact,
        character_id: str,
        core,
        recorder: CallInputRecorder,
        speaker: CallOutputSpeaker,
        state: CallStateTracker,
        calls: CallsConfig,
        scheduler: SchedulerConfig,
        gpu_queue: SingleGPUQueue | None = None,
        event_sink: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        self.call_id = call_id
        self.contact = contact
        self.character_id = character_id
        self.core = core
        self.recorder = recorder
        self.speaker = speaker
        self.state = state
        self.calls = calls
        self.scheduler = scheduler
        self.gpu_queue = gpu_queue
        self.event_sink = event_sink or (lambda _event, _fields: None)
        self.root = calls.recordings_dir / call_id
        self._turn_index = 0
        self._started = False
        self._closed = False
        self._consecutive_failures = 0
        self._last_output_hash = ""

    @property
    def session_id(self) -> str:
        return (
            f"wechat_call:{self.contact.account_key}:"
            f"{self.contact.contact_key}:{self.call_id}"
        )

    @property
    def closed(self) -> bool:
        return self._closed

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.state.call_session_active = True
        self.root.mkdir(parents=True, exist_ok=True)
        self._transition("waiting_for_first_speech")
        self._append_manifest("call_started", {"session_id": self.session_id})

    async def run_turn(self) -> CallTurnResult:
        if self._closed:
            return self._result(False, "hangup", error="call session is closed")
        self._turn_index += 1
        turn = self._turn_index
        self._transition("listening", turn_index=turn)
        try:
            captured = await asyncio.to_thread(self.recorder.listen_once, self.root)
        except Exception as exc:
            return self._failed("audio_error", f"call audio capture failed: {exc}", turn)
        if self._closed:
            if captured is not None:
                try:
                    captured.wav_path.unlink(missing_ok=True)
                except OSError:
                    pass
            return self._result(False, "hangup", turn=turn, error="call ended while listening")
        if captured is None:
            return self._failed(
                "silence_timeout", "no valid caller speech was captured", turn, retryable=True
            )
        if not captured.wav_path.exists() or captured.duration_ms <= 0:
            return self._failed("audio_error", "call recorder returned invalid audio", turn)

        input_path = self.root / f"input_{turn}.wav"
        if captured.wav_path.resolve() != input_path.resolve():
            shutil.move(str(captured.wav_path), input_path)
        else:
            input_path = captured.wav_path
        self._transition("recognizing", turn_index=turn, input_audio=str(input_path))
        message = UnifiedMessage(
            request_id=new_request_id(),
            channel="wechat_call",
            session_id=self.session_id,
            sender_id=self.contact.contact_key,
            sender_name=self.contact.display_name,
            message_type="voice_call_turn",
            audio_path=str(input_path),
            character_id=self.character_id,
            metadata={
                "call_id": self.call_id,
                "turn_index": turn,
                "half_duplex": True,
                "output_stem": f"output_{turn}",
            },
        )
        self._transition("thinking", turn_index=turn)
        self.event_sink(
            "call_model_pipeline_started",
            {
                "call_id": self.call_id,
                "turn_index": turn,
                "request_id": message.request_id,
                "input_audio": str(input_path),
            },
        )
        try:
            reply = await self._process(message)
        except Exception as exc:
            self.event_sink(
                "call_model_pipeline_failed",
                {
                    "call_id": self.call_id,
                    "turn_index": turn,
                    "request_id": message.request_id,
                    "error": str(exc),
                },
            )
            return self._failed(
                "model_error", f"call inference failed: {exc}", turn, input_path=input_path
            )
        self.event_sink(
            "call_model_pipeline_completed" if reply.audio_path else "call_model_pipeline_failed",
            {
                "call_id": self.call_id,
                "turn_index": turn,
                "request_id": message.request_id,
                "core_ok": reply.ok,
                "core_state": reply.state,
                "core_states": list(reply.states),
                "error_code": reply.error_code,
                "error": reply.error,
                "transcript": str((reply.transcription or {}).get("text", "")),
                "reply_chars": len(reply.text),
                "audio_path": reply.audio_path,
                "timings": asdict(reply.timings),
            },
        )
        if not reply.audio_path:
            return self._failed(
                "model_error",
                reply.error or "call reply has no audio",
                turn,
                input_path=input_path,
            )

        output_path = Path(reply.audio_path)
        generated_output = output_path
        target_output = self.root / f"output_{turn}.wav"
        if output_path.resolve() != target_output.resolve():
            shutil.copyfile(output_path, target_output)
            output_path = target_output
            if not self.calls.retain_audio:
                try:
                    generated_output.unlink(missing_ok=True)
                except OSError:
                    pass
        self._transition("synthesizing", turn_index=turn, output_audio=str(output_path))
        self._last_output_hash = _sha256(output_path)
        self._transition(
            "speaking", turn_index=turn, output_hash=self._last_output_hash
        )
        try:
            await asyncio.to_thread(self.speaker.play, output_path)
        except Exception as exc:
            return self._failed(
                "audio_error",
                f"default-device playback failed: {exc}",
                turn,
                input_path=input_path,
                output_path=output_path,
            )
        await asyncio.sleep(max(0, self.calls.listen_guard_ms) / 1000.0)
        self._consecutive_failures = 0
        self._transition("listening", turn_index=turn)
        transcript = str((reply.transcription or {}).get("text", ""))
        result = self._result(
            True,
            "listening",
            turn=turn,
            input_path=input_path,
            output_path=output_path,
            transcript=transcript,
            reply_text=reply.text,
            metadata={"output_hash": self._last_output_hash, "half_duplex": True},
        )
        self._append_manifest("turn_completed", result.to_dict())
        self._cleanup_audio(input_path, output_path)
        return result

    async def _process(self, message: UnifiedMessage) -> AlysiaReply:
        if self.gpu_queue is None:
            return await asyncio.to_thread(self.core.process, message)
        return await self.gpu_queue.run_inference(
            message,
            lambda: self.core.process(message),
            priority=0,
            timeout_seconds=self.scheduler.call_turn_seconds,
        )

    def close(self, reason: str = "") -> CallTurnResult:
        if self._closed:
            return self._result(True, "hangup", error=reason)
        self._closed = True
        self.state.call_session_active = False
        for endpoint in (self.recorder, self.speaker):
            try:
                endpoint.stop()
            except Exception as exc:
                self.event_sink(
                    "call_audio_stop_failed", {"call_id": self.call_id, "error": str(exc)}
                )
        self._transition("idle", reason=reason)
        result = self._result(True, "hangup", error=reason)
        self._append_manifest("call_ended", result.to_dict())
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "summary.json").write_text(
            json.dumps(
                {
                    "call_id": self.call_id,
                    "session_id": self.session_id,
                    "contact_key": self.contact.contact_key,
                    "turn_count": self._turn_index,
                    "reason": reason,
                    "retain_audio": self.calls.retain_audio,
                    "last_output_hash": self._last_output_hash,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return result

    def _failed(
        self,
        state: str,
        error: str,
        turn: int,
        input_path: Path | None = None,
        output_path: Path | None = None,
        *,
        retryable: bool = False,
    ) -> CallTurnResult:
        self._consecutive_failures += 1
        self._transition(state, turn_index=turn, error=error)
        result = self._result(
            False,
            state,
            turn=turn,
            input_path=input_path,
            output_path=output_path,
            error=error,
            metadata={"retryable": retryable},
        )
        self._append_manifest("turn_failed", result.to_dict())
        if self._consecutive_failures >= self.calls.max_consecutive_failures:
            self.close("maximum consecutive call failures reached")
        return result

    def _transition(self, state: str, **fields: object) -> None:
        self.state.transition(
            call_id=self.call_id,
            state=state,  # type: ignore[arg-type]
            contact=self.contact,
            reason=str(fields.get("error") or fields.get("reason") or ""),
        )
        self.event_sink("call_state", {"call_id": self.call_id, "state": state, **fields})

    def _result(
        self,
        ok: bool,
        state: str,
        *,
        turn: int = 0,
        input_path: Path | None = None,
        output_path: Path | None = None,
        transcript: str = "",
        reply_text: str = "",
        error: str = "",
        metadata: dict[str, object] | None = None,
    ) -> CallTurnResult:
        return CallTurnResult(
            call_id=self.call_id,
            turn_index=turn,
            ok=ok,
            state=state,
            input_audio=str(input_path or ""),
            output_audio=str(output_path or ""),
            transcript=transcript,
            reply_text=reply_text,
            error=error,
            metadata=metadata or {},
        )

    def _append_manifest(self, event: str, fields: dict[str, object]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "manifest.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {"ts": time.time(), "event": event, "call_id": self.call_id, **fields},
                    ensure_ascii=False,
                )
                + "\n"
            )

    def _cleanup_audio(self, input_path: Path, output_path: Path) -> None:
        if self.calls.retain_audio:
            return
        for path in (input_path, output_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()
