from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol

from runtime.gpt_sovits.config import GPTSoVITSConfig
from runtime.gpt_sovits.types import ReferenceAudio
from runtime.common.http import urlopen


class SpeechBackend(Protocol):
    def synthesize(self, text: str, reference: ReferenceAudio, speed: float, audio_format: str) -> tuple[bytes, int]:
        ...


class GPTSoVITSHttpBackend:
    def __init__(self, config: GPTSoVITSConfig) -> None:
        self.config = config

    def synthesize(self, text: str, reference: ReferenceAudio, speed: float, audio_format: str) -> tuple[bytes, int]:
        payload = {
            "text": text,
            "text_lang": self.config.text_lang,
            "ref_audio_path": str(reference.path),
            "prompt_text": reference.prompt_text,
            "prompt_lang": self.config.prompt_lang,
            "speed_factor": speed,
            "media_type": audio_format,
            "streaming_mode": False,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.config.upstream_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.perf_counter()
        try:
            with urlopen(request, timeout=self.config.request_timeout_seconds) as response:
                audio = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GPT-SoVITS upstream request failed: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"GPT-SoVITS upstream request failed: {exc}") from exc
        backend_ms = int((time.perf_counter() - started) * 1000)
        if not audio:
            raise RuntimeError("GPT-SoVITS upstream returned empty audio")
        return audio, backend_ms


def write_audio_bytes(path: str | Path, audio: bytes) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(audio)
    return output_path
