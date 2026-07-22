from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
import sys

from runtime.audio.devices import print_devices
from runtime.audio.player import play_wav
from runtime.audio.recorder import ButtonRecorder
from runtime.common.config import load_audio_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 1 microphone-to-speaker loopback.")
    parser.add_argument("--config", default="configs/runtime/audio.yaml")
    parser.add_argument("--list-devices", action="store_true", help="Print input/output device indexes.")
    parser.add_argument("--input-device", type=int, default=None, help="Override configured microphone index.")
    parser.add_argument("--output-device", type=int, default=None, help="Override configured speaker index.")
    parser.add_argument("--no-playback", action="store_true", help="Record and save without auto playback.")
    return parser


def run_loopback(args: argparse.Namespace) -> int:
    config = load_audio_config(args.config)
    input_device = args.input_device if args.input_device is not None else config.input_device
    output_device = args.output_device if args.output_device is not None else config.output_device
    play_after_recording = config.play_after_recording and not args.no_playback

    print("Stage 1 audio loopback")
    print(f"Format: {config.sample_rate} Hz, {config.sample_width_bits}-bit, mono PCM/WAV")
    print(f"Input device: {input_device if input_device is not None else 'system default'}")
    print(f"Output device: {output_device if output_device is not None else 'system default'}")
    print("")

    input("Press Enter to start recording...")
    recorder = ButtonRecorder(
        sample_rate=config.sample_rate,
        channels=config.channels,
        input_device=input_device,
    )
    recorder.start()
    print("Recording. Speak into the microphone, then press Enter to stop.")
    input()
    result = recorder.stop_and_save(config.recordings_dir)

    print("")
    print("Recording saved.")
    print_metrics(result.metrics, result.metrics_path)

    if play_after_recording:
        print("")
        print("Playing original recording...")
        play_wav(result.wav_path, output_device=output_device)
        print("Playback finished.")

    return 0


def print_metrics(metrics: object, metrics_path: Path) -> None:
    data = asdict(metrics)
    print(f"WAV: {data['path']}")
    print(f"Metrics: {metrics_path}")
    print(f"Duration: {data['duration_seconds']:.3f}s")
    print(f"Sample rate: {data['sample_rate']} Hz")
    print(f"Channels: {data['channels']}")
    print(f"Peak: {data['peak_pcm']} PCM ({data['peak_normalized']:.3f})")
    dbfs = data["peak_dbfs"]
    print(f"Peak dBFS: {'-inf' if dbfs is None else f'{dbfs:.2f}'}")
    print(f"Clipped: {'yes' if data['clipped'] else 'no'}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.list_devices:
            print_devices()
            return 0
        return run_loopback(args)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
