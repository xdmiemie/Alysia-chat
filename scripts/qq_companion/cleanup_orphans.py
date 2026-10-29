"""Inspect/stop proven orphan QQ companion trees; never stop NTQQ or training."""

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import psutil
from scripts.qq_companion.supervisor import stop_tree


def find_orphans():
    candidates = {}
    markers = {
        "start_qwen3_server.ps1": "qwen3_launcher", "llama-server.exe": "qwen3",
        "start_gpt_sovits_upstream.ps1": "tts_launcher", "api_v2.py": "tts_upstream",
        "runtime.gpt_sovits.api": "tts_wrapper", "runtime.sensevoice.api": "asr",
        "services.media_api.app": "media_api", "astrbot.cli": "astrbot",
    }
    for process in psutil.process_iter(["pid", "ppid", "exe", "cmdline", "create_time"]):
        try:
            info = process.info
            argv = info["cmdline"] or []
            executable = info["exe"] or ""
            if not (executable.lower().startswith(str(ROOT).lower() + "\\") or
                    any(str(ROOT).lower() in arg.lower() for arg in argv)):
                continue
            command = " ".join(argv).lower()
            if any(word in command for word in ("training_jobs.py", "train_qwen", "train_gpt", "pytest")):
                continue
            role = next((role for marker, role in markers.items() if marker in command), None)
            if role is None and "external\\snowluma" in command and "index.mjs" in command:
                role = "snowluma"
            if role:
                candidates[info["pid"]] = (process, role)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    roots = []
    for pid, (process, role) in candidates.items():
        try:
            if process.ppid() in candidates:
                continue
            # A live parent/controller makes this an owned workload, not an
            # orphan. Never kill it merely because a registry is unavailable.
            if psutil.pid_exists(process.ppid()):
                continue
            roots.append((pid, process.create_time(), role))
        except psutil.NoSuchProcess:
            continue
    # A deliberately detached local llama-server is not a broken QQ stack.
    # Require a cohort of QQ services with the same missing supervisor parent.
    cohorts = {}
    for item in roots:
        try:
            parent = psutil.Process(item[0]).ppid()
            cohorts.setdefault(parent, []).append(item)
        except psutil.NoSuchProcess:
            continue
    return [item for cohort in cohorts.values()
            if {"astrbot", "media_api"}.issubset({item[2] for item in cohort})
            for item in cohort]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    roots = find_orphans()
    for pid, started, role in roots:
        print(f"orphan {role}: pid={pid}", flush=True)
        if args.apply:
            stop_tree(pid, started)
            print(f"stopped owned {role} tree", flush=True)
    print(f"Proven orphan trees: {len(roots)}; NTQQ/training excluded.")


if __name__ == "__main__":
    main()
