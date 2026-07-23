from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.sensevoice.service import SenseVoiceRecognizer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 2 SenseVoice file transcription.")
    parser.add_argument("audio", help="Path to a WAV/PCM audio file.")
    parser.add_argument("--config", default="configs/runtime/sensevoice.yaml")
    parser.add_argument("--repeat", type=int, default=1, help="Transcribe the same file repeatedly for stability checks.")
    parser.add_argument("--json", action="store_true", help="Print each result as JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    audio_path = Path(args.audio)
    if not audio_path.exists():
        print(f"Error: audio file not found: {audio_path}", file=sys.stderr)
        return 2
    if args.repeat < 1:
        print("Error: --repeat must be at least 1.", file=sys.stderr)
        return 2

    try:
        recognizer = SenseVoiceRecognizer.from_config(args.config)
        for index in range(args.repeat):
            result = recognizer.transcribe(audio_path)
            if args.json:
                print(json.dumps(result.to_dict(), ensure_ascii=False))
            else:
                print(f"[{index + 1}/{args.repeat}] text={result.text!r}")
                print(
                    "  "
                    f"language={result.language} emotion={result.emotion} event={result.event} "
                    f"duration_ms={result.duration_ms} inference_ms={result.inference_ms}"
                )
                if result.skipped:
                    print(f"  skipped=true reason={result.reason}")
        return 0
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Error: transcription failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
