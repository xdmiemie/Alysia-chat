"""Run the Stage 11 fixed prompts against already running local model services."""

from __future__ import annotations

import argparse
import json
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.audio.audio_format import inspect_wav
from runtime.core import AlysiaCore, UnifiedMessage
from runtime.gpt_sovits.service import GPTSoVITSSynthesizer
from runtime.qwen3.service import Qwen3ChatSession
from runtime.sensevoice.service import SenseVoiceRecognizer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, help="Optional WAV for a real SenseVoice turn")
    parser.add_argument("--limit", type=int, default=20, help="Text turns to run (default: all 20)")
    parser.add_argument("--output", type=Path, default=Path("storage/logs/qq-stage11-baseline.json"))
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 20:
        parser.error("--limit must be between 1 and 20")
    for port, service in ((8080, "Qwen3 llama-server"), (9880, "GPT-SoVITS")):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                pass
        except OSError as exc:
            parser.error(f"{service} is not listening on 127.0.0.1:{port}: {exc}")

    prompts = [
        line.strip()
        for line in (PROJECT_ROOT / "docs/qq_agent/fixed_text_inputs.txt").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(prompts) != 20:
        parser.error("fixed_text_inputs.txt must contain exactly 20 nonempty lines")

    transcriber = SenseVoiceRecognizer.from_config() if args.audio else None
    synthesizer = GPTSoVITSSynthesizer.from_config()

    def make_core(output_dir: Path) -> AlysiaCore:
        return AlysiaCore(
            transcriber=transcriber,
            chat_session_factory=lambda _session_id: Qwen3ChatSession.from_config(),
            synthesizer=synthesizer,
            output_dir=output_dir,
        )

    core = make_core(PROJECT_ROOT / "storage/generated_audio/qq_stage11_baseline")
    results: list[dict[str, object]] = []

    def run(message: UnifiedMessage) -> None:
        reply = core.process(message)
        try:
            wav_ok = bool(reply.audio_path) and inspect_wav(reply.audio_path).duration_seconds > 0
        except (OSError, ValueError, EOFError):
            wav_ok = False
        item = {
            "session_id": message.session_id,
            "input_type": message.message_type,
            "ok": reply.ok and wav_ok,
            "error_code": reply.error_code,
            "error": reply.error,
            "reply_text": reply.text,
            "audio_path": reply.audio_path,
            "transcription": reply.transcription,
            "timings": reply.timings.to_dict(),
        }
        results.append(item)
        print(f"{len(results):02d} {message.session_id}: {'OK' if item['ok'] else item['error_code']}", flush=True)

    for index, prompt in enumerate(prompts[: args.limit]):
        run(UnifiedMessage.local_text(prompt, session_id=f"qq:private:{index % 2 + 1}"))
        if not results[-1]["ok"]:
            break
    fixed_completed = sum(bool(item["ok"]) for item in results)

    if args.audio and all(item["ok"] for item in results):
        if not args.audio.is_file():
            parser.error(f"audio file does not exist: {args.audio}")
        run(UnifiedMessage.local_audio(args.audio, session_id="qq:private:voice"))

    if all(item["ok"] for item in results):
        core = make_core(PROJECT_ROOT / "storage/generated_audio/qq_stage11_baseline/restarted")
        run(UnifiedMessage.local_text("重启后继续", session_id="qq:private:1"))

    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "requested_text_turns": args.limit,
        "text_turns_completed": fixed_completed,
        "audio_requested": bool(args.audio),
        "all_ok": len(results) == args.limit + bool(args.audio) + 1 and all(item["ok"] for item in results),
        "results": results,
    }
    output = args.output if args.output.is_absolute() else PROJECT_ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Report: {output}")
    return 0 if report["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
