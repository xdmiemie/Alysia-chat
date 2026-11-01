"""QQ media integration; direct image answers stay in AstrBot's native Agent."""
from __future__ import annotations

import asyncio
from functools import wraps
import json
import re
import time

from .douyin_media import LINK, resolve_douyin
from .prompt_loader import load_prompts
from .vision_analysis import VisionAnalyzer
from .vision_config import VisionGates, load_vision_config
from .vision_media import MediaError, decode_images, fetch_url
from .vision_store import VisionStore

REPLY_FORMAT = {"type": "json_schema", "json_schema": {"name": "video_reply", "strict": True,
    "schema": {"type": "object", "properties": {"reply": {"type": "string"},
        "thinking_level": {"type": "integer", "enum": [1, 2, 3, 4, 5]}, "voice": {"type": "boolean"}},
        "required": ["reply", "thinking_level", "voice"], "additionalProperties": False}}}
VOICE_ACTION_TEXT = re.compile(r"[（(]\s*(?:发(?:送)?语音|语音(?:消息)?)\s*[:：]")


def install_media_source_capture(adapter_type=None):
    """Preserve direct/quoted URLs before the pinned adapter's preprocess stage."""
    if adapter_type is None:
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter
        adapter_type = AiocqhttpAdapter
    original = adapter_type._convert_handle_message_event
    if getattr(original, "_q7_sources", False):
        return
    @wraps(original)
    async def convert(adapter, *args, **kwargs):
        message = await original(adapter, *args, **kwargs)
        from astrbot.core.message.components import Image, Video, Reply
        def capture(parts):
            for part in parts:
                if isinstance(part, (Image, Video)):
                    source = next((value for value in (part.url, part.file)
                        if isinstance(value, str) and value.startswith(("https://", "http://"))), "")
                    if source:
                        object.__setattr__(part, "_qq_source_url", source)
                elif isinstance(part, Reply):
                    capture(part.chain or [])
        capture(message.message)
        return message
    convert._q7_sources = True
    adapter_type._convert_handle_message_event = convert


