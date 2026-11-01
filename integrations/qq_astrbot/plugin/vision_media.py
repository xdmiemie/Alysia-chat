"""CPU-only, bounded media decoding. Never executes a user supplied path/command."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
from io import BytesIO
import ipaddress
import json
import math
from pathlib import Path
import socket
import subprocess
import warnings
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from urllib.error import HTTPError

from PIL import Image, ImageOps

from .vision_config import project_root


class MediaError(ValueError):
    """Safe error code; raw URLs and decoder errors stay out of public messages."""


@dataclass
class Frame:
    timestamp: float
    data: bytes
    width: int
    height: int
    also_at: list[float] = field(default_factory=list)

    @property
    def url(self):
        return "data:image/jpeg;base64," + base64.b64encode(self.data).decode("ascii")


@dataclass
class Segment:
    start: float
    end: float
    frames: list[Frame]


def segment_ranges(duration, config):
    if not math.isfinite(duration) or not 0 < duration <= config["max_video_seconds"]:
        raise MediaError("video_duration_exceeded")
    length, interval = config["segment_seconds"], config["frame_interval_seconds"]
    result = []
    for start in range(0, math.ceil(duration), length):
        end = min(float(start + length), duration)
        times = [float(t) for t in range(start, math.ceil(end), interval) if t < end]
        if len(times) > config["max_frames_per_segment"]:
            raise MediaError("segment_sampling_exceeded")
        result.append((float(start), end, times))
    return result


def validate_url(url, domains):
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (parsed.scheme not in {"https", "http"} or parsed.username or parsed.password
                or parsed.port not in {None, 80, 443} or not host
                or not any(host == d or host.endswith("." + d) for d in domains)):
            raise MediaError("media_domain_rejected")
        addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise MediaError("media_private_address_rejected")
        return url
    except (ValueError, OSError) as exc:
        if isinstance(exc, MediaError):
            raise
        raise MediaError("media_address_invalid") from exc


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_url(url, config, *, limit=None, domains=None):
    """Validate each redirect and bounded response. No cookies or browser profile."""
    domains = domains or config["allowed_media_domains"]
    limit = limit or config["max_image_bytes"]
    opener = build_opener(ProxyHandler({}), NoRedirect())
    for _ in range(5):
        validate_url(url, domains)
        try:
            request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"})
            with opener.open(request, timeout=25) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > limit:
                    raise MediaError("media_file_too_large")
                data = response.read(limit + 1)
                if not data or len(data) > limit:
                    raise MediaError("media_file_too_large")
                return data, url, response.headers.get_content_type()
        except HTTPError as exc:
            if exc.code in {301, 302, 303, 307, 308} and exc.headers.get("Location"):
                url = urljoin(url, exc.headers["Location"])
                continue
            raise MediaError("media_http_" + str(exc.code)) from exc
        except (OSError, ValueError) as exc:
            if isinstance(exc, MediaError):
                raise
            raise MediaError("media_download_failed") from exc
    raise MediaError("media_redirect_limit")


def normalize_image(image, config, timestamp=0.0):
    if image.width * image.height > config["max_image_pixels"]:
        raise MediaError("image_pixel_limit")
    image = ImageOps.exif_transpose(image).convert("RGB")
    image.thumbnail((config["max_image_long_edge"], config["max_image_long_edge"]))
    output = BytesIO()
    image.save(output, format="JPEG", quality=90)
    return Frame(timestamp, output.getvalue(), image.width, image.height)


def decode_images(data, config):
    if not 0 < len(data) <= config["max_image_bytes"]:
        raise MediaError("image_byte_limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                if image.format not in {"JPEG", "PNG", "GIF", "WEBP", "BMP"}:
                    raise MediaError("image_format_rejected")
                if image.width * image.height > config["max_image_pixels"]:
                    raise MediaError("image_pixel_limit")
                count = getattr(image, "n_frames", 1)
                if count <= 1:
                    image.load()
                    return "image", [normalize_image(image, config)]
                if count > 2000 or image.width * image.height * count > config["max_image_pixels"] * config["gif_max_frames"]:
                    raise MediaError("animation_frame_limit")
                starts, elapsed = [], 0
                for i in range(count):
                    image.seek(i)
                    starts.append(elapsed)
                    elapsed += max(10, min(10000, int(image.info.get("duration", 100))))
                if elapsed > 180000:
                    raise MediaError("animation_duration_limit")
                targets = [round((count - 1) * n / (min(count, config["gif_frames"]) - 1))
                           for n in range(min(count, config["gif_frames"]))]
                frames, seen = [], {}
                for index in dict.fromkeys(targets):
                    image.seek(index)
                    frame = normalize_image(image.copy(), config, starts[index] / 1000)
                    key = hashlib.sha256(frame.data).hexdigest()
                    if key in seen:
                        seen[key].also_at.append(frame.timestamp)
                    else:
                        seen[key] = frame
                        frames.append(frame)
                return "gif", frames
    except (OSError, SyntaxError, EOFError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise MediaError("image_decode_failed") from exc


def ffmpeg_tool(name):
    path = project_root() / "tools/ffmpeg/bin" / (name + ".exe")
    if not path.is_file():
        raise MediaError(name + "_missing")
    return str(path)


def video_info(path, config):
    if not path.is_file() or not 0 < path.stat().st_size <= config["max_video_bytes"]:
        raise MediaError("video_byte_limit")
    try:
        result = subprocess.run([ffmpeg_tool("ffprobe"), "-v", "error", "-show_format", "-show_streams",
            "-print_format", "json", str(path)], capture_output=True, timeout=15, check=True)
        info = json.loads(result.stdout)
        duration = float(info["format"]["duration"])
        segment_ranges(duration, config)
        videos = [s for s in info["streams"] if s.get("codec_type") == "video"]
        if not videos or any(int(s["width"]) * int(s["height"]) > config["max_image_pixels"] for s in videos):
            raise MediaError("invalid_video_stream")
        return duration, any(s.get("codec_type") == "audio" for s in info["streams"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        if isinstance(exc, MediaError):
            raise
        raise MediaError("video_probe_failed") from exc


def extract_segment(path, start, end, times, config):
    frames = []
    try:
        for timestamp in times:
            result = subprocess.run([ffmpeg_tool("ffmpeg"), "-v", "error", "-nostdin", "-hwaccel", "none",
                "-ss", str(timestamp), "-i", str(path), "-frames:v", "1", "-threads", "2",
                "-vf", f"scale=w='min({config['max_image_long_edge']},iw)':h='min({config['max_image_long_edge']},ih)':force_original_aspect_ratio=decrease",
                "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"], capture_output=True, timeout=25, check=True)
            _, decoded = decode_images(result.stdout, config)
            decoded[0].timestamp = timestamp
            frames.append(decoded[0])
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaError("video_frame_failed") from exc
    return Segment(start, end, frames)


def extract_audio(path, target, start=0, duration=24):
    try:
        subprocess.run([ffmpeg_tool("ffmpeg"), "-v", "error", "-nostdin", "-y", "-ss", str(start),
            "-i", str(path), "-t", str(duration), "-vn", "-ac", "1", "-ar", "16000", str(target)],
            capture_output=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaError("video_audio_failed") from exc
