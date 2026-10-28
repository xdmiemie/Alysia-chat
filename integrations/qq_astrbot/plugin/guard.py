"""Small persistent inbound guard; imported by the AstrBot plugin and unit tests."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path


class InboundGuard:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS seen_message ("
            "account_id TEXT NOT NULL, message_id TEXT NOT NULL, seen_at INTEGER NOT NULL, "
            "PRIMARY KEY(account_id, message_id))"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS inbound_event ("
            "account_id TEXT NOT NULL, message_id TEXT NOT NULL, seen_at INTEGER NOT NULL, "
            "kind TEXT NOT NULL, has_at INTEGER NOT NULL, has_image INTEGER NOT NULL, "
            "has_voice INTEGER NOT NULL, has_reply INTEGER NOT NULL, "
            "PRIMARY KEY(account_id, message_id))"
        )
        self.db.commit()
        self._last_prune = 0

    def should_process(self, account_id: str, message_id: str, sender_id: str, *,
                       kind: str = "unknown", has_at: bool = False, has_image: bool = False,
                       has_voice: bool = False, has_reply: bool = False, now: int | None = None) -> bool:
        if not account_id or not message_id or not sender_id or sender_id == account_id:
            return False
        timestamp = int(time.time()) if now is None else now
        with self.db:
            if timestamp - self._last_prune >= 3600:
                self.db.execute("DELETE FROM seen_message WHERE seen_at < ?", (timestamp - 7 * 86400,))
                self.db.execute("DELETE FROM inbound_event WHERE seen_at < ?", (timestamp - 7 * 86400,))
                self._last_prune = timestamp
            result = self.db.execute(
                "INSERT OR IGNORE INTO seen_message(account_id, message_id, seen_at) VALUES (?, ?, ?)",
                (account_id, message_id, timestamp),
            )
            if result.rowcount == 1:
                self.db.execute(
                    "INSERT INTO inbound_event(account_id, message_id, seen_at, kind, has_at, has_image, has_voice, has_reply) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (account_id, message_id, timestamp, kind, int(has_at), int(has_image), int(has_voice), int(has_reply)),
                )
        return result.rowcount == 1

    def close(self) -> None:
        self.db.close()
