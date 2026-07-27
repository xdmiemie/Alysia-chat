from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from runtime.audio.audio_format import WaveMetrics
from runtime.audio.audio_format import inspect_wav
from runtime.gpt_sovits.backend import write_audio_bytes
from runtime.gpt_sovits.types import SpeechRequest, SpeechResult
from runtime.qwen3.types import CharacterReply
from runtime.sensevoice.types import TranscriptionResult


SUPPORTED_AUDIO_SUFFIXES = {".wav", ".wave"}
DEFAULT_ASR_FALLBACK_TEXT = "我刚才没有听清，可以再说一遍吗？"
DEFAULT_LLM_FALLBACK_TEXT = "我这里稍微卡了一下，但我还在听你说。"


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
        request_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
        states: list[str] = []
        created_at = datetime.now().isoformat(timespec="seconds")
        started = time.perf_counter()
        after_speech_started = started

        def set_state(state: str) -> str:
            states.append(state)
            return state

        set_state("IDLE")
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
        transcription: TranscriptionResult | None = None
        reply: CharacterReply | None = None
        output_path = ""
        ok = False
        error = ""

        try:
            set_state("READING_FILE")
            metrics = inspect_wav(audio_path)
            timings["input_audio_ms"] = int(metrics.duration_seconds * 1000)

            set_state("TRANSCRIBING")
            asr_started = time.perf_counter()
            transcription = self.transcriber.transcribe(audio_path)
            timings["asr_ms"] = _elapsed_ms(asr_started)
            timings["asr_inference_ms"] = transcription.inference_ms

            if transcription.skipped or not transcription.text.strip():
                error = transcription.reason or "SenseVoice returned no speech text"
                reply = CharacterReply(
                    reply=self.asr_fallback_text,
                    emotion="gentle",
                    intensity=0.55,
                    speed=0.95,
                )
            else:
                set_state("GENERATING_REPLY")
                llm_started = time.perf_counter()
                try:
                    reply = self.chat_session.chat(transcription.text)
                    timings["llm_total_ms"] = _elapsed_ms(llm_started)
                    timings["llm_first_token_ms"] = timings["llm_total_ms"]
                    ok = True
                except Exception as exc:
                    timings["llm_total_ms"] = _elapsed_ms(llm_started)
                    error = f"Qwen3 failed: {exc}"
                    reply = CharacterReply(
                        reply=self.llm_fallback_text,
                        emotion="gentle",
                        intensity=0.55,
                        speed=0.95,
                    )

            set_state("SYNTHESIZING")
            tts_started = time.perf_counter()
            speech = self.synthesizer.synthesize(
                SpeechRequest(
                    text=reply.reply,
                    character_id=self.tts_character_id,
                    emotion=reply.emotion,
                    speed=reply.speed,
                    format="wav",
                )
            )
            timings["tts_ms"] = _elapsed_ms(tts_started)
            timings["tts_backend_ms"] = speech.backend_ms
            output_path = str(self._write_turn_audio(index, audio_path, request_id, speech))
            set_state("WAV_SAVED")
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
            input_audio=str(audio_path),
            output_audio=output_path,
            state=states[-1],
            states=tuple(states),
            error=error,
            transcription=transcription.to_dict() if transcription else None,
            reply=reply.to_dict() if reply else None,
            timings=StageTiming(**timings),
            created_at=created_at,
        )

    def _write_turn_audio(
        self,
        index: int,
        input_audio: Path,
        request_id: str,
        speech: SpeechResult,
    ) -> Path:
        filename = f"{index:02d}-{input_audio.stem}-{request_id}.wav"
        return write_audio_bytes(self.output_dir / filename, speech.audio_bytes)


class RealtimeDialogueRunner:
    def __init__(
        self,
        transcriber: Transcriber,
        chat_session: ChatSession,
        synthesizer: Synthesizer,
        recorder: LiveRecorder,
        speaker: Speaker,
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
        request_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
        states: list[str] = []
        created_at = datetime.now().isoformat(timespec="seconds")
        started = time.perf_counter()
        after_speech_started = started

        def set_state(state: str) -> str:
            states.append(state)
            return state

        set_state("IDLE")
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
        transcription: TranscriptionResult | None = None
        reply: CharacterReply | None = None
        input_audio = ""
        output_path = ""
        ok = False
        error = ""

        try:
            set_state("LISTENING")
            recording = self.recorder.listen_once(self.recordings_dir)
            input_audio = str(recording.wav_path)
            timings["recording_ms"] = int(recording.metrics.duration_seconds * 1000)
            timings["input_audio_ms"] = timings["recording_ms"]
            after_speech_started = time.perf_counter()
            set_state("SPEECH_CAPTURED")

            set_state("TRANSCRIBING")
            asr_started = time.perf_counter()
            transcription = self.transcriber.transcribe(recording.wav_path)
            timings["asr_ms"] = _elapsed_ms(asr_started)
            timings["asr_inference_ms"] = transcription.inference_ms

            if transcription.skipped or not transcription.text.strip():
                error = transcription.reason or "SenseVoice returned no speech text"
                reply = CharacterReply(
                    reply=self.asr_fallback_text,
                    emotion="gentle",
                    intensity=0.55,
                    speed=0.95,
                )
            else:
                set_state("GENERATING_REPLY")
                llm_started = time.perf_counter()
                try:
                    reply = self.chat_session.chat(transcription.text)
                    timings["llm_total_ms"] = _elapsed_ms(llm_started)
                    timings["llm_first_token_ms"] = timings["llm_total_ms"]
                    ok = True
                except Exception as exc:
                    timings["llm_total_ms"] = _elapsed_ms(llm_started)
                    error = f"Qwen3 failed: {exc}"
                    reply = CharacterReply(
                        reply=self.llm_fallback_text,
                        emotion="gentle",
                        intensity=0.55,
                        speed=0.95,
                    )

            set_state("SYNTHESIZING")
            tts_started = time.perf_counter()
            speech = self.synthesizer.synthesize(
                SpeechRequest(
                    text=reply.reply,
                    character_id=self.tts_character_id,
                    emotion=reply.emotion,
                    speed=reply.speed,
                    format="wav",
                )
            )
            timings["tts_ms"] = _elapsed_ms(tts_started)
            timings["tts_backend_ms"] = speech.backend_ms
            output_path = str(self._write_turn_audio(index, request_id, speech))

            set_state("PLAYING")
            self.speaker.play(output_path)
            set_state("PLAYED")
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
            transcription=transcription.to_dict() if transcription else None,
            reply=reply.to_dict() if reply else None,
            timings=StageTiming(**timings),
            created_at=created_at,
        )

    def _write_turn_audio(self, index: int, request_id: str, speech: SpeechResult) -> Path:
        filename = f"{index:02d}-{request_id}.wav"
        return write_audio_bytes(self.output_dir / filename, speech.audio_bytes)

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


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _elapsed_ms_since(started: float) -> int:
    return _elapsed_ms(started)
