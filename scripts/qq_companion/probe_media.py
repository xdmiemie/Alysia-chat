"""Exercise authenticated Q2 ASR/TTS locally without sending QQ messages."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

from services.media_api.app import _multipart_wav

ROOT = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:8765"
OPENER = build_opener(ProxyHandler({}))


def request(path: str, token: str, body: bytes | None = None, content_type: str | None = None) -> tuple[int, bytes]:
    headers = {"Authorization": f"Bearer {token}"}
    if content_type:
        headers["Content-Type"] = content_type
    with OPENER.open(Request(BASE + path, data=body, headers=headers,
                             method="POST" if body is not None else "GET"), timeout=150) as response:
        return response.status, response.read()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=10)
    args = parser.parse_args()
    if args.count < 1 or args.count > 20:
        parser.error("count must be 1..20")
    token = (ROOT / "storage/qq_companion/media_token").read_text(encoding="ascii").strip()
    status, health_bytes = request("/health", token)
    health = json.loads(health_bytes)
    if status != 200 or not health["asr"] or not health["tts"]:
        raise RuntimeError(f"Media service is not ready: {health}")
    sample = (ROOT / "model_store/registry/gpt_sovits/elysia/bailu-v1/references/neutral.wav").read_bytes()
    asr_body, asr_type = _multipart_wav(sample)
    asr_ok = tts_ok = 0
    started = time.monotonic()
    for _ in range(args.count):
        status, data = request("/v1/audio/transcriptions", token, asr_body, asr_type)
        result = json.loads(data)
        if status == 200 and isinstance(result.get("text"), str) and result.get("duration_ms", 0) > 0:
            asr_ok += 1
        else:
            raise RuntimeError("ASR contract check failed")
    for _ in range(args.count):
        body = json.dumps({"text": "你好，今天也要开心哦。", "character_id": "elysia",
                           "emotion": "neutral", "speed": 1.0, "format": "wav"}).encode()
        status, audio = request("/v1/audio/speech", token, body, "application/json")
        if status == 200 and audio.startswith(b"RIFF") and audio[8:12] == b"WAVE":
            tts_ok += 1
        else:
            raise RuntimeError("TTS contract check failed")
    print(f"Q2 local media probe: ASR {asr_ok}/{args.count}, TTS {tts_ok}/{args.count}, elapsed {time.monotonic()-started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
