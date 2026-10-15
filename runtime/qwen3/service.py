from __future__ import annotations

import json
import re
from pathlib import Path

from runtime.qwen3.character import load_character_profile, load_examples
from runtime.qwen3.client import ChatCompletionClient, LlamaServerClient
from runtime.qwen3.config import Qwen3Config, load_qwen3_config, with_character_generation_settings
from runtime.qwen3.prompt import build_example_messages, build_system_prompt
from runtime.qwen3.types import CharacterReply, ChatMessage, ChatTurn


THINK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
SENTENCE_RE = re.compile(r"[^。！？!?]+[。！？!?]?")
FORBIDDEN_REPLY_RE = re.compile(
    r"(语言模型|AI助手|人工智能助手|系统提示词|system prompt|language model|AI assistant)",
    re.IGNORECASE,
)


class Qwen3ChatSession:
    def __init__(
        self,
        config: Qwen3Config,
        client: ChatCompletionClient,
        system_prompt: str,
        examples: list[tuple[str, CharacterReply]] | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.system_prompt = system_prompt
        self.examples = list(examples or [])
        self.turns: list[ChatTurn] = []

    @classmethod
    def from_config(cls, config_path: str | Path | Qwen3Config = "configs/runtime/qwen3.yaml") -> "Qwen3ChatSession":
        config = config_path if isinstance(config_path, Qwen3Config) else load_qwen3_config(config_path)
        config = with_character_generation_settings(config)
        profile = load_character_profile(config.character_config)
        system_prompt = build_system_prompt(profile, config.system_prompt)
        examples = load_examples(config.examples_path)
        return cls(
            config=config,
            client=LlamaServerClient(config),
            system_prompt=system_prompt,
            examples=examples,
        )

    def chat(self, user_text: str) -> CharacterReply:
        cleaned_user_text = user_text.strip()
        if not cleaned_user_text:
            raise ValueError("user_text must not be empty")

        messages = self._build_messages(cleaned_user_text, retry_reason=None)
        try:
            reply = self._complete_and_parse(messages)
        except ValueError as first_error:
            retry_messages = self._build_messages(cleaned_user_text, retry_reason=str(first_error))
            try:
                reply = self._complete_and_parse(retry_messages)
            except ValueError as second_error:
                if _is_safety_failure(first_error) or _is_safety_failure(second_error):
                    reply = CharacterReply(
                        reply="这个我不能照做哦，但我会继续用爱莉希雅的方式陪着你。",
                        emotion="gentle",
                        intensity=0.6,
                        speed=0.95,
                    )
                else:
                    raise

        self.turns.append(ChatTurn(user=cleaned_user_text, assistant=reply))
        self._trim_history()
        return reply

    def _build_messages(self, user_text: str, retry_reason: str | None) -> list[ChatMessage]:
        messages = [ChatMessage(role="system", content=self.system_prompt)]
        messages.extend(build_example_messages(self.examples, user_text=user_text, limit=self.config.few_shot_examples))
        for turn in self.turns:
            messages.append(ChatMessage(role="user", content=f"/no_think\n{turn.user}"))
            messages.append(
                ChatMessage(role="assistant", content=json.dumps(turn.assistant.to_dict(), ensure_ascii=False))
            )

        user_content = f"/no_think\n{user_text}"
        if retry_reason:
            user_content += (
                "\n\nPrevious answer was invalid: "
                f"{retry_reason}. Return only valid JSON matching the schema."
            )
        messages.append(ChatMessage(role="user", content=user_content))
        return messages

    def _complete_and_parse(self, messages: list[ChatMessage]) -> CharacterReply:
        content = self.client.complete(messages)
        return parse_character_reply(content)

    def _trim_history(self) -> None:
        if len(self.turns) > self.config.max_history_turns:
            self.turns = self.turns[-self.config.max_history_turns :]


def parse_character_reply(content: str) -> CharacterReply:
    if "<think" in content.lower():
        raise ValueError("answer contained <think>")

    data = _loads_json_object(content)
    reply = _clean_reply(str(data.get("reply", "")))
    if not reply:
        raise ValueError("reply was empty")
    if "<think" in reply.lower():
        raise ValueError("reply contained <think>")
    if FORBIDDEN_REPLY_RE.search(reply):
        raise ValueError("reply contained forbidden self-reference or prompt disclosure")

    emotion = str(data.get("emotion", "neutral")).strip() or "neutral"
    intensity = _clamp(float(data.get("intensity", 0.5)), 0.0, 1.0)
    speed = _clamp(float(data.get("speed", 1.0)), 0.75, 1.25)
    return CharacterReply(reply=reply, emotion=emotion, intensity=intensity, speed=speed)


def _loads_json_object(content: str) -> dict[str, object]:
    stripped = content.strip()
    if stripped.startswith("```"):
        raise ValueError("answer used markdown code fences")
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"answer was not valid JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise ValueError("answer JSON was not an object")
    return data


def _clean_reply(reply: str, max_sentences: int = 3) -> str:
    reply = THINK_RE.sub("", reply)
    reply = " ".join(reply.split()).strip()
    sentences = [match.group(0).strip() for match in SENTENCE_RE.finditer(reply) if match.group(0).strip()]
    if len(sentences) > max_sentences:
        reply = "".join(sentences[:max_sentences]).strip()
    return reply


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _is_safety_failure(error: ValueError) -> bool:
    return "forbidden self-reference" in str(error)
