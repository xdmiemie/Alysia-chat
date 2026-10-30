"""Local operator-only Q5 inspection; no QQ commands, deletion or forgetting API."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from integrations.qq_astrbot.plugin.memory_repository import MemoryRepository


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "storage/qq_companion/memories.sqlite3")
    parser.add_argument("--character", default="elysia")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    search = commands.add_parser("search")
    search.add_argument("--owner", required=True, help="Canonical qq:bot:dm:user or qq:bot:group:id")
    search.add_argument("query")
    diary = commands.add_parser("diary")
    diary.add_argument("--bot", required=True)
    diary.add_argument("--date", required=True, help="YYYY-MM-DD")
    args = parser.parse_args()
    if not args.database.is_file():
        raise SystemExit("Memory database does not exist yet; start the Q5 plugin first.")
    repository = MemoryRepository(args.database)
    if args.command == "status":
        with repository.connection() as db:
            result = {"facts": db.execute("SELECT count(*) FROM memory_items WHERE visibility='private'").fetchone()[0],
                "shared_summaries": db.execute("SELECT count(*) FROM memory_items WHERE visibility='shared'").fetchone()[0],
                "pending_sources": db.execute("SELECT count(*) FROM memory_inbox WHERE status!='done'").fetchone()[0],
                "diaries": db.execute("SELECT count(*) FROM diary_entries").fetchone()[0],
                "retention": "indefinite"}
    elif args.command == "search":
        result = [{k: row[k] for k in ("id", "summary", "source_type", "source_ref", "visibility", "updated_at")}
                  for row in repository.retrieve(args.owner, args.query, character=args.character, limit=20)]
    else:
        result = repository.diary(args.bot, args.character, args.date)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
