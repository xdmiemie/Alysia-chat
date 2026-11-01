"""Authorized assets, versioned analyses and restartable video jobs."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import os
import sqlite3
import time
import uuid
import psutil

from .vision_media import MediaError


class VisionStore:
    def __init__(self, directory, config):
        self.directory = Path(directory)
        self.cache = self.directory / "media_cache/vision"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "media_analysis.sqlite3"
        self.config = config
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    digest TEXT, kind TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS media_analysis(cache_key TEXT PRIMARY KEY, media_id TEXT,
                    owner TEXT NOT NULL, result TEXT NOT NULL, created REAL);
                CREATE TABLE IF NOT EXISTS video_jobs(id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    media_id TEXT NOT NULL, source TEXT, question TEXT, origin TEXT, account TEXT,
                    status TEXT, attempts INTEGER DEFAULT 0, next_attempt REAL DEFAULT 0,
                    created REAL, deadline REAL, result TEXT, error TEXT,
                    worker_pid INTEGER, worker_created REAL);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(self, data, owner, kind):
        if not owner or kind not in {"image", "sticker", "gif", "video"}:
            raise MediaError("invalid_media_owner")
        limit = self.config["max_video_bytes"] if kind == "video" else self.config["max_image_bytes"]
        if not 0 < len(data) <= limit:
            raise MediaError("media_file_too_large")
        digest = hashlib.sha256(data).hexdigest()
        key = hashlib.sha256((owner + ":" + kind + ":" + digest).encode()).hexdigest()
        path = self.cache / key
        if not path.exists():
            temp = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
            temp.write_bytes(data)
            temp.replace(path)
        with self.connection() as db:
            db.execute("INSERT INTO assets VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET created=excluded.created",
                (key, owner, digest, kind, time.time()))
        return key

    def asset(self, key, owner):
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
            raise MediaError("invalid_media_id")
        with self.connection() as db:
            row = db.execute("SELECT * FROM assets WHERE id=? AND owner=?", (key, owner)).fetchone()
        if not row or row["created"] + self.config["cache_ttl_seconds"] < time.time():
            raise MediaError("media_missing_or_expired")
        path = self.cache / key
        if not path.is_file():
            raise MediaError("media_missing_or_expired")
        return path, dict(row)

    def cached(self, cache_key, owner):
        with self.connection() as db:
            row = db.execute("SELECT result FROM media_analysis WHERE cache_key=? AND owner=?", (cache_key, owner)).fetchone()
            return json.loads(row[0]) if row else None

    def save(self, cache_key, key, owner, result):
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO media_analysis VALUES(?,?,?,?,?)",
                       (cache_key, key, owner, json.dumps(result, ensure_ascii=False), time.time()))

    def submit(self, key, owner, question, *, source="", origin="", account="", request_id=""):
        self.asset(key, owner)
        identity = json.dumps([key, owner, question, request_id], ensure_ascii=False)
        job_id = hashlib.sha256(identity.encode()).hexdigest()
        now = time.time()
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO video_jobs(id,owner,media_id,source,question,origin,account,status,created,deadline) "
                "VALUES(?,?,?,?,?,?,?,'pending',?,?)", (job_id, owner, key, source[:2048], question[:4000], origin,
                    account, now, now + self.config["video_job_timeout_seconds"]))
        return job_id

    def job(self, job_id, owner):
        with self.connection() as db:
            row = db.execute("SELECT * FROM video_jobs WHERE id=? AND owner=?", (job_id, owner)).fetchone()
        if not row:
            raise MediaError("media_job_not_found")
        return dict(row)

    def cancel(self, job_id, owner):
        self.job(job_id, owner)
        with self.connection() as db:
            return db.execute("UPDATE video_jobs SET status='canceled',error='operator_canceled' "
                "WHERE id=? AND owner=? AND status IN ('pending','running','completed')",
                (job_id, owner)).rowcount == 1

    def recover(self):
        # API and plugin can share storage. Reclaim only a dead process's job.
        with self.connection() as db:
            for row in db.execute("SELECT id,status,worker_pid,worker_created FROM video_jobs WHERE status IN ('running','sending')").fetchall():
                try:
                    alive = row["worker_pid"] and abs(psutil.Process(row["worker_pid"]).create_time() - (row["worker_created"] or 0)) < 1
                except psutil.NoSuchProcess:
                    alive = False
                except (psutil.AccessDenied, OSError):
                    alive = True
                if not alive:
                    status = "uncertain" if row["status"] == "sending" else "pending"
                    db.execute("UPDATE video_jobs SET status=?,error='owner_exited' WHERE id=?", (status, row["id"]))

    def next_job(self, *, api_only=False):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            db.execute("UPDATE video_jobs SET status='failed',error='deadline' WHERE status IN ('pending','running') AND deadline<?", (now,))
            row = db.execute("SELECT * FROM video_jobs WHERE status='pending' AND next_attempt<=? AND attempts<? "
                             + ("AND origin='' " if api_only else "") + "ORDER BY created LIMIT 1",
                             (now, self.config["max_attempts"])).fetchone()
            if row:
                db.execute("UPDATE video_jobs SET status='running',attempts=attempts+1,worker_pid=?,worker_created=? WHERE id=?",
                           (os.getpid(), psutil.Process().create_time(), row["id"]))
                return dict(row, attempts=row["attempts"] + 1)

    def awaiting_delivery(self):
        with self.connection() as db:
            row = db.execute("SELECT * FROM video_jobs WHERE status='completed' AND origin!='' ORDER BY created LIMIT 1").fetchone()
            return dict(row) if row else None

    def finish_job(self, key, result):
        with self.connection() as db:
            db.execute("UPDATE video_jobs SET status='completed',result=?,error=NULL WHERE id=? AND status='running'",
                       (json.dumps(result, ensure_ascii=False), key))

    def fail_job(self, job, error):
        with self.connection() as db:
            db.execute("UPDATE video_jobs SET status=?,error=?,next_attempt=? WHERE id=? AND status NOT IN ('sent','uncertain','canceled')", (
                "pending" if job["attempts"] < self.config["max_attempts"] and time.time() < job["deadline"] else "failed",
                error[:80], time.time() + 2 ** job["attempts"], job["id"]))

    def claim_send(self, key):
        with self.connection() as db:
            return db.execute("UPDATE video_jobs SET status='sending',worker_pid=?,worker_created=? WHERE id=? AND status='completed'",
                (os.getpid(), psutil.Process().create_time(), key)).rowcount == 1

    def delivery(self, key, status):
        with self.connection() as db:
            db.execute("UPDATE video_jobs SET status=? WHERE id=? AND status='sending'", (status, key))

    def cleanup(self):
        with self.connection() as db:
            rows = db.execute("SELECT id FROM assets WHERE created<? AND id NOT IN "
                "(SELECT media_id FROM video_jobs WHERE status IN ('pending','running'))",
                (time.time() - self.config["cache_ttl_seconds"],)).fetchall()
            for row in rows:
                (self.cache / row[0]).unlink(missing_ok=True)
                db.execute("DELETE FROM assets WHERE id=?", (row[0],))
