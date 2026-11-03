"""Validate complete Douyin videos; never clip an over-limit video."""
from __future__ import annotations

import asyncio
import json
import math
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from integrations.qq_astrbot.plugin.vision_media import MediaError, ffmpeg_tool

SHORT_VIDEO_SECONDS = 180
MAX_VIDEO_SECONDS = 600


async def _run(*args, timeout=30):
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}))
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
        if process.returncode:
            raise MediaError("douyin_video_decode_failed")
        return stdout
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


async def prepare_video(data, directory, config):
    """Verify the actual file duration before any model analysis."""
    with TemporaryDirectory(prefix="douyin-probe-", dir=directory) as temporary:
        source = Path(temporary) / "source.mp4"
        source.write_bytes(data)
        raw = await _run(ffmpeg_tool("ffprobe"), "-v", "error", "-show_format", "-show_streams",
                         "-print_format", "json", str(source))
        try:
            info = json.loads(raw)
            duration = float(info["format"]["duration"])
            streams = [s for s in info["streams"] if s.get("codec_type") == "video"]
            if (not math.isfinite(duration) or duration <= 0 or not streams
                    or any(int(s["width"]) * int(s["height"]) > config["max_image_pixels"] for s in streams)):
                raise ValueError()
        except (ValueError, KeyError, TypeError) as exc:
            raise MediaError("invalid_video_stream") from exc
        if duration > MAX_VIDEO_SECONDS:
            raise MediaError("video_too_long")
        if not 0 < len(data) <= config["max_video_bytes"]:
            raise MediaError("media_file_too_large")
        return data, duration
