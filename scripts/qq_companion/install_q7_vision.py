"""Download and register the pinned projector for the existing Qwen3.5 base."""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from pathlib import Path
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

REPO = "unsloth/Qwen3.5-9B-GGUF"
REVISION = "3885219b6810b007914f3a7950a8d1b469d598a5"
SHA256 = "f70dc3509053962b0d0d3ee8a7eacebf5d60aa560cad78254ae8698516ae029f"
SIZE = 918166080
MODEL = "qwen3.5-9b-q8-0-base-v1"


def download(target):
    """Resume verified-length HTTP ranges; final SHA256 authenticates all parts."""
    url = f"https://huggingface.co/{REPO}/resolve/{REVISION}/mmproj-F16.gguf?download=true"
    parts = target.parent / ".mmproj-download"
    parts.mkdir(exist_ok=True)
    block_size = 1024 * 1024
    count = (SIZE + block_size - 1) // block_size
    def block(index):
        start, end = index * block_size, min(SIZE, (index + 1) * block_size) - 1
        path = parts / f"{index:05d}"
        if path.is_file() and path.stat().st_size == end - start + 1:
            return
        for attempt in range(3):
            try:
                suffix = f"&range={index}" + (f"&retry={attempt}" if attempt else "")
                request = urllib.request.Request(url + suffix, headers={"Range": f"bytes={start}-{end}"})
                with urllib.request.urlopen(request, timeout=90) as response:
                    if response.status != 206 or response.headers.get("Content-Range") != f"bytes {start}-{end}/{SIZE}":
                        raise ValueError("Server did not honor the pinned download range")
                    data = response.read(block_size + 1)
                if len(data) != end - start + 1:
                    raise ValueError("Incomplete download range")
                path.write_bytes(data)
                return
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)
    with ThreadPoolExecutor(max_workers=16) as pool:
        done = 0
        for future in as_completed([pool.submit(block, index) for index in range(count)]):
            future.result()
            done += 1
            if done % 16 == 0:
                print(f"Projector ranges: {done}/{count}", flush=True)
    part = target.with_suffix(".gguf.part")
    with part.open("wb") as output:
        for index in range(count):
            with (parts / f"{index:05d}").open("rb") as source:
                output.write(source.read())
    if part.stat().st_size != SIZE or digest(part) != SHA256:
        raise ValueError("Projector verification failed; no manifest changed")
    part.replace(target)


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    from runtime.qwen3.model_registry import find_model, load_character_manifest, save_character_manifest, activate_model
    entry = find_model("elysia", MODEL)
    target = entry.model_file.parent / "mmproj-F16.gguf"
    if not target.exists():
        download(target)
    if target.stat().st_size != SIZE or digest(target) != SHA256:
        raise ValueError("Projector checksum mismatch")
    data = load_character_manifest("elysia")
    raw = next(x for x in data["models"] if x["id"] == MODEL)
    relative = target.relative_to(ROOT).as_posix()
    raw["required_files"] = list(dict.fromkeys([*raw["required_files"], relative]))
    raw.update(mmproj_file=target.relative_to(ROOT).as_posix(), mmproj_sha256=SHA256,
        visual_source={"repo": REPO, "revision": REVISION, "file": target.name, "size": SIZE,
                       "base": "Qwen/Qwen3.5-9B", "license": "Apache-2.0"})
    save_character_manifest("elysia", data)
    active = json.loads((ROOT / "model_store/active/qwen3.json").read_text(encoding="utf-8-sig"))
    if active["model_id"] == MODEL:
        activate_model("elysia", MODEL)
    print("Projector verified and registered; capability gates remain closed until tested.")


if __name__ == "__main__":
    main()
