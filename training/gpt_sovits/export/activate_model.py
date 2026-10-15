from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.gpt_sovits import model_registry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Activate a registered GPT-SoVITS model after validation.")
    parser.add_argument("--character", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--skip-smoke", action="store_true")
    parser.add_argument("--acceptance-command", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    entry = model_registry.find_model(args.character, args.model)
    missing = [path for path in entry.required_files if not path.exists()]
    if missing:
        raise FileNotFoundError("candidate model file(s) missing: " + ", ".join(str(path) for path in missing))

    smoke = None
    if not args.skip_smoke:
        if args.acceptance_command:
            _run_smoke(args.acceptance_command)
            smoke = lambda _model: None
        else:
            from runtime.core.model_smoke import smoke_test_voice
            smoke = smoke_test_voice

    activated = model_registry.activate_model(args.character, args.model, smoke_test=smoke)
    print(json.dumps(activated.to_manifest(), ensure_ascii=False, indent=2))
    return 0


def _run_smoke(command: str) -> None:
    if not command:
        return
    completed = subprocess.run(command, cwd=PROJECT_ROOT, shell=True, text=True, capture_output=True, check=False)
    smoke_dir = PROJECT_ROOT / "storage" / "logs"
    smoke_dir.mkdir(parents=True, exist_ok=True)
    (smoke_dir / "gpt-sovits-activation-smoke.out.log").write_text(completed.stdout, encoding="utf-8")
    (smoke_dir / "gpt-sovits-activation-smoke.err.log").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(f"activation smoke command failed with exit code {completed.returncode}")


if __name__ == "__main__":
    raise SystemExit(main())
