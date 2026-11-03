"""Douyin gallery analysis; reuse Q7 decoding and its validated video pipeline."""
from __future__ import annotations

import asyncio
import hashlib
import json

from integrations.qq_astrbot.plugin.vision_analysis import VisionAnalyzer
from integrations.qq_astrbot.plugin.vision_media import MediaError, decode_images


class DouyinAnalyzer(VisionAnalyzer):
    async def gallery(self, keys, owner, question, *, check_active=None):
        if not keys:
            raise MediaError("gallery_images_unavailable")
        identity = hashlib.sha256(json.dumps(keys).encode()).hexdigest()
        cache = self.cache_key(identity, owner, question, "image_post_v1")
        # Check authorization/expiry even for a cached analysis.
        paths = [self.store.asset(key, owner)[0] for key in keys]
        if value := self.store.cached(cache, owner):
            return value
        parts = []
        size = self.config["max_frames_per_request"]
        for offset in range(0, len(keys), size):
            if check_active:
                check_active()
            batch_key = hashlib.sha256((cache + ":" + str(offset)).encode()).hexdigest()
            if cached := self.store.cached(batch_key, owner):
                parts.extend(cached["parts"])
                continue
            frames = []
            for index, path in enumerate(paths[offset:offset + size], offset + 1):
                _, decoded = await asyncio.to_thread(decode_images, path.read_bytes(), self.config)
                # Native gallery entries are still pictures. No background audio/ASR.
                frame = decoded[0]
                frame.timestamp, frame.also_at = index, []
                frames.append(frame)
            batch = await self.analyze_frames(frames, question, "image_post", end=len(keys), check_active=check_active)
            self.store.save(batch_key, keys[0], owner, {"parts": batch})
            parts.extend(batch)
        while len(parts) > 8:
            combined = []
            for offset in range(0, len(parts), 8):
                if check_active:
                    check_active()
                combined.append(await self.merge(parts[offset:offset + 8], question, "image_post", len(keys)))
            parts = combined
        if check_active:
            check_active()
        value = await self.merge(parts, question, "image_post", len(keys))
        value.update(source_type="image_post", media_ids=keys, image_count=len(keys),
            covered_images=list(range(1, len(keys) + 1)), uncovered_intervals=[], audio_status="not_applicable")
        self.store.save(cache, keys[0], owner, value)
        return value
