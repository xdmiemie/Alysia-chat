from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.app.orchestrator import Stage5DialogueRunner, default_stage5_output_dir
from runtime.gpt_sovits.config import load_gpt_sovits_config
from runtime.gpt_sovits.references import validate_model_manifest
from runtime.gpt_sovits.service import GPTSoVITSSynthesizer
from runtime.qwen3.config import load_qwen3_config
from runtime.qwen3.service import Qwen3ChatSession
from runtime.sensevoice.backend import resolve_model_path
from runtime.sensevoice.config import load_sensevoice_config
from runtime.sensevoice.service import SenseVoiceRecognizer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 5 serial dialogue from WAV files to generated WAV replies.")
    parser.add_argument("--input-dir", default="storage/generated_audio/dialogue_questions_20")
    parser.add_argument("--output-dir", default=None, help="Directory for generated WAV replies and manifest.jsonl.")
    parser.add_argument("--sensevoice-config", default="configs/runtime/sensevoice.yaml")
    parser.add_argument("--qwen3-config", default="configs/runtime/qwen3.yaml")
    parser.add_argument("--gpt-sovits-config", default="configs/runtime/gpt_sovits.yaml")
    parser.add_argument("--character-id", default="elysia")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N WAV files.")
    parser.add_argument("--json", action="store_true", help="Print each turn as JSON.")
    parser.add_argument("--skip-health", action="store_true", help="Skip model-file and service-port preflight checks.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        print("Error: --limit must be at least 1.", file=sys.stderr)
        return 2

    output_dir = Path(args.output_dir) if args.output_dir else default_stage5_output_dir()

    try:
        if not args.skip_health:
            health_errors = check_stage5_health(
                sensevoice_config=args.sensevoice_config,
                qwen3_config=args.qwen3_config,
                gpt_sovits_config=args.gpt_sovits_config,
            )
            if health_errors:
                for error in health_errors:
                    print(f"Health check failed: {error}", file=sys.stderr)
                return 1

        runner = Stage5DialogueRunner(
            transcriber=SenseVoiceRecognizer.from_config(args.sensevoice_config),
            chat_session=Qwen3ChatSession.from_config(args.qwen3_config),
            synthesizer=GPTSoVITSSynthesizer.from_config(args.gpt_sovits_config),
            input_dir=args.input_dir,
            output_dir=output_dir,
            character_id=args.character_id,
        )
        results = runner.run_all(limit=args.limit)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        for result in results:
            print(json.dumps(result.to_dict(), ensure_ascii=False))
    else:
        for result in results:
            status = "OK" if result.ok else "WARN"
            output = result.output_audio or "(no wav)"
            text = ""
            if result.transcription:
                text = str(result.transcription.get("text", ""))
            print(f"[{status}] {result.index:02d} input={result.input_audio} text={text!r} output={output}")
            if result.error:
                print(f"  error={result.error}")
        print(f"Manifest: {output_dir / 'manifest.jsonl'}")

    return 0 if all(result.output_audio for result in results) else 1


def check_stage5_health(
    sensevoice_config: str | Path,
    qwen3_config: str | Path,
    gpt_sovits_config: str | Path,
) -> list[str]:
    errors: list[str] = []

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


if __name__ == "__main__":
    raise SystemExit(main())
