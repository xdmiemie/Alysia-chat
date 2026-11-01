"""Validated media budgets and independently versioned capability gates."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import yaml

from .inference_queue import state_directory
from .prompt_loader import prompt_path

KINDS = {"image", "sticker", "gif", "video"}


def project_root():
    return prompt_path().parents[2]


def load_vision_config():
    path = Path(os.environ.get("ASTRBOT_CHAT_VISION_CONFIG", project_root() / "configs/qq_companion/vision.yaml"))
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("invalid_vision_config")
    ranges = {"max_image_long_edge": (32, 1024), "max_frames_per_segment": (1, 12),
              "max_frames_per_request": (1, 12), "frame_interval_seconds": (1, 10),
              "segment_seconds": (1, 24), "gif_frames": (2, 8), "gif_max_frames": (2, 8),
              "visual_tokens_per_image": (1024, 8192), "max_video_seconds": (1, 180),
              "max_video_bytes": (1024, 104857600), "max_image_bytes": (1024, 20971520),
              "max_image_pixels": (1024, 20000000), "max_attempts": (1, 3),
              "request_timeout_seconds": (1, 180), "queue_timeout_seconds": (1, 1800),
              "video_job_timeout_seconds": (10, 3600), "cache_ttl_seconds": (60, 86400)}
    ranges["media_question_window_seconds"] = (1, 300)
    for key, (low, high) in ranges.items():
        if type(data.get(key)) is not int or not low <= data[key] <= high:
            raise ValueError("invalid_vision_config_" + key)
    if data["segment_seconds"] / data["frame_interval_seconds"] > data["max_frames_per_segment"]:
        raise ValueError("segment_sampling_would_omit_frames")
    if not 2 <= data["gif_frames"] <= data["gif_max_frames"]:
        raise ValueError("invalid_gif_sampling")
    if not isinstance(data.get("allowed_media_domains"), list):
        raise ValueError("invalid_media_domains")
    settings = yaml.safe_load((project_root() / "configs/runtime/qwen3.yaml").read_text(encoding="utf-8-sig"))
    if data["visual_tokens_per_image"] < int(settings.get("image_max_tokens", 4096)):
        raise ValueError("visual_reservation_below_backend_limit")
    return data


def fingerprint(config=None):
    root = project_root()
    active = json.loads((root / "model_store/active/qwen3.json").read_text(encoding="utf-8-sig"))
    config = config or load_vision_config()
    # Identity and immutable component checksum invalidate gates after a model switch.
    from .prompt_loader import load_prompts
    prompts = load_prompts()
    binary = root / "tools/llama.cpp/llama-server.exe"
    build = {"size": binary.stat().st_size, "modified_ns": binary.stat().st_mtime_ns} if binary.is_file() else None
    payload = {"model": active.get("model_id"), "runtime": active.get("runtime"), "build": build,
               "pipeline": {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                   for name in ("vision_media.py", "vision_analysis.py", "vision_runtime.py", "vision_client.py", "request_provider.py", "model_messages.py")},
               "runtime_settings": yaml.safe_load((root / "configs/runtime/qwen3.yaml").read_text(encoding="utf-8-sig")),
               "prompts": {key: prompts[key] for key in ("vision_analyze", "vision_merge", "vision_context", "vision_reply")},
               "budgets": {k: v for k, v in config.items() if k != "enabled"}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class VisionGates:
    def __init__(self, directory=None, config=None):
        self.directory = Path(directory) if directory is not None else state_directory()
        self.config = config or load_vision_config()
        self.path = self.directory / "vision_gates.json"

    def enabled(self, kind):
        if kind not in KINDS or not self.config.get("enabled"):
            return False
        try:
            report = json.loads(self.path.read_text(encoding="utf-8"))
            return report.get("fingerprint") == fingerprint(self.config) and report.get("capabilities", {}).get(kind, {}).get("passed") is True
        except (OSError, ValueError):
            return False

    def record(self, kind, evidence):
        if kind not in KINDS or not evidence.get("passed") or not evidence.get("report"):
            raise ValueError("invalid_vision_gate_evidence")
        current = fingerprint(self.config)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if data.get("fingerprint") != current:
            data = {"fingerprint": current, "capabilities": {}}
        data["capabilities"][kind] = evidence
        self.directory.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)
