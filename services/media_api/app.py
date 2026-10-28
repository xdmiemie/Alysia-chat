"""Local, authenticated facade over the existing SenseVoice and GPT-SoVITS services.

The facade never loads model weights. Its upstreams remain separately managed by
the QQ supervisor, so failure of speech synthesis leaves text chat available.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from threading import Lock
import wave
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from runtime.gpt_sovits.types import SpeechRequest

ROOT = Path(__file__).resolve().parents[2]
MAX_UPLOAD = 12 * 1024 * 1024
MAX_DURATION_MS = 60_000
ASR_URL = "http://127.0.0.1:5001"
TTS_URL = "http://127.0.0.1:5002"
LOCAL_OPENER = build_opener(ProxyHandler({}))
MEDIA_LOCK = Lock()


class SpeechPayload(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    character_id: str = "elysia"
    emotion: str = "neutral"
    speed: float = Field(default=1.0, ge=0.75, le=1.25)
    format: str = "wav"


def _token(authorization: str | None = Header(default=None)) -> None:
    import hmac

    expected = os.environ.get("QQ_COMPANION_MEDIA_TOKEN", "")
    if not expected:
        raise HTTPException(503, "Media API token is not configured")
    candidate = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
    if not hmac.compare_digest(candidate, expected):
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})


def _upstream(path: str, *, body: bytes | None = None, content_type: str | None = None) -> bytes:
    headers = {"Content-Type": content_type} if content_type else {}
    request = Request(path, data=body, headers=headers, method="POST" if body is not None else "GET")
    try:
        with MEDIA_LOCK, LOCAL_OPENER.open(request, timeout=130) as response:
            return response.read()
    except HTTPError as exc:
        raise HTTPException(502, f"Upstream returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError) as exc:
        raise HTTPException(503, "Media upstream is unavailable") from exc


def _available(url: str) -> bool:
    try:
        with LOCAL_OPENER.open(f"{url}/health", timeout=1.5) as response:
            return response.status == 200
    except (URLError, TimeoutError):
        return False


def _wav_duration(data: bytes) -> int:
    import io

    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            if wav.getnchannels() not in (1, 2) or wav.getframerate() < 8000:
                raise ValueError("Unsupported WAV channels or sample rate")
            return round(wav.getnframes() * 1000 / wav.getframerate())
    except (EOFError, wave.Error) as exc:
        raise ValueError("Invalid WAV audio") from exc


def _as_wav(data: bytes, filename: str) -> bytes:
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return data
    if not (data.startswith(b"OggS") and Path(filename).suffix.lower() in (".opus", ".ogg")):
        raise ValueError("Only WAV or Ogg Opus audio is supported")
    with tempfile.TemporaryDirectory(prefix="qq_media_") as directory:
        source = Path(directory) / "input.opus"
        target = Path(directory) / "output.wav"
        source.write_bytes(data)
        try:
            result = subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source),
                 "-t", "61", "-ac", "1", "-ar", "16000", str(target)],
                capture_output=True, timeout=20, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("Audio conversion failed") from exc
        if result.returncode or not target.exists():
            raise ValueError("Invalid Ogg Opus audio")
        return target.read_bytes()


def _multipart_wav(data: bytes) -> tuple[bytes, str]:
    import secrets

    boundary = f"qqmedia{secrets.token_hex(12)}"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\n"
            "Content-Type: audio/wav\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def create_app() -> FastAPI:
    app = FastAPI(title="astrbot-chat media API", version="0.2.0")

    @app.get("/health", dependencies=[Depends(_token)])
    def health():
        return {"service": "media_api", "version": "0.2.0", "asr": _available(ASR_URL), "tts": _available(TTS_URL)}

    @app.get("/v1/characters", dependencies=[Depends(_token)])
    def characters():
        character_id = json.loads((ROOT / "model_store/active/qwen3.json").read_text(encoding="utf-8-sig"))["character_id"]
        model = json.loads((ROOT / f"model_store/active/gpt_sovits_{character_id}.json").read_text(encoding="utf-8-sig"))
        from runtime.common.config import load_flat_yaml

        emotions = list(load_flat_yaml(ROOT / "configs/characters" / character_id / "emotion_map.yaml"))
        return {"characters": [{"character_id": character_id, "tts_model_id": model["model_id"], "emotions": emotions}]}

    @app.get("/v1/jobs/{job_id}", dependencies=[Depends(_token)])
    def job_status(job_id: str):
        if len(job_id) != 32 or any(character not in "0123456789abcdef" for character in job_id):
            raise HTTPException(404, "Job not found")
        record = ROOT / "storage/qq_companion/jobs" / f"{job_id}.json"
        if not record.exists():
            record = ROOT / "storage/qq_companion/model_backups" / job_id / "record.json"
            if not record.exists():
                raise HTTPException(404, "Job not found")
        return json.loads(record.read_text(encoding="utf-8"))

    @app.post("/v1/audio/transcriptions", dependencies=[Depends(_token)])
    async def transcriptions(file: UploadFile = File(...)):
        data = await file.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "Audio exceeds 12 MiB")
        try:
            wav = _as_wav(data, file.filename or "")
            duration = _wav_duration(wav)
        except ValueError as exc:
            raise HTTPException(415, str(exc)) from exc
        if duration > MAX_DURATION_MS:
            raise HTTPException(413, "Audio exceeds 60 seconds")
        body, content_type = _multipart_wav(wav)
        result = json.loads(_upstream(f"{ASR_URL}/v1/audio/transcriptions", body=body, content_type=content_type))
        return {"text": result["text"], "language": result["language"], "duration_ms": result["duration_ms"]}

    @app.post("/v1/audio/speech", dependencies=[Depends(_token)])
    def speech(payload: SpeechPayload):
        request = SpeechRequest(**payload.model_dump())
        active = json.loads((ROOT / "model_store/active/qwen3.json").read_text(encoding="utf-8-sig"))
        if request.character_id != active["character_id"]:
            raise HTTPException(400, "Character is not active")
        if request.format.lower() != "wav":
            raise HTTPException(400, "Only WAV output is supported")
        from runtime.common.config import load_flat_yaml

        emotions = load_flat_yaml(ROOT / "configs/characters" / request.character_id / "emotion_map.yaml")
        if request.emotion not in emotions:
            raise HTTPException(400, "Unsupported emotion")
        audio = _upstream(f"{TTS_URL}/v1/audio/speech", body=json.dumps(payload.model_dump()).encode(), content_type="application/json")
        return Response(content=audio, media_type="audio/wav")

    return app


app = create_app()
