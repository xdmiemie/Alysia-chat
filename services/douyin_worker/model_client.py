"""Cancelable, tool-free HTTP inference in the existing Q7 queue."""
from __future__ import annotations

import json
import os
import asyncio
from datetime import datetime, timezone
import logging
import time
import uuid

import httpx
import yaml

from integrations.qq_astrbot.plugin.api_client import MediaClient
from integrations.qq_astrbot.plugin.inference_queue import InferenceQueue
from integrations.qq_astrbot.plugin.model_messages import separate_image_parts
from integrations.qq_astrbot.plugin.prompt_loader import load_prompts
from integrations.qq_astrbot.plugin.vision_config import project_root
from integrations.qq_astrbot.plugin.vision_media import MediaError


class ModelClient:
    # These are output tokens, independent of the QQ conversation budget.
    OUTPUT_BUDGETS = {"vision_analyze": (3072, 6144), "vision_merge": (4096, 8192),
                      "douyin_title_interest": (256, 512)}

    def __init__(self, directory, vision_config):
        self.directory, self.config = directory, vision_config
        self.media = MediaClient(directory)

    async def count_text(self, text):
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8080", trust_env=False, timeout=5) as client:
            response = await client.post("/tokenize", json={"content": text, "add_special": False, "parse_special": False})
            response.raise_for_status()
            return len(response.json()["tokens"])

    async def memory_task(self, service, context, key, payload, schema, tokens):
        import jsonschema
        from integrations.qq_astrbot.plugin.memory_request import assemble
        from integrations.qq_astrbot.plugin.shared_memory import encoded, digest
        settings = yaml.safe_load((project_root() / "configs/runtime/qwen3.yaml").read_text(encoding="utf-8-sig"))
        prompts = load_prompts()
        payload = dict(payload)
        system = payload.pop("persona", "") + "\n" + prompts[key] + "\n" + prompts["shared_memory_use"]
        def messages(value):
            return [{"role": "system", "content": system}, {"role": "user", "content": encoded(value)}]
        async def count_request(value):
            async with httpx.AsyncClient(base_url="http://127.0.0.1:8080", trust_env=False, timeout=5) as client:
                response = await client.post("/apply-template", json={"messages": messages(value), "chat_template_kwargs": {"enable_thinking": False}})
                response.raise_for_status()
                prompt = response.json()["prompt"]
                if isinstance(prompt, list):
                    return len(prompt)
                response = await client.post("/tokenize", json={"content": prompt, "add_special": False, "parse_special": True})
                response.raise_for_status()
                return len(response.json()["tokens"])
        active = json.loads((project_root() / "model_store/active/qwen3.json").read_text(encoding="utf-8-sig"))
        payload, usage = await assemble(service, context, payload, count_request=count_request, count_text=self.count_text,
            window=settings["context_size"], output=tokens, margin=settings["context_margin_tokens"], model=digest(encoded([settings, active])))
        result = await self._complete(messages(payload), {"type": "json_schema", "json_schema": {
            "name": key, "strict": True, "schema": schema}}, max_tokens=tokens)
        choice = result["choices"][0]
        if choice.get("finish_reason") != "stop" or choice["message"].get("tool_calls"):
            raise ValueError("invalid_memory_model_response")
        value = json.loads(choice["message"]["content"])
        jsonschema.validate(value, schema)
        usage["model"] = result.get("model", active.get("id", "active local model"))
        service.record_usage(context, **usage)
        return value

    async def call(self, prompt_key, payload, images, response_format):
        content = [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]
        for index, image in enumerate(images):
            content.extend([{"type": "text", "text": json.dumps({"image_index": index,
                "frame": payload["sampled_frames"][index]}, ensure_ascii=False)},
                {"type": "image_url", "image_url": {"url": image}}])
        instruction = ""
        if prompt_key in self.OUTPUT_BUDGETS:
            instruction = ("\n输出应精炼，summary不超过400字；对象、动作、疑点各保留最多6项，"
                "OCR只保留与理解有关的关键文字、最多10项；timestamps最多4项。避免重复描述，输出完整JSON。")
        return await self.complete([{"role": "system", "content": load_prompts()[prompt_key] + instruction},
            {"role": "user", "content": content}], response_format, phase=prompt_key)

    async def transcribe(self, data):
        # Unlike the synchronous MediaClient transport, cancellation closes this
        # request immediately. The separate ASR service retains its own limits.
        if not 0 < len(data) <= 12 * 1024 * 1024:
            raise MediaError("audio_file_too_large")
        token = os.environ.get("QQ_COMPANION_MEDIA_TOKEN")
        if not token and (self.directory / "media_token").exists():
            token = (self.directory / "media_token").read_text(encoding="ascii").strip()
        if not token:
            raise MediaError("media_token_unavailable")
        async with self.media.admission(timeout=180):
            async with httpx.AsyncClient(trust_env=False, timeout=45) as client:
                response = await client.post("http://127.0.0.1:8765/v1/audio/transcriptions",
                    headers={"Authorization": "Bearer " + token}, files={"file": ("input.wav", data, "audio/wav")})
                response.raise_for_status()
                if len(response.content) > 1024 * 1024:
                    raise MediaError("asr_response_too_large")
                return response.json()

    async def complete(self, messages, response_format, *, phase="douyin"):
        budgets = self.OUTPUT_BUDGETS.get(phase, (None,))
        for attempt, budget in enumerate(budgets, 1):
            # Each attempt releases and re-enters the shared queue so QQ retains priority.
            result = await self._attempt(messages, response_format, phase, attempt, budget)
            choice = result["choices"][0]
            if choice.get("finish_reason") == "stop" and not choice["message"].get("tool_calls"):
                return choice["message"]["content"]
            if choice.get("finish_reason") != "length" or attempt == len(budgets):
                raise MediaError("incomplete_douyin_output")

    async def _attempt(self, messages, response_format, phase, attempt, budget):
        from integrations.qq_astrbot.plugin.model_debug import append_exchange
        started = time.monotonic()
        record = {"request_id": uuid.uuid4().hex, "phase": "douyin", "session_id": None,
                  "started_at": datetime.now(timezone.utc).isoformat(), "status": "pending",
                  "request": {"analysis_phase": phase, "attempt": attempt, "max_tokens": budget,
                      "messages": [{**m, "content": [p if p.get("type") == "text" else
                      {"type": "image_url", "image_url": {"url": "[local frame]"}} for p in m["content"]]
                      if isinstance(m.get("content"), list) else m.get("content")} for m in messages]},
                  "response": None}
        try:
            result = await self._complete(messages, response_format, max_tokens=budget)
            record["response"] = result
            record["status"] = "received" if result["choices"][0].get("finish_reason") == "stop" else "incomplete"
            return result
        except BaseException as exc:
            record["status"] = "canceled" if isinstance(exc, asyncio.CancelledError) else "error"
            record["error"] = str(exc)[:200]
            raise
        finally:
            record["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
            try:
                await asyncio.to_thread(append_exchange, record, self.directory / "logs/model_debug")
            except Exception:
                logging.getLogger(__name__).exception("Could not record Douyin model usage")

    async def _complete(self, messages, response_format, *, max_tokens=None):
        settings = yaml.safe_load((project_root() / "configs/runtime/qwen3.yaml").read_text(encoding="utf-8-sig"))
        max_tokens = max_tokens or settings["max_tokens"]
        messages = [{**m, "content": separate_image_parts(m["content"])} for m in messages]
        count_messages = json.loads(json.dumps(messages))
        images = 0
        for message in count_messages:
            if isinstance(message["content"], list):
                images += sum(p.get("type") == "image_url" for p in message["content"])
                message["content"] = [p for p in message["content"] if p.get("type") != "image_url"] or " "
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8080", trust_env=False,
                timeout=httpx.Timeout(45, connect=3)) as client:
            async def post(route, body):
                async with client.stream("POST", route, json=body) as response:
                    response.raise_for_status()
                    raw = bytearray()
                    async for block in response.aiter_bytes():
                        raw.extend(block)
                        if len(raw) > 4 * 1024 * 1024:
                            raise MediaError("douyin_model_response_limit")
                    return json.loads(raw)
            async def completion(body):
                # Streaming lets llama.cpp observe a disconnected client during
                # generation. A non-streaming disconnect can retain the slot.
                content, finish, size, usage = [], None, 0, None
                async with client.stream("POST", "/v1/chat/completions", json={**body, "stream": True,
                        "stream_options": {"include_usage": True}}) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        size += len(line.encode("utf-8"))
                        if size > 4 * 1024 * 1024:
                            raise MediaError("douyin_model_response_limit")
                        if not line.startswith("data: "):
                            continue
                        if line[6:] == "[DONE]":
                            break
                        item = json.loads(line[6:])
                        if item.get("usage"):
                            usage = item["usage"]
                        if "error" in item:
                            raise MediaError("douyin_model_stream_error")
                        for choice in item.get("choices", []):
                            delta = choice.get("delta", {})
                            if delta.get("tool_calls"):
                                raise MediaError("unexpected_douyin_tool_call")
                            if delta.get("content"):
                                content.append(delta["content"])
                            if choice.get("finish_reason"):
                                finish = choice["finish_reason"]
                return {"choices": [{"finish_reason": finish, "message": {"role": "assistant", "content": "".join(content)}}], "usage": usage}
            template = await post("/apply-template", {"messages": count_messages,
                "chat_template_kwargs": {"enable_thinking": False}})
            prompt = template["prompt"]
            text_tokens = len(prompt) if isinstance(prompt, list) else len((await post("/tokenize",
                {"content": prompt, "add_special": False, "parse_special": True}))["tokens"])
            if images > self.config["max_frames_per_request"] or (text_tokens + images * self.config["visual_tokens_per_image"]
                    + max_tokens + settings["context_margin_tokens"] > settings["context_size"]):
                raise MediaError("multimodal_context_capacity_exceeded")
            async with InferenceQueue(self.directory).admission(priority=20, phase="douyin", timeout=180, images=images):
                async with self.media.admission(timeout=180):
                    response = await client.get("/v1/models")
                    response.raise_for_status()
                    model = response.json()["data"][0]["id"]
                    result = await completion({"model": model, "messages": messages,
                        "temperature": settings.get("utility_temperature", 0), "top_p": settings.get("top_p", .9),
                        "top_k": settings.get("top_k", 20), "max_tokens": max_tokens, "stream": False,
                        "response_format": response_format, "chat_template_kwargs": {"enable_thinking": False},
                        "reasoning_budget_tokens": 0})
                    result["model"] = model
            return result
