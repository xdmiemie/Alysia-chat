"""Use the operator's AstrBot persona, including edits made in its native UI."""

import sqlite3


def active_persona(root, character):
    path = root / "external/AstrBot/data/data_v4.db"
    if path.exists():
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            row = db.execute(
                "SELECT system_prompt FROM personas WHERE persona_id=?", (character,)
            ).fetchone()
            if row and row[0]:
                return row[0]
    return (root / "configs/characters" / character / "system_prompt.txt").read_text(
        encoding="utf-8-sig"
    )
