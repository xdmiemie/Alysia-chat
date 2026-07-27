from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.gpt_sovits import model_registry
from training.gpt_sovits.config import config_hash, load_training_config
from training.gpt_sovits.preprocess.dataset_manifest import validate_dataset


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect trained GPT-SoVITS weights into model_store and register them.")
    parser.add_argument("--config", default="training/gpt_sovits/configs/characters/elysia.yaml")
    parser.add_argument("--model-id", default="")
    parser.add_argument("--display-name", default="")
    parser.add_argument("--gpt-weight", default="", help="Path to trained S1/GPT .ckpt")
    parser.add_argument("--sovits-weight", default="", help="Path to trained S2/SoVITS .pth")
    parser.add_argument("--references-dir", default="", help="Directory containing reference audio and .txt sidecars")
    parser.add_argument("--activate", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = PROJECT_ROOT / args.config
    config = load_training_config(config_path)
    validation = validate_dataset(config, write_normalized=False)
    if not validation.ready:
        return 1

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    model_id = args.model_id or config.export.model_id_template.format(
        character=config.character_id,
        run=config.run_name,
        timestamp=timestamp,
    )
    registry_dir = config.export.registry_root / config.character_id / model_id
    references_target = registry_dir / "references"
    registry_dir.mkdir(parents=True, exist_ok=True)
    references_target.mkdir(parents=True, exist_ok=True)

    gpt_weight = _project_path(args.gpt_weight) if args.gpt_weight else discover_latest_gpt_weight(config.run_dir)
    sovits_weight = _project_path(args.sovits_weight) if args.sovits_weight else discover_latest_sovits_weight(config.run_dir)
    references_dir = _project_path(args.references_dir) if args.references_dir else config.run_dir / "references"
    if gpt_weight.suffix.lower() != ".ckpt":
        raise ValueError(f"--gpt-weight must be a .ckpt file: {gpt_weight}")
    if sovits_weight.suffix.lower() != ".pth":
        raise ValueError(f"--sovits-weight must be a .pth file: {sovits_weight}")
    if not references_dir.exists():
        raise FileNotFoundError(f"references directory does not exist: {references_dir}")

    registered_gpt = registry_dir / gpt_weight.name
    registered_sovits = registry_dir / sovits_weight.name
    shutil.copy2(gpt_weight, registered_gpt)
    shutil.copy2(sovits_weight, registered_sovits)
    reference_files: list[Path] = []
    for source in references_dir.iterdir():
        if source.is_file() and source.suffix.lower() in {".wav", ".flac", ".mp3", ".m4a", ".txt"}:
            target = references_target / source.name
            shutil.copy2(source, target)
            if target.suffix.lower() in {".wav", ".flac", ".mp3", ".m4a"}:
                reference_files.append(target)
    if not reference_files:
        raise RuntimeError("at least one reference audio file is required")

    export_log = config.run_dir / "export_log.json"
    entry = model_registry.register_model(
        character_id=config.character_id,
        model_id=model_id,
        display_name=args.display_name or f"{config.character_id} GPT-SoVITS {timestamp}",
        kind="trained",
        required_files=[registered_gpt, registered_sovits, *reference_files],
        data_hash=validation.dataset_hash,
        config_hash=config_hash(config_path),
        source_run=_relative(config.run_dir),
        export_log=_relative(export_log),
        activate=args.activate,
    )
    export_log.parent.mkdir(parents=True, exist_ok=True)
    export_log.write_text(json.dumps(entry.to_manifest(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(entry.to_manifest(), ensure_ascii=False, indent=2))
    return 0


def discover_latest_gpt_weight(run_dir: Path) -> Path:
    candidates = [
        *run_dir.glob("weights/GPT_weights*/*.ckpt"),
        *run_dir.glob("s1/ckpt/*.ckpt"),
    ]
    return _latest_file(candidates, "trained GPT/S1 .ckpt")


def discover_latest_sovits_weight(run_dir: Path) -> Path:
    candidates = [
        *run_dir.glob("weights/SoVITS_weights*/*.pth"),
    ]
    return _latest_file(candidates, "trained SoVITS/S2 .pth")


def _latest_file(candidates: list[Path], label: str) -> Path:
    files = [path for path in candidates if path.is_file()]
    if not files:
        raise FileNotFoundError(f"could not auto-discover {label}")
    return max(files, key=lambda path: (path.stat().st_mtime_ns, path.name))


def _project_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return PROJECT_ROOT / candidate


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
