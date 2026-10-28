from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


DEFAULT_REPO_ID = "Qwen/Qwen3-14B-GGUF"
DEFAULT_FILENAME = "Qwen3-14B-Q4_K_M.gguf"
DEFAULT_MANIFEST = "model_store/active/qwen3.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download Qwen3-14B Q4_K_M GGUF into model_store.")
    parser.add_argument("--repo-id", default=DEFAULT_REPO_ID)
    parser.add_argument("--filename", default=DEFAULT_FILENAME)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    manifest_path = PROJECT_ROOT / args.manifest
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    required_files = data.get("required_files", [])
    if not required_files:
        print(f"Error: no required_files in {manifest_path}", file=sys.stderr)
        return 2

    target = PROJECT_ROOT / str(required_files[0])
    if target.exists() and target.stat().st_size > 1024 * 1024 * 1024:
        print(f"Qwen3 model already exists: {target}")
        return 0

    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        print("Error: missing dependency 'huggingface_hub'. Install runtime/qwen3/requirements.txt first.", file=sys.stderr)
        return 2

    target.parent.mkdir(parents=True, exist_ok=True)
    downloaded = Path(hf_hub_download(repo_id=args.repo_id, filename=args.filename))
    if downloaded.resolve() != target.resolve():
        shutil.copy2(downloaded, target)
    print(f"Qwen3 model ready: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
