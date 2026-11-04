"""Inspect free-only search status, or perform an explicit bounded smoke query."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from integrations.qq_astrbot.plugin.web_search import SearchService


async def run(args):
    service = SearchService()
    if args.query:
        return await service.execute("web_search", {"query": args.query})
    if args.account:
        return await service.account_status()
    return service.status()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--account", action="store_true", help="Read Tavily /usage; does not spend search credits")
    mode.add_argument("--query", help="Perform one basic search; spends at most one free credit")
    result = asyncio.run(run(parser.parse_args()))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok", result.get("available", False)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
