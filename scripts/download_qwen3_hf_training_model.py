from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPO_ID = "Qwen/Qwen3-8B"
DEFAULT_TARGET = "model_store/cache/qwen3/hf/Qwen3-8B"
DEFAULT_HF_CACHE = "model_store/cache/huggingface"
DEFAULT_CONFIG = "training/qwen3/configs/characters/elysia.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download the HF-format Qwen3 base model used by QLoRA hf_path.")
    parser.add_argument("--repo-id", default=DEFAULT_REPO_ID)
    parser.add_argument("--revision", default=None)
    parser.add_argument("--target-dir", default=DEFAULT_TARGET)
    parser.add_argument("--hf-cache-dir", default=DEFAULT_HF_CACHE)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--force-download", action="store_true")
    parser.add_argument("--local-files-only", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--skip-config-update", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    target = _project_path(args.target_dir)
    cache_dir = _project_path(args.hf_cache_dir)
    ready, problems = verify_model_dir(target)
    if args.verify_only:
        if ready:
            print(f"Qwen3 HF training model is complete: {target}")
            return 0
        print(f"Qwen3 HF training model is incomplete: {target}", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1

    if ready:
        print(f"Qwen3 HF training model already exists: {target}")
    else:
        if target.exists():
            print(f"Existing Qwen3 HF training model is incomplete: {target}")
            for problem in problems:
                print(f"- {problem}")
            print("Continuing download and reusing existing cache/files.")
        try:
            from huggingface_hub import snapshot_download
        except ImportError:
            print(
                "Error: missing dependency 'huggingface_hub'. Run quickstart/bootstrap.ps1 first.",
                file=sys.stderr,
            )
            return 2

        target.parent.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)
        print(f"Downloading {args.repo_id} to {target}")
        print(f"Using Hugging Face cache: {cache_dir}")
        snapshot_download(
            repo_id=args.repo_id,
            revision=args.revision,
            cache_dir=str(cache_dir),
            local_dir=str(target),
            local_dir_use_symlinks=False,
            force_download=args.force_download,
            local_files_only=args.local_files_only,
            max_workers=args.max_workers,
            allow_patterns=[
                "*.json",
                "*.safetensors",
                "*.model",
                "tokenizer*",
                "vocab*",
                "merges.txt",
            ],
        )
        ready, problems = verify_model_dir(target)
        if not ready:
            print(f"Error: downloaded model is incomplete: {target}", file=sys.stderr)
            for problem in problems:
                print(f"- {problem}", file=sys.stderr)
            return 2
        print(f"Qwen3 HF training model ready: {target}")

    if not args.skip_config_update:
        config_path = _project_path(args.config)
        update_hf_path(config_path, target)
        print(f"Updated hf_path in {config_path.relative_to(PROJECT_ROOT)}")
    return 0


def update_hf_path(config_path: Path, target: Path) -> None:
    relative_target = _relative(target)
    lines = config_path.read_text(encoding="utf-8").splitlines()
    in_base_model = False
    updated = False
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped == "base_model:":
            in_base_model = True
            output.append(line)
            continue
        if in_base_model and line and not line.startswith(" "):
            if not updated:
                output.append(f"  hf_path: {relative_target}")
                updated = True
            in_base_model = False
        if in_base_model and stripped.startswith("hf_path:"):
            output.append(f"  hf_path: {relative_target}")
            updated = True
        else:
            output.append(line)
    if in_base_model and not updated:
        output.append(f"  hf_path: {relative_target}")
    config_path.write_text("\n".join(output) + "\n", encoding="utf-8")


def verify_model_dir(path: Path) -> tuple[bool, list[str]]:
    problems: list[str] = []
    if not path.exists():
        return False, [f"missing directory: {path}"]
    if not (path / "config.json").exists():
        problems.append("missing config.json")
    if not (any(path.glob("tokenizer*")) or (path / "vocab.json").exists()):
        problems.append("missing tokenizer files")

    index_path = path / "model.safetensors.index.json"
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
            weight_map = index.get("weight_map", {})
            expected_shards = sorted({str(value) for value in weight_map.values()})
        except Exception as exc:
            problems.append(f"invalid model.safetensors.index.json: {exc}")
            expected_shards = []
        for shard in expected_shards:
            shard_path = path / shard
            if not shard_path.exists():
                problems.append(f"missing weight shard: {shard}")
            elif shard_path.stat().st_size <= 0:
                problems.append(f"empty weight shard: {shard}")
    elif not any(path.glob("*.safetensors")):
        problems.append("missing safetensors weights")

    incomplete_files = [file.name for file in path.rglob("*.incomplete")]
    for incomplete in incomplete_files:
        problems.append(f"incomplete download marker: {incomplete}")

    return not problems, problems


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
