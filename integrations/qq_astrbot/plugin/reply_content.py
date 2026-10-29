"""Separate conversational text from legacy control syntax and private reasoning."""
from __future__ import annotations

from copy import deepcopy
import json
import re
import unicodedata

CONTROL_MARKUP = re.compile(r"(?:(?<![A-Za-z0-9_])sticker\s*[:：]\s*[^\s]+|\[已发送[^\]\r\n]*\])", re.IGNORECASE)
PRIVATE_PARTS = {"think", "thinking", "reasoning", "reasoning_content", "redacted_thinking"}
SPOKEN_WORD = re.compile(r"[\u3400-\u9fff\u3040-\u30ffA-Za-z0-9]")
FACE = re.compile(r"[（(]([^()（）]*)[）)](?:[^\s\u3400-\u9fff\u3040-\u30ffA-Za-z0-9，。！？、；：,.!?;:'\"《》-]*)")


def has_control_markup(text: str) -> bool:
    return bool(CONTROL_MARKUP.search(text))


def speech_parts(text: str) -> list[tuple[str, bool]]:
    """Keep decorative faces/emoji as text, before TTS can normalize their digits."""
    spans = []
    for match in FACE.finditer(text):
        body = match[1].strip()
        prose = (re.search(r"[\u3400-\u9fff\u3040-\u30ff]|[A-Za-z]{2,}", body)
                 or re.fullmatch(r"[A-Za-z]|[0-9]+(?:[.,][0-9]+)?", body))
        if body and not prose:
            spans.append((match.start(), match.end()))
    index = 0
    while index < len(text):
        if unicodedata.category(text[index]) == 'So':
            end = index + 1
            while end < len(text) and unicodedata.category(text[end]) in {'So', 'Sk', 'Mn', 'Me', 'Cf'}:
                end += 1
            spans.append((index, end))
            index = end
        else:
            index += 1
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    parts, start = [], 0
    for left, right in merged:
        if content := text[start:left].strip():
            parts.append((content, bool(SPOKEN_WORD.search(content))))
        parts.append((text[left:right], False))
        start = right
    if content := text[start:].strip():
        parts.append((content, bool(SPOKEN_WORD.search(content))))
    return parts


def spoken_text(text: str) -> str:
    return ' '.join(content for content, voice in speech_parts(text) if voice)


def chat_history(contexts):
    """Preserve dialogue/tool structure while excluding legacy private thought blocks."""
    clean = []
    for message in contexts:
        value = message.model_dump() if hasattr(message, "model_dump") else deepcopy(message)
        if not isinstance(value, dict):
            continue
        for key in PRIVATE_PARTS:
            value.pop(key, None)
        content = value.get("content")
        if value.get("role") == "assistant":
            # Older transport versions stored content parts as a JSON string.
            if isinstance(content, str):
                try:
                    decoded = json.loads(content)
                except (ValueError, TypeError):
                    decoded = None
                if isinstance(decoded, list) and decoded and all(isinstance(p, dict) and "type" in p for p in decoded):
                    content = decoded
                else:
                    content = CONTROL_MARKUP.sub("", content).strip()
            if isinstance(content, list):
                content = [dict(part, text=CONTROL_MARKUP.sub("", part.get("text", "")).strip())
                           if isinstance(part, dict) and part.get("type") == "text" else part for part in content]
                content = [part for part in content if not isinstance(part, dict) or part.get("type") != "text" or part.get("text")]
            value["content"] = content
        if isinstance(content, list):
            value["content"] = [part for part in content if not isinstance(part, dict)
                                or part.get("type") not in PRIVATE_PARTS]
        if value.get("content") or value.get("tool_calls"):
            clean.append(value)
    return clean


def dialogue_view(messages):
    """Only prior real dialogue, never persona/system messages or rejected drafts."""
    rows = []
    for message in chat_history(messages):
        if message.get("role") not in {"user", "assistant"}:
            continue
        content = message.get("content", "")
        text = content if isinstance(content, str) else "\n".join(
            part.get("text", "") for part in (content if isinstance(content, list) else [])
            if isinstance(part, dict) and part.get("type") == "text")
        if message["role"] == "user":
            # QQ's assembled user message also contains IDs and date reminders.
            # The checker needs what the person said, not those transport fields.
            parts = [content] if isinstance(content, str) else [
                p.get("text", "") for p in (content if isinstance(content, list) else []) if isinstance(p, dict)]
            for part in parts:
                try:
                    value = json.loads(part)
                except (ValueError, TypeError):
                    continue
                current = value.get("current_message") if isinstance(value, dict) else None
                if isinstance(current, dict) and isinstance(current.get("text"), str):
                    text = current["text"]
                    break
        if text:
            rows.append({"role": message["role"], "content": text[:1200]})
    return rows[-8:]


def request_view(prompt):
    try:
        value = json.loads(prompt)
        if isinstance(value, dict) and isinstance(value.get("current_message"), dict):
            return value["current_message"]
    except (ValueError, TypeError):
        pass
    return prompt
