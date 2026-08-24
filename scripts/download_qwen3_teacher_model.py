"""Install the fixed local Stage 16 Teacher model without touching active roles."""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPO_ID = "Qwen/Qwen3-8B-GGUF"
DEFAULT_FILENAME = "Qwen3-8B-Q4_K_M.gguf"
DEFAULT_TARGET = "model_store/teacher/qwen3-8b-instruct/Qwen3-8B-Q4_K_M.gguf"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download the fixed local Qwen3-8B-Instruct Teacher.")
    parser.add_argument("--repo-id", default=DEFAULT_REPO_ID)
    parser.add_argument("--filename", default=DEFAULT_FILENAME)
    parser.add_argument("--target", default=DEFAULT_TARGET)
    args = parser.parse_args(argv)
    target = Path(args.target)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    if target.exists() and target.stat().st_size > 1024 * 1024 * 1024:
        print(f"Teacher model already exists: {target}")
        return 0
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        print("Missing huggingface_hub. Install runtime/qwen3/requirements.txt first.", file=sys.stderr)
        return 2
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        downloaded = Path(hf_hub_download(repo_id=args.repo_id, filename=args.filename))
        if downloaded.resolve() != target.resolve():
            shutil.copy2(downloaded, target)
    except Exception as exc:
        print(f"Teacher model download failed: {exc}", file=sys.stderr)
        return 1
    print(f"Teacher model ready: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
