"""Render the shared Qwen3 runtime settings for the PowerShell launcher."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime.qwen3.config import DEFAULT_CONFIG_PATH, load_qwen3_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    args = parser.parse_args()
    config = load_qwen3_config(args.config)
    print(json.dumps({**asdict(config), "server_args": config.server_args()}, default=str))


if __name__ == "__main__":
    main()
