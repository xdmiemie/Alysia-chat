"""Structured media analysis using an injected caller of the single main model."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import uuid

import jsonschema

from .prompt_loader import load_prompts
from .vision_config import fingerprint
from .vision_media import MediaError, decode_images, extract_audio, extract_segment, segment_ranges, video_info

ANALYSIS_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
    **{k: {"type": "array", "maxItems": 64, "items": {"type": "string", "maxLength": 500}}
       for k in ("objects", "actions", "ocr_text", "uncertainty")},
    "emotion": {"type": ["string", "null"]}, "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "timestamps": {"type": "array", "maxItems": 64, "items": {"type": "object", "additionalProperties": False,
        "properties": {"timestamp": {"type": "number", "minimum": 0}, "description": {"type": "string", "maxLength": 500}},
        "required": ["timestamp", "description"]}}},
    "required": ["summary", "objects", "actions", "ocr_text", "uncertainty", "emotion", "confidence", "timestamps"]}
def grammar_schema(value):
    # Expanding nested bounded strings/arrays exceeds this pinned llama.cpp's
    # grammar rule limit. Keep shape/types in decoding and enforce every size
    # bound below with the full schema, plus the model's output-token limit.
    if isinstance(value, dict):
        return {key: grammar_schema(item) for key, item in value.items() if key not in {"maxLength", "maxItems"}}
    if isinstance(value, list):
        return [grammar_schema(item) for item in value]
    return value


RESPONSE_FORMAT = {"type": "json_schema", "json_schema": {"name": "media_analysis", "strict": True, "schema": grammar_schema(ANALYSIS_SCHEMA)}}


def validate_analysis(raw, start=0, end=None):
    value = json.loads(raw) if isinstance(raw, str) else raw
    jsonschema.validate(value, ANALYSIS_SCHEMA)
    if not math.isfinite(value["confidence"]):
        raise MediaError("invalid_media_confidence")
    for row in value["timestamps"]:
        if not math.isfinite(row["timestamp"]) or row["timestamp"] < start or end is not None and row["timestamp"] > end:
            raise MediaError("invalid_media_timestamp")
    return value


class VisionAnalyzer:
    def __init__(self, store, config, call, *, transcribe=None):
        self.store, self.config, self.call, self.transcribe = store, config, call, transcribe

    def cache_key(self, key, owner, question, kind):
        payload = [key, owner, question, kind, fingerprint(self.config), load_prompts()["vision_analyze"], load_prompts()["vision_merge"]]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    async def analyze_frames(self, frames, question, kind, *, start=0, end=None, transcript=None, check_active=None):
        results, offset, size = [], 0, self.config["max_frames_per_request"]
        while offset < len(frames):
            if check_active:
                check_active()
            batch = frames[offset:offset + size]
            payload = {"question": question, "source_type": kind, "segment_start": start, "segment_end": end,
                "sampled_frames": [{"timestamp": f.timestamp, "same_image_at": f.also_at} for f in batch],
                "transcript": transcript, "audio_status": "transcribed" if transcript else "visual_only"}
            try:
                raw = await self.call("vision_analyze", payload, [f.url for f in batch], RESPONSE_FORMAT)
                value = validate_analysis(raw, start, end)
            except Exception as exc:
                # A conservative context rejection can still occur with a long
                # user question; split images without dropping sampled intervals.
                if len(batch) > 1 and ("context" in str(exc).lower() or "capacity" in str(exc).lower()):
                    size = max(1, len(batch) // 2)
                    continue
                raise
            value["sampled_timestamps"] = [f.timestamp for f in batch]
            results.append(value)
            offset += len(batch)
            await asyncio.sleep(0)  # The next request re-enters the common priority queue.
        return results

    async def merge(self, parts, question, kind, end):
        if len(parts) == 1:
            return {k: v for k, v in parts[0].items() if k in ANALYSIS_SCHEMA["properties"]}
        raw = await self.call("vision_merge", {"question": question, "source_type": kind, "parts": parts,
            "duration": end}, [], RESPONSE_FORMAT)
        return validate_analysis(raw, 0, end)

    async def image(self, key, owner, question="", *, kind=None):
        path, info = self.store.asset(key, owner)
        actual, frames = await asyncio.to_thread(decode_images, path.read_bytes(), self.config)
        kind = kind or actual
        cache = self.cache_key(key, owner, question, kind)
        if value := self.store.cached(cache, owner):
            return value
        parts = await self.analyze_frames(frames, question, kind)
        value = await self.merge(parts, question, kind, None)
        value.update(media_id=key, source_type=kind, sampled_timestamps=[f.timestamp for f in frames])
        self.store.save(cache, key, owner, value)
        return value

    async def video(self, key, owner, question="", *, deadline=None, check_active=None):
        path, _ = self.store.asset(key, owner)
        cache = self.cache_key(key, owner, question, "video")
        if value := self.store.cached(cache, owner):
            return value
        duration, has_audio = await asyncio.to_thread(video_info, path, self.config)
        parts, covered, missed, audio = [], [], [], []
        deadline = deadline or time.time() + self.config["video_job_timeout_seconds"]
        for start, end, times in segment_ranges(duration, self.config):
            if check_active:
                check_active()
            if time.time() >= deadline:
                raise TimeoutError("video_job_deadline")
            segment_key = hashlib.sha256((cache + f":{start}:{end}").encode()).hexdigest()
            cached = self.store.cached(segment_key, owner)
            if cached:
                parts.extend(cached["parts"])
                covered.append([start, end])
                audio.append(cached["audio"])
                continue
            try:
                segment = await asyncio.to_thread(extract_segment, path, start, end, times, self.config)
            except MediaError:
                missed.append([start, end])
                continue
            transcript = None
            audio_status = "no_audio" if not has_audio else "asr_disabled"
            if has_audio and self.config.get("asr_enabled") and self.transcribe:
                target = self.store.cache / (key + f"-{int(start)}-" + uuid.uuid4().hex + ".wav")
                try:
                    await asyncio.to_thread(extract_audio, path, target, start, end - start)
                    text = await self.transcribe(target.read_bytes())
                    if isinstance(text, dict):
                        text = text.get("text", "")
                    transcript = {"start": start, "end": end, "text": str(text)[:12000], "alignment": "segment"}
                    audio_status = "transcribed" if text else "asr_empty"
                except Exception:
                    audio_status = "asr_failed"
                finally:
                    target.unlink(missing_ok=True)
            segment_parts = await self.analyze_frames(segment.frames, question, "video", start=start, end=end,
                transcript=transcript, check_active=check_active)
            self.store.save(segment_key, key, owner, {"parts": segment_parts, "audio": audio_status})
            parts.extend(segment_parts)
            covered.append([start, end])
            audio.append(audio_status)
        if not parts:
            raise MediaError("video_no_frames_analyzed")
        if check_active:
            check_active()
        # Bound aggregation: summarize groups rather than building a huge prompt.
        while len(parts) > 8:
            parts = [await self.merge(parts[i:i + 8], question, "video", duration) for i in range(0, len(parts), 8)]
        value = await self.merge(parts, question, "video", duration)
        value.update(media_id=key, source_type="video", duration=duration, covered_intervals=covered,
            uncovered_intervals=missed, audio_status=audio, sampling_interval_seconds=self.config["frame_interval_seconds"])
        if missed:
            value["uncertainty"].append("部分区间抽帧失败，未覆盖区间已单独列出。")
        if has_audio and any(status != "transcribed" for status in audio):
            value["uncertainty"].append("部分或全部音轨未转写，相应区间只分析了画面。")
        self.store.save(cache, key, owner, value)
        return value
