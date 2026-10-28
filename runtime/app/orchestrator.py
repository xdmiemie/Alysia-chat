from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from runtime.audio.audio_format import WaveMetrics
from runtime.core.models import AstrbotChatReply, UnifiedMessage, new_request_id
from runtime.core.service import AstrbotChatCore, DEFAULT_ASR_FALLBACK_TEXT, DEFAULT_LLM_FALLBACK_TEXT
from runtime.gpt_sovits.types import SpeechRequest, SpeechResult
from runtime.qwen3.types import CharacterReply
from runtime.sensevoice.types import TranscriptionResult


SUPPORTED_AUDIO_SUFFIXES = {".wav", ".wave"}


class Transcriber(Protocol):
    def transcribe(self, audio_path: str | Path) -> TranscriptionResult:
        ...


class ChatSession(Protocol):
    def chat(self, user_text: str) -> CharacterReply:
        ...


class Synthesizer(Protocol):
    def synthesize(self, request: SpeechRequest) -> SpeechResult:
        ...


class LiveRecording(Protocol):
    wav_path: Path
    metrics: WaveMetrics


class LiveRecorder(Protocol):
    def listen_once(self, recordings_dir: str | Path) -> LiveRecording:
        ...


class Speaker(Protocol):
    def play(self, audio_path: str | Path) -> None:
        ...


@dataclass(frozen=True)
class StageTiming:
    recording_ms: int
    input_audio_ms: int
    asr_ms: int | None = None
    asr_inference_ms: int | None = None
    llm_first_token_ms: int | None = None
    llm_total_ms: int | None = None
    tts_ms: int | None = None
    tts_backend_ms: int | None = None
    total_after_speech_ms: int | None = None
    total_ms: int | None = None


@dataclass(frozen=True)
class DialogueTurnResult:
    request_id: str
    index: int
    ok: bool
    input_audio: str
    output_audio: str
    state: str
    states: tuple[str, ...]
    error: str
    transcription: dict[str, object] | None
    reply: dict[str, object] | None
    timings: StageTiming
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class Stage5DialogueRunner:
    def __init__(
        self,
        transcriber: Transcriber,
        chat_session: ChatSession,
        synthesizer: Synthesizer,
        chat_session_factory: Callable[[str], ChatSession] | None = None,
        input_dir: str | Path = "storage/generated_audio/dialogue_questions_20",
        output_dir: str | Path = "storage/generated_audio/stage5_dialogue",
        character_id: str = "elysia",
        tts_character_id: str | None = None,
        asr_fallback_text: str = DEFAULT_ASR_FALLBACK_TEXT,
        llm_fallback_text: str = DEFAULT_LLM_FALLBACK_TEXT,
    ) -> None:
        self.transcriber = transcriber
        self.chat_session = chat_session
        self.synthesizer = synthesizer
        self.input_dir = Path(input_dir)
        self.output_dir = Path(output_dir)
        self.character_id = character_id
        self.tts_character_id = tts_character_id or character_id
        self.asr_fallback_text = asr_fallback_text
        self.llm_fallback_text = llm_fallback_text
        self.core = AstrbotChatCore(
            transcriber=transcriber,
            chat_session_factory=chat_session_factory or (lambda _session_id: chat_session),
            synthesizer=synthesizer,
            output_dir=self.output_dir,
            tts_character_id=self.tts_character_id,
            asr_fallback_text=asr_fallback_text,
            llm_fallback_text=llm_fallback_text,
        )

    def discover_audio_files(self) -> list[Path]:
        if not self.input_dir.exists():
            raise FileNotFoundError(f"input audio directory not found: {self.input_dir}")
        files = [
            path
            for path in self.input_dir.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_AUDIO_SUFFIXES
        ]
        return sorted(files, key=lambda path: path.name.lower())

    def run_all(self, limit: int | None = None) -> list[DialogueTurnResult]:
        audio_files = self.discover_audio_files()
        if limit is not None:
            audio_files = audio_files[:limit]
        if not audio_files:
            raise FileNotFoundError(f"no WAV files found in: {self.input_dir}")

        self.output_dir.mkdir(parents=True, exist_ok=True)
        results = [self.run_one(index, audio_path) for index, audio_path in enumerate(audio_files, start=1)]
        summary_path = self.output_dir / "manifest.jsonl"
        summary_path.write_text(
            "".join(json.dumps(result.to_dict(), ensure_ascii=False) + "\n" for result in results),
            encoding="utf-8",
        )
        return results

    def run_one(self, index: int, input_audio: str | Path) -> DialogueTurnResult:
        audio_path = Path(input_audio)
        core_reply = self.core.process(
            UnifiedMessage.local_audio(
                audio_path,
                session_id="local:file:default",
                character_id=self.character_id,
                metadata={"output_stem": f"{index:02d}-{audio_path.stem}"},
            )
        )
        return _dialogue_result_from_core(index=index, input_audio=str(audio_path), core_reply=core_reply)


