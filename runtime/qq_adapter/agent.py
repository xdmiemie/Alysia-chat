"""Structured, one-message-at-a-time QQ conversation decisions."""

from __future__ import annotations

import json
from dataclasses import dataclass

from runtime.qwen3.client import LlamaServerClient
from runtime.qwen3.config import Qwen3Config
from runtime.qwen3.service import FORBIDDEN_REPLY_RE, Qwen3ChatSession
from runtime.qwen3.types import ChatMessage


ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["send_message", "finish_turn", "call_tool"]},
        "content": {"type": "string"},
        "continue_turn": {"type": "boolean"},
        "pace": {"type": "string", "enum": ["instant", "quick", "normal", "thoughtful"]},
        "reason_code": {"type": "string"},
    },
    "required": ["action", "content", "continue_turn", "pace", "reason_code"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ConversationAction:
    action: str
    content: str = ""
    continue_turn: bool = False
    pace: str = "normal"
    reason_code: str = ""


def parse_action(raw: str, *, max_length: int, previous: tuple[str, ...]) -> ConversationAction:
    if "<think" in raw.lower():
        raise ValueError("thinking content")
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != set(ACTION_SCHEMA["required"]):
        raise ValueError("invalid action fields")
    action = data["action"]
    if action not in {"send_message", "finish_turn", "call_tool"}:
        raise ValueError("invalid action")
    if type(data["continue_turn"]) is not bool or data["pace"] not in {"instant", "quick", "normal", "thoughtful"}:
        raise ValueError("invalid control fields")
    if not isinstance(data["reason_code"], str) or len(data["reason_code"]) > 64:
        raise ValueError("invalid reason code")
    if not isinstance(data["content"], str):
        raise ValueError("invalid content")
    content = " ".join(data["content"].split())
    if action == "call_tool":
        raise ValueError("tools are unavailable before Stage 13D")
    if action == "finish_turn":
        if content or data["continue_turn"]:
            raise ValueError("finish action has content or continuation")
    elif not content or len(content) > max_length or FORBIDDEN_REPLY_RE.search(content) or content in previous:
        raise ValueError("unsafe, empty, oversized or repeated message")
    elif action == "send_message":
        sentences = split_sentences(content)
        if len(set(sentences)) != len(sentences) or any(sentence in previous for sentence in sentences):
            raise ValueError("repeated sentence")
    return ConversationAction(action, content, data["continue_turn"], data["pace"], data["reason_code"])


def split_sentences(content: str) -> list[str]:
    """Split at sentence endings; an ellipsis is a pause within a sentence."""
    sentences: list[str] = []
    start = 0
    index = 0
    closers = '”’」』）》】"\''
    while index < len(content):
        char = content[index]
        boundary = char in "。！？!?" or (
            char == "." and not (
                (index > 0 and content[index - 1] == ".")
                or (index + 1 < len(content) and content[index + 1] == ".")
                or (index > 0 and index + 1 < len(content)
                    and content[index - 1].isdigit() and content[index + 1].isdigit())))
        if not boundary:
            index += 1
            continue
        index += 1
        while index < len(content) and content[index] in "。！？!?":
            index += 1
        while index < len(content) and content[index] in closers:
            index += 1
        sentence = content[start:index].strip()
        if sentence:
            sentences.append(sentence)
        start = index
    tail = content[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def split_plain_fallback(raw: str, *, max_length: int, previous: tuple[str, ...]) -> ConversationAction:
    """Use only when the backend rejects constrained JSON generation."""
    if "<think" in raw.lower() or raw.lstrip().startswith(("{", "[", "```")):
        return ConversationAction("finish_turn", reason_code="invalid_plain_fallback")
    content = " ".join(raw.split())
    parts = split_sentences(content)
    if not parts:
        return ConversationAction("finish_turn", reason_code="empty_plain_fallback")
    if (len(content) > max_length or FORBIDDEN_REPLY_RE.search(content)
            or len(set(parts)) != len(parts) or any(part in previous for part in parts)):
        return ConversationAction("finish_turn", reason_code="unsafe_plain_fallback")
    return ConversationAction("send_message", content, False, "normal", "plain_fallback")


class AgentSession:
    def __init__(self, config: Qwen3Config) -> None:
        session = Qwen3ChatSession.from_config(config)
        self.config = config
        self.client = session.client
        self.system_prompt = "\n".join(line for line in session.system_prompt.splitlines()
                                       if not line.startswith("JSON schema:"))
        self.history: list[tuple[str, tuple[str, ...]]] = []

    def decide(self, user_text: str, sent: tuple[str, ...], max_length: int,
               *, force_reply: bool = False) -> ConversationAction:
        instruction = (
            "You are replying in QQ. Return exactly one JSON action. send_message proposes a short reply; "
            "the sender will deliver each sentence as a separate QQ message. "
            "Set continue_turn true only if another distinct message is needed; after it is actually sent, "
            "you will be asked again with the original user text and the sent messages. "
            "finish_turn sends nothing and waits for the user. It is appropriate for a closing acknowledgement "
            "such as the user replying goodnight to your goodnight. Answer questions. "
            "重要：如果你上一条消息已经说了晚安，而用户仅回『晚安大肥鱼』等道别、没有新问题，"
            "直接返回 finish_turn、空 content、continue_turn=false、reason_code=conversation_closed。"
            "若用户接着问『那你明天几点起床？』，应发送答案。"
            "同样，如果你已经回答问题，用户仅说『嗯嗯』『好的』『明白了』『谢谢你』『收到』"
            "『谢谢啦』『原来如此』『好哦』『这样啊』『好，记住了』等简短确认且没有新问题或请求，"
            "就返回 finish_turn、空 content、continue_turn=false、"
            "reason_code=ack_only。不要为了礼貌再补一句。"
            "『你也早点睡』『做个好梦』同属道别回应。"
            "即使用户说『先别继续』，只要后面有新问题或请求（例如『给我一个建议？』），仍应回复新请求。"
            "Choose pace from instant, quick, normal, thoughtful. Do not call tools. "
            "Never describe private reasoning. Return only JSON matching this schema: "
            + json.dumps(ACTION_SCHEMA, ensure_ascii=False)
        )
        if force_reply and not sent:
            instruction += " This test session requires one reply to an allowed user message."
        messages = [ChatMessage("system", self.system_prompt + "\n" + instruction)]
        for old_user, old_sent in self.history[-self.config.max_history_turns:]:
            messages.append(ChatMessage("user", "/no_think\n" + old_user))
            for item in old_sent:
                messages.append(ChatMessage("assistant", item))
        messages.append(ChatMessage("user", "/no_think\n" + user_text))
        for item in sent:
            messages.append(ChatMessage("assistant", item))
        if sent:
            messages.append(ChatMessage("user", "Decide whether one more distinct QQ message is needed now, or finish."))
        for attempt in range(2):
            try:
                raw = self.client.complete_with_schema(messages, ACTION_SCHEMA, "qq_agent_action")
                result = parse_action(raw, max_length=max_length, previous=sent)
                if force_reply and not sent and result.action == "finish_turn":
                    raise ValueError("test mode requires reply")
                return result
            except RuntimeError as exc:
                if "HTTP Error 400" not in str(exc):
                    raise
                plain = self.client.complete_plain(messages)
                try:
                    return parse_action(plain, max_length=max_length, previous=sent)
                except (ValueError, json.JSONDecodeError):
                    return split_plain_fallback(plain, max_length=max_length, previous=sent)
            except (ValueError, json.JSONDecodeError) as exc:
                if attempt:
                    if force_reply and not sent:
                        return ConversationAction("send_message", "我在听，你接着说吧。", False,
                                                  "quick", "test_fallback")
                    return ConversationAction("finish_turn", reason_code="invalid_model_output")
                messages.append(ChatMessage("user", f"Invalid action ({type(exc).__name__}). Return a valid, nonrepeated JSON action."))
        raise AssertionError("unreachable")

    def commit(self, user_text: str, sent: tuple[str, ...]) -> None:
        self.history.append((user_text, sent))
        self.history = self.history[-self.config.max_history_turns:]
