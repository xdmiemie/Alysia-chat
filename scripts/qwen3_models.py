from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.qwen3 import model_registry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="List, select, and activate registered Qwen3 role models.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--character")
    list_parser.add_argument("--json", action="store_true")

    resolve_parser = subparsers.add_parser("resolve")
    resolve_parser.add_argument("--character")
    resolve_parser.add_argument("--model")
    resolve_parser.add_argument("--interactive", action="store_true")
    resolve_parser.add_argument("--activate", action="store_true")
    resolve_parser.add_argument("--json", action="store_true")

    activate_parser = subparsers.add_parser("activate")
    activate_parser.add_argument("--character", required=True)
    activate_parser.add_argument("--model", required=True)
    activate_parser.add_argument("--json", action="store_true")

    ensure_parser = subparsers.add_parser("ensure-default")
    ensure_parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "ensure-default":
        entry = model_registry.ensure_default_elysia_base()
        _print_entry(entry, as_json=args.json)
        return 0

    if args.command == "list":
        payload = _list_payload(args.character)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            for character in payload["characters"]:
                print(character["character_id"])
                for model in character["models"]:
                    marker = "*" if model["active"] else " "
                    print(f"  {marker} {model['id']} ({model['kind']}, {model['format']})")
        return 0

    if args.command == "resolve":
        entry = model_registry.choose_interactively(args.character) if args.interactive else model_registry.resolve_model(args.character, args.model)
        if args.activate:
            entry = model_registry.activate_model(entry.character_id, entry.model_id)
        _print_entry(entry, as_json=args.json)
        return 0

    if args.command == "activate":
        entry = model_registry.activate_model(args.character, args.model)
        _print_entry(entry, as_json=args.json)
        return 0

    raise AssertionError(args.command)


def _list_payload(character_id: str | None) -> dict[str, object]:
    characters = [character_id] if character_id else model_registry.list_characters()
    payload = {"characters": []}
    for character in characters:
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
                        "format": model.model_format,
                        "active": model.model_id == active,
                    }
                    for model in model_registry.list_models(character)
                ],
            }
        )
    return payload


def _print_entry(entry: model_registry.Qwen3ModelEntry, *, as_json: bool) -> None:
    payload = entry.to_manifest()
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"{entry.character_id}/{entry.model_id}")
        print(f"model_file={payload['runtime']['model_file']}")
        if "lora_adapter" in payload["runtime"]:
            print(f"lora_adapter={payload['runtime']['lora_adapter']}")


if __name__ == "__main__":
    raise SystemExit(main())
