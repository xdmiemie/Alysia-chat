"""Inspect/copy/migrate memory without printing bodies. Default is read-only.

Rollback creates a separate compatible database, with old writers still blocked;
it never restores a live database or resurrects deleted/revoked source material.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.qq_astrbot.plugin.shared_memory import SharedMemory


def inspect(path):
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("memory_items", "diary_entries", "memory_inbox", "sm_records", "sm_jobs", "sm_tombstones") if table in tables}
        version = db.execute("SELECT value FROM sm_meta WHERE key='schema_version'").fetchone()[0] if "sm_meta" in tables else "legacy"
    return {"schema_version": version, "counts": counts, "planned": "additive schema; legacy ACL retained; extraction queue quarantined; natural-day boundary; no historical batch guessing"}


def migrate(source, target=None, *, apply=False):
    source = Path(source).resolve()
    report = inspect(source)
    if not apply and target is None:
        return {**report, "dry_run": True}
    destination = Path(target).resolve() if target else source
    if target:
        if destination == source or destination.exists():
            raise ValueError("copy_destination_must_be_new")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as db, sqlite3.connect(destination) as out:
            db.backup(out)
    service = SharedMemory(destination)
    return {**inspect(destination), "dry_run": False, "copy": str(destination) if target else None,
            "config": service.config, "backup": str(destination.with_name(destination.name + ".pre-shared-v1.bak"))}


def rollback_copy(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if target.exists() or target == source:
        raise ValueError("rollback_destination_must_be_new")
    # Current DB is authoritative for deletions. Do not restore the pre-migration image.
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as db, sqlite3.connect(target) as out:
        db.backup(out)
        out.execute("INSERT OR REPLACE INTO sm_meta VALUES('rollback_state','read-only-rollback')")
    return {"rollback_copy": str(target), "mode": "legacy read-only; new tables and tombstones retained; old write triggers remain"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--copy-to", type=Path)
    parser.add_argument("--apply", action="store_true", help="Migrate specified DB after stopping both writers")
    parser.add_argument("--rollback-copy", type=Path)
    args = parser.parse_args()
    if args.rollback_copy and (args.apply or args.copy_to):
        parser.error("rollback-copy cannot be combined with migration")
    result = rollback_copy(args.database, args.rollback_copy) if args.rollback_copy else migrate(args.database, args.copy_to, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
