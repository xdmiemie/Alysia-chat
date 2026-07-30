from __future__ import annotations

import asyncio
import subprocess
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from runtime.adapters.wechat_desktop.client import WeChatDesktopClient
from runtime.adapters.wechat_desktop.config import UIConfig, VoiceConfig
from runtime.adapters.wechat_desktop.models import DesktopMessage
from runtime.adapters.wechat_desktop.process_locator import find_wechat_processes
from runtime.adapters.wechat_desktop.ui_queue import SingleUIQueue, get_global_ui_queue
from runtime.audio.audio_format import WaveMetrics, inspect_wav, save_metrics_json, write_pcm16_wav
from runtime.audio.normalize import NormalizedAudio, normalize_wav_for_sensevoice
from runtime.core.models import UnifiedMessage


@dataclass(frozen=True)
class CapturedAudio:
    path: Path
    method: str
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class VoiceCaptureResult:
    ok: bool
    request_id: str
    contact_key: str
    message_fingerprint: str
    captured_path: Path | None = None
    normalized_path: Path | None = None
    captured_metrics: WaveMetrics | None = None
    normalized_metrics: WaveMetrics | None = None
    capture_method: str = ""
    normalize_method: str = ""
    error: str = ""
    retryable: bool = False
    metadata: dict[str, object] = field(default_factory=dict)

    def to_message(self, source: DesktopMessage, *, character_id: str) -> UnifiedMessage:
        if not self.ok or self.normalized_path is None:
            raise RuntimeError(self.error or "voice capture did not produce normalized audio")
        return UnifiedMessage(
            request_id=self.request_id,
            channel="wechat_desktop",
            session_id=source.session_id,
            sender_id=source.contact_key,
            sender_name=source.contact_display_name,
            message_type="voice",
            audio_path=str(self.normalized_path),
            character_id=character_id,
            metadata={
                "chat_title": source.chat_title,
                "contact_key": source.contact_key,
                "message_fingerprint": self.message_fingerprint,
                "voice_duration_ms": source.voice_duration_ms,
                "capture_method": self.capture_method,
                "captured_path": str(self.captured_path) if self.captured_path else "",
                **self.metadata,
            },
        )

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["captured_path"] = str(self.captured_path) if self.captured_path else ""
        data["normalized_path"] = str(self.normalized_path) if self.normalized_path else ""
        data["captured_metrics"] = asdict(self.captured_metrics) if self.captured_metrics else None
        data["normalized_metrics"] = asdict(self.normalized_metrics) if self.normalized_metrics else None
        return data


class VoiceAudioCaptureBackend(Protocol):
    def capture(
        self,
        *,
        request_id: str,
        output_path: Path,
        expected_duration_ms: int,
        timeout_seconds: float,
        trigger_playback: Callable[[], None],
    ) -> CapturedAudio:
        ...


class ApplicationLoopbackProcessBackend:
    def __init__(self, *, config: VoiceConfig, executable_names: tuple[str, ...]) -> None:
        self.config = config
        self.executable_names = executable_names

    def capture(
        self,
        *,
        request_id: str,
        output_path: Path,
        expected_duration_ms: int,
        timeout_seconds: float,
        trigger_playback: Callable[[], None],
    ) -> CapturedAudio:
        helper = self.config.application_loopback_helper_path
        if not helper.exists():
            raise RuntimeError(
                f"Application Loopback helper not found: {helper}. "
                "Build it with scripts/build_application_loopback_capture.ps1."
            )
        process = _select_wechat_process(self.executable_names)
        seconds = (
            min(timeout_seconds, max(1.0, expected_duration_ms / 1000.0 + 1.0))
            if expected_duration_ms > 0
            else timeout_seconds
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            str(helper),
            "--pid",
            str(process.pid),
            "--out",
            str(output_path),
            "--seconds",
            f"{seconds:.3f}",
            "--sample-rate",
            "48000",
            "--channels",
            "2",
        ]
        started = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _wait_for_ready(started, timeout_seconds=5.0)
            trigger_playback()
            stdout, stderr = started.communicate(timeout=seconds + 5.0)
        except Exception:
            _terminate_process(started)
            raise
        if started.returncode != 0:
            detail = (stderr or stdout or "").strip()
            raise RuntimeError(f"Application Loopback helper failed: {detail}")
        return CapturedAudio(
            path=output_path,
            method="application_loopback",
            metadata={"pid": process.pid, "process_name": process.name, "helper_stdout": stdout.strip()},
        )


