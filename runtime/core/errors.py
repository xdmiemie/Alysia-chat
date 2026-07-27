from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    NONE = "none"
    INVALID_MESSAGE = "invalid_message"
    AUDIO_INVALID = "audio_invalid"
    ASR_SKIPPED = "asr_skipped"
    ASR_FAILED = "asr_failed"
    LLM_FAILED = "llm_failed"
    TTS_FAILED = "tts_failed"
    UNEXPECTED = "unexpected"
