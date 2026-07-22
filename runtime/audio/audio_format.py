from __future__ import annotations

import json
import math
import sys
import wave
from array import array
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class WaveMetrics:
    path: str
    duration_seconds: float
    sample_rate: int
    channels: int
    sample_width_bits: int
    frame_count: int
    peak_pcm: int
    peak_normalized: float
    peak_dbfs: float | None
    clipped: bool
    clipped_samples: int


def write_pcm16_wav(path: str | Path, pcm_bytes: bytes, sample_rate: int, channels: int) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_bytes)
    return output_path


def inspect_wav(path: str | Path) -> WaveMetrics:
    wav_path = Path(path)
    with wave.open(str(wav_path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        frame_count = wav_file.getnframes()
        frames = wav_file.readframes(frame_count)

    if sample_width != 2:
        raise ValueError(f"Expected 16-bit PCM WAV, got {sample_width * 8}-bit samples.")

    samples = array("h")
    samples.frombytes(frames)
    if sys.byteorder != "little":
        samples.byteswap()

    peak_pcm = 0
    clipped_samples = 0
    for sample in samples:
        abs_sample = 32768 if sample == -32768 else abs(sample)
        if abs_sample > peak_pcm:
            peak_pcm = abs_sample
        if sample <= -32768 or sample >= 32767:
            clipped_samples += 1

    peak_normalized = peak_pcm / 32768.0
    peak_dbfs = None if peak_pcm == 0 else 20.0 * math.log10(peak_normalized)
    duration_seconds = frame_count / sample_rate if sample_rate else 0.0

    return WaveMetrics(
        path=str(wav_path),
        duration_seconds=duration_seconds,
        sample_rate=sample_rate,
        channels=channels,
        sample_width_bits=sample_width * 8,
        frame_count=frame_count,
        peak_pcm=peak_pcm,
        peak_normalized=peak_normalized,
        peak_dbfs=peak_dbfs,
        clipped=clipped_samples > 0,
        clipped_samples=clipped_samples,
    )


def save_metrics_json(metrics: WaveMetrics, path: str | Path) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(asdict(metrics), ensure_ascii=True, indent=2),
        encoding="utf-8",
    )
    return output_path
