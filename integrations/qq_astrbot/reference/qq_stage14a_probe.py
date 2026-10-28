"""Run a local Step 14A clip-to-cover probe without sending to QQ."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime.qq_adapter.songs import SongPolicy
from runtime.qq_adapter.song_audio import SongAudioPipeline, inspect_audio


async def probe(source: Path, target: Path, policy: SongPolicy, start: int, duration: int) -> dict:
    if target.exists():
        raise ValueError("probe output directory already exists")
    target.mkdir(parents=True)
    pipeline = SongAudioPipeline(policy.audio_config())
    report: dict = {"source": str(source.resolve(strict=True)), "stages": {}}

    async def stage(name: str, operation):
        began = time.monotonic()
        result = await operation
        report["stages"][name] = {"elapsed_seconds": round(time.monotonic() - began, 2)}
        return result

    clip = target / "clip.wav"
    clip_info = await stage("clip", pipeline.clip(source, clip, start, duration))
    report["stages"]["clip"]["audio"] = clip_info
    vocals, instrumental = await stage("separate", pipeline.separate(clip, target / "stems"))
    converted = await stage("convert", pipeline.convert(vocals, target / "rvc"))
    report["stages"]["convert"]["audio"] = inspect_audio(converted, policy.ffprobe)
    mix_info = await stage("mix", pipeline.mix(converted, instrumental, target / "cover.wav"))
    report["stages"]["mix"]["audio"] = mix_info
    report["model"] = "FeiyuVoice-RVC-v1"
    report["output"] = str((target / "cover.wav").resolve())
    (target / "probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                       encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--duration", type=int, default=8)
    parser.add_argument("--separator-executable", default=str(ROOT / "envs/song/Scripts/audio-separator.exe"))
    parser.add_argument("--rvc-python", default=str(ROOT / "envs/song/Scripts/python.exe"))
    args = parser.parse_args()
    policy = SongPolicy(enabled=True, separator_executable=args.separator_executable,
                        rvc_python=args.rvc_python,
                        max_duration_seconds=max(90, args.duration))
    print(json.dumps(asyncio.run(probe(args.source, args.output_dir, policy,
                                        args.start, args.duration)), ensure_ascii=False, indent=2))
