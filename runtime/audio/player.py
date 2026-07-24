from __future__ import annotations

from pathlib import Path
import sys
import wave
from array import array

from runtime.audio.devices import select_output_device


def play_wav(path: str | Path, output_device: int | None = None) -> None:
    sd = _sounddevice()
    np = _numpy()
    selected_output = select_output_device(output_device)

    wav_path = Path(path)
    with wave.open(str(wav_path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        frames = wav_file.readframes(wav_file.getnframes())

    if sample_width != 2:
        raise ValueError(f"Expected 16-bit PCM WAV, got {sample_width * 8}-bit samples.")

    samples = array("h")
    samples.frombytes(frames)
    if sys.byteorder != "little":
        samples.byteswap()

    audio = np.asarray(samples, dtype=np.int16)
    if channels > 1:
        audio = audio.reshape(-1, channels)

    sd.play(audio, samplerate=sample_rate, device=selected_output.index, blocking=True)


def _sounddevice():
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency 'sounddevice'. Install stage 1 requirements with "
            "'powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/bootstrap.ps1'."
        ) from exc
    return sd


def _numpy():
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency 'numpy'. Install stage 1 requirements with "
            "'powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/bootstrap.ps1'."
        ) from exc
    return np
