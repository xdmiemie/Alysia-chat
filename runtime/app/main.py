from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

from runtime.app.orchestrator import RealtimeDialogueRunner, default_stage6_output_dir
from runtime.audio.devices import list_devices
from runtime.audio.devices import select_input_device, select_output_device
from runtime.audio.player import play_wav
from runtime.audio.vad_recorder import RealtimeVadRecorder, VadConfig
from runtime.common.config import load_audio_config
from runtime.gpt_sovits.config import load_gpt_sovits_config
from runtime.gpt_sovits.references import validate_model_manifest
from runtime.gpt_sovits.service import GPTSoVITSSynthesizer
from runtime.qwen3.config import load_qwen3_config
from runtime.qwen3.service import Qwen3ChatSession
from runtime.sensevoice.backend import resolve_model_path
from runtime.sensevoice.config import load_sensevoice_config
from runtime.sensevoice.service import SenseVoiceRecognizer


class SoundDeviceSpeaker:
    def __init__(self, output_device: int | None = None) -> None:
        self.output_device = output_device

    def play(self, audio_path: str | Path) -> None:
        play_wav(audio_path, output_device=self.output_device)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 6 realtime microphone dialogue app.")
    parser.add_argument("--character-id", "--character", default="elysia")
    parser.add_argument("--audio-config", default="configs/runtime/audio.yaml")
    parser.add_argument("--sensevoice-config", default="configs/runtime/sensevoice.yaml")
    parser.add_argument("--qwen3-config", default="configs/runtime/qwen3.yaml")
    parser.add_argument("--gpt-sovits-config", default="configs/runtime/gpt_sovits.yaml")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--recordings-dir", default=None)
    parser.add_argument("--max-turns", type=int, default=None, help="Stop after N turns. Default: run until Ctrl+C.")
    parser.add_argument("--skip-health", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print each turn as JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_turns is not None and args.max_turns < 1:
        print("Error: --max-turns must be at least 1.", file=sys.stderr)
        return 2

    try:
        audio = load_audio_config(args.audio_config)
        output_dir = Path(args.output_dir) if args.output_dir else default_stage6_output_dir()
        recordings_dir = Path(args.recordings_dir) if args.recordings_dir else audio.recordings_dir / "stage6"

        if not args.skip_health:
            health_errors = check_stage6_health(
                audio_config_path=args.audio_config,
                sensevoice_config=args.sensevoice_config,
                qwen3_config=args.qwen3_config,
                gpt_sovits_config=args.gpt_sovits_config,
            )
            if health_errors:
                for error in health_errors:
                    print(f"Health check failed: {error}", file=sys.stderr)
                return 1

        runner = RealtimeDialogueRunner(
            transcriber=SenseVoiceRecognizer.from_config(args.sensevoice_config),
            chat_session=Qwen3ChatSession.from_config(args.qwen3_config),
            synthesizer=GPTSoVITSSynthesizer.from_config(args.gpt_sovits_config),
            recorder=RealtimeVadRecorder(
                config=VadConfig(sample_rate=audio.sample_rate, channels=audio.channels),
                input_device=audio.input_device,
            ),
            speaker=SoundDeviceSpeaker(output_device=audio.output_device),
            recordings_dir=recordings_dir,
            output_dir=output_dir,
            character_id=args.character_id,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    index = 1
    print("Stage 6 realtime dialogue is ready. Speak after the prompt; press Ctrl+C to stop.")
    try:
        while args.max_turns is None or index <= args.max_turns:
            print(f"[{index:02d}] Listening...")
            result = runner.run_one(index)
            runner.append_manifest(result)
            _print_result(result, json_output=args.json)
            if _is_fatal_turn_setup_error(result):
                print("Fatal realtime setup error; stopping instead of retrying forever.", file=sys.stderr)
                return 1
            index += 1
    except KeyboardInterrupt:
        print("\nInterrupted. Runtime app stopped.")
        return 0

    print(f"Manifest: {output_dir / 'manifest.jsonl'}")
    return 0


def check_stage6_health(
    audio_config_path: str | Path,
    sensevoice_config: str | Path,
    qwen3_config: str | Path,
    gpt_sovits_config: str | Path,
) -> list[str]:
    errors: list[str] = []

    try:
        audio = load_audio_config(audio_config_path)
        _validate_audio_devices(audio.input_device, audio.output_device)
    except Exception as exc:
        errors.append(f"audio devices are not ready: {exc}")

    try:
        sensevoice = load_sensevoice_config(sensevoice_config)
        resolve_model_path(sensevoice.model_manifest)
    except Exception as exc:
        errors.append(f"SenseVoice model is not ready: {exc}")

    try:
        qwen3 = load_qwen3_config(qwen3_config)
        _validate_manifest_files(qwen3.model_manifest, "Qwen3")
        _check_tcp_port(qwen3.host, qwen3.port, "Qwen3 llama-server")
    except Exception as exc:
        errors.append(str(exc))

    try:
        gpt_sovits = load_gpt_sovits_config(gpt_sovits_config)
        validate_model_manifest(gpt_sovits.model_manifest)
        parsed = urlparse(gpt_sovits.upstream_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        _check_tcp_port(host, port, "GPT-SoVITS upstream")
    except Exception as exc:
        errors.append(str(exc))

    return errors


def _validate_audio_devices(input_device: int | None, output_device: int | None) -> None:
    devices = list_devices()
    select_input_device(input_device, devices)
    select_output_device(output_device, devices)


def _validate_manifest_files(manifest_path: str | Path, label: str) -> None:
    manifest = Path(manifest_path)
    if not manifest.exists():
        raise RuntimeError(f"{label} model manifest not found: {manifest}")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    required_files = data.get("required_files", [])
    if not isinstance(required_files, list) or not required_files:
        raise RuntimeError(f"{label} manifest has no required_files: {manifest}")
    missing = [Path(str(path)) for path in required_files if not Path(str(path)).exists()]
    if missing:
        raise RuntimeError(f"{label} required file(s) missing: {'; '.join(str(path) for path in missing)}")


def _check_tcp_port(host: str, port: int, label: str, timeout: float = 2.0) -> None:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return
    except OSError as exc:
        raise RuntimeError(f"{label} is not reachable at {host}:{port}: {exc}") from exc


def _print_result(result, json_output: bool) -> None:
    if json_output:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
        return

    text = ""
    if result.transcription:
        text = str(result.transcription.get("text", ""))
    status = "OK" if result.ok else "WARN"
    print(f"[{status}] input={result.input_audio} text={text!r} output={result.output_audio}")
    if result.error:
        print(f"  error={result.error}")


def _is_fatal_turn_setup_error(result) -> bool:
    return bool(result.error and not result.input_audio and not result.output_audio)


if __name__ == "__main__":
    raise SystemExit(main())
