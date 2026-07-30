from __future__ import annotations

import shutil
import subprocess
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path

from runtime.audio.audio_format import WaveMetrics, inspect_wav, write_pcm16_wav


SENSEVOICE_SAMPLE_RATE = 16000
SENSEVOICE_CHANNELS = 1
SENSEVOICE_SAMPLE_WIDTH_BITS = 16


@dataclass(frozen=True)
class NormalizedAudio:
    source_path: Path
    output_path: Path
    metrics: WaveMetrics
    method: str


def normalize_wav_for_sensevoice(
    source_path: str | Path,
    output_path: str | Path,
    *,
    ffmpeg_path: str | Path | None = None,
) -> NormalizedAudio:
    source = Path(source_path)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    try:
        source_metrics = inspect_wav(source)
    except Exception:
        if ffmpeg_path is None:
            raise
        _normalize_with_ffmpeg(source, output, Path(ffmpeg_path))
        return NormalizedAudio(
            source_path=source,
            output_path=output,
            metrics=inspect_wav(output),
            method="ffmpeg",
        )

    if (
        source_metrics.sample_rate == SENSEVOICE_SAMPLE_RATE
        and source_metrics.channels == SENSEVOICE_CHANNELS
        and source_metrics.sample_width_bits == SENSEVOICE_SAMPLE_WIDTH_BITS
    ):
        if source.resolve() != output.resolve():
            shutil.copyfile(source, output)
        return NormalizedAudio(
            source_path=source,
            output_path=output,
            metrics=inspect_wav(output),
            method="copy",
        )

    try:
        pcm = _read_pcm16_mono(source)
        if source_metrics.sample_rate != SENSEVOICE_SAMPLE_RATE:
            pcm = _resample_linear(pcm, source_metrics.sample_rate, SENSEVOICE_SAMPLE_RATE)
        write_pcm16_wav(output, pcm.tobytes(), SENSEVOICE_SAMPLE_RATE, SENSEVOICE_CHANNELS)
        return NormalizedAudio(
            source_path=source,
            output_path=output,
            metrics=inspect_wav(output),
            method="python",
        )
    except Exception:
        if ffmpeg_path is None:
            raise
        _normalize_with_ffmpeg(source, output, Path(ffmpeg_path))
        return NormalizedAudio(
            source_path=source,
            output_path=output,
            metrics=inspect_wav(output),
            method="ffmpeg",
        )


def _normalize_with_ffmpeg(source: Path, output: Path, ffmpeg_path: Path) -> None:
    if not ffmpeg_path.exists():
        raise RuntimeError(f"FFmpeg not found: {ffmpeg_path}")
    completed = subprocess.run(
        [
            str(ffmpeg_path),
            "-y",
            "-i",
            str(source),
            "-ar",
            str(SENSEVOICE_SAMPLE_RATE),
            "-ac",
            str(SENSEVOICE_CHANNELS),
            "-c:a",
            "pcm_s16le",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"FFmpeg normalization failed: {detail}")


def _read_pcm16_mono(path: Path) -> array:
    with wave.open(str(path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        frame_count = wav_file.getnframes()
        frames = wav_file.readframes(frame_count)

    if sample_width != 2:
        raise ValueError(f"Expected 16-bit PCM WAV, got {sample_width * 8}-bit samples.")

    samples = array("h")
    samples.frombytes(frames)
    if channels <= 1:
        return samples

    mono = array("h")
    for index in range(0, len(samples), channels):
        frame = samples[index : index + channels]
        if not frame:
            continue
        mono.append(_clamp_pcm16(round(sum(frame) / len(frame))))
    return mono


def _resample_linear(samples: array, source_rate: int, target_rate: int) -> array:
    if source_rate <= 0:
        raise ValueError("source sample rate must be positive")
    if not samples or source_rate == target_rate:
        return samples
    output_length = max(1, round(len(samples) * target_rate / source_rate))
    if len(samples) == 1:
        return array("h", [samples[0]] * output_length)

    result = array("h")
    scale = (len(samples) - 1) / max(1, output_length - 1)
    for index in range(output_length):
        position = index * scale
        left = int(position)
        right = min(left + 1, len(samples) - 1)
        fraction = position - left
        value = samples[left] * (1.0 - fraction) + samples[right] * fraction
        result.append(_clamp_pcm16(round(value)))
    return result


def _clamp_pcm16(value: int) -> int:
    return max(-32768, min(32767, int(value)))
