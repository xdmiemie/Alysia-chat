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


def delivered_content(raw: str, text: str, *, complete: bool, thinking_level: int = 3, voice: bool = False) -> str:
    """Keep the original completed response; partial delivery keeps its metadata."""
    if complete and raw:
        return raw
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        value = None
    key = ('reply' if isinstance(value.get('reply'), str) else
           'follow_up' if isinstance(value.get('follow_up'), str) else None) if isinstance(value, dict) else None
    if key is None:
        value = {'reply': text, 'thinking_level': thinking_level, 'voice': voice}
    else:
        value[key] = text
    return json.dumps(value, ensure_ascii=False)


def _clean_assistant_text(text: str) -> str:
    try:
        structured = isinstance(json.loads(text), dict)
    except (ValueError, TypeError):
        structured = False
    return text if structured else CONTROL_MARKUP.sub('', text).strip()


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
                elif not value.get('tool_calls'):
                    content = _clean_assistant_text(content)
            if isinstance(content, list):
                content = [dict(part, text=part.get('text', '') if value.get('tool_calls') else _clean_assistant_text(part.get("text", "")))
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
    """Recent real dialogue for optional post-Agent replies, without system data."""
    rows = []
    for message in chat_history(messages):
        if message.get("role") not in {"user", "assistant"} or message.get('tool_calls'):
            continue
        content = message.get("content", "")
        text = content if isinstance(content, str) else "\n".join(
            part.get("text", "") for part in (content if isinstance(content, list) else [])
            if isinstance(part, dict) and part.get("type") == "text")
        if message['role'] == 'assistant':
            try:
                value = json.loads(text)
            except (ValueError, TypeError):
                value = None
            if isinstance(value, dict):
                text = value.get('reply', value.get('follow_up', ''))
                if not isinstance(text, str):
                    continue
        if message["role"] == "user":
            # QQ's assembled user message also contains IDs and date reminders.
            # Follow-up decisions need what the person said, not transport fields.
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


def protocol_view(messages, *, max_turns: int = 8):
    """Recent complete user turns, preserving native content and tool pairs."""
    rows = [m for m in chat_history(messages) if m.get('role') in {'user', 'assistant', 'tool'}]
    users = [index for index, message in enumerate(rows) if message['role'] == 'user']
    return rows[users[-max_turns]:] if len(users) > max_turns else rows


def request_view(prompt):
    try:
        value = json.loads(prompt)
        if isinstance(value, dict) and isinstance(value.get("current_message"), dict):
            return value["current_message"]
    except (ValueError, TypeError):
        pass
    return prompt
