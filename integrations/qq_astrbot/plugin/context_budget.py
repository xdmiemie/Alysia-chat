"""Bound complete local model requests, including native tool schemas."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from astrbot.core.agent.context.round_utils import split_into_rounds
from .request_provider import request_config

OUTPUT_TOKENS = 1536
MARGIN_TOKENS = 256
SONG_COLUMNS = ["candidate", "title", "artists", "aliases", "duration_seconds", "available", "link"]


class ContextCapacityError(ValueError):
    pass


def drop_oldest_round(messages):
    first = next((i for i, message in enumerate(messages) if message.role != "system"), len(messages))
    rounds = split_into_rounds(messages[first:])
    if len(rounds) <= 1:
        return messages
    return messages[:first] + [message for turn in rounds[1:] for message in turn]


class WholeRoundCompressor:
    """Native ContextManager extension: keep calls with results and current user."""
    def should_compress(self, messages, current_tokens, max_tokens):
        return current_tokens > max_tokens * 0.82

    async def __call__(self, messages):
        return drop_oldest_round(messages)


def tool_result_view(result, run=None):
    """Compact model view; actual IDs, albums and authorization stay in code."""
    result = deepcopy(result)
    refs = {song_id: ref for ref, song_id in run.candidate_refs.items()} if run else {}

    def clean(value):
        if isinstance(value, list):
            return [clean(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "song_id" in value and "title" in value:
            song_id = value["song_id"]
            refs.setdefault(song_id, len(refs) + 1)
            value["candidate"] = refs[song_id]
        return {key: clean(item) for key, item in value.items()
                if key not in {"song_id", "album", "source_receipt", "source_version", "selected_song_id"}}

    result = clean(result)
    if isinstance(result, dict) and isinstance(result.get("songs"), list):
        songs = result.pop("songs")
        columns = SONG_COLUMNS + (["audio_url"] if any("audio_url" in song for song in songs) else [])
        result["song_columns"] = columns
        result["songs"] = [[song.get(key) for key in columns] for song in songs if isinstance(song, dict)]
    return result


def action_receipts(actions, run=None):
    """Completion checks need execution evidence, not another full search list."""
    actions = deepcopy(actions)
    for action in actions:
        result = action.get("result")
        if isinstance(result, dict) and isinstance(result.get("songs"), list):
            result["candidate_count"] = len(result.pop("songs"))
        action["result"] = tool_result_view(result, run)
    return actions


class RequestBudget:
    def __init__(self, provider, phase=None):
        config = request_config(provider, phase)
        self.window = int(config.get("max_context_tokens") or 0)
        self.output_tokens = int((config.get("custom_extra_body") or {}).get("max_tokens") or OUTPUT_TOKENS)
        self.margin_tokens = int(config.get("context_margin_tokens", MARGIN_TOKENS))
        self.enable_thinking = (config.get("custom_extra_body") or {}).get("chat_template_kwargs", {}).get("enable_thinking", True)
        base = config.get("api_base", "")
        if not base and config.get("id") == "astrbot_chat_qwen3":
            base = "http://127.0.0.1:8080/v1"
        parsed = urlsplit(base)
        # This is a local llama.cpp extension, never send private dialogue to
        # an arbitrary endpoint discovered from message text or tool results.
        self.base = base.rstrip("/").removesuffix("/v1") if (
            parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}) else ""

    async def count(self, messages, tools=None):
        if not self.base:
            raise ContextCapacityError("local_token_counter_unavailable")
        payload = {"messages": [m.model_dump() if hasattr(m, "model_dump") else m for m in messages],
                   "chat_template_kwargs": {"enable_thinking": self.enable_thinking}}
        if tools:
            payload["tools"] = tools.openai_schema()

        def tokenize():
            opener = build_opener(ProxyHandler({}))
            def post(route, value):
                request = Request(self.base + route, data=json.dumps(value, ensure_ascii=False).encode("utf-8"),
                                  headers={"Content-Type": "application/json"}, method="POST")
                with opener.open(request, timeout=3) as response:
                    return json.load(response)
            rendered = post("/apply-template", payload)["prompt"]
            # Some llama.cpp builds return token IDs directly from the template.
            if isinstance(rendered, list) and all(type(value) is int for value in rendered):
                return len(rendered)
            if not isinstance(rendered, str):
                raise ContextCapacityError("invalid_template_response")
            return len(post("/tokenize", {"content": rendered, "add_special": False,
                                          "parse_special": True})["tokens"])
        try:
            return await asyncio.to_thread(tokenize)
        except Exception as exc:
            raise ContextCapacityError("local_token_counter_failed") from exc

    async def fit(self, messages, tools=None):
        if self.window <= 0:
            return messages, None, 0
        limit = self.window - self.output_tokens - self.margin_tokens
        messages = list(messages)
        dropped = 0
        while True:
            count = await self.count(messages, tools)
            if count <= limit:
                return messages, count, dropped
            turns = sum(message.role == "user" for message in messages)
            if turns <= 1:
                raise ContextCapacityError("current_turn_exceeds_context_capacity")
            reduced = drop_oldest_round(messages)
            if len(reduced) >= len(messages):
                raise ContextCapacityError("context_cannot_be_reduced")
            messages = reduced
            dropped += 1
