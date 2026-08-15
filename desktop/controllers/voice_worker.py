from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
import json
import logging
from pathlib import Path
import time
from typing import Callable

from PySide6.QtCore import QObject, Signal, Slot

from desktop.controllers.chat_worker import generate_reply
from runtime.audio.audio_format import inspect_wav
from runtime.conversation import Message
from runtime.gpt_sovits.backend import GPTSoVITSHttpBackend, write_audio_bytes
from runtime.gpt_sovits.config import load_gpt_sovits_config
from runtime.gpt_sovits.service import GPTSoVITSSynthesizer
from runtime.gpt_sovits.references import validate_model_manifest
from runtime.gpt_sovits.types import SpeechRequest
from runtime.sensevoice.backend import FunASRSenseVoiceBackend, resolve_model_path
from runtime.sensevoice.config import load_sensevoice_config
from runtime.sensevoice.service import SenseVoiceRecognizer


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class NoSpeechError(RuntimeError):
    """The recording was valid audio but contained no sendable speech."""


class VoicePipelineFailure(RuntimeError):
    """A failed stage plus any transcript/reply already produced safely."""

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        transcript: str = "",
        reply_text: str = "",
        emotion: str = "",
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.transcript = transcript
        self.reply_text = reply_text
        self.emotion = emotion


@dataclass(frozen=True)
class VoicePipelineResult:
    transcript: str
    reply_text: str
    emotion: str
    audio_path: str
    duration_ms: int
    sample_rate: int
    reference_audio: str
    tts_model_version: str
    synthesis_ms: int
    metadata: dict[str, object]


VoicePipeline = Callable[..., VoicePipelineResult]
SynthesisPipeline = Callable[..., VoicePipelineResult]


def generate_voice_reply(
    *,
    character_id: str,
    qwen_model_id: str,
    tts_model_id: str,
    audio_path: str | Path,
    output_path: str | Path,
    history: list[Message],
    progress: Callable[[str], None] | None = None,
) -> VoicePipelineResult:
    """Run the Stage 13 ASR → Qwen3 → GPT-SoVITS pipeline."""
    report = progress or (lambda _stage: None)
    report("transcribing")
    transcription = _recognizer().transcribe(audio_path)
    if transcription.skipped or not transcription.text.strip():
        raise NoSpeechError(transcription.reason or "没有识别到可发送的语音")

    report("generating")
    try:
        reply = generate_reply(
            character_id,
            qwen_model_id,
            transcription.text,
            history,
        )
    except Exception as exc:
        raise VoicePipelineFailure(
            str(exc),
            stage="generating",
            transcript=transcription.text,
        ) from exc
    report("synthesizing")
    try:
        return synthesize_voice_reply(
            character_id=character_id,
            tts_model_id=tts_model_id,
            text=reply.reply,
            emotion=reply.emotion,
            speed=reply.speed,
            output_path=output_path,
            transcript=transcription.text,
            metadata={
                "asr_language": transcription.language,
                "asr_emotion": transcription.emotion,
                "asr_event": transcription.event,
                "asr_inference_ms": transcription.inference_ms,
            },
        )
    except Exception as exc:
        raise VoicePipelineFailure(
            str(exc),
            stage="synthesizing",
            transcript=transcription.text,
            reply_text=reply.reply,
            emotion=reply.emotion,
        ) from exc


@lru_cache(maxsize=1)
def _recognizer() -> SenseVoiceRecognizer:
    """Keep the local ASR model warm across consecutive voice bubbles."""
    asr_config = load_sensevoice_config(
        PROJECT_ROOT / "configs" / "runtime" / "sensevoice.yaml"
    )
    asr_config = replace(
        asr_config,
        model_manifest=PROJECT_ROOT / asr_config.model_manifest,
    )
    return SenseVoiceRecognizer(
        asr_config,
        FunASRSenseVoiceBackend(
            resolve_model_path(asr_config.model_manifest, PROJECT_ROOT),
            device=asr_config.device,
        ),
    )


