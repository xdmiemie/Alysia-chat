from __future__ import annotations

import json
import math
import queue
import sys
from array import array
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from runtime.audio.audio_format import WaveMetrics, inspect_wav, save_metrics_json, write_pcm16_wav


@dataclass(frozen=True)
class VadConfig:
    sample_rate: int = 16000
    channels: int = 1
    frame_ms: int = 30
    pre_roll_ms: int = 450
    speech_start_frames: int = 4
    silence_end_ms: int = 750
    min_speech_ms: int = 300
    max_speech_ms: int = 25_000
    rms_start_multiplier: float = 3.0
    rms_stop_multiplier: float = 1.8
    rms_min_threshold: float = 350.0
    webrtc_aggressiveness: int = 2

    @property
    def frame_samples(self) -> int:
        return int(self.sample_rate * self.frame_ms / 1000)

    @property
    def frame_bytes(self) -> int:
        return self.frame_samples * self.channels * 2

    @property
    def pre_roll_frames(self) -> int:
        return max(1, math.ceil(self.pre_roll_ms / self.frame_ms))

    @property
    def silence_end_frames(self) -> int:
        return max(1, math.ceil(self.silence_end_ms / self.frame_ms))

    @property
    def min_speech_frames(self) -> int:
        return max(1, math.ceil(self.min_speech_ms / self.frame_ms))

    @property
    def max_speech_frames(self) -> int:
        return max(1, math.ceil(self.max_speech_ms / self.frame_ms))

    def validate(self) -> None:
        if self.sample_rate != 16000:
            raise ValueError("Stage 6 microphone capture requires 16 kHz.")
        if self.channels != 1:
            raise ValueError("Stage 6 microphone capture requires mono audio.")
        if self.frame_ms not in {20, 30}:
            raise ValueError("Stage 6 VAD frame_ms must be 20 or 30 ms.")


@dataclass(frozen=True)
class VadEvent:
    event: str
    frame_index: int
    at_ms: int
    duration_ms: int = 0
    silence_ms: int = 0
    reason: str = ""


@dataclass(frozen=True)
class SegmentedUtterance:
    pcm_bytes: bytes
    events: tuple[VadEvent, ...]
    discarded: bool
    reason: str = ""


@dataclass(frozen=True)
class VadRecordingResult:
    wav_path: Path
    metrics_path: Path
    events_path: Path
    metrics: WaveMetrics
    events: tuple[VadEvent, ...]


class VadBackend(Protocol):
    name: str

    def is_speech(self, frame_bytes: bytes, in_speech: bool) -> bool:
        ...


class EnergyVad:
    name = "rms-energy"

    def __init__(self, config: VadConfig) -> None:
        self.config = config
        self.noise_floor = config.rms_min_threshold / config.rms_start_multiplier

    def is_speech(self, frame_bytes: bytes, in_speech: bool) -> bool:
        rms = _pcm16_rms(frame_bytes)
        multiplier = self.config.rms_stop_multiplier if in_speech else self.config.rms_start_multiplier
        threshold = max(self.config.rms_min_threshold, self.noise_floor * multiplier)
        voiced = rms >= threshold
        if not voiced and not in_speech:
            self.noise_floor = (self.noise_floor * 0.95) + (rms * 0.05)
        return voiced


class WebRtcOrEnergyVad:
    def __init__(self, config: VadConfig) -> None:
        self.energy = EnergyVad(config)
        self._webrtc = None
        self.name = self.energy.name
        try:
            import webrtcvad  # type: ignore[import-not-found]
        except ImportError:
            return

        self._webrtc = webrtcvad.Vad(config.webrtc_aggressiveness)
        self.name = "webrtcvad"
        self.config = config

    def is_speech(self, frame_bytes: bytes, in_speech: bool) -> bool:
        if self._webrtc is None:
            return self.energy.is_speech(frame_bytes, in_speech)
        return bool(self._webrtc.is_speech(frame_bytes, self.config.sample_rate))


