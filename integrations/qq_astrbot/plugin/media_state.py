"""Cross-process admission and durable, at-most-once media delivery state."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
import psutil


class MediaState:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "media_state.sqlite3"
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS gate (id INTEGER PRIMARY KEY, mode TEXT)")
            db.execute("INSERT OR IGNORE INTO gate VALUES (1, 'ready')")
            db.execute("CREATE TABLE IF NOT EXISTS activity (id TEXT PRIMARY KEY, pid INTEGER, created REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS delivery (id TEXT PRIMARY KEY, payload TEXT, state TEXT, updated REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS song_reply_attempts ("
                       "id INTEGER PRIMARY KEY, job_id TEXT, attempt INTEGER, status TEXT, "
                       "raw TEXT, reply TEXT, error TEXT, created REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS song_selection_attempts ("
                       "id INTEGER PRIMARY KEY, job_id TEXT, attempt INTEGER, status TEXT, "
                       "raw TEXT, song_id INTEGER, error TEXT, reasoning_chars INTEGER, created REAL)")

    def record_song_selection(self, key, attempt, *, status, raw="", song_id=None, error="", reasoning_chars=0):
        with self.connection() as db:
            db.execute("INSERT INTO song_selection_attempts(job_id,attempt,status,raw,song_id,error,reasoning_chars,created) "
                       "VALUES (?,?,?,?,?,?,?,?)", (key, attempt, status, raw[:2000], song_id, error, reasoning_chars, time.time()))

    def record_song_reply(self, key, attempt, *, status, raw="", reply="", error=""):
        # Local audit only; no persona, access token or transport credentials.
        with self.connection() as db:
            db.execute("INSERT INTO song_reply_attempts(job_id,attempt,status,raw,reply,error,created) "
                       "VALUES (?,?,?,?,?,?,?)", (key, attempt, status, raw[:2000], reply, error, time.time()))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def mode(self) -> str:
        with self.connection() as db:
            return db.execute("SELECT mode FROM gate WHERE id=1").fetchone()[0]

    def set_mode(self, mode: str):
        if mode not in {"ready", "draining", "cover", "restoring", "recovering"}:
            raise ValueError("Unknown GPU admission state")
        with self.connection() as db:
            db.execute("UPDATE gate SET mode=? WHERE id=1", (mode,))

    def enter(self) -> str | None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT mode FROM gate WHERE id=1").fetchone()[0] != "ready":
                return None
            lease = uuid.uuid4().hex
            db.execute("INSERT INTO activity VALUES (?, ?, ?)", (lease, os.getpid(), psutil.Process().create_time()))
            return lease

    def leave(self, lease: str | None):
        if lease:
            with self.connection() as db:
                db.execute("DELETE FROM activity WHERE id=?", (lease,))

    def active(self) -> int:
        with self.connection() as db:
            for lease, pid, created in db.execute("SELECT id,pid,created FROM activity").fetchall():
                try:
                    if abs(psutil.Process(pid).create_time() - created) > 1:
                        db.execute("DELETE FROM activity WHERE id=?", (lease,))
                except psutil.NoSuchProcess:
                    db.execute("DELETE FROM activity WHERE id=?", (lease,))
                except (psutil.AccessDenied, OSError):
                    pass  # Unknown process state is never permission to unload.
            return db.execute("SELECT count(*) FROM activity").fetchone()[0]

    def forget_process(self):
        with self.connection() as db:
            db.execute("DELETE FROM activity WHERE pid=?", (os.getpid(),))

    def remember(self, key: str, payload: dict) -> bool:
        with self.connection() as db:
            return db.execute("INSERT OR IGNORE INTO delivery VALUES (?,?,'pending',?)",
                              (key, json.dumps(payload, ensure_ascii=False), time.time())).rowcount == 1

    def claim_send(self, key: str) -> bool:
        with self.connection() as db:
            return db.execute("UPDATE delivery SET state='sending',updated=? WHERE id=? AND state='pending'",
                              (time.time(), key)).rowcount == 1

    def mark(self, key: str, state: str):
        if state not in {"sent", "uncertain", "canceled", "pending"}:
            raise ValueError("Unknown delivery state")
        with self.connection() as db:
            db.execute("UPDATE delivery SET state=?,updated=? WHERE id=?", (state, time.time(), key))

    def recover(self) -> list[tuple[str, dict]]:
        with self.connection() as db:
            # A send interrupted by a crash may already have reached QQ.
            db.execute("UPDATE delivery SET state='uncertain' WHERE state='sending'")
            return [(key, json.loads(payload)) for key, payload in
                    db.execute("SELECT id,payload FROM delivery WHERE state='pending'").fetchall()]
