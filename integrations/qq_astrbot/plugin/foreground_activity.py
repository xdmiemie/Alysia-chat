"""Cross-process QQ activity, from accepted input through final delivery."""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import os
from pathlib import Path
import sqlite3
import time
import uuid

import psutil

from .inference_queue import state_directory


class ForegroundActivity:
    def __init__(self, directory=None):
        directory = Path(directory) if directory is not None else state_directory()
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "foreground_activity.sqlite3"
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS foreground(id TEXT PRIMARY KEY, pid INTEGER, "
                       "process_created REAL, deadline REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS idle(id INTEGER PRIMARY KEY, since REAL)")
            db.execute("INSERT OR IGNORE INTO idle VALUES(1,0)")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def begin(self):
        key = uuid.uuid4().hex
        with self.connection() as db:
            db.execute("INSERT INTO foreground VALUES(?,?,?,?)", (key, os.getpid(),
                psutil.Process().create_time(), time.time() + 1200))
        return key

    def end(self, key):
        if not key:
            return
        with self.connection() as db:
            if db.execute("DELETE FROM foreground WHERE id=?", (key,)).rowcount:
                db.execute("UPDATE idle SET since=? WHERE id=1", (time.time(),))

    def forget_process(self):
        with self.connection() as db:
            if db.execute("DELETE FROM foreground WHERE pid=?", (os.getpid(),)).rowcount:
                db.execute("UPDATE idle SET since=? WHERE id=1", (time.time(),))

    def blocked(self, idle_seconds=0, *, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for key, pid, created, deadline in db.execute("SELECT * FROM foreground").fetchall():
                try:
                    alive = abs(psutil.Process(pid).create_time() - created) < 1
                except psutil.NoSuchProcess:
                    alive = False
                except (psutil.AccessDenied, OSError):
                    alive = True
                if not alive or deadline <= now:
                    db.execute("DELETE FROM foreground WHERE id=?", (key,))
                    db.execute("UPDATE idle SET since=? WHERE id=1", (now,))
            return bool(db.execute("SELECT 1 FROM foreground LIMIT 1").fetchone() or
                        now - db.execute("SELECT since FROM idle WHERE id=1").fetchone()[0] < idle_seconds)


def release_activity_on_error(method):
    @wraps(method)
    async def guarded(self, event, *args, **kwargs):
        try:
            return await method(self, event, *args, **kwargs)
        except BaseException:
            self._release_lease(event)
            raise
    return guarded
