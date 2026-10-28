"""Download SHA-pinned ContentVec and RMVPE assets for FeiyuVoice RVC."""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from pathlib import Path

ASSETS = (
    ("rvc/models/predictors/rmvpe.pt",
     "https://huggingface.co/IAHispano/Applio/resolve/b81f9708367b124b29a47788e4c89b721166234f/Resources/predictors/rmvpe.pt",
     "6d62215f4306e3ca278246188607209f09af3dc77ed4232efdd069798c4ec193"),
    ("rvc/models/embedders/contentvec/pytorch_model.bin",
     "https://huggingface.co/IAHispano/Applio/resolve/b81f9708367b124b29a47788e4c89b721166234f/Resources/embedders/contentvec/pytorch_model.bin",
     "d8dd400e054ddf4e6be75dab5a2549db748cc99e756a097c496c099f65a4854e"),
    ("rvc/models/embedders/contentvec/config.json",
     "https://huggingface.co/IAHispano/Applio/resolve/b81f9708367b124b29a47788e4c89b721166234f/Resources/embedders/contentvec/config.json",
     "2ddde063b795d38d9051a7215a092fecf4cfe148b54251e38de51d88d356898b"),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def install(repo: Path) -> None:
    for relative, url, expected in ASSETS:
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_file() and sha256(destination) == expected:
            print(f"verified {relative}", flush=True)
            continue
        partial = destination.with_name(destination.name + ".part")
        offset = partial.stat().st_size if partial.exists() else 0
        request = urllib.request.Request(url, headers={"Range": f"bytes={offset}-"} if offset else {})
        with urllib.request.urlopen(request, timeout=120) as response:
            with partial.open("ab" if offset and response.status == 206 else "wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
        if sha256(partial) != expected:
            raise ValueError(f"SHA256 mismatch: {relative}")
        partial.replace(destination)
        print(f"verified {relative} ({destination.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--applio-repo", type=Path, required=True)
    args = parser.parse_args()
    install(args.applio_repo)
