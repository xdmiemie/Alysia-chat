"""Durable private-chat plans, randomized rounds and candidate-only pity draws."""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
import json
import math
from pathlib import Path
import random
import sqlite3
import time
import uuid


DEFAULTS = {
    "context_plans_enabled": True, "context_plan_limit": 5,
    "periodic_proactive_enabled": True, "periodic_interval_minutes": 120,
    "periodic_jitter_minutes": 20,
    "followup_average_probability": .30, "followup_pity_limit": 5,
}


def normalize_settings(values):
    result = {key: values.get(key, default) for key, default in DEFAULTS.items()}
    for key in ("context_plans_enabled", "periodic_proactive_enabled"):
        if type(result[key]) is not bool:
            raise ValueError(key + " must be boolean")
    for key in ("context_plan_limit", "followup_pity_limit"):
        if type(result[key]) is not int or not 1 <= result[key] <= 100:
            raise ValueError(key + " must be an integer from 1 to 100")
    for key in ("periodic_interval_minutes", "periodic_jitter_minutes", "followup_average_probability"):
        if type(result[key]) not in (int, float) or not math.isfinite(result[key]):
            raise ValueError(key + " must be finite")
    if not 1 <= result["periodic_interval_minutes"] <= 10080:
        raise ValueError("periodic_interval_minutes must be from 1 to 10080")
    result["periodic_jitter_minutes"] = max(0, min(result["periodic_jitter_minutes"], result["periodic_interval_minutes"] - 1))
    result["followup_average_probability"] = max(1 / result["followup_pity_limit"], min(1, result["followup_average_probability"]))
    return result


@lru_cache(maxsize=128)
def probability_curve(average=.30, pity=5):
    """Increasing conditional probabilities calibrated to 1/E[draws per hit]."""
    average = max(1 / pity, min(1, average))
    if average <= 1 / pity:
        return (0.0,) * (pity - 1) + (1.0,)
    def expected(scale):
        survival, total = 1.0, 1.0
        for index in range(1, pity):
            survival *= 1 - min(1, scale * index)
            total += survival
        return total
    low, high = 0.0, 1.0
    for _ in range(70):
        middle = (low + high) / 2
        if expected(middle) > 1 / average:
            low = middle
        else:
            high = middle
    return tuple(min(1.0, high * index) for index in range(1, pity)) + (1.0,)


