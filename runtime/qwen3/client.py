from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Protocol

from runtime.qwen3.config import Qwen3Config
from runtime.qwen3.prompt import REPLY_SCHEMA
from runtime.qwen3.types import ChatMessage


class ChatCompletionClient(Protocol):
    def complete(self, messages: list[ChatMessage]) -> str:
        ...


class LlamaServerClient:
    def __init__(self, config: Qwen3Config) -> None:
        self.config = config
        self.last_usage: dict[str, int] = {}
        self.last_timings: dict[str, float] = {}

    def complete(self, messages: list[ChatMessage]) -> str:
        return self.complete_with_schema(messages, REPLY_SCHEMA, "elysia_reply")

    def complete_with_schema(self, messages: list[ChatMessage], schema: dict, name: str) -> str:
        return self._complete(messages, schema, name)

    def complete_plain(self, messages: list[ChatMessage]) -> str:
        return self._complete(messages, None, "")

    def _complete(self, messages: list[ChatMessage], schema: dict | None, name: str) -> str:
        payload = {
            "messages": [message.to_dict() for message in messages],
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
            "top_k": self.config.top_k,
            "max_tokens": self.config.max_tokens,
        }
        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": name,
                    "strict": True,
                    "schema": schema,
                },
            }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.config.base_url}/v1/chat/completions",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.request_timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise RuntimeError(f"llama-server request failed: {exc}") from exc

        packet = json.loads(raw)
        usage = packet.get("usage", {}) if isinstance(packet, dict) else {}
        self.last_usage = {key: int(usage[key]) for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                           if isinstance(usage, dict) and isinstance(usage.get(key), (int, float))}
        timings = packet.get("timings", {}) if isinstance(packet, dict) else {}
        if not isinstance(timings, dict):
            timings = {}
        self.last_timings = {key: float(value) for key, value in timings.items()
                             if isinstance(value, (int, float))}
        return _extract_message_content(packet)


def _extract_message_content(data: object) -> str:
    if not isinstance(data, dict):
        raise RuntimeError("llama-server response was not a JSON object")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("llama-server response did not include choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise RuntimeError("llama-server choice was not an object")
    message = first.get("message")
    if not isinstance(message, dict):
        raise RuntimeError("llama-server choice did not include message")
    content = message.get("content")
    if not isinstance(content, str):
        raise RuntimeError("llama-server message content was not a string")
    return content
