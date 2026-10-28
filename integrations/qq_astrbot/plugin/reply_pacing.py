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

OUTPUT_INSTRUCTION = """\
本轮 QQ 文字回复请只输出一个 JSON 对象，不要 Markdown 或代码围栏：
{"reply":"你要发送给对方的自然回复","thinking_level":1}
thinking_level 必须是整数 1、2、3、4、5。依据用户消息和对话上下文所需的理解、回忆与情绪斟酌来选，不要因为最终 reply 很短就一律选 1：
1=简单招呼或确认；2=熟悉的日常聊天；3=需要回忆、安慰或组织措辞；4=有相互冲突的选择、复杂关系或需要仔细分析；5=非常困难或敏感、需要格外斟酌。
只选档位，不输出思考过程。reply 保持当前角色说话风格，不能提及 JSON、档位或延迟。
如果使用工具，先正常调用工具，最终给用户的回复仍按上述 JSON 格式输出。
"""

FOLLOWUP_INSTRUCTION = """\
你刚给对方发完回复。现在单独判断：此刻自然聊天的人会不会再补发一句话？
只输出 JSON：{"follow_up":null,"thinking_level":1} 或 {"follow_up":"一句自然的后续话","thinking_level":1}。
如果补发一句能表达真实关心、接住对方情绪、追问关键细节，或自然延续当前话题，可以补发；可以是问题，也可以是陈述。
尤其当对方提出愿望，而刚才只表达了“我不太会/做不到”却没有接住愿望时，可以问对方的偏好或给一个可行选择。对方问候你的状态而你只回答了自己时，也可以顺势关心对方。
如果刚才的回复已经完整、补发会重复、显得强行找话题或刷屏，就用 null。不要每轮都追问，不要把普通问题机械地变成“你呢？”。
例如“想听你唱歌”→“我好像不太会唱呢。”之后，可以补“你想听什么歌？”；“昨晚睡得好吗？”→“睡得很香。”之后，可以补“你呢，睡得好吗？”；若刚才已经问过对方，通常用 null。
只允许一条后续消息，不要再给后续消息安排下一条。follow_up 必须只有一句话，保持当前角色风格，不要提及 JSON、档位或此判断。
thinking_level 按这句后续话所需的斟酌程度选整数 1 至 5，不输出思考过程。
"""


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
