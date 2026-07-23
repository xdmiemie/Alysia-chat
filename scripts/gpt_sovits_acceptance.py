from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.gpt_sovits.backend import write_audio_bytes
from runtime.gpt_sovits.config import load_gpt_sovits_config
from runtime.gpt_sovits.service import GPTSoVITSSynthesizer
from runtime.gpt_sovits.types import SpeechRequest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the stage 4 GPT-SoVITS fixed-sentence acceptance check.")
    parser.add_argument("--config", default="configs/runtime/gpt_sovits.yaml")
    parser.add_argument("--inputs", default="tests/fixtures/gpt_sovits_stage4_texts.txt")
    parser.add_argument("--output-dir", default="storage/generated_audio/stage4_acceptance")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    inputs = _load_inputs(Path(args.inputs))
    if not inputs:
        print("Error: no acceptance texts found.", file=sys.stderr)
        return 2

    synthesizer = GPTSoVITSSynthesizer.from_config(args.config)
    output_dir = Path(args.output_dir)
    failures = 0
    for index, item in enumerate(inputs, start=1):
        try:
            result = synthesizer.synthesize(
                SpeechRequest(
                    text=item["text"],
                    character_id="elysia",
                    emotion=item["emotion"],
                    speed=float(item["speed"]),
                    format="wav",
                )
            )
            output = write_audio_bytes(output_dir / f"{index:02d}-{item['emotion']}.wav", result.audio_bytes)
            ok = output.stat().st_size > 44
            failures += 0 if ok else 1
            print(json.dumps({"index": index, "ok": ok, "output": str(output), "text": item["text"]}, ensure_ascii=False))
        except Exception as exc:
            failures += 1
            print(json.dumps({"index": index, "ok": False, "error": str(exc), "text": item["text"]}, ensure_ascii=False))

    print(json.dumps({"items": len(inputs), "failures": failures}, ensure_ascii=False))
    return 1 if failures else 0


def _load_inputs(path: Path) -> list[dict[str, object]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        rows.append(json.loads(stripped))
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