class ProactiveStore:
    def __init__(self, directory, *, clock=time.time, rng=None):
        self.path = Path(directory) / "proactive.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock, self.rng = clock, rng or random.SystemRandom()
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS peers(scope TEXT PRIMARY KEY, origin TEXT, transport TEXT,
                    platform TEXT, bot TEXT, recipient TEXT, version INTEGER DEFAULT 0,
                    last_user REAL DEFAULT 0, message_id TEXT DEFAULT '', message TEXT DEFAULT '',
                    planned_version INTEGER DEFAULT 0, plan_retry REAL DEFAULT 0, next_periodic REAL);
                CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, scope TEXT, kind TEXT, topic TEXT,
                    purpose TEXT, due REAL, status TEXT, source_version INTEGER, created REAL,
                    updated REAL, text TEXT DEFAULT '', receipt TEXT DEFAULT '', error TEXT DEFAULT '',
                    history_saved INTEGER DEFAULT 0);
                CREATE INDEX IF NOT EXISTS plan_due ON plans(status,due);
                CREATE TABLE IF NOT EXISTS pity(scope TEXT PRIMARY KEY, misses INTEGER);
                CREATE TABLE IF NOT EXISTS draws(id TEXT PRIMARY KEY,scope TEXT,number INTEGER,
                    probability REAL,hit INTEGER,created REAL);
                CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY,value TEXT);
                CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,scope TEXT,kind TEXT,detail TEXT,created REAL);
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

    def interval(self, config):
        return 60 * (config["periodic_interval_minutes"] + self.rng.uniform(
            -config["periodic_jitter_minutes"], config["periodic_jitter_minutes"]))

    def audit(self, scope, kind, detail):
        with self.connection() as db:
            db.execute("INSERT INTO audit(scope,kind,detail,created) VALUES(?,?,?,?)",
                (scope, kind, json.dumps(detail, ensure_ascii=False), self.clock()))

    def configure(self, config):
        config = normalize_settings(config)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM settings WHERE id=1").fetchone()
            old = json.loads(row[0]) if row else {}
            periodic_changed = any(old.get(k) != config[k] for k in (
                "periodic_proactive_enabled", "periodic_interval_minutes", "periodic_jitter_minutes"))
            if periodic_changed:
                for peer in db.execute("SELECT scope FROM peers").fetchall():
                    due = self.clock() + self.interval(config) if config["periodic_proactive_enabled"] else None
                    db.execute("UPDATE peers SET next_periodic=? WHERE scope=?", (due, peer[0]))
                db.execute("UPDATE plans SET status='canceled',updated=? WHERE kind='periodic' AND status='pending'", (self.clock(),))
            if not config["context_plans_enabled"]:
                db.execute("UPDATE peers SET planned_version=version")
            db.execute("INSERT OR REPLACE INTO settings VALUES(1,?)", (json.dumps(config),))
        return config

    def observe(self, binding, message_id, text, config, *, received=None, seed=False):
        now = self.clock() if received is None else received
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            exists = db.execute("SELECT message_id FROM peers WHERE scope=?", (binding['scope'],)).fetchone()
            due = self.clock() + self.interval(config) if config['periodic_proactive_enabled'] else None
            db.execute("INSERT OR IGNORE INTO peers(scope,origin,transport,platform,bot,recipient,next_periodic) VALUES(?,?,?,?,?,?,?)",
                tuple(binding[k] for k in ('scope','origin','transport','platform','bot','recipient')) + (due,))
            if seed or (exists and exists[0] == message_id):
                return
            db.execute("UPDATE peers SET origin=?,transport=?,version=version+1,last_user=?,message_id=?,message=?,plan_retry=0 WHERE scope=?",
                (binding['origin'], binding['transport'], now, message_id, text[:12000], binding['scope']))
            if not config['context_plans_enabled']:
                db.execute("UPDATE peers SET planned_version=version WHERE scope=?", (binding['scope'],))

    def peer(self, scope):
        with self.connection() as db:
            row = db.execute("SELECT * FROM peers WHERE scope=?", (scope,)).fetchone()
            return dict(row) if row else None

    def pending(self, scope=None):
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM plans WHERE status='pending' AND kind='context'" +
                (" AND scope=?" if scope else "") + " ORDER BY due", (scope,) if scope else ())]

    def planner_peer(self):
        with self.connection() as db:
            row = db.execute("SELECT * FROM peers WHERE version>planned_version AND plan_retry<=? ORDER BY last_user LIMIT 1", (self.clock(),)).fetchone()
            return dict(row) if row else None

    def apply_plans(self, peer, actions, limit):
        outcomes = []
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT version FROM peers WHERE scope=?", (peer['scope'],)).fetchone()
            if not current or current[0] != peer['version']:
                return ['superseded']
            for action in actions:
                row = db.execute("SELECT id FROM plans WHERE scope=? AND kind='context' AND topic=? AND status='pending'",
                    (peer['scope'], action['topic'])).fetchone()
                if action['action'] == 'cancel':
                    if row:
                        db.execute("UPDATE plans SET status='canceled',updated=? WHERE id=?", (self.clock(), row[0]))
                    outcomes.append('canceled' if row else 'absent')
                    continue
                if row:
                    db.execute("UPDATE plans SET purpose=?,due=?,source_version=?,updated=? WHERE id=?",
                        (action['purpose'], action['due'], peer['version'], self.clock(), row[0]))
                    outcomes.append('updated')
                    continue
                count = db.execute("SELECT count(*) FROM plans WHERE scope=? AND kind='context' AND status='pending'", (peer['scope'],)).fetchone()[0]
                if count >= limit:
                    outcomes.append('queue_full_skipped')
                    continue
                self._insert(db, peer['scope'], 'context', action['topic'], action['purpose'], action['due'], peer['version'])
                outcomes.append('scheduled')
            db.execute("UPDATE peers SET planned_version=?,plan_retry=0 WHERE scope=?", (peer['version'], peer['scope']))
        self.audit(peer['scope'], 'planning', outcomes)
        return outcomes

    def retry_planner(self, scope):
        with self.connection() as db:
            db.execute("UPDATE peers SET plan_retry=? WHERE scope=?", (self.clock() + 60, scope))

    def _insert(self, db, scope, kind, topic, purpose, due, version):
        key = uuid.uuid4().hex
        db.execute("INSERT INTO plans(id,scope,kind,topic,purpose,due,status,source_version,created,updated) VALUES(?,?,?,?,?,?,'pending',?,?,?)",
            (key, scope, kind, topic, purpose, due, version, self.clock(), self.clock()))
        return key

    def due(self, config):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if config['periodic_proactive_enabled']:
                for peer in db.execute("SELECT * FROM peers WHERE next_periodic<=?", (self.clock(),)).fetchall():
                    # One overdue round after downtime; never replay a backlog.
                    if not db.execute("SELECT 1 FROM plans WHERE scope=? AND kind='periodic' AND status='pending'", (peer['scope'],)).fetchone():
                        self._insert(db, peer['scope'], 'periodic', '', '', peer['next_periodic'], peer['version'])
                    db.execute("UPDATE peers SET next_periodic=? WHERE scope=?", (self.clock() + self.interval(config), peer['scope']))
            kinds = []
            if config['context_plans_enabled']: kinds.append('context')
            if config['periodic_proactive_enabled']: kinds.append('periodic')
            if not kinds:
                return None
            row = db.execute("SELECT * FROM plans WHERE status='pending' AND due<=? AND kind IN (" +
                ','.join('?' for _ in kinds) + ") ORDER BY due LIMIT 1", (self.clock(), *kinds)).fetchone()
            return dict(row) if row else None

    def finish(self, key, status, *, text='', receipt='', error=''):
        with self.connection() as db:
            db.execute("UPDATE plans SET status=?,updated=?,text=?,receipt=?,error=? WHERE id=?",
                (status, self.clock(), text, receipt, error, key))

    def reserve_send(self, plan, peer, text):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT version FROM peers WHERE scope=?", (peer['scope'],)).fetchone()
            if not current or current[0] != peer['version']:
                return False
            return bool(db.execute("UPDATE plans SET status='sending',text=?,updated=? WHERE id=? AND status='pending'",
                (text, self.clock(), plan['id'])).rowcount)

    def recover(self):
        with self.connection() as db:
            db.execute("UPDATE plans SET status='uncertain',error='interrupted_send',updated=? WHERE status='sending'", (self.clock(),))

    def draw(self, scope, candidate_id, average, pity):
        curve = probability_curve(average, pity)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT number,probability,hit FROM draws WHERE id=? AND scope=?", (candidate_id, scope)).fetchone()
            if previous:
                return dict(previous)
            row = db.execute("SELECT misses FROM pity WHERE scope=?", (scope,)).fetchone()
            number = min(pity, (row[0] if row else 0) + 1)
            probability = curve[number - 1]
            hit = self.rng.random() < probability
            db.execute("INSERT OR REPLACE INTO pity VALUES(?,?)", (scope, 0 if hit else number))
            db.execute("INSERT INTO draws VALUES(?,?,?,?,?,?)", (candidate_id, scope, number, probability, int(hit), self.clock()))
            return {'number': number, 'probability': probability, 'hit': int(hit)}

    def recent(self, scope, limit=5):
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT kind,text,updated FROM plans WHERE scope=? AND status='sent' ORDER BY updated DESC LIMIT ?", (scope, limit))]
