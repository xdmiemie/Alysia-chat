from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import subprocess
from array import array
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.adapters.wechat_desktop.config import VoiceConfig, load_wechat_desktop_config
from runtime.adapters.wechat_desktop.process_locator import find_wechat_processes
from runtime.audio.audio_format import WaveMetrics, inspect_wav, save_metrics_json, write_pcm16_wav
from runtime.audio.normalize import normalize_wav_for_sensevoice
from runtime.core.models import new_request_id
from runtime.sensevoice.service import SenseVoiceRecognizer


@dataclass(frozen=True)
class ManualAcceptanceResult:
    ok: bool
    request_id: str
    capture_method: str
    captured_path: str
    normalized_path: str
    captured_metrics: dict[str, object]
    normalized_metrics: dict[str, object]
    transcription: dict[str, object] | None
    report_path: str
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stage 13 manual WeChat voice capture acceptance. Start capture, manually play one WeChat voice bubble, then transcribe it.",
    )
    parser.add_argument("--config", default="configs/runtime/wechat.yaml")
    parser.add_argument("--sensevoice-config", default="configs/runtime/sensevoice.yaml")
    parser.add_argument("--request-id", default="")
    parser.add_argument("--capture-seconds", type=float, default=12.0)
    parser.add_argument("--captured-wav", default="", help="Use an existing captured WAV instead of opening a live capture stream.")
    parser.add_argument("--skip-transcribe", action="store_true", help="Only capture/normalize/inspect the WAV.")
    parser.add_argument("--json", action="store_true")
    return parser