class TimedSoundDeviceLoopbackBackend:
    """Device-level loopback fallback for a dedicated WeChat playback device."""

    def __init__(self, *, input_device: int | None = None, sample_rate: int = 48000, channels: int = 2) -> None:
        self.input_device = input_device
        self.sample_rate = sample_rate
        self.channels = channels

    def capture(
        self,
        *,
        request_id: str,
        output_path: Path,
        expected_duration_ms: int,
        timeout_seconds: float,
        trigger_playback: Callable[[], None],
    ) -> CapturedAudio:
        sd = _sounddevice()
        np = _numpy()
        frames: list[object] = []
        statuses: list[str] = []
        capture_seconds = (
            min(timeout_seconds, max(0.8, expected_duration_ms / 1000.0 + 0.8))
            if expected_duration_ms > 0
            else timeout_seconds
        )

        def callback(indata, _frames, _time, status) -> None:
            if status:
                statuses.append(str(status))
            frames.append(indata.copy())

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="int16",
            device=self.input_device,
            callback=callback,
        ):
            time.sleep(0.12)
            trigger_playback()
            time.sleep(capture_seconds)

        pcm_bytes = b"" if not frames else np.concatenate(frames, axis=0).astype("int16").tobytes()
        write_pcm16_wav(output_path, pcm_bytes, self.sample_rate, self.channels)
        return CapturedAudio(
            path=output_path,
            method="wasapi_loopback",
            metadata={"request_id": request_id, "statuses": statuses},
        )


