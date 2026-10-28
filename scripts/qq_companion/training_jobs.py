"""Operator-only, preset training launcher guarded by GPUResourceBroker.

No HTTP or QQ route invokes this CLI. Keep the original training scripts and
their checkpoint logic as the only source of training behavior.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    from scripts.qq_companion.gpu_broker import GPUResourceBroker, ROOT, STATE
except ModuleNotFoundError:  # direct script execution
    from gpu_broker import GPUResourceBroker, ROOT, STATE

PRESETS = {
    "qwen3_elysia": ROOT / "quickstart/train_qwen3.ps1",
    "gpt_sovits_elysia": ROOT / "quickstart/train_gpt_sovits.ps1",
}
JOBS = STATE / "jobs"


class TrainingJobManager:
    """Operator CLI facade for fixed recipes, status and verified activation."""

    def run_preset(self, preset: str, *, dry_run: bool = False) -> str:
        return run(preset, dry_run=dry_run)

    def status(self, job_id: str) -> dict:
        if len(job_id) != 32 or any(character not in "0123456789abcdef" for character in job_id):
            raise ValueError("Invalid job ID")
        return json.loads((JOBS / f"{job_id}.json").read_text(encoding="utf-8"))

    def activate(self, kind: str, character_id: str, model_id: str) -> str:
        from scripts.qq_companion.model_activation import activate

        return activate(kind, character_id, model_id)

    def rollback(self, job_id: str) -> None:
        from scripts.qq_companion.model_activation import rollback

        rollback(job_id)


def run(preset: str, *, dry_run: bool) -> str:
    if preset not in PRESETS:
        raise ValueError("Unknown training preset")
    job_id = uuid.uuid4().hex
    JOBS.mkdir(parents=True, exist_ok=True)
    record = JOBS / f"{job_id}.json"
    log = JOBS / f"{job_id}.log"
    command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PRESETS[preset])]
    if dry_run:
        command.append("-DryRun")
    broker = GPUResourceBroker()
    broker.acquire(f"training:{preset}")
    try:
        state = {"job_id": job_id, "preset": preset, "status": "running",
                 "started_at": datetime.now(timezone.utc).isoformat(), "dry_run": dry_run,
                 "last_progress": None, "checkpoints": []}
        record.write_text(json.dumps(state), encoding="utf-8")
        with log.open("wb") as output:
            process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            assert process.stdout is not None
            for line in process.stdout:
                output.write(line)
                decoded = line.decode("utf-8", errors="replace").strip()
                if decoded.startswith("Step ") or "checkpoint saved:" in decoded:
                    state["last_progress"] = decoded[:240]
                    if "checkpoint saved:" in decoded:
                        state["checkpoints"].append(decoded.split("checkpoint saved:", 1)[1].strip()[:200])
                    record.write_text(json.dumps(state), encoding="utf-8")
            return_code = process.wait()
        state.update({"status": "succeeded" if return_code == 0 else "failed",
                      "return_code": return_code, "finished_at": datetime.now(timezone.utc).isoformat()})
        record.write_text(json.dumps(state), encoding="utf-8")
        return job_id
    finally:
        broker.release()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("preset", choices=PRESETS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    job_id = TrainingJobManager().run_preset(args.preset, dry_run=args.dry_run)
    print(f"Training job {job_id}: see ignored storage/qq_companion/jobs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
