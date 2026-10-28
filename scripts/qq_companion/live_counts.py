"""Show aggregate Q1 inbound counts from the ignored local guard database."""

from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "storage" / "qq_companion" / "inbound.sqlite3"


def main() -> int:
    if not DB.exists():
        print("No live Q1 inbound events recorded yet.")
        return 0
    with sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT kind, COUNT(*), SUM(has_at), SUM(has_image), SUM(has_voice), SUM(has_reply) "
            "FROM inbound_event GROUP BY kind ORDER BY kind"
        ).fetchall()
    for kind, count, at, image, voice, reply in rows:
        print(f"{kind}: messages={count}, at_bot={at}, image={image}, voice={voice}, reply={reply}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
