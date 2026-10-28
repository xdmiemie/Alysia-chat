"""Offline, verified model activation with an ignored rollback snapshot.

Only an operator can run this CLI. The GPU Broker rejects activation while
the managed QQ stack or a training job holds the card.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
import uuid
from pathlib import Path
from urllib.error import URLError

import psutil

from runtime.gpt_sovits.model_registry import active_manifest_path as tts_active_path
from runtime.gpt_sovits.model_registry import activate_model as activate_tts
from runtime.gpt_sovits.model_registry import character_manifest_path as tts_registry_path
from runtime.gpt_sovits.config import gpt_sovits_config_for_character
from runtime.gpt_sovits.service import GPTSoVITSSynthesizer
from runtime.gpt_sovits.types import SpeechRequest
from runtime.qwen3.model_registry import ACTIVE_MANIFEST as QWEN_ACTIVE
from runtime.qwen3.model_registry import activate_model as activate_qwen
from runtime.qwen3.model_registry import character_manifest_path as qwen_registry_path
from scripts.qq_companion.gpu_broker import GPUResourceBroker, ROOT, STATE
from scripts.qq_companion.probe_live_model import run_active_probe
from scripts.qq_companion.supervisor import stop_tree

BACKUPS = STATE / "model_backups"


def paths(kind: str, character_id: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"[a-z0-9_]+", character_id):
        raise ValueError("Invalid character ID")
    if kind == "qwen3":
        return QWEN_ACTIVE, qwen_registry_path(character_id)
    if kind == "gpt_sovits":
        return tts_active_path(character_id), tts_registry_path(character_id)
    raise ValueError("Unknown model kind")


def tts_smoke(character_id: str) -> None:
    process = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                str(ROOT / "scripts/start_gpt_sovits_upstream.ps1")],
                               cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    started = psutil.Process(process.pid).create_time()
    try:
        synthesizer = GPTSoVITSSynthesizer.from_config(gpt_sovits_config_for_character(character_id=character_id))
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("GPT-SoVITS upstream exited during activation smoke")
            try:
                result = synthesizer.synthesize(SpeechRequest(text="你好", character_id=character_id))
                if result.audio_bytes.startswith(b"RIFF") and result.audio_bytes[8:12] == b"WAVE":
                    return
            except (RuntimeError, URLError):
                time.sleep(2)
        raise RuntimeError("GPT-SoVITS activation smoke did not produce WAV")
    finally:
        stop_tree(process.pid, started)


def activate(kind: str, character_id: str, model_id: str) -> str:
    if not character_id or not model_id:
        raise ValueError("Character and registered model ID are required")
    active_path, registry_path = paths(kind, character_id)
    broker = GPUResourceBroker()
    broker.acquire(f"activate:{kind}")
    try:
        job_id = uuid.uuid4().hex
        snapshot_dir = BACKUPS / job_id
        snapshot_dir.mkdir(parents=True, exist_ok=False)
        original = {"active": active_path.read_bytes() if active_path.exists() else None,
                    "registry": registry_path.read_bytes() if registry_path.exists() else None}
        for name, content in original.items():
            if content is not None:
                (snapshot_dir / name).write_bytes(content)
        record = {"job_id": job_id, "kind": kind, "character_id": character_id,
                  "model_id": model_id, "status": "testing", "had_active": original["active"] is not None,
                  "had_registry": original["registry"] is not None}
        (snapshot_dir / "record.json").write_text(json.dumps(record), encoding="utf-8")
        try:
            if kind == "qwen3":
                activate_qwen(character_id, model_id)
                run_active_probe()
            else:
                activate_tts(character_id, model_id)
                tts_smoke(character_id)
            record["status"] = "activated"
            return job_id
        except Exception:
            for name, path in (("active", active_path), ("registry", registry_path)):
                content = original[name]
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(content)
            record["status"] = "rolled_back_after_failure"
            raise
        finally:
            (snapshot_dir / "record.json").write_text(json.dumps(record), encoding="utf-8")
    finally:
        broker.release()


def rollback(job_id: str) -> None:
    if len(job_id) != 32 or any(char not in "0123456789abcdef" for char in job_id):
        raise ValueError("Invalid activation job ID")
    snapshot_dir = BACKUPS / job_id
    record = json.loads((snapshot_dir / "record.json").read_text(encoding="utf-8"))
    if record["status"] != "activated":
        raise RuntimeError("This activation cannot be rolled back again")
    broker = GPUResourceBroker()
    broker.acquire("model_rollback")
    try:
        active_path, registry_path = paths(record["kind"], record["character_id"])
        current = json.loads(active_path.read_text(encoding="utf-8-sig"))
        if current.get("model_id") != record["model_id"]:
            raise RuntimeError("A newer model is active; refusing to overwrite it")
        for name, path in (("active", active_path), ("registry", registry_path)):
            if record[f"had_{name}"]:
                path.write_bytes((snapshot_dir / name).read_bytes())
            else:
                path.unlink(missing_ok=True)
        record["status"] = "rolled_back_by_operator"
        (snapshot_dir / "record.json").write_text(json.dumps(record), encoding="utf-8")
    finally:
        broker.release()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    activate_parser = commands.add_parser("activate")
    activate_parser.add_argument("kind", choices=("qwen3", "gpt_sovits"))
    activate_parser.add_argument("character_id")
    activate_parser.add_argument("model_id")
    rollback_parser = commands.add_parser("rollback")
    rollback_parser.add_argument("job_id")
    args = parser.parse_args()
    if args.action == "activate":
        print(f"Activation job: {activate(args.kind, args.character_id, args.model_id)}")
    else:
        rollback(args.job_id)
        print("Activation rolled back")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
