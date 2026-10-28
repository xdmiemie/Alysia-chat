"""Install the pinned FeiyuVoice RVC release without executing model code."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPO = "Esmerubphire/FeiyuVoice-RVC"
REVISION = "04e5011"
MODEL_SHA = "bcc3f4de74f87e102358db704c0fb75fc700fd8f44f491494287cc0b071a1dfe"
INDEX_SHA = "3821de84cf7ad3e2be40475ee06c261da03be25b0583e571f568da2658a0732a"
FILES = ("manifest.json", "files_sha256.json", "FeiyuVoice-RVC-v1.pth",
         "FeiyuVoice-RVC-v1.index", "run_rvc.py", "runtime_pins.json",
         "runtime.template.json", "NOTICE.md", "COPYRIGHT.md", "Applio_CODE_LICENSE.txt",
         ".gitignore", "Apply-Breath.ps1", "CHANGELOG.md", "Convert.ps1",
         "README.md", "apply_breath.py", "configuration.json", "provenance.json",
         "regions.example.json", "requirements-tools.txt", "validation.json",
         "换气区间标记.html")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def install(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        path = destination / name
        if path.is_file() and name == "FeiyuVoice-RVC-v1.pth" and sha256(path) == MODEL_SHA:
            continue
        if path.is_file() and name == "FeiyuVoice-RVC-v1.index" and sha256(path) == INDEX_SHA:
            continue
        url = f"https://huggingface.co/{REPO}/resolve/{REVISION}/{urllib.parse.quote(name)}"
        partial = path.with_name(path.name + ".part")
        headers = {"User-Agent": "astrbot-chat/step14A"}
        offset = partial.stat().st_size if partial.exists() else 0
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=90) as response:
            mode = "ab" if offset and response.status == 206 else "wb"
            with partial.open(mode) as output:
                while block := response.read(1024 * 1024):
                    output.write(block)
        partial.replace(path)
        print(f"downloaded {name}: {path.stat().st_size} bytes", flush=True)
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    if (manifest.get("schema") != "feiyu-rvc-release-v1" or
            manifest.get("model", {}).get("pth_sha256") != MODEL_SHA or
            manifest.get("model", {}).get("index_sha256") != INDEX_SHA):
        raise ValueError("Unexpected FeiyuVoice release manifest")
    if sha256(destination / manifest["model"]["pth"]) != MODEL_SHA:
        raise ValueError("FeiyuVoice weight SHA256 mismatch")
    if sha256(destination / manifest["model"]["index"]) != INDEX_SHA:
        raise ValueError("FeiyuVoice index SHA256 mismatch")
    for name, record in json.loads((destination / "files_sha256.json").read_text(encoding="utf-8")).items():
        if name in FILES and sha256(destination / name) != record["sha256"]:
            raise ValueError(f"FeiyuVoice package SHA256 mismatch: {name}")
    print(f"verified {manifest['id']} {manifest['version']} at {destination}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path,
                        default=ROOT / "model_store/registry/rvc/feiyu/v1")
    args = parser.parse_args()
    try:
        install(args.destination)
    except Exception as exc:
        print(f"FeiyuVoice installation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
