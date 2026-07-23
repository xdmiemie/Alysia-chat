from __future__ import annotations

import re


FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`([^`]*)`")
LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
MARKDOWN_RE = re.compile(r"[*_>#~]+")
SPACE_RE = re.compile(r"\s+")
UNSUPPORTED_SYMBOL_RE = re.compile(r"[^\w\s\u4e00-\u9fff，。！？、；：,.!?;:'\"（）()《》\-]")
SENTENCE_SPLIT_RE = re.compile(r"[^。！？!?；;]+[。！？!?；;]?")

DIGITS = "零一二三四五六七八九"


def normalize_tts_text(text: str) -> str:
    cleaned = FENCE_RE.sub(" ", text)
    cleaned = LINK_RE.sub(r"\1", cleaned)
    cleaned = INLINE_CODE_RE.sub(r"\1", cleaned)
    cleaned = MARKDOWN_RE.sub(" ", cleaned)
    cleaned = UNSUPPORTED_SYMBOL_RE.sub(" ", cleaned)
    cleaned = _normalize_dates(cleaned)
    cleaned = _normalize_numbers(cleaned)
    cleaned = SPACE_RE.sub(" ", cleaned).strip()
    cleaned = re.sub(r"\s+([，。！？、；：,.!?;:])", r"\1", cleaned)
    cleaned = re.sub(r"([（(《])\s+", r"\1", cleaned)
    cleaned = re.sub(r"\s+([）)》])", r"\1", cleaned)
    cleaned = re.sub(r"([一-龥])\s+([一-龥])", r"\1\2", cleaned)
    return cleaned


def split_tts_sentences(text: str, max_chars: int = 80) -> list[str]:
    normalized = normalize_tts_text(text)
    if not normalized:
        return []

    sentences = [match.group(0).strip() for match in SENTENCE_SPLIT_RE.finditer(normalized)]
    chunks: list[str] = []
    for sentence in sentences or [normalized]:
        if len(sentence) <= max_chars:
            chunks.append(sentence)
            continue
        for start in range(0, len(sentence), max_chars):
            chunk = sentence[start : start + max_chars].strip()
            if chunk:
                chunks.append(chunk)
    return [chunk for chunk in chunks if chunk]


def _normalize_dates(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        year, month, day = match.groups()
        return f"{_digits_individually(year)}年{_number_to_chinese(int(month))}月{_number_to_chinese(int(day))}日"

    return re.sub(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b", replace, text)


def _normalize_numbers(text: str) -> str:
    return re.sub(r"\d+", lambda match: _digits_individually(match.group(0)), text)


def _digits_individually(value: str) -> str:
    return "".join(DIGITS[int(char)] for char in value if char.isdigit())


def _number_to_chinese(value: int) -> str:
    if value < 10:
        return DIGITS[value]
    if value == 10:
        return "十"
    if value < 20:
        return "十" + DIGITS[value % 10]
    if value < 100:
        ones = value % 10
        return DIGITS[value // 10] + "十" + (DIGITS[ones] if ones else "")
    return _digits_individually(str(value))
