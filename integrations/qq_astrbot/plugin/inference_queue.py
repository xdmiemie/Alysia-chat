"""One durable priority queue shared by every local Qwen caller/process.

This schedules requests, not model loading. The existing MediaState gate still
owns cover/training admission. No message text, image or recipient is logged.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
import hashlib
import os
from pathlib import Path
import sqlite3
import time
import uuid

import psutil


def state_directory():
    if explicit := os.environ.get("ASTRBOT_CHAT_STATE_DIR"):
        return Path(explicit).resolve()
    from .prompt_loader import prompt_path
    return prompt_path().parents[2] / "storage/qq_companion"


class DuplicateRequest(ValueError):
    pass


class InferenceQueue:
    def __init__(self, directory=None):
        directory = Path(directory) if directory is not None else state_directory()
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "inference_queue.sqlite3"
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS inference_requests (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                "id TEXT UNIQUE NOT NULL, priority INTEGER NOT NULL, scope_hash TEXT, phase TEXT, "
                "pid INTEGER, process_created REAL, created REAL, deadline REAL, started REAL, finished REAL, "
                "status TEXT NOT NULL, error TEXT, images INTEGER DEFAULT 0)")
            db.execute("CREATE INDEX IF NOT EXISTS inference_waiting ON inference_requests(status,priority,seq)")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, request_id, priority, scope, phase, timeout, images):
        now = time.time()
        with self.connection() as db:
            db.execute("DELETE FROM inference_requests WHERE finished < ?", (now - 7 * 86400,))
            try:
                db.execute("INSERT INTO inference_requests(id,priority,scope_hash,phase,pid,process_created,created,deadline,status,images) "
                    "VALUES(?,?,?,?,?,?,?,?,'waiting',?)", (request_id, priority,
                        hashlib.sha256(scope.encode()).hexdigest() if scope else "", phase,
                        os.getpid(), psutil.Process().create_time(), now, now + timeout, images))
            except sqlite3.IntegrityError as exc:
                raise DuplicateRequest("duplicate_inference_request") from exc

    def claim(self, request_id):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            for key, pid, created in db.execute(
                    "SELECT id,pid,process_created FROM inference_requests WHERE status IN ('waiting','running')"):
                try:
                    alive = abs(psutil.Process(pid).create_time() - created) < 1
                except psutil.NoSuchProcess:
                    alive = False
                except (psutil.AccessDenied, OSError):
                    alive = True
                if not alive:
                    db.execute("UPDATE inference_requests SET status='failed',error='owner_exited',finished=? WHERE id=?", (now, key))
            db.execute("UPDATE inference_requests SET status='expired',finished=? WHERE status='waiting' AND deadline<=?", (now, now))
            own = db.execute("SELECT status FROM inference_requests WHERE id=?", (request_id,)).fetchone()
            if not own or own[0] != "waiting":
                raise TimeoutError("inference_queue_deadline")
            if db.execute("SELECT 1 FROM inference_requests WHERE status='running'").fetchone():
                return False
            first = db.execute("SELECT id FROM inference_requests WHERE status='waiting' ORDER BY priority,seq LIMIT 1").fetchone()
            if first and first[0] == request_id:
                db.execute("UPDATE inference_requests SET status='running',started=? WHERE id=?", (now, request_id))
                return True
            return False

    def finish(self, request_id, status, error=""):
        with self.connection() as db:
            db.execute("UPDATE inference_requests SET status=?,error=?,finished=? WHERE id=?",
                       (status, error[:80], time.time(), request_id))

    @asynccontextmanager
    async def admission(self, *, request_id=None, priority=0, scope="", phase="agent", timeout=180, images=0):
        key = request_id or uuid.uuid4().hex
        self.enqueue(key, priority, scope, phase, timeout, images)
        try:
            while not self.claim(key):
                await asyncio.sleep(0.025)
            yield key
        except asyncio.CancelledError:
            self.finish(key, "canceled", "canceled")
            raise
        except BaseException as exc:
            self.finish(key, "failed", type(exc).__name__)
            raise
        else:
            self.finish(key, "completed")