class WeChatVoiceCaptureService:
    def __init__(
        self,
        *,
        client: WeChatDesktopClient,
        voice: VoiceConfig,
        ui: UIConfig,
        queue: SingleUIQueue | None = None,
        backend: VoiceAudioCaptureBackend | None = None,
        executable_names: tuple[str, ...] = ("Weixin.exe", "WeChat.exe", "WeChatAppEx.exe", "wechat_starter_v1.0.0.exe"),
    ) -> None:
        self.client = client
        self.voice = voice
        self.ui = ui
        self.queue = queue
        self.backend = backend or _backend_from_config(voice, executable_names=executable_names)
        self._audio_lock = asyncio.Lock()
        self._contact_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def capture(self, message: DesktopMessage, request_id: str) -> VoiceCaptureResult:
        async with self._audio_lock:
            async with self._contact_locks[message.contact_key]:
                queue = self.queue or get_global_ui_queue()
                return await queue.run(
                    lambda: self._capture_locked(message, request_id),
                    request_id=request_id,
                    contact_key=message.contact_key,
                    priority=60,
                    timeout_seconds=self.voice.capture_timeout_seconds + self.ui.operation_timeout_seconds,
                )

    def _capture_locked(self, message: DesktopMessage, request_id: str) -> VoiceCaptureResult:
        if message.message_type != "voice":
            return _failed(message, request_id, "message is not a voice bubble", retryable=False)
        if not message.is_bound_to_ui:
            return _failed(message, request_id, "voice bubble is missing a stable UI binding", retryable=False)
        if not self.voice.enabled:
            return _failed(message, request_id, "wechat voice capture is disabled", retryable=False)

        captured_path = self.voice.captured_dir / f"{request_id}.wav"
        normalized_path = self.voice.normalized_dir / f"{request_id}.wav"
        last_error = ""
        for attempt in range(1, self.voice.max_capture_retries + 2):
            try:
                matched = self._relocate_voice_message(message)
                captured = self.backend.capture(
                    request_id=request_id,
                    output_path=captured_path,
                    expected_duration_ms=_expected_duration_ms(matched),
                    timeout_seconds=self.voice.capture_timeout_seconds,
                    trigger_playback=lambda: self._play_voice_message(matched),
                )
                self._verify_current_contact(message)
                captured_metrics = inspect_wav(captured.path)
                self._validate_captured_audio(captured_metrics, message)
                save_metrics_json(captured_metrics, captured.path.with_suffix(".json"))
                normalized = normalize_wav_for_sensevoice(
                    captured.path,
                    normalized_path,
                    ffmpeg_path=self.voice.ffmpeg_path if self.voice.ffmpeg_path.exists() else None,
                )
                self._validate_normalized_audio(normalized)
                if self.voice.delete_captured_after_success:
                    _delete_if_exists(captured.path)
                    _delete_if_exists(captured.path.with_suffix(".json"))
                return VoiceCaptureResult(
                    ok=True,
                    request_id=request_id,
                    contact_key=message.contact_key,
                    message_fingerprint=message.fingerprint,
                    captured_path=captured.path,
                    normalized_path=normalized.output_path,
                    captured_metrics=captured_metrics,
                    normalized_metrics=normalized.metrics,
                    capture_method=captured.method,
                    normalize_method=normalized.method,
                    metadata={
                        "capture_attempts": attempt,
                        "voice_runtime_id": matched.runtime_id,
                        "voice_control_path": matched.control_path,
                    },
                )
            except Exception as exc:
                last_error = str(exc)
                self._stop_any_playback()
        self._move_failed_capture(captured_path, request_id)
        return _failed(message, request_id, last_error or "voice capture failed", retryable=True)

    def _relocate_voice_message(self, message: DesktopMessage) -> DesktopMessage:
        self.client.open_chat(message.contact_key)
        self._verify_current_contact(message)
        for snapshot in reversed(self.client.recent_messages(limit=self.voice.relocation_scan_limit)):
            candidate = DesktopMessage(
                account_key=message.account_key,
                contact_key=message.contact_key,
                contact_display_name=message.contact_display_name,
                chat_title=message.chat_title,
                direction=snapshot.direction,
                message_type=snapshot.message_type,
                text=snapshot.text,
                runtime_id=snapshot.runtime_id,
                control_path=snapshot.control_path,
                relative_order=snapshot.relative_order,
                time_label=snapshot.time_label,
                raw_fingerprint=str(snapshot.metadata.get("fingerprint", "")),
                metadata={**snapshot.metadata, "session_last_text": message.metadata.get("session_last_text", "")},
            )
            if _same_voice_bubble(message, candidate):
                return candidate
        raise RuntimeError("could not relocate the original voice bubble")

    def _play_voice_message(self, message: DesktopMessage) -> None:
        play = getattr(self.client, "play_voice_message", None)
        if not callable(play):
            raise RuntimeError("wechat client does not support voice bubble playback")
        play(message)

    def _verify_current_contact(self, message: DesktopMessage) -> None:
        if self.ui.verify_chat_title_before_send and not self.client.current_chat_title():
            raise RuntimeError("could not verify current WeChat chat before voice capture")

    def _validate_captured_audio(self, metrics: WaveMetrics, message: DesktopMessage) -> None:
        duration_ms = int(metrics.duration_seconds * 1000)
        if duration_ms < self.voice.min_capture_ms:
            raise RuntimeError(f"captured audio is too short: {duration_ms} ms")
        if metrics.peak_normalized <= self.voice.silence_peak_threshold:
            raise RuntimeError("captured audio is silent")
        expected = message.voice_duration_ms
        if expected > 0 and duration_ms < max(self.voice.min_capture_ms, expected - self.voice.duration_tolerance_ms):
            raise RuntimeError(f"captured audio ended early: {duration_ms} ms for expected {expected} ms")
        if metrics.clipped:
            raise RuntimeError("captured audio is clipped")

    def _validate_normalized_audio(self, normalized: NormalizedAudio) -> None:
        metrics = normalized.metrics
        if metrics.sample_rate != 16000 or metrics.channels != 1 or metrics.sample_width_bits != 16:
            raise RuntimeError("normalized WAV is not 16 kHz mono PCM16")

    def _stop_any_playback(self) -> None:
        stop = getattr(self.client, "stop_voice_playback", None)
        if callable(stop):
            try:
                stop()
            except Exception:
                pass

    def _move_failed_capture(self, captured_path: Path, request_id: str) -> None:
        if not captured_path.exists():
            return
        self.voice.failed_dir.mkdir(parents=True, exist_ok=True)
        target = self.voice.failed_dir / f"{request_id}.wav"
        captured_path.replace(target)


