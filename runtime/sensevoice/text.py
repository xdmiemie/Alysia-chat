from __future__ import annotations

import re
from dataclasses import dataclass


TAG_RE = re.compile(r"<\|([^|>]+)\|>")
SPACE_RE = re.compile(r"\s+")

LANGUAGE_TAGS = {
    "zh": "zh",
    "yue": "yue",
    "en": "en",
    "ja": "ja",
    "ko": "ko",
}

EMOTION_TAGS = {
    "happy": "happy",
    "sad": "sad",
    "angry": "angry",
    "neutral": "neutral",
    "fear": "fear",
    "surprise": "surprise",
    "disgust": "disgust",
}

EVENT_TAGS = {
    "speech": "speech",
    "nospeech": "nospeech",
    "no_speech": "nospeech",
    "laughter": "laughter",
    "laugh": "laughter",
    "cough": "cough",
    "applause": "applause",
    "music": "music",
    "noise": "noise",
}

PUNCTUATION_FIXES = {
    ",": "，",
    "?": "？",
    "!": "！",
}


@dataclass(frozen=True)
class ParsedSenseVoiceText:
    text: str
    language: str = "unknown"
    emotion: str = "unknown"
    event: str = "speech"


def parse_sensevoice_text(raw_text: str) -> ParsedSenseVoiceText:
    language = "unknown"
    emotion = "unknown"
    event = "speech"

    for tag in TAG_RE.findall(raw_text):
        normalized = tag.strip().lower().replace("-", "_")
        if normalized in LANGUAGE_TAGS:
            language = LANGUAGE_TAGS[normalized]
        elif normalized in EMOTION_TAGS:
            emotion = EMOTION_TAGS[normalized]
        elif normalized in EVENT_TAGS:
            event = EVENT_TAGS[normalized]

    text = normalize_transcribed_text(TAG_RE.sub("", raw_text))
    if not text and event == "speech":
        event = "nospeech"
    return ParsedSenseVoiceText(text=text, language=language, emotion=emotion, event=event)


def normalize_transcribed_text(text: str) -> str:
    cleaned = SPACE_RE.sub(" ", text).strip()
    if not cleaned:
        return ""

    for source, target in PUNCTUATION_FIXES.items():
        cleaned = cleaned.replace(source, target)

    cleaned = re.sub(r"\s+([，。！？；：、])", r"\1", cleaned)
    cleaned = re.sub(r"([（【《])\s+", r"\1", cleaned)
    cleaned = re.sub(r"\s+([）】》])", r"\1", cleaned)
    cleaned = re.sub(r"([一-龥])\s+([一-龥])", r"\1\2", cleaned)
    return cleaned.strip()
