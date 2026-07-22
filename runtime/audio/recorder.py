from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from runtime.audio.audio_format import WaveMetrics, inspect_wav, save_metrics_json, write_pcm16_wav


@dataclass(frozen=True)
class RecordingResult:
    wav_path: Path
    metrics_path: Path
    metrics: WaveMetrics


class ButtonRecorder:
    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        input_device: int | None = None,
        block_size: int = 1024,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.input_device = input_device
        self.block_size = block_size
        self._stream: Any | None = None
        self._frames: list[Any] = []
        self._statuses: list[str] = []

    def start(self) -> None:
        sd = _sounddevice()

        def callback(indata: Any, _frames: int, _time: Any, status: Any) -> None:
            if status:
                self._statuses.append(str(status))
            self._frames.append(indata.copy())

        self._frames = []
        self._statuses = []
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="int16",
            device=self.input_device,
            blocksize=self.block_size,
            callback=callback,
        )
        self._stream.start()

    def stop_and_save(self, recordings_dir: str | Path) -> RecordingResult:
        if self._stream is None:
            raise RuntimeError("Recording has not been started.")

        self._stream.stop()
        self._stream.close()
        self._stream = None

        pcm_bytes = self._frames_to_pcm_bytes(self._frames)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        wav_path = Path(recordings_dir) / f"recording-{timestamp}.wav"
        write_pcm16_wav(wav_path, pcm_bytes, self.sample_rate, self.channels)

        metrics = inspect_wav(wav_path)
        metrics_path = wav_path.with_suffix(".json")
        save_metrics_json(metrics, metrics_path)
        return RecordingResult(wav_path=wav_path, metrics_path=metrics_path, metrics=metrics)

    @staticmethod
    def _frames_to_pcm_bytes(frames: list[Any]) -> bytes:
        if not frames:
            return b""
        np = _numpy()
        return np.concatenate(frames, axis=0).astype("int16").tobytes()


def _sounddevice():
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency 'sounddevice'. Install stage 1 requirements with "
            "'python -m pip install -r runtime/audio/requirements.txt'."
        ) from exc
    return sd


def _numpy():
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency 'numpy'. Install stage 1 requirements with "
            "'python -m pip install -r runtime/audio/requirements.txt'."
        ) from exc
    return np
