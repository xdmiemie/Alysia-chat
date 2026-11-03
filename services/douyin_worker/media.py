"""Bound Douyin analysis to the first three minutes without changing Q7 budgets."""
from __future__ import annotations

import asyncio
import json
import math
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from integrations.qq_astrbot.plugin.vision_media import MediaError, ffmpeg_tool

MAX_VIDEO_SECONDS = 180


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


async def video_prefix(data, directory, config):
    """Return bounded local media and its original duration; cancellation kills ffmpeg."""
    with TemporaryDirectory(prefix="douyin-prefix-", dir=directory) as temporary:
        source, target = Path(temporary) / "source.mp4", Path(temporary) / "prefix.mp4"
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
        limit = min(MAX_VIDEO_SECONDS, config["max_video_seconds"])
        if duration <= limit:
            return data, duration
        # Re-encode at the boundary: stream copying may include packets past 180s.
        edge = config["max_image_long_edge"]
        await _run(ffmpeg_tool("ffmpeg"), "-v", "error", "-nostdin", "-y", "-hwaccel", "none",
                   "-i", str(source), "-t", str(limit), "-map", "0:v:0", "-map", "0:a:0?",
                   "-vf", f"scale=w='min({edge},iw)':h='min({edge},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                   "-c:v", "libx264", "-preset", "ultrafast", "-threads", "2",
                   "-b:v", "2M", "-maxrate", "2M", "-bufsize", "4M", "-c:a", "aac", "-b:a", "128k",
                   "-movflags", "+faststart", str(target), timeout=180)
        result = target.read_bytes()
        if not 0 < len(result) <= config["max_video_bytes"]:
            raise MediaError("media_file_too_large")
        return result, duration
