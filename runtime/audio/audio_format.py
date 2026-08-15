from __future__ import annotations

import json
import math
import shutil
import sys
import wave
import audioop
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


def normalize_wav(
    source: str | Path,
    destination: str | Path,
    *,
    sample_rate: int = 16000,
) -> Path:
    """Convert an uncompressed WAV to the chat/ASR PCM contract.

    Stage 13 deliberately keeps this small and deterministic: microphone input
    and imported WAV files become 16 kHz, signed 16-bit, mono PCM.  Compressed
    containers remain the responsibility of the existing FFmpeg ingestion
    tools instead of being accepted here and failing later inside SenseVoice.
    """
    source_path = Path(source)
    if not source_path.is_file():
        raise FileNotFoundError(f"audio file not found: {source_path}")
    with wave.open(str(source_path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        width = wav_file.getsampwidth()
        source_rate = wav_file.getframerate()
        frames = wav_file.readframes(wav_file.getnframes())
    if channels not in {1, 2}:
        raise ValueError(f"Expected mono or stereo WAV, got {channels} channels.")
    if width not in {1, 2, 3, 4}:
        raise ValueError(f"Unsupported PCM sample width: {width * 8} bits.")
    if width != 2:
        frames = audioop.lin2lin(frames, width, 2)
    if channels == 2:
        frames = audioop.tomono(frames, 2, 0.5, 0.5)
    if source_rate != sample_rate:
        frames, _state = audioop.ratecv(frames, 2, 1, source_rate, sample_rate, None)
    return write_pcm16_wav(destination, frames, sample_rate, 1)


def persist_chat_wav(
    source: str | Path,
    recordings_root: str | Path,
    conversation_id: str,
    message_id: str,
) -> tuple[Path, Path, WaveMetrics]:
    """Persist both the original capture and normalized Stage 13 input."""
    if not conversation_id.strip() or not message_id.strip():
        raise ValueError("conversation_id and message_id are required")
    source_path = Path(source)
    target_dir = Path(recordings_root) / conversation_id / message_id
    target_dir.mkdir(parents=True, exist_ok=True)
    raw_path = target_dir / "raw.wav"
    normalized_path = target_dir / "normalized.wav"
    if source_path.resolve() != raw_path.resolve():
        shutil.copy2(source_path, raw_path)
    normalize_wav(raw_path, normalized_path)
    metrics = inspect_wav(normalized_path)
    save_metrics_json(metrics, target_dir / "metrics.json")
    return raw_path, normalized_path, metrics


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
