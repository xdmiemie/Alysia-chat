"""Bind the verified FeiyuVoice bundle to a compatible local Applio runtime."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from install_feiyu_rvc import ROOT, sha256


def configure(bundle: Path, repo: Path, python: Path) -> None:
    bundle, repo, python = bundle.resolve(strict=True), repo.resolve(strict=True), python.resolve(strict=True)
    pins = json.loads((bundle / "runtime_pins.json").read_text(encoding="utf-8"))
    for name, expected in pins.items():
        path = repo / name
        if not path.is_file() or sha256(path) != expected:
            raise ValueError(f"Applio runtime checksum mismatch: {name}")
    subprocess.run([str(python), "-c",
                    "import torch; from rvc.infer.infer import VoiceConverter; "
                    "assert torch.cuda.is_available(); print(torch.__version__)"] ,
                   cwd=repo, timeout=45, check=True)
    settings = json.loads((bundle / "runtime.template.json").read_text(encoding="utf-8"))["settings"]
    target = bundle / "runtime.local.json"
    target.write_text(json.dumps({"repo": str(repo), "settings": settings}, indent=2) + "\n",
                      encoding="utf-8")
    print(f"configured {target}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, default=ROOT / "model_store/registry/rvc/feiyu/v1")
    parser.add_argument("--applio-repo", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    args = parser.parse_args()
    configure(args.bundle, args.applio_repo, args.python)
