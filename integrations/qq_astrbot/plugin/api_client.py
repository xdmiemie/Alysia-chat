"""Bounded local media client. Model output never supplies a path or URL."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import urllib.request
import uuid

from .media_state import MediaState


class MediaClient:
    def __init__(self, directory: Path):
        self.directory = directory
        self.state = MediaState(directory)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _request(self, path: str, body=None, content_type="application/json", *, binary=False):
        token = os.environ.get("QQ_COMPANION_MEDIA_TOKEN", "")
        token_file = self.directory / "media_token"
        if not token and token_file.is_file():
            token = token_file.read_text(encoding="ascii").strip()
        if not token:
            raise RuntimeError("Media token is unavailable")
        data = (json.dumps(body, ensure_ascii=False).encode() if isinstance(body, dict) else body)
        request = urllib.request.Request("http://127.0.0.1:8765" + path, data=data,
            headers={"Authorization": "Bearer " + token, "Content-Type": content_type})
        with self.opener.open(request, timeout=150) as response:
            content = response.read(40 * 1024 * 1024 + 1)
        if len(content) > 40 * 1024 * 1024:
            raise ValueError("Media response is too large")
        return content if binary else json.loads(content)

    async def request(self, path, body=None, **kwargs):
        return await asyncio.to_thread(self._request, path, body, **kwargs)

    @asynccontextmanager
    async def admission(self, *, timeout=1800):
        deadline = asyncio.get_running_loop().time() + timeout
        lease = None
        while lease is None:
            lease = self.state.enter()
            if lease:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("GPU job wait expired")
            await asyncio.sleep(0.25)
        try:
            yield
        finally:
            self.state.leave(lease)

    async def transcribe(self, data: bytes):
        if not 0 < len(data) <= 12 * 1024 * 1024:
            raise ValueError("Voice input exceeds limit")
        boundary = uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="input.wav"\r\n'
                'Content-Type: audio/wav\r\n\r\n').encode() + data + f"\r\n--{boundary}--\r\n".encode()
        async with self.admission():
            return await self.request("/v1/audio/transcriptions", body,
                content_type="multipart/form-data; boundary=" + boundary)

    async def speech(self, text: str, emotion: str, character_id: str) -> bytes:
        async with self.admission():
            return await self.request("/v1/audio/speech", {"text": text, "emotion": emotion,
                "character_id": character_id, "format": "wav"}, binary=True)

    def save_audio(self, data: bytes) -> Path:
        if not data.startswith(b"RIFF") or data[8:12] != b"WAVE":
            raise ValueError("Expected WAV media")
        directory = self.directory / "media_cache"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (uuid.uuid4().hex + ".wav")
        path.write_bytes(data)
        return path