def run_acceptance(args: argparse.Namespace) -> ManualAcceptanceResult:
    config = load_wechat_desktop_config(args.config)
    request_id = args.request_id.strip() or new_request_id()
    voice = config.voice
    captured_path = Path(args.captured_wav) if args.captured_wav else voice.captured_dir / f"manual-{request_id}.wav"
    normalized_path = voice.normalized_dir / f"manual-{request_id}.wav"
    report_path = voice.normalized_dir / f"manual-{request_id}.json"

    try:
        if args.captured_wav:
            capture_method = "provided_wav"
            if not captured_path.exists():
                raise FileNotFoundError(f"captured WAV not found: {captured_path}")
        else:
            capture_method = _capture_manual_wechat_audio(
                voice=voice,
                config=config,
                output_path=captured_path,
                capture_seconds=float(args.capture_seconds),
            )

        captured_metrics = inspect_wav(captured_path)
        _validate_captured_audio(captured_metrics, voice)
        save_metrics_json(captured_metrics, captured_path.with_suffix(".json"))

        normalized = normalize_wav_for_sensevoice(
            captured_path,
            normalized_path,
            ffmpeg_path=voice.ffmpeg_path if voice.ffmpeg_path.exists() else None,
        )
        transcription = None
        ok = True
        error = ""
        if not args.skip_transcribe:
            transcription_result = SenseVoiceRecognizer.from_config(args.sensevoice_config).transcribe(
                normalized.output_path
            )
            transcription = transcription_result.to_dict()
            ok = not transcription_result.skipped and bool(transcription_result.text.strip())
            error = transcription_result.reason if not ok else ""

        result = ManualAcceptanceResult(
            ok=ok,
            request_id=request_id,
            capture_method=capture_method,
            captured_path=str(captured_path),
            normalized_path=str(normalized.output_path),
            captured_metrics=asdict(captured_metrics),
            normalized_metrics=asdict(normalized.metrics),
            transcription=transcription,
            report_path=str(report_path),
            error=error,
        )
    except Exception as exc:
        empty_metrics: dict[str, object] = {}
        result = ManualAcceptanceResult(
            ok=False,
            request_id=request_id,
            capture_method="",
            captured_path=str(captured_path),
            normalized_path=str(normalized_path),
            captured_metrics=empty_metrics,
            normalized_metrics=empty_metrics,
            transcription=None,
            report_path=str(report_path),
            error=str(exc),
        )

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _capture_manual_wechat_audio(
    *,
    voice: VoiceConfig,
    config,
    output_path: Path,
    capture_seconds: float,
) -> str:
    if voice.capture_backend == "application_loopback":
        return _capture_manual_application_loopback(
            voice=voice,
            config=config,
            output_path=output_path,
            capture_seconds=capture_seconds,
        )
    if voice.capture_backend != "wasapi_loopback":
        raise RuntimeError(f"Unsupported voice.capture_backend for manual acceptance: {voice.capture_backend}")

    if capture_seconds <= 0:
        raise ValueError("--capture-seconds must be positive")

    sd = _sounddevice()
    np = _numpy()
    frames: list[Any] = []
    statuses: list[str] = []
    stop_event = threading.Event()

    def callback(indata: Any, _frames: int, _time: Any, status: Any) -> None:
        if status:
            statuses.append(str(status))
        frames.append(indata.copy())

    def wait_for_enter() -> None:
        try:
            input("捕获已开始。现在手动播放一条微信语音，播放结束后按 Enter 停止；或等待超时自动停止。")
        finally:
            stop_event.set()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"WeChat manual voice capture output: {output_path}", flush=True)
    print(f"Capture timeout: {capture_seconds:.1f}s", flush=True)
    with sd.InputStream(
        samplerate=48000,
        channels=2,
        dtype="int16",
        device=voice.loopback_input_device,
        callback=callback,
    ):
        threading.Thread(target=wait_for_enter, daemon=True).start()
        deadline = time.monotonic() + capture_seconds
        while time.monotonic() < deadline and not stop_event.is_set():
            time.sleep(0.05)

    pcm_bytes = b"" if not frames else np.concatenate(frames, axis=0).astype("int16").tobytes()
    write_pcm16_wav(output_path, pcm_bytes, 48000, 2)
    if statuses:
        (output_path.with_suffix(".status.json")).write_text(
            json.dumps({"statuses": statuses}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return "wasapi_loopback_manual"


def _capture_manual_application_loopback(
    *,
    voice: VoiceConfig,
    config,
    output_path: Path,
    capture_seconds: float,
) -> str:
    helper = voice.application_loopback_helper_path
    if not helper.exists():
        raise RuntimeError(
            f"Application Loopback helper not found: {helper}. "
            "Build it with scripts/build_application_loopback_capture.ps1."
        )
    processes = find_wechat_processes(config.client.executable_names)
    if not processes:
        raise RuntimeError("No WeChat process was found for Application Loopback capture")
    process = _select_wechat_process(processes)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(helper),
        "--pid",
        str(process.pid),
        "--out",
        str(output_path),
        "--seconds",
        f"{capture_seconds:.3f}",
        "--sample-rate",
        "48000",
        "--channels",
        "2",
    ]
    print(f"WeChat process loopback target: {process.name}:{process.pid}", flush=True)
    print(f"WeChat manual voice capture output: {output_path}", flush=True)
    started = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_ready(started, timeout_seconds=5.0)
        print("捕获已开始。现在手动播放一条微信语音，等待超时自动停止。", flush=True)
        stdout, stderr = started.communicate(timeout=capture_seconds + 5.0)
    except Exception:
        _terminate_process(started)
        raise
    if started.returncode != 0:
        detail = (stderr or stdout or "").strip()
        raise RuntimeError(f"Application Loopback helper failed: {detail}")
    return "application_loopback_manual"


def _select_wechat_process(processes):
    preferred = {"weixin.exe", "wechat.exe", "weixin", "wechat"}
    for process in processes:
        if process.name.strip().lower() in preferred:
            return process
    return processes[0]


def _wait_for_ready(process: subprocess.Popen, *, timeout_seconds: float) -> None:
    if process.stdout is None:
        raise RuntimeError("Application Loopback helper stdout was not captured")
    ready_line = _readline_with_timeout(process.stdout, timeout_seconds=timeout_seconds)
    if ready_line.startswith("READY"):
        return
    stderr = process.stderr.read() if process.stderr is not None and process.poll() is not None else ""
    raise RuntimeError(f"Application Loopback helper did not become ready: {ready_line.strip()} {stderr.strip()}")


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


def _validate_captured_audio(metrics: WaveMetrics, voice: VoiceConfig) -> None:
    duration_ms = int(metrics.duration_seconds * 1000)
    if duration_ms < voice.min_capture_ms:
        raise RuntimeError(f"captured audio is too short: {duration_ms} ms")
    if metrics.peak_normalized <= voice.silence_peak_threshold:
        raise RuntimeError("captured audio is silent")
    if metrics.clipped:
        raise RuntimeError("captured audio is clipped")


def _sounddevice():
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise RuntimeError("Missing dependency 'sounddevice' for manual WASAPI loopback capture.") from exc
    return sd


def _numpy():
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("Missing dependency 'numpy' for manual WASAPI loopback capture.") from exc
    return np


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result = run_acceptance(args)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False), flush=True)
    else:
        _print_human(result)
    return 0 if result.ok else 1


def _print_human(result: ManualAcceptanceResult) -> None:
    print(f"ok={result.ok}")
    print(f"request_id={result.request_id}")
    if result.error:
        print(f"error={result.error}", file=sys.stderr)
    print(f"captured={result.captured_path}")
    print(f"normalized={result.normalized_path}")
    if result.normalized_metrics:
        print(
            "normalized_format="
            f"{result.normalized_metrics.get('sample_rate')}Hz/"
            f"{result.normalized_metrics.get('channels')}ch/"
            f"{result.normalized_metrics.get('sample_width_bits')}bit"
        )
    if result.transcription:
        print(f"text={result.transcription.get('text', '')}")
        print(f"language={result.transcription.get('language', '')}")
        print(f"event={result.transcription.get('event', '')}")
    print(f"report={result.report_path}")


if __name__ == "__main__":
    raise SystemExit(main())