class VisionRuntime:
    def __init__(self, directory, media, *, context=None, memory=None, character="elysia", logger=None):
        self.config = load_vision_config()
        self.gates = VisionGates(directory, self.config)
        self.store = VisionStore(directory, self.config)
        self.media, self.context, self.memory = media, context, memory
        self.character, self.logger = character, logger
        self.worker = None
        self.closed = False
        self.provider_factory = None
        self.reply_media = None
        self.active_scope = lambda owner: True
        self.pending = {}
        self.questions = {}
        self.fragment_window = 2.5

    @staticmethod
    def event_media(event):
        from astrbot.core.message.components import Image, Video, Reply
        parts = list(event.get_messages())
        # AstrBot's preprocessing replaces Image.url/file with a local path
        # before plugin filters run. Recover only this event's original OneBot
        # URLs; never trust or read a user-supplied local attachment path.
        raw = getattr(event.message_obj, "raw_message", {})
        segments = raw.get("message", []) if hasattr(raw, "get") else []
        original = []
        for segment in segments if isinstance(segments, list) else []:
            if not isinstance(segment, dict) or segment.get("type") not in {"image", "video"}:
                continue
            data = segment.get("data", {})
            if isinstance(data, dict):
                cls = Video if segment["type"] == "video" else Image
                original.append(cls(file=str(data.get("file") or ""), url=str(data.get("url") or "")))
        if original:
            parts = [p for p in parts if not isinstance(p, (Image, Video))] + original
        for part in list(parts):
            if isinstance(part, Reply):
                parts.extend(p for p in (part.chain or []) if isinstance(p, (Image, Video)))
        return [p for p in parts if isinstance(p, (Image, Video))]

    def observe(self, event, incoming):
        """Keep media from the same sender's fragments until merge admission."""
        now = time.monotonic()
        self.pending = {key: [row for row in rows if now - row[0] <= self.fragment_window]
                        for key, rows in self.pending.items() if rows and now - rows[-1][0] <= self.fragment_window}
        self.questions = {key: row for key, row in self.questions.items()
                          if now - row[0] <= self.config["media_question_window_seconds"]}
        key = (incoming.scope, incoming.sender_uin)
        if incoming.text.strip():
            self.questions.pop(key, None)
            self.questions[key] = (now, incoming.text[:4000])
        while len(self.questions) > 256:
            self.questions.pop(next(iter(self.questions)))
        if parts := self.event_media(event):
            key = (incoming.scope, incoming.sender_uin)
            rows = self.pending.setdefault(key, [])
            raw = getattr(event.message_obj, "raw_message", {}) or {}
            raw_images = [p.get("data", {}) for p in raw.get("message", []) if isinstance(p, dict) and p.get("type") == "image"] if hasattr(raw, "get") and isinstance(raw.get("message"), list) else []
            rows.append((now, incoming.message_id, parts, raw_images))
            self.pending[key] = rows[-4:]
        while len(self.pending) > 256:
            self.pending.pop(next(iter(self.pending)))

    async def start(self, provider_factory):
        install_media_source_capture()
        self.provider_factory = provider_factory
        self.store.recover()
        self.worker = asyncio.create_task(self.work())

    async def call(self, prompt_key, payload, images, response_format):
        from astrbot.core.agent.message import Message
        from .context_budget import RequestBudget
        from .request_provider import request_chat
        provider = await self.provider_factory()
        content = [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]
        for index, url in enumerate(images):
            content.append({"type": "text", "text": json.dumps({"image_index": index,
                "frame": (payload.get("sampled_frames") or [{}] * len(images))[index]}, ensure_ascii=False)})
            content.append({"type": "image_url", "image_url": {"url": url}})
        messages = [Message(role="system", content=load_prompts()[prompt_key]), Message(role="user", content=content)]
        await RequestBudget(provider, "vision").fit(messages)
        async with self.media.admission(timeout=self.config["queue_timeout_seconds"]):
            result = await asyncio.wait_for(request_chat(provider, phase="vision", contexts=messages,
                func_tool=None, response_format=response_format, queue_timeout=self.config["queue_timeout_seconds"],
                request_max_retries=1), self.config["request_timeout_seconds"] + self.config["queue_timeout_seconds"])
        if result.tools_call_name:
            raise MediaError("media_unexpected_tool_call")
        raw = getattr(result, "raw_completion", None)
        if raw and raw.choices[0].finish_reason != "stop":
            raise MediaError("incomplete_media_output")
        return result.completion_text or ""

    @staticmethod
    def component_url(part):
        for value in (getattr(part, "_qq_source_url", ""), getattr(part, "url", ""), getattr(part, "file", "")):
            if isinstance(value, str) and value.startswith(("https://", "http://")):
                return value
        raise MediaError("media_download_url_unavailable")

    async def prepare(self, event, req, incoming):
        """Replace unvalidated framework refs with bounded, actual image pixels."""
        if previous := event.get_extra("qq_vision_prepared"):
            req.image_urls = previous["images"]
            return previous["status"]
        from astrbot.core.message.components import Image, Video
        question = incoming.text
        previous_question = self.questions.get((incoming.scope, incoming.sender_uin))
        if not question.strip() and previous_question and time.monotonic() - previous_question[0] <= self.config["media_question_window_seconds"]:
            question = previous_question[1]
        event.set_extra("qq_vision_question", question)
        rows = [row for row in self.pending.pop((incoming.scope, incoming.sender_uin), [])
                if time.monotonic() - row[0] <= self.fragment_window]
        parts = [part for _, _, media, _ in rows for part in media]
        fragment_raw_images = [raw for row in rows for raw in row[3]]
        if incoming.message_id not in {row[1] for row in rows}:
            parts.extend(self.event_media(event))
        images, status = [], []
        owner = incoming.scope
        for part in parts:
            if not isinstance(part, (Image, Video)):
                continue
            kind = "video" if isinstance(part, Video) else "image"
            try:
                if kind == "video" and not self.gates.enabled("video"):
                    raise MediaError("video_capability_not_verified")
                if kind == "image" and not any(self.gates.enabled(k) for k in ("image", "sticker", "gif")):
                    raise MediaError("image_capability_not_verified")
                original_url = self.component_url(part)
                data, source, _ = await asyncio.to_thread(fetch_url, original_url, self.config,
                    limit=self.config["max_video_bytes"] if kind == "video" else self.config["max_image_bytes"])
                if kind == "video":
                    key = self.store.put(data, owner, "video")
                    job = self.store.submit(key, owner, question, source=source,
                        origin=event.get_extra("qq_transport_origin") or event.unified_msg_origin,
                        account=incoming.bot_uin, request_id=incoming.message_id)
                    status.append({"type": "video", "state": "pending", "job_id": job})
                    continue
                actual, frames = await asyncio.to_thread(decode_images, data, self.config)
                # OneBot sub_type=1 identifies a static expression when available.
                raw = getattr(event.message_obj, "raw_message", {}) or {}
                raw_images = [p.get("data", {}) for p in raw.get("message", []) if isinstance(p, dict) and p.get("type") == "image"] if hasattr(raw, "get") and isinstance(raw.get("message"), list) else []
                raw_images.extend(fragment_raw_images)
                if actual == "image" and any(p.get("sub_type") in {1, "1"} and p.get("url") in {original_url, source} for p in raw_images):
                    actual = "sticker"
                if not self.gates.enabled(actual):
                    raise MediaError(actual + "_capability_not_verified")
                if len(images) + len(frames) > self.config["max_frames_per_request"]:
                    raise MediaError("foreground_image_budget_exceeded")
                key = self.store.put(data, owner, actual)
                images.extend(f.url for f in frames)
                status.append({"media_id": key, "type": actual, "state": "pixels_attached",
                    "sampled_frames": [{"timestamp": f.timestamp, "same_image_at": f.also_at} for f in frames]})
            except Exception as exc:
                status.append({"type": kind, "state": "unavailable", "reason": str(exc) if isinstance(exc, MediaError) else type(exc).__name__})
        if match := LINK.search(incoming.text):
            try:
                metadata = await asyncio.to_thread(resolve_douyin, match[0], self.config)
                if not self.gates.enabled("video") or not metadata["media_url"]:
                    status.append({"type": "douyin", "state": "metadata_only", "source_url": metadata["source_url"],
                        "title": metadata["title"], "author": metadata["author"], "reason": "video_unavailable"})
                else:
                    data, _, _ = await asyncio.to_thread(fetch_url, metadata["media_url"], self.config, limit=self.config["max_video_bytes"])
                    key = self.store.put(data, owner, "video")
                    job = self.store.submit(key, owner, question, source=metadata["source_url"],
                        origin=event.get_extra("qq_transport_origin") or event.unified_msg_origin,
                        account=incoming.bot_uin, request_id=incoming.message_id)
                    status.append({"type": "douyin", "state": "pending", "job_id": job})
            except Exception as exc:
                status.append({"type": "douyin", "state": "metadata_only", "source_url": match[0],
                    "reason": str(exc) if isinstance(exc, MediaError) else type(exc).__name__})
        req.image_urls = images
        event.set_extra("qq_vision_prepared", {"images": images, "status": status})
        return status

    async def work(self):
        analyzer = VisionAnalyzer(self.store, self.config, self.call, transcribe=self.media.transcribe)
        last_cleanup = 0
        while not self.closed:
            try:
                if time.monotonic() - last_cleanup > 3600:
                    self.store.cleanup()
                    last_cleanup = time.monotonic()
                if self.gates.enabled("video") and (completed := self.store.awaiting_delivery()):
                    try:
                        await self.deliver(completed, json.loads(completed["result"]))
                    except Exception as exc:
                        self.store.fail_job(completed, type(exc).__name__)
                    continue
                job = self.store.next_job() if self.gates.enabled("video") else None
                if not job:
                    await asyncio.sleep(0.5)
                    continue
                try:
                    def check_active():
                        if not self.active_scope(job["owner"]):
                            self.store.cancel(job["id"], job["owner"])
                        if self.store.job(job["id"], job["owner"])["status"] != "running":
                            raise MediaError("video_job_canceled")
                    value = await asyncio.wait_for(analyzer.video(job["media_id"], job["owner"], job["question"],
                        deadline=job["deadline"], check_active=check_active), max(1, job["deadline"] - time.time()))
                    check_active()
                    self.store.finish_job(job["id"], value)
                    # External media remains video knowledge, never a user fact.
                    from .persistent_memory import data_safe
                    if (job["origin"] and self.memory and value["confidence"] >= 0.65 and not value["uncovered_intervals"]
                            and data_safe(value["summary"]) and all(data_safe(tag) for tag in value["objects"][:12])):
                        self.memory.repository.add_video(job["owner"], job["source"] or job["media_id"],
                            value["summary"], value["objects"][:12], character=self.character,
                            confidence=value["confidence"])
                    await self.deliver(job, value)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.store.fail_job(job, str(exc) if isinstance(exc, MediaError) else type(exc).__name__)
                    if self.logger:
                        self.logger.warning("Q7 video job failed: %s", type(exc).__name__)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if self.logger:
                    self.logger.warning("Q7 worker deferred: %s", type(exc).__name__)
                await asyncio.sleep(1)

    async def deliver(self, job, value):
        if not job["origin"] or self.context is None:
            return
        if not self.active_scope(job["owner"]):
            self.store.cancel(job["id"], job["owner"])
            return
        from astrbot.core.message.components import Record
        from .reply_pacing import text_reply_chains, remaining_seconds
        from .vision_config import project_root
        persona = (project_root() / "configs/characters" / self.character / "system_prompt.txt").read_text(encoding="utf-8-sig")
        # This is prose generation with the same Provider, no Agent/tools/history.
        from .request_provider import request_chat
        from .context_budget import RequestBudget
        from astrbot.core.agent.message import Message
        provider = await self.provider_factory()
        prompts = load_prompts()
        task = prompts["vision_reply"]
        messages = [Message(role="system", content=persona + "\n" + task + "\n" + prompts["reply_output"]),
            Message(role="user", content=task + "\n" + json.dumps({"question": job["question"], "analysis": value}, ensure_ascii=False))]
        await RequestBudget(provider, "vision").fit(messages)
        started = time.monotonic()
        async with self.media.admission():
            result = await request_chat(provider, phase="vision", contexts=messages,
                func_tool=None, response_format=REPLY_FORMAT, request_max_retries=1)
        from .agent_runtime import parse_final_reply
        raw = result.completion_text or ""
        try:
            envelope = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise MediaError("invalid_video_reply") from exc
        if (not isinstance(envelope, dict) or set(envelope) != {"reply", "thinking_level", "voice"}
                or not isinstance(envelope.get("reply"), str)
                or type(envelope.get("voice")) is not bool or type(envelope.get("thinking_level")) is not int
                or envelope["thinking_level"] not in range(1, 6) or VOICE_ACTION_TEXT.search(envelope["reply"])):
            raise MediaError("invalid_video_reply")
        parsed = parse_final_reply(raw)
        text = parsed["reply"] if parsed else ""
        if not text or len(text) > 2000 or result.tools_call_name or "<think" in text:
            raise MediaError("invalid_video_reply")
        if not self.active_scope(job["owner"]):
            self.store.cancel(job["id"], job["owner"])
            return
        group = ":group:" in job["owner"]
        chains, transcripts = (await self.reply_media.reply_chains(text, voice=parsed["voice"], group=group)
            if self.reply_media else text_reply_chains(text, group=group))
        if self.store.claim_send(job["id"]):
            try:
                for chain, transcript in zip(chains, transcripts):
                    voice = any(isinstance(part, Record) for part in chain.chain)
                    while True:
                        if not self.active_scope(job["owner"]):
                            self.store.delivery(job["id"], "canceled")
                            return
                        if time.time() >= job["deadline"]:
                            raise MediaError("video_delivery_deadline")
                        delay = 0 if voice else remaining_seconds(transcript, parsed["thinking_level"], started)
                        if delay <= 0:
                            break
                        await asyncio.sleep(min(.25, delay))
                    sent = await self.context.send_message(job["origin"], chain)
                    if sent is False:
                        raise MediaError("video_delivery_unconfirmed")
                    started = time.monotonic()
            except BaseException:
                self.store.delivery(job["id"], "uncertain")
                raise
            else:
                self.store.delivery(job["id"], "sent")

    async def close(self):
        self.closed = True
        self.pending.clear()
        self.questions.clear()
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
