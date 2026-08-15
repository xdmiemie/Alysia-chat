from __future__ import annotations

import audioop
from pathlib import Path
import sys
import wave
from array import array
from dataclasses import dataclass

from runtime.audio.devices import select_output_device


@dataclass(frozen=True)
class PlaybackInfo:
    output_device_index: int
    output_device_name: str
    source_sample_rate: int
    playback_sample_rate: int
    channels: int


def play_wav(path: str | Path, output_device: int | None = None) -> PlaybackInfo:
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

    playback_sample_rate = _compatible_sample_rate(
        sd,
        device_index=selected_output.index,
        channels=channels,
        source_sample_rate=sample_rate,
        default_sample_rate=selected_output.default_sample_rate,
    )
    if playback_sample_rate != sample_rate:
        frames, _state = audioop.ratecv(
            frames,
            sample_width,
            channels,
            sample_rate,
            playback_sample_rate,
            None,
        )

    samples = array("h")
    samples.frombytes(frames)
    if sys.byteorder != "little":
        samples.byteswap()

    audio = np.asarray(samples, dtype=np.int16)
    if channels > 1:
        audio = audio.reshape(-1, channels)

    sd.play(
        audio,
        samplerate=playback_sample_rate,
        device=selected_output.index,
        blocking=True,
    )
    return PlaybackInfo(
        output_device_index=selected_output.index,
        output_device_name=selected_output.name,
        source_sample_rate=sample_rate,
        playback_sample_rate=playback_sample_rate,
        channels=channels,
    )


def _compatible_sample_rate(
    sd,
    *,
    device_index: int,
    channels: int,
    source_sample_rate: int,
    default_sample_rate: float,
) -> int:
    try:
        sd.check_output_settings(
            device=device_index,
            channels=channels,
            dtype="int16",
            samplerate=source_sample_rate,
        )
        return source_sample_rate
    except Exception as source_error:
        target_rate = int(round(default_sample_rate))
        if target_rate <= 0 or target_rate == source_sample_rate:
            raise source_error
        try:
            sd.check_output_settings(
                device=device_index,
                channels=channels,
                dtype="int16",
                samplerate=target_rate,
            )
        except Exception as target_error:
            raise RuntimeError(
                f"Output device {device_index} accepts neither WAV sample rate "
                f"{source_sample_rate} Hz nor default sample rate {target_rate} Hz: {target_error}"
            ) from source_error
        return target_rate


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
