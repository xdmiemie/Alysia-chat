"""Compatibility entry point for current native-tool model acceptance; no QQ."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]

def run():
    return subprocess.run([str(ROOT / "envs/astrbot/Scripts/python.exe"),
        str(ROOT / "scripts/qq_companion/probe_agent_native.py"), *sys.argv[1:]], cwd=ROOT).returncode

if __name__ == "__main__":
    raise SystemExit(run())
