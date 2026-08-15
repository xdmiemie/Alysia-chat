from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import time
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
        max_duration_seconds: float = 60.0,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.input_device = input_device
        self.block_size = block_size
        self.max_duration_seconds = max(1.0, float(max_duration_seconds))
        self._stream: Any | None = None
        self._frames: list[Any] = []
        self._statuses: list[str] = []
        self._started_at: float | None = None
        self._level = 0.0

    @property
    def is_recording(self) -> bool:
        return self._stream is not None

    @property
    def elapsed_seconds(self) -> float:
        if self._started_at is None:
            return 0.0
        return min(time.monotonic() - self._started_at, self.max_duration_seconds)

    @property
    def level(self) -> float:
        """Latest normalized input peak, suitable for a lightweight meter."""
        return self._level

    @property
    def reached_max_duration(self) -> bool:
        return self.is_recording and self.elapsed_seconds >= self.max_duration_seconds

    def start(self) -> None:
        sd = _sounddevice()

        def callback(indata: Any, _frames: int, _time: Any, status: Any) -> None:
            if status:
                self._statuses.append(str(status))
            self._frames.append(indata.copy())
            try:
                self._level = min(1.0, float(abs(indata).max()) / 32768.0)
            except Exception:
                self._level = 0.0

        self._frames = []
        self._statuses = []
        self._level = 0.0
        def open_stream(device: int | None) -> Any:
            stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                device=device,
                blocksize=self.block_size,
                callback=callback,
            )
            try:
                stream.start()
            except Exception:
                stream.close()
                raise
            return stream

        try:
            self._stream = open_stream(self.input_device)
        except Exception:
            # A saved device index can disappear after unplug/reboot. Restore
            # the system default once before reporting that recording is
            # unavailable; the configured value itself is left untouched.
            if self.input_device is None:
                raise
            self._stream = open_stream(None)
        self._started_at = time.monotonic()

    def stop_and_save(self, recordings_dir: str | Path) -> RecordingResult:
        pcm_bytes = self._stop_and_collect()
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        wav_path = Path(recordings_dir) / f"recording-{timestamp}.wav"
        write_pcm16_wav(wav_path, pcm_bytes, self.sample_rate, self.channels)

        metrics = inspect_wav(wav_path)
        metrics_path = wav_path.with_suffix(".json")
        save_metrics_json(metrics, metrics_path)
        return RecordingResult(wav_path=wav_path, metrics_path=metrics_path, metrics=metrics)

    def stop_chat_message(
        self,
        recordings_root: str | Path,
        conversation_id: str,
        message_id: str,
    ) -> RecordingResult:
        """Save raw and normalized files using the Stage 13 layout."""
        pcm_bytes = self._stop_and_collect()
        message_dir = Path(recordings_root) / conversation_id / message_id
        raw_path = message_dir / "raw.wav"
        normalized_path = message_dir / "normalized.wav"
        # The capture stream already uses the ASR contract. Keeping both names
        # preserves the original and leaves room for other input formats later.
        write_pcm16_wav(raw_path, pcm_bytes, self.sample_rate, self.channels)
        write_pcm16_wav(normalized_path, pcm_bytes, self.sample_rate, self.channels)
        metrics = inspect_wav(normalized_path)
        metrics_path = message_dir / "metrics.json"
        save_metrics_json(metrics, metrics_path)
        return RecordingResult(normalized_path, metrics_path, metrics)

    def _stop_and_collect(self) -> bytes:
        if self._stream is None:
            raise RuntimeError("Recording has not been started.")
        self._stream.stop()
        self._stream.close()
        self._stream = None
        self._started_at = None
        return self._frames_to_pcm_bytes(self._frames)

    def cancel(self) -> None:
        """Stop capture without creating a file."""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
        self._stream = None
        self._frames = []
        self._statuses = []
        self._started_at = None
        self._level = 0.0

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