class UtteranceSegmenter:
    def __init__(self, config: VadConfig, vad: VadBackend | None = None) -> None:
        config.validate()
        self.config = config
        self.vad = vad or WebRtcOrEnergyVad(config)
        self._pre_roll: deque[bytes] = deque(maxlen=config.pre_roll_frames)
        self._recording: list[bytes] = []
        self._events: list[VadEvent] = []
        self._state = "idle"
        self._speech_count = 0
        self._silence_count = 0
        self._speech_frames = 0
        self._recording_start_frame = 0

    def feed(self, frame_bytes: bytes, frame_index: int) -> SegmentedUtterance | None:
        if len(frame_bytes) != self.config.frame_bytes:
            raise ValueError(f"expected {self.config.frame_bytes} bytes per VAD frame, got {len(frame_bytes)}")

        voiced = self.vad.is_speech(frame_bytes, in_speech=self._state == "recording")
        at_ms = frame_index * self.config.frame_ms

        if self._state == "idle":
            self._pre_roll.append(frame_bytes)
            self._speech_count = self._speech_count + 1 if voiced else 0
            if self._speech_count < self.config.speech_start_frames:
                return None

            self._state = "recording"
            self._recording = list(self._pre_roll)
            self._events = [
                VadEvent(
                    event="speech_start",
                    frame_index=frame_index,
                    at_ms=max(0, at_ms - (len(self._pre_roll) - 1) * self.config.frame_ms),
                )
            ]
            self._recording_start_frame = max(0, frame_index - len(self._pre_roll) + 1)
            self._silence_count = 0
            self._speech_frames = self._speech_count
            return None

        self._recording.append(frame_bytes)
        if voiced:
            self._silence_count = 0
            self._speech_frames += 1
        else:
            self._silence_count += 1

        recorded_frames = len(self._recording)
        if recorded_frames >= self.config.max_speech_frames:
            return self._finish(frame_index, "max_duration")
        if self._silence_count >= self.config.silence_end_frames:
            return self._finish(frame_index, "silence")
        return None

    def _finish(self, frame_index: int, reason: str) -> SegmentedUtterance:
        duration_ms = len(self._recording) * self.config.frame_ms
        silence_ms = self._silence_count * self.config.frame_ms
        events = list(self._events)
        discarded = self._speech_frames < self.config.min_speech_frames
        event_name = "discarded" if discarded else "speech_end"
        discard_reason = "too_short" if discarded else ""
        events.append(
            VadEvent(
                event=event_name,
                frame_index=frame_index,
                at_ms=(self._recording_start_frame + len(self._recording)) * self.config.frame_ms,
                duration_ms=duration_ms,
                silence_ms=silence_ms,
                reason=discard_reason or reason,
            )
        )
        pcm_bytes = b"" if discarded else b"".join(self._recording)
        self.reset()
        return SegmentedUtterance(
            pcm_bytes=pcm_bytes,
            events=tuple(events),
            discarded=discarded,
            reason=discard_reason,
        )

    def reset(self) -> None:
        self._pre_roll.clear()
        self._recording = []
        self._events = []
        self._state = "idle"
        self._speech_count = 0
        self._silence_count = 0
        self._speech_frames = 0
        self._recording_start_frame = 0


class RealtimeVadRecorder:
    def __init__(
        self,
        config: VadConfig | None = None,
        input_device: int | None = None,
        vad: VadBackend | None = None,
    ) -> None:
        self.config = config or VadConfig()
        self.input_device = input_device
        self.vad = vad

    def listen_once(self, recordings_dir: str | Path) -> VadRecordingResult:
        sd = _sounddevice()
        frames: queue.Queue[bytes] = queue.Queue()
        segmenter = UtteranceSegmenter(self.config, self.vad)
        frame_index = 0

        def callback(indata, _frames, _time, status) -> None:
            if status:
                return
            frames.put(bytes(indata))

        with sd.RawInputStream(
            samplerate=self.config.sample_rate,
            channels=self.config.channels,
            dtype="int16",
            device=self.input_device,
            blocksize=self.config.frame_samples,
            callback=callback,
        ):
            while True:
                frame = frames.get()
                utterance = segmenter.feed(frame, frame_index)
                frame_index += 1
                if utterance is None:
                    continue
                if utterance.discarded:
                    self._write_events(recordings_dir, utterance.events, discarded=True)
                    continue
                return self._save_utterance(recordings_dir, utterance)

    def _save_utterance(self, recordings_dir: str | Path, utterance: SegmentedUtterance) -> VadRecordingResult:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        wav_path = Path(recordings_dir) / f"utterance-{timestamp}.wav"
        write_pcm16_wav(wav_path, utterance.pcm_bytes, self.config.sample_rate, self.config.channels)
        metrics = inspect_wav(wav_path)
        metrics_path = wav_path.with_suffix(".json")
        save_metrics_json(metrics, metrics_path)
        events_path = wav_path.with_suffix(".vad.json")
        events_path.write_text(
            json.dumps([asdict(event) for event in utterance.events], ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
        return VadRecordingResult(
            wav_path=wav_path,
            metrics_path=metrics_path,
            events_path=events_path,
            metrics=metrics,
            events=utterance.events,
        )

    def _write_events(self, recordings_dir: str | Path, events: tuple[VadEvent, ...], discarded: bool) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        suffix = "discarded" if discarded else "events"
        path = Path(recordings_dir) / f"utterance-{timestamp}.{suffix}.vad.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([asdict(event) for event in events], ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
        return path


def _pcm16_rms(frame_bytes: bytes) -> float:
    samples = array("h")
    samples.frombytes(frame_bytes)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return 0.0
    return math.sqrt(sum(int(sample) * int(sample) for sample in samples) / len(samples))


def _sounddevice():
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency 'sounddevice'. Install audio requirements with "
            "'python -m pip install -r runtime/audio/requirements.txt'."
        ) from exc
    return sd
