from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.gpt_sovits.backend import write_audio_bytes
from runtime.gpt_sovits.config import load_gpt_sovits_config
from runtime.gpt_sovits.service import GPTSoVITSSynthesizer
from runtime.gpt_sovits.types import SpeechRequest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 4 GPT-SoVITS speech synthesis client.")
    parser.add_argument("text", help="Text to synthesize.")
    parser.add_argument("--config", default="configs/runtime/gpt_sovits.yaml")
    parser.add_argument("--character-id", default="elysia")
    parser.add_argument("--emotion", default="neutral")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--output", default=None, help="Output WAV path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        synthesizer = GPTSoVITSSynthesizer.from_config(args.config)
        result = synthesizer.synthesize(
            SpeechRequest(
                text=args.text,
                character_id=args.character_id,
                emotion=args.emotion,
                speed=args.speed,
                format="wav",
            )
        )
        output = Path(args.output) if args.output else _default_output_path(load_gpt_sovits_config(args.config))
        write_audio_bytes(output, result.audio_bytes)
        print(f"WAV: {output}")
        print(f"Reference: {result.reference_audio}")
        print(f"Normalized text: {result.normalized_text}")
        print(f"Backend: {result.backend_ms} ms")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _default_output_path(config) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return config.generated_dir / f"gpt-sovits-{timestamp}.wav"


if __name__ == "__main__":
    raise SystemExit(main())
