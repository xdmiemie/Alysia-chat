"""Q6 outbox and capability evidence, completely separate from chat memory."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class QzoneStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS qzone_gates (
                  account TEXT, capability TEXT, passed INTEGER, evidence TEXT, checked REAL,
                  PRIMARY KEY(account,capability));
                CREATE TABLE IF NOT EXISTS qzone_control (
                  account TEXT PRIMARY KEY, stopped INTEGER NOT NULL DEFAULT 0, reason TEXT);
                CREATE TABLE IF NOT EXISTS qzone_outbox (
                  id TEXT PRIMARY KEY, account TEXT NOT NULL, kind TEXT NOT NULL, day TEXT NOT NULL,
                  target TEXT NOT NULL, source_id TEXT NOT NULL, content TEXT NOT NULL,
                  content_hash TEXT NOT NULL, audience TEXT NOT NULL, status TEXT NOT NULL,
                  platform_id TEXT, created REAL NOT NULL, updated REAL NOT NULL,
                  error TEXT, attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0);
                CREATE UNIQUE INDEX IF NOT EXISTS qzone_source
                  ON qzone_outbox(account,kind,source_id);
                CREATE TABLE IF NOT EXISTS qzone_scan (
                  account TEXT, day TEXT, PRIMARY KEY(account,day));
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def gate(self, account, capability, passed, evidence=""):
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO qzone_gates VALUES(?,?,?,?,?)",
                       (account, capability, int(passed), evidence[:200], time.time()))

    def passed(self, account, *capabilities):
        with self.connection() as db:
            values = {r["capability"] for r in db.execute(
                "SELECT capability FROM qzone_gates WHERE account=? AND passed=1", (account,))}
        return set(capabilities) <= values

    def stop(self, account, reason):
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO qzone_control VALUES(?,1,?)", (account, reason[:80]))

    def resume(self, account):
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO qzone_control VALUES(?,0,NULL)", (account,))

    def stopped(self, account):
        with self.connection() as db:
            row = db.execute("SELECT stopped FROM qzone_control WHERE account=?", (account,)).fetchone()
            return bool(row and row[0])

    def draft(self, account, kind, day, source, content, audience, *, target=""):
        if kind not in {"daily", "event", "comment", "like", "test"}:
            raise ValueError("invalid_qzone_kind")
        key = digest(json.dumps([account, kind, source]))
        now = time.time()
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO qzone_outbox "
                "(id,account,kind,day,target,source_id,content,content_hash,audience,status,created,updated) "
                "VALUES(?,?,?,?,?,?,?,?,?,'draft',?,?)",
                (key, account, kind, day, target, source, content, digest(content),
                 json.dumps(audience, sort_keys=True), now, now))
        return self.get(key)

    def get(self, key):
        with self.connection() as db:
            row = db.execute("SELECT * FROM qzone_outbox WHERE id=?", (key,)).fetchone()
            return dict(row) if row else None

    def rows(self, account, *, statuses=None):
        with self.connection() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT * FROM qzone_outbox WHERE account=? ORDER BY created,id", (account,))]
        return [r for r in rows if not statuses or r["status"] in statuses]

    def claim(self, key, *, daily_limit=None, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM qzone_outbox WHERE id=?", (key,)).fetchone()
            if not row or row["status"] != "draft" or row["retry_at"] > now:
                return False
            stopped = db.execute("SELECT stopped FROM qzone_control WHERE account=?", (row["account"],)).fetchone()
            if stopped and stopped[0]:
                return False
            if daily_limit is not None:
                # Uncertain writes and deletions still consume their reservation.
                used = db.execute("SELECT count(*) FROM qzone_outbox WHERE account=? AND kind=? AND day=? "
                    "AND (attempts>0 OR status IN ('sending','sent','unknown','deleted'))",
                    (row["account"], row["kind"], row["day"])).fetchone()[0]
                if used >= daily_limit:
                    return False
            db.execute("UPDATE qzone_outbox SET status='sending',updated=?,attempts=attempts+1 WHERE id=?", (now, key))
            return True

    def finish(self, key, status, *, platform_id=None, error=None):
        if status not in {"sent", "unknown", "failed", "deleted", "delete_unknown", "expired"}:
            raise ValueError("invalid_qzone_status")
        with self.connection() as db:
            db.execute("UPDATE qzone_outbox SET status=?,platform_id=coalesce(?,platform_id),error=?,updated=? WHERE id=?",
                       (status, platform_id, error, time.time(), key))

    def retry_read_failure(self, key):
        # Only failures BEFORE a side-effect request may return to draft.
        with self.connection() as db:
            db.execute("UPDATE qzone_outbox SET status='draft',attempts=max(0,attempts-1),retry_at=?,updated=? WHERE id=?",
                       (time.time() + 300, time.time(), key))

    def recover(self, account):
        with self.connection() as db:
            db.execute("UPDATE qzone_outbox SET status='unknown',error='interrupted_write' "
                       "WHERE account=? AND status='sending' AND updated<?", (account, time.time() - 180))

    def scanned(self, account, day):
        with self.connection() as db:
            return db.execute("SELECT 1 FROM qzone_scan WHERE account=? AND day=?", (account, day)).fetchone() is not None

    def mark_scanned(self, account, day):
        with self.connection() as db:
            return db.execute("INSERT OR IGNORE INTO qzone_scan VALUES(?,?)", (account, day)).rowcount == 1

    def summary(self, account):
        with self.connection() as db:
            return {"gates": {r["capability"]: {"passed": bool(r["passed"]), "evidence": r["evidence"]}
                    for r in db.execute("SELECT * FROM qzone_gates WHERE account=?", (account,))},
                    "outbox": {r[0]: r[1] for r in db.execute(
                        "SELECT status,count(*) FROM qzone_outbox WHERE account=? GROUP BY status", (account,))},
                    "stopped": self.stopped(account)}