def _same_voice_bubble(original: DesktopMessage, candidate: DesktopMessage) -> bool:
    if candidate.direction != "incoming" or candidate.message_type != "voice":
        return False
    if original.raw_fingerprint and candidate.raw_fingerprint:
        return original.raw_fingerprint == candidate.raw_fingerprint
    if original.runtime_id and candidate.runtime_id:
        return original.runtime_id == candidate.runtime_id
    if original.control_path and candidate.control_path:
        return original.control_path == candidate.control_path
    return bool(original.fingerprint and original.fingerprint == candidate.fingerprint)


def _expected_duration_ms(message: DesktopMessage) -> int:
    return max(0, message.voice_duration_ms)


def _failed(message: DesktopMessage, request_id: str, error: str, *, retryable: bool) -> VoiceCaptureResult:
    return VoiceCaptureResult(
        ok=False,
        request_id=request_id,
        contact_key=message.contact_key,
        message_fingerprint=message.fingerprint,
        error=error,
        retryable=retryable,
    )


def _backend_from_config(config: VoiceConfig, *, executable_names: tuple[str, ...]) -> VoiceAudioCaptureBackend:
    if config.capture_backend == "wasapi_loopback":
        return TimedSoundDeviceLoopbackBackend(input_device=config.loopback_input_device)
    return ApplicationLoopbackProcessBackend(
        config=config,
        executable_names=executable_names,
    )


def _select_wechat_process(executable_names: tuple[str, ...]):
    processes = find_wechat_processes(executable_names)
    if not processes:
        raise RuntimeError("No WeChat process was found for Application Loopback capture")
    preferred = {"weixin.exe", "wechat.exe", "weixin", "wechat"}
    for process in processes:
        if process.name.strip().lower() in preferred:
            return process
    return processes[0]


def _wait_for_ready(process: subprocess.Popen, *, timeout_seconds: float) -> None:
    if process.stdout is None:
        raise RuntimeError("Application Loopback helper stdout was not captured")
    line = _readline_with_timeout(process.stdout, timeout_seconds=timeout_seconds)
    if line.startswith("READY"):
        return
    if process.poll() is not None:
        stderr = process.stderr.read() if process.stderr is not None else ""
        raise RuntimeError(f"Application Loopback helper exited before ready: {stderr.strip()}")
    raise RuntimeError(f"Application Loopback helper did not become ready: {line.strip()}")


def _readline_with_timeout(stream, *, timeout_seconds: float) -> str:
    result: list[str] = []

    def read_line() -> None:
        result.append(stream.readline())

    thread = threading.Thread(target=read_line, daemon=True)
    thread.start()
    thread.join(timeout_seconds)
    if not result:
        raise RuntimeError("Timed out waiting for Application Loopback helper readiness")
    return result[0]


def _terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()


def _delete_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _sounddevice():
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise RuntimeError("Missing dependency 'sounddevice' for WASAPI loopback capture.") from exc
    return sd


def _numpy():
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("Missing dependency 'numpy' for WASAPI loopback capture.") from exc
    return np