class RealtimeDialogueRunner:
    def __init__(
        self,
        transcriber: Transcriber,
        chat_session: ChatSession,
        synthesizer: Synthesizer,
        recorder: LiveRecorder,
        speaker: Speaker,
        chat_session_factory: Callable[[str], ChatSession] | None = None,
        recordings_dir: str | Path = "storage/recordings/stage6",
        output_dir: str | Path = "storage/generated_audio/stage6_dialogue",
        character_id: str = "elysia",
        tts_character_id: str | None = None,
        asr_fallback_text: str = DEFAULT_ASR_FALLBACK_TEXT,
        llm_fallback_text: str = DEFAULT_LLM_FALLBACK_TEXT,
    ) -> None:
        self.transcriber = transcriber
        self.chat_session = chat_session
        self.synthesizer = synthesizer
        self.recorder = recorder
        self.speaker = speaker
        self.recordings_dir = Path(recordings_dir)
        self.output_dir = Path(output_dir)
        self.character_id = character_id
        self.tts_character_id = tts_character_id or character_id
        self.asr_fallback_text = asr_fallback_text
        self.llm_fallback_text = llm_fallback_text
        self.core = AstrbotChatCore(
            transcriber=transcriber,
            chat_session_factory=chat_session_factory or (lambda _session_id: chat_session),
            synthesizer=synthesizer,
            output_dir=self.output_dir,
            tts_character_id=self.tts_character_id,
            asr_fallback_text=asr_fallback_text,
            llm_fallback_text=llm_fallback_text,
        )

    def run(self, max_turns: int | None = None) -> list[DialogueTurnResult]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        results: list[DialogueTurnResult] = []
        index = 1
        while max_turns is None or index <= max_turns:
            result = self.run_one(index)
            results.append(result)
            self.append_manifest(result)
            index += 1
        return results

    def run_one(self, index: int) -> DialogueTurnResult:
        request_id = new_request_id()
        states: list[str] = ["IDLE"]
        created_at = datetime.now().isoformat(timespec="seconds")
        started = time.perf_counter()
        after_speech_started = started
        timings = {
            "recording_ms": 0,
            "input_audio_ms": 0,
            "asr_ms": None,
            "asr_inference_ms": None,
            "llm_first_token_ms": None,
            "llm_total_ms": None,
            "tts_ms": None,
            "tts_backend_ms": None,
            "total_after_speech_ms": None,
            "total_ms": None,
        }
        input_audio = ""
        output_path = ""
        ok = False
        error = ""
        core_reply: AstrbotChatReply | None = None

        def set_state(state: str) -> str:
            states.append(state)
            return state

        try:
            set_state("LISTENING")
            recording = self.recorder.listen_once(self.recordings_dir)
            input_audio = str(recording.wav_path)
            timings["recording_ms"] = int(recording.metrics.duration_seconds * 1000)
            timings["input_audio_ms"] = timings["recording_ms"]
            after_speech_started = time.perf_counter()
            set_state("SPEECH_CAPTURED")

            core_reply = self.core.process(
                UnifiedMessage.local_audio(
                    recording.wav_path,
                    request_id=request_id,
                    session_id="local:desktop:default",
                    character_id=self.character_id,
                    metadata={"output_stem": f"{index:02d}"},
                )
            )
            states.extend(core_reply.states)
            timings.update(
                {
                    "input_audio_ms": core_reply.timings.input_audio_ms,
                    "asr_ms": core_reply.timings.asr_ms,
                    "asr_inference_ms": core_reply.timings.asr_inference_ms,
                    "llm_first_token_ms": core_reply.timings.llm_first_token_ms,
                    "llm_total_ms": core_reply.timings.llm_total_ms,
                    "tts_ms": core_reply.timings.tts_ms,
                    "tts_backend_ms": core_reply.timings.tts_backend_ms,
                }
            )
            output_path = core_reply.audio_path
            ok = core_reply.ok
            error = core_reply.error

            set_state("PLAYING")
            if output_path:
                self.speaker.play(output_path)
                set_state("PLAYED")
            else:
                set_state("FAILED")
        except KeyboardInterrupt:
            error = "interrupted"
            set_state("INTERRUPTED")
            raise
        except Exception as exc:
            error = error or str(exc)
            set_state("FAILED")
        finally:
            set_state("IDLE")
            timings["total_after_speech_ms"] = _elapsed_ms_since(after_speech_started)
            timings["total_ms"] = _elapsed_ms_since(started)

        return DialogueTurnResult(
            request_id=request_id,
            index=index,
            ok=ok and bool(output_path),
            input_audio=input_audio,
            output_audio=output_path,
            state=states[-1],
            states=tuple(states),
            error=error,
            transcription=core_reply.transcription if core_reply else None,
            reply=core_reply.reply if core_reply else None,
            timings=StageTiming(**timings),
            created_at=created_at,
        )

    def append_manifest(self, result: DialogueTurnResult) -> None:
        manifest_path = self.output_dir / "manifest.jsonl"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with manifest_path.open("a", encoding="utf-8") as manifest:
            manifest.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")