def synthesize_voice_reply(
    *,
    character_id: str,
    tts_model_id: str,
    text: str,
    emotion: str,
    output_path: str | Path,
    speed: float = 1.0,
    transcript: str = "",
    metadata: dict[str, object] | None = None,
) -> VoicePipelineResult:
    synthesizer = _synthesizer(character_id, tts_model_id)
    started = time.perf_counter()
    speech = synthesizer.synthesize(
        SpeechRequest(
            text=text,
            character_id=character_id,
            emotion=emotion or "neutral",
            speed=speed,
            format="wav",
        )
    )
    synthesis_ms = int((time.perf_counter() - started) * 1000)
    saved = write_audio_bytes(output_path, speech.audio_bytes)
    metrics = inspect_wav(saved)
    details = dict(metadata or {})
    details["tts_backend_ms"] = speech.backend_ms
    return VoicePipelineResult(
        transcript=transcript,
        reply_text=text,
        emotion=emotion or "neutral",
        audio_path=str(saved),
        duration_ms=int(metrics.duration_seconds * 1000),
        sample_rate=metrics.sample_rate,
        reference_audio=str(speech.reference_audio),
        tts_model_version=tts_model_id,
        synthesis_ms=synthesis_ms,
        metadata=details,
    )


@lru_cache(maxsize=8)
def _synthesizer(character_id: str, tts_model_id: str) -> GPTSoVITSSynthesizer:
    config = _tts_config(character_id, tts_model_id)
    validate_model_manifest(config.model_manifest, PROJECT_ROOT)
    return GPTSoVITSSynthesizer(config, GPTSoVITSHttpBackend(config))


def _tts_config(character_id: str, tts_model_id: str):
    """Resolve character-scoped paths without relying on the process cwd."""
    config = load_gpt_sovits_config(PROJECT_ROOT / "configs" / "runtime" / "gpt_sovits.yaml")
    character_root = PROJECT_ROOT / "configs" / "characters" / character_id
    manifest_path = PROJECT_ROOT / "model_store" / "active" / f"gpt_sovits_{character_id}.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"当前角色没有可用的 GPT-SoVITS 模型：{character_id}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    active_id = str(manifest.get("model_id") or manifest.get("version") or "")
    if tts_model_id and active_id and tts_model_id != active_id:
        raise RuntimeError("当前 GPT-SoVITS 模型与所选模型不一致，请重新切换模型")
    runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else {}
    reference_value = runtime.get("reference_dir") if isinstance(runtime, dict) else ""
    reference_dir = (
        Path(str(reference_value))
        if reference_value
        else Path("model_store") / "registry" / "gpt_sovits" / character_id / (tts_model_id or "base-v1") / "references"
    )
    if not reference_dir.is_absolute():
        reference_dir = PROJECT_ROOT / reference_dir
    return replace(
        config,
        model_manifest=manifest_path,
        character_id=character_id,
        emotion_map=character_root / "emotion_map.yaml",
        reference_texts=character_root / "reference_texts.yaml",
        reference_dir=reference_dir,
    )


class VoiceChatWorker(QObject):
    progress = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str)
    failed_with_context = Signal(object)
    finished = Signal()

    def __init__(
        self,
        *,
        character_id: str,
        qwen_model_id: str,
        tts_model_id: str,
        audio_path: str,
        output_path: str,
        history: list[Message],
        pipeline: VoicePipeline = generate_voice_reply,
    ) -> None:
        super().__init__()
        self.arguments = {
            "character_id": character_id,
            "qwen_model_id": qwen_model_id,
            "tts_model_id": tts_model_id,
            "audio_path": audio_path,
            "output_path": output_path,
            "history": list(history),
        }
        self.pipeline = pipeline

    @Slot()
    def run(self) -> None:
        try:
            result = self.pipeline(**self.arguments, progress=self.progress.emit)
            self.succeeded.emit(result)
        except VoicePipelineFailure as exc:
            logging.getLogger(__name__).exception("Desktop voice message failed")
            self.failed_with_context.emit(exc)
        except Exception as exc:
            logging.getLogger(__name__).exception("Desktop voice message failed")
            self.failed.emit(str(exc) or exc.__class__.__name__)
        finally:
            self.finished.emit()


class VoiceSynthesisWorker(QObject):
    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, *, pipeline: SynthesisPipeline = synthesize_voice_reply, **arguments) -> None:
        super().__init__()
        self.pipeline = pipeline
        self.arguments = arguments

    @Slot()
    def run(self) -> None:
        try:
            self.succeeded.emit(self.pipeline(**self.arguments))
        except Exception as exc:
            logging.getLogger(__name__).exception("Desktop voice regeneration failed")
            self.failed.emit(str(exc) or exc.__class__.__name__)
        finally:
            self.finished.emit()
