"""Persistent pacing, checkpoint and transactional at-most-once share ledger."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from zoneinfo import ZoneInfo

RECOVERY_VERSION = "gallery-output-v1"
RECOVERABLE_ERRORS = ("incomplete_douyin_output", "media_domain_rejected", "video_too_long", "media_file_too_large")


class Store:
    def __init__(self, directory, clock=time.time):
        self.directory, self.clock = Path(directory), clock
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "douyin.sqlite3"
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS views(id INTEGER PRIMARY KEY, account TEXT, video TEXT, created REAL);
                CREATE INDEX IF NOT EXISTS views_time ON views(account,created);
                CREATE TABLE IF NOT EXISTS videos(account TEXT, id TEXT, url TEXT, title TEXT, status TEXT,
                    media_id TEXT, analysis TEXT, interest TEXT, error TEXT, updated REAL,
                    PRIMARY KEY(account,id));
                CREATE TABLE IF NOT EXISTS share_ledger(id TEXT PRIMARY KEY, account TEXT, recipient TEXT,
                    video TEXT, url TEXT, state TEXT, day TEXT, created REAL, sent REAL, evidence TEXT);
                CREATE INDEX IF NOT EXISTS share_limits ON share_ledger(account,recipient,created);
                CREATE TABLE IF NOT EXISTS share_decisions(account TEXT,video TEXT,recipient TEXT,reason TEXT,created REAL,
                    PRIMARY KEY(account,video,recipient));
                CREATE TABLE IF NOT EXISTS retry_jobs(account TEXT,video TEXT,version TEXT,state TEXT,
                    previous_error TEXT,created REAL,updated REAL,PRIMARY KEY(account,video,version));
                CREATE TABLE IF NOT EXISTS watch_events(account TEXT,video TEXT,occurred REAL,
                    PRIMARY KEY(account,video));
                CREATE TABLE IF NOT EXISTS watch_admissions(account TEXT,video TEXT,kind TEXT,duration REAL,started REAL,
                    PRIMARY KEY(account,video));
                CREATE INDEX IF NOT EXISTS admissions_time ON watch_admissions(account,kind,started);
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

    def get(self, key, default=None):
        with self.connection() as db:
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def delay(self, config):
        now, account = self.clock(), config.account.douyin_id
        with self.connection() as db:
            rows = db.execute("SELECT created FROM views WHERE account=? AND created>? ORDER BY created",
                (account, now - 3600)).fetchall()
            last = db.execute("SELECT max(created) FROM views WHERE account=?", (account,)).fetchone()[0]
        interval = 3600 / config.hourly_videos
        wait = max(0, (last + interval - now) if last is not None else 0)
        # Rolling-hour ceiling also holds after restart/config frequency change.
        if len(rows) >= config.hourly_videos:
            wait = max(wait, rows[-config.hourly_videos][0] + 3600 - now)
        return wait

    def encounter(self, account, video):
        now = self.clock()
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO watch_events VALUES(?,?,?)", (account, video["id"], now))
            db.execute("INSERT INTO views(account,video,created) VALUES(?,?,?)", (account, video["id"], now))
            db.execute("DELETE FROM views WHERE created<?", (now - 2 * 86400,))
            new = db.execute("INSERT OR IGNORE INTO videos(account,id,url,title,status,updated) "
                "VALUES(?,?,?,?,'pending',?)", (account, video["id"], video["url"], video.get("title", "")[:500], now)).rowcount
            if not new:
                # A previous worker rejected long videos entirely. Retry them
                # only when encountered again with current, visible metadata.
                new = db.execute("UPDATE videos SET status='pending',error='',updated=? "
                    "WHERE account=? AND id=? AND status='unavailable' AND error='video_too_long'",
                    (now, account, video["id"])).rowcount
            return bool(new)

    def observe(self, account, video):
        """A candidate is not a watch: no quota or memory timestamp yet."""
        with self.connection() as db:
            db.execute("INSERT INTO videos(account,id,url,title,status,updated) VALUES(?,?,?,?,'pending',?) "
                "ON CONFLICT(account,id) DO UPDATE SET url=excluded.url,"
                "title=CASE WHEN excluded.title!='' THEN excluded.title ELSE videos.title END",
                (account, video["id"], video["url"], video.get("title", "")[:500], self.clock()))

    def admission(self, account, video_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM watch_admissions WHERE account=? AND video=?", (account, video_id)).fetchone()
            return dict(row) if row else None

    def long_quota(self, config):
        with self.connection() as db:
            used = db.execute("SELECT count(*) FROM watch_admissions WHERE account=? AND kind='long' AND started>?",
                (config.account.douyin_id if config.account else "", self.clock() - 3600)).fetchone()[0]
        return {"limit": config.long_video_limit, "used": used,
                "remaining": max(0, config.long_video_limit - used), "window_seconds": 3600}

    def admit_watch(self, config, video, *, duration=None, gallery=False):
        """Atomically charge a formal watch once, including across resume/restart."""
        from .media import MAX_VIDEO_SECONDS, SHORT_VIDEO_SECONDS
        if not gallery and (isinstance(duration, bool) or not isinstance(duration, (int, float))
                or not math.isfinite(duration) or duration <= 0):
            return "video_duration_unknown"
        if not gallery and duration > MAX_VIDEO_SECONDS:
            return "video_too_long"
        kind = "gallery" if gallery else "long" if duration > SHORT_VIDEO_SECONDS else "short"
        account, now = config.account.douyin_id, self.clock()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM watch_admissions WHERE account=? AND video=?",
                (account, video["id"])).fetchone()
            if previous and previous["kind"] == kind:
                db.execute("UPDATE watch_admissions SET duration=? WHERE account=? AND video=?",
                    (duration, account, video["id"]))
                return ""
            if kind == "long":
                used = db.execute("SELECT count(*) FROM watch_admissions WHERE account=? AND kind='long' AND started>?",
                    (account, now - 3600)).fetchone()[0]
                if used >= config.long_video_limit:
                    return "long_video_quota_exhausted"
            if not previous:
                rows = db.execute("SELECT created FROM views WHERE account=? AND created>? ORDER BY created",
                    (account, now - 3600)).fetchall()
                if len(rows) >= config.hourly_videos or rows and now - rows[-1][0] < 3600 / config.hourly_videos:
                    return "rate_wait"
                db.execute("INSERT INTO views(account,video,created) VALUES(?,?,?)", (account, video["id"], now))
                db.execute("INSERT OR IGNORE INTO watch_events VALUES(?,?,?)", (account, video["id"], now))
                db.execute("DELETE FROM views WHERE created<?", (now - 2 * 86400,))
                if not gallery:
                    # In the same transaction as admission: a crash cannot leave
                    # an old 180-second prefix attached to a new full watch.
                    db.execute("UPDATE videos SET media_id=NULL,analysis=NULL,interest=NULL WHERE account=? AND id=?",
                        (account, video["id"]))
            db.execute("INSERT OR REPLACE INTO watch_admissions VALUES(?,?,?,?,?)",
                (account, video["id"], kind, duration, now if not previous or kind == "long" else previous["started"]))
        return ""

    def skipped_encounter(self, account):
        with self.connection() as db:
            db.execute("INSERT INTO views(account,video,created) VALUES(?,'',?)", (account, self.clock()))
            db.execute("DELETE FROM views WHERE created<?", (self.clock() - 2 * 86400,))

    def watch_time(self, account, video_id):
        with self.connection() as db:
            row = db.execute("SELECT occurred FROM watch_events WHERE account=? AND video=?", (account, video_id)).fetchone()
        if not row:
            # Historical retries lack reliable watch dates; do not invent one from completion time.
            raise ValueError("legacy_watch_time_unknown")
        return row[0]

    def video(self, account, video_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM videos WHERE account=? AND id=?", (account, video_id)).fetchone()
            return dict(row) if row else None

    def save_video(self, account, video_id, status, *, media_id=None, analysis=None, interest=None, error=""):
        with self.connection() as db:
            db.execute("UPDATE videos SET status=?,media_id=coalesce(?,media_id),analysis=coalesce(?,analysis),"
                "interest=coalesce(?,interest),error=?,updated=? WHERE account=? AND id=?", (status, media_id,
                json.dumps(analysis, ensure_ascii=False) if analysis is not None else None,
                json.dumps(interest, ensure_ascii=False) if interest is not None else None,
                error[:80], self.clock(), account, video_id))

    def queue_recoverable(self, account):
        """One recovery per item per mechanism version; preserve analyses and share ledger."""
        queued = []
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in RECOVERABLE_ERRORS)
            rows = db.execute("SELECT id,error FROM videos WHERE account=? AND status='unavailable' "
                f"AND error IN ({placeholders}) ORDER BY updated", (account, *RECOVERABLE_ERRORS)).fetchall()
            for row in rows:
                version = "native-variant-v1" if row["error"] == "media_file_too_large" else RECOVERY_VERSION
                if db.execute("INSERT OR IGNORE INTO retry_jobs VALUES(?,?,?,'queued',?,?,?)",
                        (account, row["id"], version, row["error"], self.clock(), self.clock())).rowcount:
                    db.execute("UPDATE videos SET status='pending',error='',updated=? WHERE account=? AND id=?",
                        (self.clock(), account, row["id"]))
                    queued.append(row["id"])
        return queued

    def next_retry(self, account):
        with self.connection() as db:
            row = db.execute("SELECT v.* FROM retry_jobs r JOIN videos v ON v.account=r.account AND v.id=r.video "
                "WHERE r.account=? AND r.state='queued' ORDER BY "
                "CASE r.previous_error WHEN 'incomplete_douyin_output' THEN 0 WHEN 'media_domain_rejected' THEN 1 ELSE 2 END,"
                "r.created,r.video LIMIT 1",
                (account,)).fetchone()
            return dict(row) if row else None

    def finish_retry(self, account, video_id):
        with self.connection() as db:
            row = db.execute("SELECT status FROM videos WHERE account=? AND id=?", (account, video_id)).fetchone()
            state = "failed" if not row or row[0] == "unavailable" else "done"
            db.execute("UPDATE retry_jobs SET state=?,updated=? WHERE account=? AND video=? AND state='queued'",
                (state, self.clock(), account, video_id))

    def retries(self, account):
        with self.connection() as db:
            rows = db.execute("SELECT r.*,v.status,v.error FROM retry_jobs r JOIN videos v "
                "ON v.account=r.account AND v.id=r.video WHERE r.account=? ORDER BY r.created,r.rowid", (account,))
            return list({row["video"]: dict(row) for row in rows}.values())

    def reserve_share(self, config, friend, video, *, approved=False):
        """A pending/uncertain send blocks that recipient until reconciled."""
        now, account = self.clock(), config.account.douyin_id
        day = datetime.fromtimestamp(now, ZoneInfo(config.timezone)).date().isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            allowed = next((f for f in config.friends if f.douyin_id == friend and f.opted_in), None)
            if not config.enabled or not config.share_enabled or not allowed or not config.awake(now):
                return None, "disabled_or_not_allowed"
            if config.draft_only and not approved:
                return None, "draft_only"
            if db.execute("SELECT 1 FROM share_ledger WHERE account=? AND recipient=? AND state IN ('sending','uncertain')",
                          (account, friend)).fetchone():
                return None, "unresolved_send"
            count = db.execute("SELECT count(*) FROM share_ledger WHERE account=? AND recipient=? AND day=? AND state='sent'",
                               (account, friend, day)).fetchone()[0]
            if count >= config.daily_share_limit:
                return None, "daily_limit"
            last = db.execute("SELECT max(sent) FROM share_ledger WHERE account=? AND recipient=? AND state='sent'",
                              (account, friend)).fetchone()[0]
            if last is not None and now - last < config.share_cooldown_seconds:
                return None, "cooldown_skipped"
            if db.execute("SELECT 1 FROM share_ledger WHERE account=? AND recipient=? AND video=? AND created>? "
                          "AND state IN ('sent','sending','uncertain')", (account, friend, video["id"],
                          now - config.dedup_days * 86400)).fetchone():
                return None, "duplicate"
            key = hashlib.sha256(json.dumps([account, friend, video["id"], day]).encode()).hexdigest()
            if db.execute("SELECT 1 FROM share_ledger WHERE id=?", (key,)).fetchone():
                return None, "already_attempted"
            db.execute("INSERT INTO share_ledger VALUES(?,?,?,?,?,'sending',?,?,NULL,'')",
                       (key, account, friend, video["id"], video["url"], day, now))
            return key, "reserved"

    def finish_share(self, key, status, evidence=""):
        if status not in {"sent", "uncertain", "failed"}:
            raise ValueError("invalid_share_status")
        with self.connection() as db:
            db.execute("UPDATE share_ledger SET state=?,sent=?,evidence=? WHERE id=? AND state IN ('sending','uncertain')",
                       (status, self.clock() if status == "sent" else None, evidence[:200], key))

    def recover(self):
        with self.connection() as db:
            db.execute("UPDATE share_ledger SET state='uncertain' WHERE state='sending'")

    def shares(self, limit=100):
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM share_ledger ORDER BY created DESC LIMIT ?", (limit,))]

    def share_decision(self, account, video, recipient, reason):
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO share_decisions VALUES(?,?,?,?,?)", (account, video, recipient, reason, self.clock()))

    def revoke_memory(self, account, video_id):
        from integrations.qq_astrbot.plugin.memory_repository import MemoryRepository, identity
        row = self.video(account, video_id)
        if not row:
            raise ValueError("unknown_video")
        refs = self.get("memory_owners:" + account + ":" + video_id, [])
        repo = MemoryRepository(self.directory / "memories.sqlite3")
        from integrations.qq_astrbot.plugin.shared_memory import SharedMemory
        shared = SharedMemory(repo.path, clock=self.clock)
        for key, character in refs:
            shared.withdraw(key, reason="video_revoked")
        with repo.connection() as db:
            for owner, character in refs:
                db.execute("UPDATE memory_items SET deleted_at=?,updated_at=? WHERE id=?",
                    (self.clock(), self.clock(), identity(character, owner, row["url"])))
        self.save_video(account, video_id, "revoked")
        shared.scrub_backups()
