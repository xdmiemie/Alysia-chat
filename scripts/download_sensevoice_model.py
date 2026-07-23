from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.sensevoice.backend import resolve_model_path


DEFAULT_MODEL_ID = "iic/SenseVoiceSmall"
DEFAULT_MANIFEST = "model_store/active/sensevoice.json"
EXPECTED_MODEL_FILES = {
    "model.pt",
    "config.yaml",
    "tokens.json",
    "am.mvn",
    "chn_jpn_yue_eng_ko_spectok.bpe.model",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download SenseVoiceSmall into the local model_store.")
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
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

    target_model_file = PROJECT_ROOT / str(required_files[0])
    target_dir = target_model_file.parent
    missing_expected = [name for name in EXPECTED_MODEL_FILES if not (target_dir / name).exists()]
    if target_model_file.exists() and not missing_expected:
        print(f"SenseVoice model already exists: {target_model_file}")
        return 0

    try:
        from modelscope import snapshot_download
    except ImportError as exc:
        print(
            "Error: missing dependency 'modelscope'. Install runtime/sensevoice/requirements-model.txt first.",
            file=sys.stderr,
        )
        return 2

    target_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = snapshot_download(args.model_id)
    cache_path = Path(cache_dir)

    for source in cache_path.rglob("*"):
        if source.is_file():
            relative = source.relative_to(cache_path)
            destination = target_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    try:
        resolved = resolve_model_path(args.manifest, project_root=PROJECT_ROOT)
    except RuntimeError as exc:
        print(f"Error: downloaded model did not satisfy manifest: {exc}", file=sys.stderr)
        return 1

    print(f"SenseVoice model ready: {resolved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
