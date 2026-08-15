from __future__ import annotations

import audioop
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from runtime.adapters.wechat_desktop.config import CallsConfig, VoiceConfig
from runtime.adapters.wechat_desktop.process_locator import find_wechat_processes
from runtime.audio.audio_format import inspect_wav, write_pcm16_wav
from runtime.audio.devices import select_output_device_by_name
from runtime.audio.player import play_wav
from runtime.audio.vad_recorder import UtteranceSegmenter, VadConfig


@dataclass(frozen=True)
class CallAudioInput:
    wav_path: Path
    duration_ms: int


class ApplicationLoopbackVadCallRecorder:
    """Capture one caller turn directly from the WeChat process output.

    The Stage 13 process-loopback helper writes a growing 48 kHz stereo PCM
    WAV. This recorder tails its payload, converts it to 16 kHz mono, and feeds
    the Stage 6 VAD. A fresh helper is opened for every listening phase, so no
    caller audio is captured while the AI is synthesizing or speaking.
    """

    def __init__(
        self,
        *,
        voice: VoiceConfig,
        calls: CallsConfig,
        executable_names: tuple[str, ...],
        event_sink: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        self.voice = voice
        self.calls = calls
        self.executable_names = executable_names
        self.event_sink = event_sink or (lambda _event, _fields: None)
        self._stop = threading.Event()
        self._process_lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None

    def listen_once(self, recordings_dir: str | Path) -> CallAudioInput | None:
        if self._stop.is_set():
            return None
        helper = self.voice.application_loopback_helper_path
        if not helper.exists():
            raise RuntimeError(
                f"Application Loopback helper not found: {helper}. "
                "Build it with scripts/build_application_loopback_capture.ps1."
            )
        process_info = _select_wechat_process(self.executable_names)
        root = Path(recordings_dir)
        root.mkdir(parents=True, exist_ok=True)
        capture_path = root / f"loopback-{uuid4().hex[:10]}.wav"
        output_path = root / f"utterance-{uuid4().hex[:10]}.wav"
        command = [
            str(helper), "--pid", str(process_info.pid), "--out", str(capture_path),
            "--seconds", f"{self.calls.listen_timeout_seconds:.3f}",
            "--sample-rate", "48000", "--channels", "2",
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        with self._process_lock:
            self._process = process
        try:
            _wait_ready(process, timeout_seconds=5.0)
            self.event_sink(
                "call_audio_helper_ready",
                {"pid": process_info.pid, "process_name": process_info.name},
            )
            utterance = self._tail_until_utterance(capture_path, process)
            if utterance is None:
                self.event_sink(
                    "call_audio_capture_timeout",
                    {"timeout_seconds": self.calls.listen_timeout_seconds},
                )
                return None
            write_pcm16_wav(output_path, utterance, 16000, 1)
            metrics = inspect_wav(output_path)
            self.event_sink(
                "call_audio_segment_ready",
                {
                    "duration_ms": int(metrics.duration_seconds * 1000),
                    "peak_normalized": metrics.peak_normalized,
                    "sample_rate": metrics.sample_rate,
                    "channels": metrics.channels,
                },
            )
            return CallAudioInput(output_path, int(metrics.duration_seconds * 1000))
        except Exception as exc:
            self.event_sink("call_audio_capture_failed", {"error": str(exc)})
            raise
        finally:
            _stop_process(process)
            with self._process_lock:
                if self._process is process:
                    self._process = None
            try:
                capture_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _tail_until_utterance(self, path: Path, process: subprocess.Popen[str]) -> bytes | None:
        config = VadConfig(
            frame_ms=self.calls.vad_frame_ms,
            pre_roll_ms=self.calls.vad_pre_roll_ms,
            speech_start_frames=self.calls.vad_speech_start_frames,
            silence_end_ms=self.calls.vad_silence_end_ms,
            min_speech_ms=self.calls.vad_min_speech_ms,
            max_speech_ms=self.calls.vad_max_speech_ms,
        )
        segmenter = UtteranceSegmenter(config)
        pending = bytearray()
        source_offset = 44
        rate_state = None
        frame_index = 0
        deadline = time.monotonic() + self.calls.listen_timeout_seconds
        while not self._stop.is_set() and time.monotonic() < deadline:
            if path.exists():
                size = path.stat().st_size
                readable = size - source_offset
                readable -= readable % 4
                if readable > 0:
                    with path.open("rb") as stream:
                        stream.seek(source_offset)
                        stereo = stream.read(readable)
                    source_offset += len(stereo)
                    mono = audioop.tomono(stereo, 2, 0.5, 0.5)
                    converted, rate_state = audioop.ratecv(mono, 2, 1, 48000, 16000, rate_state)
                    pending.extend(converted)
                    while len(pending) >= config.frame_bytes:
                        frame = bytes(pending[: config.frame_bytes])
                        del pending[: config.frame_bytes]
                        result = segmenter.feed(frame, frame_index)
                        frame_index += 1
                        if result is not None and not result.discarded:
                            return result.pcm_bytes
            if process.poll() is not None:
                detail = _process_detail(process)
                if process.returncode not in (0, None):
                    raise RuntimeError(f"Application Loopback helper failed: {detail}")
                break
            time.sleep(0.02)
        return None

    def stop(self) -> None:
        self._stop.set()
        with self._process_lock:
            process = self._process
        if process is not None:
            _stop_process(process)


class DefaultDeviceCallSpeaker:
    """Play TTS to a stable named endpoint, explicit index, or OS default."""

    def __init__(self, output_device: int | None = None, output_device_name: str = "") -> None:
        self.output_device = output_device
        self.output_device_name = output_device_name
        self.event_sink: Callable[[str, dict[str, object]], None] = lambda _event, _fields: None
        self._stopped = False

    def set_event_sink(
        self,
        event_sink: Callable[[str, dict[str, object]], None] | None,
    ) -> "DefaultDeviceCallSpeaker":
        self.event_sink = event_sink or (lambda _event, _fields: None)
        return self

    def play(self, audio_path: str | Path) -> None:
        if not self._stopped:
            output_device = self.output_device
            selected = None
            if self.output_device_name.strip():
                selected = select_output_device_by_name(self.output_device_name)
                output_device = selected.index
            self.event_sink(
                "call_audio_playback_started",
                {
                    "audio_path": str(audio_path),
                    "output_device_index": output_device,
                    "output_device_name": selected.name if selected is not None else "",
                },
            )
            try:
                playback = play_wav(audio_path, output_device=output_device)
            except Exception as exc:
                self.event_sink(
                    "call_audio_playback_failed",
                    {
                        "audio_path": str(audio_path),
                        "output_device_index": output_device,
                        "output_device_name": selected.name if selected is not None else "",
                        "error": str(exc),
                    },
                )
                raise
            self.event_sink(
                "call_audio_playback_completed",
                {
                    "audio_path": str(audio_path),
                    "output_device_index": playback.output_device_index,
                    "output_device_name": playback.output_device_name,
                    "source_sample_rate": playback.source_sample_rate,
                    "playback_sample_rate": playback.playback_sample_rate,
                    "channels": playback.channels,
                },
            )

    def stop(self) -> None:
        self._stopped = True
        try:
            from runtime.audio.player import _sounddevice

            _sounddevice().stop()
        except Exception:
            pass


def _select_wechat_process(executable_names: tuple[str, ...]):
    processes = find_wechat_processes(executable_names)
    if not processes:
        raise RuntimeError("No WeChat process was found for Application Loopback capture")
    preferred = {"weixin.exe", "wechat.exe", "weixin", "wechat"}
    return next((item for item in processes if item.name.strip().lower() in preferred), processes[0])


def _wait_ready(process: subprocess.Popen[str], *, timeout_seconds: float) -> None:
    if process.stdout is None:
        raise RuntimeError("Application Loopback helper stdout was not captured")
    rows: queue.Queue[str] = queue.Queue(maxsize=1)
    thread = threading.Thread(target=lambda: rows.put(process.stdout.readline()), daemon=True)
    thread.start()
    try:
        line = rows.get(timeout=timeout_seconds)
    except queue.Empty as exc:
        raise RuntimeError("Timed out waiting for Application Loopback helper readiness") from exc
    if not line.startswith("READY"):
        raise RuntimeError(f"Application Loopback helper did not become ready: {line.strip()}")


def _process_detail(process: subprocess.Popen[str]) -> str:
    if process.stderr is None:
        return ""
    try:
        return process.stderr.read().strip()
    except Exception:
        return ""


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
