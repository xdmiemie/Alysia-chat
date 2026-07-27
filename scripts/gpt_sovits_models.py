from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.gpt_sovits import model_registry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="List, select, activate, and materialize GPT-SoVITS role models.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--character")
    list_parser.add_argument("--json", action="store_true")

    resolve_parser = subparsers.add_parser("resolve")
    resolve_parser.add_argument("--character", required=True)
    resolve_parser.add_argument("--model")
    resolve_parser.add_argument("--interactive", action="store_true")
    resolve_parser.add_argument("--activate", action="store_true")
    resolve_parser.add_argument("--json", action="store_true")

    activate_parser = subparsers.add_parser("activate")
    activate_parser.add_argument("--character", required=True)
    activate_parser.add_argument("--model", required=True)
    activate_parser.add_argument("--json", action="store_true")

    infer_parser = subparsers.add_parser("write-infer-config")
    infer_parser.add_argument("--character", required=True)
    infer_parser.add_argument("--model", default="")
    infer_parser.add_argument("--output", required=True)
    infer_parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "list":
        payload = _list_payload(args.character)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            for character in payload["characters"]:
                print(character["character_id"])
                for model in character["models"]:
                    marker = "*" if model["active"] else " "
                    print(f"  {marker} {model['id']} ({model['kind']})")
        return 0

    if args.command == "resolve":
        entry = (
            model_registry.choose_interactively(args.character)
            if args.interactive
            else model_registry.resolve_model(args.character, args.model)
        )
        if args.activate:
            entry = model_registry.activate_model(entry.character_id, entry.model_id)
        _print_entry(entry, as_json=args.json)
        return 0

    if args.command == "activate":
        entry = model_registry.activate_model(args.character, args.model)
        _print_entry(entry, as_json=args.json)
        return 0

    if args.command == "write-infer-config":
        entry = model_registry.resolve_model(args.character, args.model)
        output = _project_path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(_infer_config_yaml(entry), encoding="utf-8")
        payload = {"config": _relative(output), **entry.to_manifest()}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(_relative(output))
        return 0

    raise AssertionError(args.command)


def _list_payload(character_id: str | None) -> dict[str, object]:
    characters = [character_id] if character_id else model_registry.list_characters()
    payload = {"characters": []}
    for character in characters:
        models = model_registry.list_models(character)
        manifest = model_registry.load_character_manifest(character)
        active = manifest.get("active_model_id")
        payload["characters"].append(
            {
                "character_id": character,
                "active_model_id": active,
                "models": [
                    {
                        "id": model.model_id,
                        "display_name": model.display_name,
                        "kind": model.kind,
                        "active": model.model_id == active,
                    }
                    for model in models
                ],
            }
        )
    return payload


def _print_entry(entry: model_registry.GPTSoVITSModelEntry, *, as_json: bool) -> None:
    payload = entry.to_manifest()
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"{entry.character_id}/{entry.model_id}")
        print(f"gpt_weight={payload['runtime']['gpt_weight']}")
        print(f"sovits_weight={payload['runtime']['sovits_weight']}")


def _infer_config_yaml(entry: model_registry.GPTSoVITSModelEntry) -> str:
    return "\n".join(
        [
            "custom:",
            "  bert_base_path: third_party/GPT-SoVITS/GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large",
            "  cnhuhbert_base_path: third_party/GPT-SoVITS/GPT_SoVITS/pretrained_models/chinese-hubert-base",
            "  device: cuda",
            "  is_half: true",
            f"  t2s_weights_path: {_relative(entry.gpt_weight)}",
            f"  version: {_detect_version(entry)}",
            f"  vits_weights_path: {_relative(entry.sovits_weight)}",
            "",
        ]
    )


def _detect_version(entry: model_registry.GPTSoVITSModelEntry) -> str:
    probe = f"{entry.model_id} {entry.gpt_weight} {entry.sovits_weight}".lower()
    if "v2proplus" in probe:
        return "v2ProPlus"
    if "v2pro" in probe:
        return "v2Pro"
    if "v4" in probe:
        return "v4"
    if "v3" in probe:
        return "v3"
    if "gsv-v2final" in probe or "s2g2333k" in probe or "v2" in probe:
        return "v2"
    return "v1"


def _project_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return PROJECT_ROOT / candidate


def _relative(path: str | Path) -> str:
    resolved = _project_path(path).resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return str(resolved)


if __name__ == "__main__":
    raise SystemExit(main())
