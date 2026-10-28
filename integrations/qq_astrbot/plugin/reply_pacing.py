"""Human-paced QQ text replies and one optional post-reply continuation."""

from __future__ import annotations

import json
import time
import unicodedata

THINKING_SECONDS = {1: 0.5, 2: 1.5, 3: 2.75, 4: 4.25, 5: 6.0}
DEFAULT_LEVEL = 3
CHINESE_SECONDS = 0.4
OTHER_SECONDS = 0.12
SENTENCE_END = frozenset("。！？!?；;…~.")
SENTENCE_CLOSERS = frozenset("”’」』）)]】")

def _unwrap_json(raw: str):
    candidate = raw.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            candidate = "\n".join(lines[1:-1]).strip()
    return json.loads(candidate)


def parse_reply(raw: str) -> tuple[str, int, bool]:
    """Return the visible reply, validated level and whether JSON was valid."""
    try:
        value = _unwrap_json(raw)
    except (json.JSONDecodeError, TypeError):
        return raw, DEFAULT_LEVEL, False
    if not isinstance(value, dict) or not isinstance(value.get("reply"), str):
        return raw, DEFAULT_LEVEL, False
    reply = value["reply"].strip()
    if not reply:
        return raw, DEFAULT_LEVEL, False
    level = value.get("thinking_level")
    if type(level) is not int or level not in THINKING_SECONDS:
        return reply, DEFAULT_LEVEL, False
    return reply, level, True


def split_sentences(text: str) -> list[str]:
    """Keep ending punctuation with each sentence; line breaks also separate."""
    parts: list[str] = []
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char in "\r\n":
            part = text[start:index].strip()
            if part:
                parts.append(part)
            index += 1
            start = index
            continue
        decimal_point = (char == "." and index > 0 and index + 1 < len(text)
                         and text[index - 1].isdigit() and text[index + 1].isdigit())
        if char in SENTENCE_END and not decimal_point:
            index += 1
            while index < len(text) and text[index] in SENTENCE_END | SENTENCE_CLOSERS:
                index += 1
            part = text[start:index].strip()
            if part:
                parts.append(part)
            start = index
            continue
        index += 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def parse_followup(raw: str, previous_reply: str) -> tuple[str | None, int, bool]:
    try:
        value = _unwrap_json(raw)
    except (json.JSONDecodeError, TypeError):
        return None, DEFAULT_LEVEL, False
    if not isinstance(value, dict) or "follow_up" not in value:
        return None, DEFAULT_LEVEL, False
    level = value.get("thinking_level")
    valid_level = type(level) is int and level in THINKING_SECONDS
    level = level if valid_level else DEFAULT_LEVEL
    text = value["follow_up"]
    if text is None:
        return None, level, valid_level
    if not isinstance(text, str):
        return None, DEFAULT_LEVEL, False
    sentences = split_sentences(text)
    if not sentences:
        return None, level, valid_level
    followup = sentences[0]
    if len(followup) > 80 or followup in split_sentences(previous_reply):
        return None, level, valid_level
    return followup, level, valid_level


def is_chinese(character: str) -> bool:
    name = unicodedata.name(character, "")
    return name.startswith(("CJK UNIFIED IDEOGRAPH", "CJK COMPATIBILITY IDEOGRAPH"))


def typing_seconds(reply: str) -> float:
    chinese = sum(is_chinese(character) for character in reply)
    return chinese * CHINESE_SECONDS + (len(reply) - chinese) * OTHER_SECONDS


def remaining_seconds(reply: str, level: int, started_at: float, *, now: float | None = None) -> float:
    elapsed = (time.monotonic() if now is None else now) - started_at
    return max(0.0, THINKING_SECONDS[level] + typing_seconds(reply) - max(0.0, elapsed))
