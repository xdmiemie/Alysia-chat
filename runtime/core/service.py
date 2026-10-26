from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Protocol

from runtime.audio.audio_format import inspect_wav
from runtime.conversation.session_manager import ChatSession, ChatSessionManager
from runtime.core.errors import ErrorCode
from runtime.core.models import AlysiaReply, CoreTiming, UnifiedMessage
from runtime.gpt_sovits.backend import write_audio_bytes
from runtime.gpt_sovits.types import SpeechRequest, SpeechResult
from runtime.qwen3.types import CharacterReply
from runtime.sensevoice.types import TranscriptionResult


DEFAULT_ASR_FALLBACK_TEXT = "我刚才没有听清，可以再说一遍吗？"
DEFAULT_LLM_FALLBACK_TEXT = "我这里稍微卡了一下，但我还在听你说。"


class Transcriber(Protocol):
    def transcribe(self, audio_path: str | Path) -> TranscriptionResult:
        ...


class Synthesizer(Protocol):
    def synthesize(self, request: SpeechRequest) -> SpeechResult:
        ...


class AlysiaCore:
    def __init__(
        self,
        *,
        transcriber: Transcriber,
        chat_session_factory: Callable[[str], ChatSession],
        synthesizer: Synthesizer,
        output_dir: str | Path,
        tts_character_id: str | None = None,
        asr_fallback_text: str = DEFAULT_ASR_FALLBACK_TEXT,
        llm_fallback_text: str = DEFAULT_LLM_FALLBACK_TEXT,
    ) -> None:
        self.transcriber = transcriber
        self.sessions = ChatSessionManager(chat_session_factory)
        self.synthesizer = synthesizer
        self.output_dir = Path(output_dir)
        self.tts_character_id = tts_character_id
        self.asr_fallback_text = asr_fallback_text
        self.llm_fallback_text = llm_fallback_text

    def process_text(self, message: UnifiedMessage) -> AlysiaReply:
        """Run the shared session/character pipeline for text-only channels."""
        if message.message_type != "text" or not (message.text or "").strip():
            raise ValueError("a nonempty text message is required")
        started = time.perf_counter()
        try:
            result = self.sessions.get(message.session_id).chat(message.text or "")
            elapsed = _elapsed_ms(started)
            return AlysiaReply(
                request_id=message.request_id, session_id=message.session_id,
                channel=message.channel, ok=True, text=result.reply,
                message_type="text", audio_path="", state="COMPLETED",
                states=("ACCEPTED", "GENERATING_REPLY", "COMPLETED"),
                error_code=ErrorCode.NONE.value, error="", transcription=None,
                reply=result.to_dict(), timings=CoreTiming(
                    llm_first_token_ms=elapsed, llm_total_ms=elapsed, total_ms=elapsed),
                metadata=dict(message.metadata),
            )
        except Exception as exc:
            elapsed = _elapsed_ms(started)
            return AlysiaReply(
                request_id=message.request_id, session_id=message.session_id,
                channel=message.channel, ok=False, text=self.llm_fallback_text,
                message_type="text", audio_path="", state="FAILED",
                states=("ACCEPTED", "GENERATING_REPLY", "FAILED"),
                error_code=ErrorCode.LLM_FAILED.value,
                error=f"Qwen3 failed: {type(exc).__name__}", transcription=None,
                reply=None, timings=CoreTiming(llm_total_ms=elapsed, total_ms=elapsed),
                metadata=dict(message.metadata),
            )

    def process(self, message: UnifiedMessage) -> AlysiaReply:
        states: list[str] = []
        started = time.perf_counter()
        timings = {
            "input_audio_ms": 0,
            "asr_ms": None,
            "asr_inference_ms": None,
            "llm_first_token_ms": None,
            "llm_total_ms": None,
            "tts_ms": None,
            "tts_backend_ms": None,
            "total_ms": None,
        }
        transcription: TranscriptionResult | None = None
        reply: CharacterReply | None = None
        output_path = ""
        error = ""
        error_code = ErrorCode.NONE
        ok = False

        def set_state(state: str) -> str:
            states.append(state)
            return state

        set_state("ACCEPTED")
        try:
            user_text, transcription, asr_skip_reason = self._resolve_user_text(message, set_state, timings)
            if asr_skip_reason:
                error = asr_skip_reason
                error_code = ErrorCode.ASR_SKIPPED
                reply = CharacterReply(
                    reply=self.asr_fallback_text,
                    emotion="gentle",
                    intensity=0.55,
                    speed=0.95,
                )
            else:
                if not user_text.strip():
                    raise ValueError("message text must not be empty")

                set_state("GENERATING_REPLY")
                llm_started = time.perf_counter()
                try:
                    reply = self.sessions.get(message.session_id).chat(user_text)
                    timings["llm_total_ms"] = _elapsed_ms(llm_started)
                    timings["llm_first_token_ms"] = timings["llm_total_ms"]
                    ok = True
                except Exception as exc:
                    timings["llm_total_ms"] = _elapsed_ms(llm_started)
                    error = f"Qwen3 failed: {exc}"
                    error_code = ErrorCode.LLM_FAILED
                    reply = CharacterReply(
                        reply=self.llm_fallback_text,
                        emotion="gentle",
                        intensity=0.55,
                        speed=0.95,
                    )

            set_state("SYNTHESIZING")
            tts_started = time.perf_counter()
            try:
                speech = self.synthesizer.synthesize(
                    SpeechRequest(
                        text=reply.reply,
                        character_id=self.tts_character_id or message.character_id,
                        emotion=reply.emotion,
                        speed=reply.speed,
                        format="wav",
                    )
                )
            except Exception as exc:
                error = error or f"GPT-SoVITS failed: {exc}"
                error_code = ErrorCode.TTS_FAILED if error_code == ErrorCode.NONE else error_code
                raise
            timings["tts_ms"] = _elapsed_ms(tts_started)
            timings["tts_backend_ms"] = speech.backend_ms
            output_path = str(self._write_reply_audio(message, speech))
            set_state("WAV_SAVED")
            set_state("COMPLETED" if ok else "COMPLETED_WITH_FALLBACK")
        except Exception as exc:
            if not error:
                error = str(exc)
            if error_code == ErrorCode.NONE:
                error_code = (
                    ErrorCode.INVALID_MESSAGE
                    if isinstance(exc, ValueError)
                    else _error_code_for_state(states[-1] if states else "")
                )
            set_state("FAILED")
        finally:
            timings["total_ms"] = _elapsed_ms_since(started)

        return AlysiaReply(
            request_id=message.request_id,
            session_id=message.session_id,
            channel=message.channel,
            ok=ok and bool(output_path),
            text=reply.reply if reply else "",
            message_type="voice" if output_path else "text",
            audio_path=output_path,
            state=states[-1],
            states=tuple(states),
            error_code=error_code.value,
            error=error,
            transcription=transcription.to_dict() if transcription else None,
            reply=reply.to_dict() if reply else None,
            timings=CoreTiming(**timings),
            metadata=dict(message.metadata),
        )

    def _resolve_user_text(
        self,
        message: UnifiedMessage,
        set_state,
        timings: dict[str, int | None],
    ) -> tuple[str, TranscriptionResult | None, str]:
        if message.message_type == "text":
            return message.text or "", None, ""
        if message.message_type not in {"voice", "audio"}:
            raise ValueError(f"unsupported message_type: {message.message_type}")
        if not message.audio_path:
            raise ValueError("audio message requires audio_path")

        audio_path = Path(message.audio_path)
        set_state("READING_FILE")
        metrics = inspect_wav(audio_path)
        timings["input_audio_ms"] = int(metrics.duration_seconds * 1000)

        set_state("TRANSCRIBING")
        asr_started = time.perf_counter()
        transcription = self.transcriber.transcribe(audio_path)
        timings["asr_ms"] = _elapsed_ms(asr_started)
        timings["asr_inference_ms"] = transcription.inference_ms
        if transcription.skipped or not transcription.text.strip():
            return "", transcription, transcription.reason or "SenseVoice returned no speech text"
        return transcription.text, transcription, ""

    def _write_reply_audio(self, message: UnifiedMessage, speech: SpeechResult) -> Path:
        output_stem = str(message.metadata.get("output_stem") or message.request_id)
        filename = f"{output_stem}-{message.request_id}.wav"
        return write_audio_bytes(self.output_dir / filename, speech.audio_bytes)

def _error_code_for_state(state: str) -> ErrorCode:
    if state == "READING_FILE":
        return ErrorCode.AUDIO_INVALID
    if state == "TRANSCRIBING":
        return ErrorCode.ASR_FAILED
    if state == "SYNTHESIZING":
        return ErrorCode.TTS_FAILED
    return ErrorCode.UNEXPECTED


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _elapsed_ms_since(started: float) -> int:
    return _elapsed_ms(started)