def default_stage5_output_dir(base_dir: str | Path = "storage/generated_audio/stage5_dialogue") -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path(base_dir) / timestamp


def default_stage6_output_dir(base_dir: str | Path = "storage/generated_audio/stage6_dialogue") -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path(base_dir) / timestamp


def _dialogue_result_from_core(index: int, input_audio: str, core_reply: AstrbotChatReply) -> DialogueTurnResult:
    states = tuple(["IDLE", *core_reply.states, "IDLE"])
    return DialogueTurnResult(
        request_id=core_reply.request_id,
        index=index,
        ok=core_reply.ok,
        input_audio=input_audio,
        output_audio=core_reply.audio_path,
        state=states[-1],
        states=states,
        error=core_reply.error,
        transcription=core_reply.transcription,
        reply=core_reply.reply,
        timings=StageTiming(
            recording_ms=0,
            input_audio_ms=core_reply.timings.input_audio_ms,
            asr_ms=core_reply.timings.asr_ms,
            asr_inference_ms=core_reply.timings.asr_inference_ms,
            llm_first_token_ms=core_reply.timings.llm_first_token_ms,
            llm_total_ms=core_reply.timings.llm_total_ms,
            tts_ms=core_reply.timings.tts_ms,
            tts_backend_ms=core_reply.timings.tts_backend_ms,
            total_after_speech_ms=core_reply.timings.total_ms,
            total_ms=core_reply.timings.total_ms,
        ),
        created_at=core_reply.created_at,
    )


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _elapsed_ms_since(started: float) -> int:
    return _elapsed_ms(started)
