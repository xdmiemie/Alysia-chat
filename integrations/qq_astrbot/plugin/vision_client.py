"""Local vision caller with the same bounded read-only search as other modules."""
from __future__ import annotations

import asyncio
import json
import time
from urllib.request import ProxyHandler, Request, build_opener

import yaml

from .api_client import MediaClient
from .inference_queue import InferenceQueue
from .prompt_loader import load_prompts
from .vision_config import project_root
from .vision_media import MediaError
from .model_messages import separate_image_parts
from .search_loop import complete_with_search, SearchContextOverflow, ToolCallStream


class LocalVisionClient:
    def __init__(self, directory, config):
        self.directory, self.config = directory, config
        self.base = "http://127.0.0.1:8080"
        self.opener = build_opener(ProxyHandler({}))
        self.media = MediaClient(directory)
        self.model = None
        self.receipts = []

    def http(self, route, body=None, *, timeout=90):
        request = Request(self.base + route, data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
            headers={"Content-Type": "application/json"})
        with self.opener.open(request, timeout=timeout) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise MediaError("model_response_limit")
        return json.loads(raw)

    def stream_http(self, route, body, *, timeout=90):
        started, first, content, size = time.monotonic(), None, [], 0
        usage, timings, finish = None, None, None
        tool_stream = ToolCallStream()
        request = Request(self.base + route, data=json.dumps(body, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"})
        with self.opener.open(request, timeout=timeout) as response:
            for line in response:
                size += len(line)
                if size > 4 * 1024 * 1024:
                    raise MediaError("model_response_limit")
                if not line.startswith(b"data:"):
                    continue
                raw = line[5:].strip()
                if raw == b"[DONE]":
                    break
                event = json.loads(raw)
                usage, timings = event.get("usage") or usage, event.get("timings") or timings
                for choice in event.get("choices", []):
                    delta = choice.get("delta", {})
                    if delta.get("tool_calls"):
                        tool_stream.add(delta["tool_calls"])
                    if text := delta.get("content"):
                        first = first if first is not None else time.monotonic() - started
                        content.append(text)
                    finish = choice.get("finish_reason") or finish
        return {"choices": [{"finish_reason": finish, "message": tool_stream.message("".join(content))}],
            "usage": usage, "timings": timings, "first_token_seconds": first}

    async def call(self, prompt_key, payload, images, response_format, *, priority=10):
        system = load_prompts()[prompt_key]
        content = [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]
        for index, url in enumerate(images):
            content.append({"type": "text", "text": json.dumps({"image_index": index,
                "frame": (payload.get("sampled_frames") or [{}] * len(images))[index]}, ensure_ascii=False)})
            content.append({"type": "image_url", "image_url": {"url": url}})
        messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
        return await self.complete(messages, response_format=response_format, priority=priority)

    async def complete(self, messages, *, response_format=None, priority=0, request_id=None, stream=False):
        async def call(current, tools, fmt):
            return await self._complete_once(current, response_format=fmt, priority=priority,
                request_id=request_id if not tools and current == messages else None, stream=stream, tools=tools)
        result = await complete_with_search(call, messages, response_format)
        choice = result["choices"][0]
        if choice.get("finish_reason") != "stop" or choice["message"].get("tool_calls"):
            raise MediaError("incomplete_media_output")
        return choice["message"]["content"]

    async def _complete_once(self, messages, *, response_format=None, priority=0, request_id=None, stream=False, tools=None):
        from .model_messages import normalize_messages
        messages = normalize_messages(messages)
        messages = [{**message, "content": separate_image_parts(message.get("content"))} for message in messages]
        settings = yaml.safe_load((project_root() / "configs/runtime/qwen3.yaml").read_text(encoding="utf-8-sig"))
        count_messages = json.loads(json.dumps(messages))
        image_count = 0
        for message in count_messages:
            if isinstance(message.get("content"), list):
                image_count += sum(p.get("type") == "image_url" for p in message["content"])
                message["content"] = [p for p in message["content"] if p.get("type") != "image_url"] or " "
        def count():
            prompt = self.http("/apply-template", {"messages": count_messages, "chat_template_kwargs": {"enable_thinking": False},
                **({"tools": tools} if tools else {})})["prompt"]
            text = len(prompt) if isinstance(prompt, list) else len(self.http("/tokenize", {"content": prompt,
                "add_special": False, "parse_special": True})["tokens"])
            return text + image_count * self.config["visual_tokens_per_image"]
        reserved = await asyncio.to_thread(count)
        if reserved + settings["max_tokens"] + settings["context_margin_tokens"] > settings["context_size"]:
            if tools or any(m.get("role") == "tool" for m in messages):
                raise SearchContextOverflow("multimodal_context_capacity_exceeded")
            raise MediaError("multimodal_context_capacity_exceeded")
        body = {"messages": messages, "temperature": 0, "max_tokens": settings["max_tokens"],
                "chat_template_kwargs": {"enable_thinking": False}, "reasoning_budget_tokens": 0, "stream": stream}
        if stream:
            body["stream_options"] = {"include_usage": True}
        if response_format:
            body["response_format"] = response_format
        if tools:
            body.update(tools=tools, tool_choice="auto")
        queued = time.monotonic()
        async with InferenceQueue(self.directory).admission(priority=priority, phase="vision", request_id=request_id,
                timeout=self.config["queue_timeout_seconds"], images=image_count):
            async with self.media.admission(timeout=self.config["queue_timeout_seconds"]):
                if self.model is None:
                    models = await asyncio.to_thread(self.http, "/v1/models")
                    self.model = models["data"][0]["id"]
                body["model"] = self.model
                started = time.monotonic()
                queue_seconds = started - queued
                operation = asyncio.create_task(asyncio.to_thread(self.stream_http if stream else self.http, "/v1/chat/completions", body,
                    timeout=self.config["request_timeout_seconds"]))
                try:
                    result = await asyncio.shield(operation)
                except asyncio.CancelledError:
                    # urllib's thread cannot be canceled. Keep its queue/GPU
                    # leases until it finishes, so the next call cannot overlap.
                    try:
                        await operation
                    except Exception:
                        pass
                    raise
                choice = result["choices"][0]
                self.receipts.append({"images": image_count, "reserved_input_tokens": reserved,
                    "usage": result.get("usage"), "seconds": time.monotonic() - started,
                    "queue_seconds": queue_seconds, "first_token_seconds": result.get("first_token_seconds"),
                    "timings": result.get("timings"), "finish_reason": choice.get("finish_reason")})
                return result
