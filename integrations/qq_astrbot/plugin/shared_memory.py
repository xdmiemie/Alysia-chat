"""Shared memory storage. No model, tools or outbound transport in this layer.

Content identity is independent of its short/long visibility. Events and edges
are normalized; consumers and leased jobs are separate, transactional ledgers.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import re
from pathlib import Path
import shutil
import sqlite3
import time
import uuid
from zoneinfo import ZoneInfo

from .memory_repository import identity, search_terms
from .shared_memory_config import PARAMETERS, defaults, read_config, validate_config

SCHEMA_VERSION = 1

# Only internal codes may be persisted; arbitrary exception messages can contain
# private messages or model output (notably jsonschema.ValidationError).
JOB_ERROR_CODES = frozenset({
    "summary_character_limit", "summary_token_limit", "empty_summary",
    "special_missing_real_agreement", "special_agreement_precedes_intent",
    "special_unsupported_quote", "special_summary_too_long", "invalid_existing_special",
    "special_cannot_replace_other_attribute", "memory_unexpected_tool_call", "memory_incomplete_output",
})


def canonical_video_id(value):
    match = re.search(r"(?:douyin:|/video/|/note/|modal_id=)(\d+)", str(value))
    return "douyin:" + match[1] if match else str(value).split("?")[0].rstrip("/")


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class MemoryConflict(ValueError):
    pass


class MemoryPressure(OSError):
    pass


@dataclass(frozen=True)
class ModuleDescriptor:
    module_id: str
    name: str
    types: tuple[str, ...]
    parameters: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ("kind", "visibility", "scope", "day")
    version: int = 1


@dataclass(frozen=True)
class RequestContext:
    character: str
    bot: str
    module: str
    scope: str = ""
    internal: bool = False
    query: str = ""
    purpose: str = "reply"
    activity: str = ""
    allowed_ids: tuple[str, ...] | None = None
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)


class SharedMemory:
    def __init__(self, path, *, clock=time.time, register=True, initial_timezone=None):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.modules, self.policies = {}, {}
        self._stats_cache = {}
        self._initialize()
        with self.connection() as db:
            initialized = db.execute("SELECT 1 FROM sm_meta WHERE key='timezone_initialized'").fetchone()
        if not initialized:
            if initial_timezone and initial_timezone != "system":
                self.configure({"timezone": initial_timezone})
            with self.connection() as db:
                db.execute("INSERT OR IGNORE INTO sm_meta VALUES('timezone_initialized','true')")
        if register:
            from .memory_policies import register_builtin_modules
            register_builtin_modules(self)
            self._migrate_pending_sources()

    def _migrate_pending_sources(self):
        with self.connection() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_inbox'").fetchone():
                return
            rows = [dict(r) for r in db.execute("SELECT * FROM memory_inbox WHERE status='legacy_pending'")]
        from .memory_repository import owner_bot
        for row in rows:
            try:
                payload = json.loads(row["payload"])
                if not isinstance(payload.get("text"), str) or not payload["text"].strip():
                    continue
                self.append_event(bot=owner_bot(row["owner_key"]), character=row["character_id"], module="qq",
                    event_id=row["source_ref"], scope=row["owner_key"], subject=row["sender_key"],
                    payload={"text": payload["text"], "legacy_source": True}, occurred=row["created_at"])
            except (ValueError, TypeError):
                continue  # Unknown provenance stays visibly quarantined; do not invent it.
            except MemoryPressure:
                break  # Keep legacy source for a later recovery pass; normal QQ stays available.
            with self.connection() as db:
                db.execute("UPDATE memory_inbox SET status='migrated_short_source',payload='{}' WHERE id=?", (row["id"],))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA secure_delete=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _initialize(self):
        with self.connection() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='sm_meta'").fetchone()
            legacy = db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_items'").fetchone()
            if not exists and legacy:
                # SQLite backup includes committed WAL pages; never copy a live .db alone.
                backup = self.path.with_name(self.path.name + ".pre-shared-v1.bak")
                if not backup.exists():
                    with sqlite3.connect(backup) as target:
                        db.backup(target)
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sm_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sm_modules(id TEXT PRIMARY KEY,descriptor TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sm_epochs(bot TEXT,character TEXT,version INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(bot,character));
                CREATE TABLE IF NOT EXISTS sm_events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,bot TEXT NOT NULL,
                    character TEXT NOT NULL,module TEXT NOT NULL,scope TEXT NOT NULL,subject TEXT NOT NULL,
                    role TEXT NOT NULL,payload TEXT NOT NULL,occurred REAL NOT NULL,day TEXT NOT NULL,
                    created REAL NOT NULL,retention TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
                    version INTEGER NOT NULL DEFAULT 1,internal_allowed INTEGER NOT NULL DEFAULT 1,
                    visibility TEXT NOT NULL DEFAULT 'session');
                CREATE INDEX IF NOT EXISTS sm_event_pending ON sm_events(character,status,scope,day,seq);
                CREATE TABLE IF NOT EXISTS sm_records(
                    id TEXT PRIMARY KEY,bot TEXT NOT NULL,character TEXT NOT NULL,module TEXT NOT NULL,
                    kind TEXT NOT NULL,event_id TEXT NOT NULL,material_id TEXT NOT NULL,scope TEXT NOT NULL,
                    subject TEXT NOT NULL,visibility TEXT NOT NULL,retention TEXT NOT NULL,
                    internal_allowed INTEGER NOT NULL,body TEXT NOT NULL,hash TEXT NOT NULL,version INTEGER NOT NULL,
                    occurred REAL NOT NULL,created REAL NOT NULL,day TEXT NOT NULL,start REAL NOT NULL,end REAL NOT NULL,
                    short INTEGER NOT NULL DEFAULT 1,long INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'active',
                    derived INTEGER NOT NULL DEFAULT 0,experience INTEGER NOT NULL DEFAULT 1,
                    policy_version INTEGER NOT NULL,generation INTEGER NOT NULL DEFAULT 0,
                    retired REAL,metadata TEXT NOT NULL DEFAULT '{}',UNIQUE(bot,character,module,event_id,kind));
                CREATE INDEX IF NOT EXISTS sm_recall ON sm_records(bot,character,status,visibility,scope);
                CREATE INDEX IF NOT EXISTS sm_day ON sm_records(bot,character,module,day,kind);
                CREATE TABLE IF NOT EXISTS sm_edges(child TEXT,parent TEXT,PRIMARY KEY(child,parent));
                CREATE INDEX IF NOT EXISTS sm_parent ON sm_edges(parent);
                CREATE TABLE IF NOT EXISTS sm_consumers(record_id TEXT,consumer TEXT,state TEXT NOT NULL DEFAULT 'pending',
                    reason TEXT NOT NULL DEFAULT '',updated REAL NOT NULL,PRIMARY KEY(record_id,consumer));
                CREATE TABLE IF NOT EXISTS sm_jobs(id TEXT PRIMARY KEY,bot TEXT,character TEXT,module TEXT,operation TEXT,
                    day TEXT,state TEXT NOT NULL,snapshot TEXT NOT NULL,epoch INTEGER NOT NULL,lease REAL NOT NULL,
                    token TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,retry_at REAL NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',result TEXT NOT NULL DEFAULT '',updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS sm_pins(job TEXT,record_id TEXT,PRIMARY KEY(job,record_id));
                CREATE TABLE IF NOT EXISTS sm_batches(id TEXT PRIMARY KEY,bot TEXT,character TEXT,module TEXT,
                    day TEXT,kind TEXT,summary_id TEXT,active INTEGER,generation INTEGER,created REAL,retired REAL);
                CREATE TABLE IF NOT EXISTS sm_members(batch TEXT,record_id TEXT,hash TEXT,PRIMARY KEY(batch,record_id));
                CREATE TABLE IF NOT EXISTS sm_usage(id TEXT PRIMARY KEY,created REAL,character TEXT,module TEXT,scope TEXT,payload TEXT);
                CREATE TABLE IF NOT EXISTS sm_governance(bot TEXT,character TEXT,scope TEXT,retention TEXT,
                    PRIMARY KEY(bot,character,scope));
                CREATE TABLE IF NOT EXISTS sm_subject_rules(bot TEXT,character TEXT,scope TEXT,subject TEXT,retention TEXT,
                    PRIMARY KEY(bot,character,scope,subject));
                CREATE TABLE IF NOT EXISTS sm_tombstones(id TEXT PRIMARY KEY,created REAL,reason TEXT);
                CREATE TABLE IF NOT EXISTS sm_days(bot TEXT,character TEXT,day TEXT,state TEXT,input_version TEXT,
                    updated REAL,PRIMARY KEY(bot,character,day));
                CREATE TABLE IF NOT EXISTS sm_tokens(hash TEXT,model TEXT,tokens INTEGER,PRIMARY KEY(hash,model));
            ''')
            version = db.execute("SELECT value FROM sm_meta WHERE key='schema_version'").fetchone()
            if version and int(version[0]) != SCHEMA_VERSION:
                raise ValueError("unsupported_shared_memory_schema")
            db.execute("INSERT OR IGNORE INTO sm_meta VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
            db.execute("INSERT OR IGNORE INTO sm_meta VALUES('config',?)", (encoded(defaults()),))
            if not exists:
                db.execute("INSERT OR REPLACE INTO sm_meta VALUES('migration',?)", (encoded({
                    "at": self.clock(), "legacy": bool(legacy), "boundary": "event-local-midnight",
                    "legacy_boundary": "23:30-to-23:30; historical diaries not replayed as new experiences",
                    "policy": "legacy read-only, original ACL; no invented dates or generations"}),))
                if legacy:
                    # Preserve every old body/ACL. Quarantine old extraction work.
                    db.execute("UPDATE memory_inbox SET status='legacy_pending',lease_until=0 WHERE status!='done'")
            if legacy:
                for table in ("memory_items", "diary_entries"):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS sm_no_insert_{table} BEFORE INSERT ON {table} "
                               "BEGIN SELECT RAISE(ABORT,'legacy_long_term_write_disabled'); END")

    @property
    def config(self):
        with self.connection() as db:
            return read_config(db)

    def configure(self, patch):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = read_config(db)
            config = validate_config({**previous, **patch})
            if config["timezone"] != previous["timezone"] and db.execute("SELECT 1 FROM sm_events UNION SELECT 1 FROM sm_records LIMIT 1").fetchone():
                raise ValueError("timezone_change_requires_explicit_date_migration")
            db.execute("UPDATE sm_meta SET value=? WHERE key='config'", (encoded(config),))
            for row in db.execute("SELECT bot,character FROM sm_epochs").fetchall():
                self.bump(db, *row)
        self._stats_cache.clear()
        return config

    def register_module(self, descriptor, policy):
        if descriptor.module_id in self.modules:
            raise ValueError("duplicate_memory_module")
        if not descriptor.types or any(k not in PARAMETERS for k in descriptor.parameters):
            raise ValueError("invalid_module_descriptor")
        self.modules[descriptor.module_id] = descriptor
        self.policies[descriptor.module_id] = policy
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO sm_modules VALUES(?,?)", (descriptor.module_id, encoded(descriptor.__dict__)))

    def day(self, timestamp):
        return datetime.fromtimestamp(timestamp, ZoneInfo(self.config["timezone"])).date().isoformat()

    @staticmethod
    def bump(db, bot, character):
        db.execute("INSERT INTO sm_epochs VALUES(?,?,1) ON CONFLICT(bot,character) DO UPDATE SET version=version+1", (bot, character))

    @staticmethod
    def epoch(db, bot, character):
        row = db.execute("SELECT version FROM sm_epochs WHERE bot=? AND character=?", (bot, character)).fetchone()
        return row[0] if row else 0

    @staticmethod
    def generation(db, bot, character, module):
        row = db.execute("SELECT value FROM sm_meta WHERE key=?", ("generation:" + identity(bot, character, module),)).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def set_generation(db, bot, character, module, generation):
        db.execute("INSERT OR REPLACE INTO sm_meta VALUES(?,?)", ("generation:" + identity(bot, character, module), str(generation)))

    def retention(self, bot, character, scope, subject=None):
        with self.connection() as db:
            row = db.execute("SELECT retention FROM sm_governance WHERE bot=? AND character=? AND scope=?", (bot, character, scope)).fetchone()
            personal = db.execute("SELECT retention FROM sm_subject_rules WHERE bot=? AND character=? AND scope=? AND subject=?", (bot, character, scope, subject)).fetchone() if subject else None
        rank = {"normal": 0, "no_long_term": 1, "no_store": 2}
        return max([r[0] for r in (row, personal) if r] or ["normal"], key=rank.get)

    def _capacity(self, db, added):
        size = db.execute("SELECT coalesce(sum(length(CAST(body AS BLOB))),0) FROM sm_records WHERE short=1").fetchone()[0]
        size += db.execute("SELECT coalesce(sum(length(CAST(payload AS BLOB))),0) FROM sm_events WHERE status='pending'").fetchone()[0]
        if size + added > read_config(db)["hard_bytes"] or shutil.disk_usage(self.path.parent).free < 16 * 1024 * 1024:
            raise MemoryPressure("memory_hard_capacity_exhausted")

    def append_event(self, *, bot, character, module, event_id, scope, subject, payload,
                     occurred=None, role="user", retention=None, internal_allowed=True, visibility="session"):
        if module not in self.modules or role not in {"user", "assistant", "receipt", "source"}:
            raise ValueError("invalid_source_event")
        occurred = self.clock() if occurred is None else occurred
        retention = retention or self.retention(bot, character, scope, subject)
        if role != "user":
            with self.connection() as db:
                restrictions = [r[0] for r in db.execute("SELECT retention FROM sm_subject_rules WHERE bot=? AND character=? AND scope=?", (bot, character, scope))]
            retention = max([retention, *restrictions], key={"normal": 0, "no_long_term": 1, "no_store": 2}.get)
        key = identity("event", bot, character, module, scope, event_id)
        if retention not in {"normal", "no_long_term", "no_store"}:
            raise ValueError("invalid_retention")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM sm_tombstones WHERE id=?", (key,)).fetchone():
                return None
            if retention == "no_store":
                db.execute("INSERT OR IGNORE INTO sm_tombstones VALUES(?,?,?)", (key, self.clock(), "no_store"))
                return None
            if db.execute("SELECT 1 FROM sm_events WHERE id=?", (key,)).fetchone():
                return key
            raw = encoded(payload)
            self._capacity(db, len(raw.encode()))
            db.execute("INSERT INTO sm_events(id,bot,character,module,scope,subject,role,payload,occurred,day,created,retention,internal_allowed,visibility) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (key, bot, character, module, scope, subject, role, raw, occurred,
                        self.day(occurred), self.clock(), retention, int(internal_allowed), visibility))
            db.execute("INSERT OR IGNORE INTO sm_epochs VALUES(?,?,0)", (bot, character))
            db.execute("UPDATE sm_days SET state='dirty' WHERE bot=? AND character=? AND day=?", (bot, character, self.day(occurred)))
        return key

    def append_short_term(self, *, bot, character, module, kind, event_id, body, scope="", subject="character",
                          visibility="internal", retention="normal", occurred=None, material_id="", parents=(),
                          derived=False, experience=True, start=None, end=None, metadata=None,
                          internal_allowed=True, db=None):
        descriptor = self.modules[module]
        if kind not in descriptor.types or visibility not in {"shared", "session", "internal"}:
            raise ValueError("invalid_memory_type_or_acl")
        if retention == "no_store":
            return None
        if not isinstance(body, str) or not body.strip() or retention not in {"normal", "no_long_term"}:
            raise ValueError("invalid_memory_body")
        if visibility == "session" and not scope:
            raise ValueError("session_scope_required")
        if db is None:
            with self.connection() as transaction:
                transaction.execute("BEGIN IMMEDIATE")
                return self.append_short_term(bot=bot, character=character, module=module, kind=kind, event_id=event_id,
                    body=body, scope=scope, subject=subject, visibility=visibility, retention=retention, occurred=occurred,
                    material_id=material_id, parents=parents, derived=derived, experience=experience, start=start, end=end,
                    metadata=metadata, internal_allowed=internal_allowed, db=transaction)
        occurred = self.clock() if occurred is None else occurred
        key = identity("memory", bot, character, module, event_id, kind)
        if db.execute("SELECT 1 FROM sm_tombstones WHERE id=?", (key,)).fetchone():
            raise MemoryConflict("withdrawn_identity")
        parent_rows = []
        for parent in set(parents):
            row = db.execute("SELECT * FROM sm_records WHERE id=?", (parent,)).fetchone()
            if row:
                if row["status"] in {"withdrawn", "purged"}:
                    raise MemoryConflict("withdrawn_parent")
                parent_rows.append(dict(row))
            else:
                row = db.execute("SELECT * FROM sm_events WHERE id=? AND status NOT IN ('withdrawn','purged')", (parent,)).fetchone()
                if not row:
                    raise MemoryConflict("missing_parent")
                parent_rows.append(dict(row))
        if any(p["bot"] != bot or p["character"] != character for p in parent_rows):
            raise ValueError("cross_character_parent")
        if parent_rows:
            if any(p["retention"] != "normal" for p in parent_rows):
                retention = "no_long_term"
            internal_allowed = internal_allowed and all(p["internal_allowed"] for p in parent_rows)
            restricted = [p for p in parent_rows if p["visibility"] != "shared"]
            if restricted:
                scopes = {p["scope"] for p in restricted}
                if any(p["visibility"] == "internal" for p in restricted) or len(scopes) != 1:
                    visibility, scope = "internal", ""
                elif visibility != "internal":
                    visibility, scope = "session", scopes.pop()
        old = db.execute("SELECT * FROM sm_records WHERE id=?", (key,)).fetchone()
        if old:
            if old["hash"] != digest(body):
                raise MemoryConflict("use_revision_job_for_changed_content")
            return key
        self._capacity(db, len(body.encode()))
        db.execute("INSERT INTO sm_records(id,bot,character,module,kind,event_id,material_id,scope,subject,visibility,retention,"
            "internal_allowed,body,hash,version,occurred,created,day,start,end,derived,experience,policy_version,metadata) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?)", (key, bot, character, module, kind, event_id,
             material_id, scope, subject, visibility, retention, int(internal_allowed), body, digest(body), occurred,
             self.clock(), self.day(occurred), occurred if start is None else start, occurred if end is None else end,
             int(derived), int(experience), descriptor.version, encoded(metadata or {})))
        db.executemany("INSERT OR IGNORE INTO sm_edges VALUES(?,?)", [(key, p) for p in set(parents)])
        for consumer in self.policies[module].consumers(kind, experience):
            state, reason = ("skipped", "no_long_term") if retention == "no_long_term" else ("pending", "")
            db.execute("INSERT INTO sm_consumers VALUES(?,?,?,?,?)", (key, consumer, state, reason, self.clock()))
        self.bump(db, bot, character)
        db.execute("UPDATE sm_days SET state='dirty' WHERE bot=? AND character=? AND day=?", (bot, character, self.day(occurred)))
        return key

    def promote_from_short_term(self, key, *, policy, db):
        row = db.execute("SELECT * FROM sm_records WHERE id=?", (key,)).fetchone()
        if not row or self.policies.get(row["module"]) is not policy or not policy.can_promote(row["kind"]):
            raise PermissionError("registered_policy_required")
        if row["retention"] != "normal" or row["status"] != "active" or not row["short"]:
            raise MemoryConflict("not_promotable")
        db.execute("UPDATE sm_records SET long=1 WHERE id=?", (key,))

    def ack(self, db, keys, consumer, *, skipped=None):
        if skipped is not None and not skipped:
            raise ValueError("skip_reason_required")
        for key in keys:
            db.execute("UPDATE sm_consumers SET state=?,reason=?,updated=? WHERE record_id=? AND consumer=?",
                ("skipped" if skipped else "done", skipped or "", self.clock(), key, consumer))

    def rows(self, *, bot, character, module=None, day=None, kind=None, long=None, experiences=False):
        where, args = ["bot=?", "character=?", "status='active'"], [bot, character]
        for col, val in (("module", module), ("day", day), ("kind", kind), ("long", long)):
            if val is not None:
                where.append(col + "=?")
                args.append(val)
        if experiences:
            where.extend(["experience=1", "short=1", "internal_allowed=1", "retention='normal'"])
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM sm_records WHERE " + " AND ".join(where) + " ORDER BY occurred,id", args)]

    def begin_job(self, *, bot, character, module, operation, day="", inputs=(), suffix=""):
        key = identity("job", bot, character, module, operation, day, suffix)
        now = self.clock()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            snapshot = []
            for item in inputs:
                table = "sm_events" if "seq" in item else "sm_records"
                row = db.execute(f"SELECT version,status FROM {table} WHERE id=?", (item["id"],)).fetchone()
                if not row or row["version"] != item["version"] or row["status"] in {"withdrawn", "purged"}:
                    raise MemoryConflict("input_changed")
                snapshot.append([table, item["id"], row["version"]])
            raw = encoded(snapshot)
            old = db.execute("SELECT * FROM sm_jobs WHERE id=?", (key,)).fetchone()
            if old and ((old["state"] == "done" and old["snapshot"] == raw) or old["lease"] > now
                        or old["retry_at"] > now or old["attempts"] >= read_config(db)["max_attempts"]):
                return None
            token = uuid.uuid4().hex
            epoch = self.epoch(db, bot, character)
            db.execute("INSERT INTO sm_jobs(id,bot,character,module,operation,day,state,snapshot,epoch,lease,token,updated) "
                "VALUES(?,?,?,?,?,?,'running',?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state='running',snapshot=excluded.snapshot,"
                "epoch=excluded.epoch,lease=excluded.lease,token=excluded.token,updated=excluded.updated",
                (key, bot, character, module, operation, day, raw, epoch, now + 300, token, now))
            db.execute("DELETE FROM sm_pins WHERE job=?", (key,))
            db.executemany("INSERT INTO sm_pins VALUES(?,?)", [(key, r[1]) for r in snapshot])
            return dict(id=key, token=token, epoch=epoch, bot=bot, character=character, module=module, snapshot=raw)

    def renew(self, job):
        with self.connection() as db:
            if not db.execute("UPDATE sm_jobs SET lease=? WHERE id=? AND token=? AND state='running'",
                (self.clock() + 300, job["id"], job["token"])).rowcount:
                raise MemoryConflict("lost_job_lease")

    def commit_job(self, job, commit):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM sm_jobs WHERE id=? AND token=? AND state='running'", (job["id"], job["token"])).fetchone()
            if not current or current["lease"] < self.clock() or self.epoch(db, job["bot"], job["character"]) != job["epoch"]:
                raise MemoryConflict("snapshot_changed")
            result = commit(db)
            db.execute("UPDATE sm_jobs SET state='done',lease=0,error='',result=?,updated=? WHERE id=?", (encoded(result), self.clock(), job["id"]))
            db.execute("DELETE FROM sm_pins WHERE job=?", (job["id"],))
            self.bump(db, job["bot"], job["character"])
            return result

    def fail_job(self, job, error):
        detail = type(error).__name__
        if type(error) is ValueError and str(error) in JOB_ERROR_CODES:
            detail += ":" + str(error)
        with self.connection() as db:
            db.execute("UPDATE sm_jobs SET state='failed',lease=0,attempts=attempts+1,error=?,retry_at=?,updated=? "
                "WHERE id=? AND token=? AND state='running'", (detail, self.clock() + 60, self.clock(), job["id"], job["token"]))
            # Failed inputs still require consumers; release pins to permit governance.
            db.execute("DELETE FROM sm_pins WHERE job=? AND EXISTS(SELECT 1 FROM sm_jobs WHERE id=? AND token=?)", (job["id"], job["id"], job["token"]))

    def retry(self, key):
        with self.connection() as db:
            if not db.execute("UPDATE sm_jobs SET state='retry',retry_at=0,attempts=0,error='' WHERE id=? AND lease<=? AND state!='done'", (key, self.clock())).rowcount:
                raise MemoryConflict("job_not_retryable")

    def retire(self, db, keys):
        for key in set(keys):
            db.execute("UPDATE sm_records SET long=0,status='superseded',retired=?,version=version+1 WHERE id=?", (self.clock(), key))

    def replace_dependency(self, db, old, new):
        """Invalidate descendants before making a revised parent available."""
        descendants = [r[0] for r in db.execute("WITH RECURSIVE d(id) AS (SELECT child FROM sm_edges WHERE parent=? "
            "UNION SELECT e.child FROM sm_edges e JOIN d ON e.parent=d.id) SELECT id FROM d", (old,))]
        for key in descendants:
            if key == new:
                continue
            db.execute("UPDATE sm_records SET status='stale',version=version+1 WHERE id=? AND status NOT IN ('withdrawn','purged')", (key,))
        children = [r[0] for r in db.execute("SELECT child FROM sm_edges WHERE parent=? AND child!=?", (old, new))]
        for child in children:
            db.execute("INSERT OR IGNORE INTO sm_edges VALUES(?,?)", (child, new))
            db.execute("DELETE FROM sm_edges WHERE child=? AND parent=?", (child, old))
        return children

    def withdraw(self, key, *, reason="deleted", db=None):
        if db is None:
            with self.connection() as transaction:
                transaction.execute("BEGIN IMMEDIATE")
                return self.withdraw(key, reason=reason, db=transaction)
        roots = [key]
        if reason in {"deleted", "no_store", "no_long_term", "video_revoked"}:
            root = db.execute("SELECT experience FROM sm_records WHERE id=?", (key,)).fetchone()
            if root and root[0]:
                roots.extend(r[0] for r in db.execute("SELECT e.parent FROM sm_edges e JOIN sm_events s ON e.parent=s.id WHERE e.child=?", (key,)))
        descendants = list(roots)
        for root in roots:
            descendants.extend(r[0] for r in db.execute("WITH RECURSIVE d(id) AS (SELECT child FROM sm_edges WHERE parent=? "
                "UNION SELECT e.child FROM sm_edges e JOIN d ON e.parent=d.id) SELECT id FROM d", (root,)))
        for item in set(descendants):
            db.execute("INSERT OR IGNORE INTO sm_tombstones VALUES(?,?,?)", (item, self.clock(), reason))
            for table, body in (("sm_records", "body"), ("sm_events", "payload")):
                rows = db.execute(f"SELECT bot,character FROM {table} WHERE id=?", (item,)).fetchall()
                db.execute(f"UPDATE {table} SET {body}='',status='withdrawn',version=version+1 WHERE id=?", (item,))
                for row in rows:
                    self.bump(db, *row)
            db.execute("UPDATE sm_records SET short=0,long=0,metadata='{}',hash='' WHERE id=?", (item,))
            db.execute("DELETE FROM sm_tokens WHERE hash NOT IN (SELECT hash FROM sm_records)")
            db.execute("UPDATE sm_consumers SET state='skipped',reason='withdrawn' WHERE record_id=?", (item,))
            db.execute("UPDATE sm_jobs SET state='invalidated',lease=0,result='',error='source_withdrawn',attempts=0,retry_at=0 "
                "WHERE id IN (SELECT job FROM sm_pins WHERE record_id=?)", (item,))
            db.execute("DELETE FROM sm_pins WHERE record_id=?", (item,))
            db.execute("UPDATE sm_batches SET active=0,retired=? WHERE summary_id=?", (self.clock(), item))
        db.execute("UPDATE sm_records SET long=0 WHERE module='video' AND kind='video_watch' AND long=1 AND NOT EXISTS "
            "(SELECT 1 FROM sm_members m JOIN sm_batches b ON b.id=m.batch WHERE m.record_id=sm_records.id AND b.active=1)")
        # No request usage stores bodies. Backups made by this service must not restore deleted data.
        db.execute("INSERT OR REPLACE INTO sm_meta VALUES('backup_scrub_required','true')")
        return len(set(descendants))

    def set_retention(self, bot, character, scope, retention):
        if retention not in {"normal", "no_store", "no_long_term"}:
            raise ValueError("invalid_retention")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR REPLACE INTO sm_governance VALUES(?,?,?,?)", (bot, character, scope, retention))
            if retention != "normal":
                # Conservative withdrawal is safer than reusing inseparable derived prose.
                keys = db.execute("SELECT id FROM sm_records WHERE bot=? AND character=? AND scope=? UNION "
                    "SELECT id FROM sm_events WHERE bot=? AND character=? AND scope=?", (bot, character, scope) * 2).fetchall()
                for row in keys:
                    self.withdraw(row[0], reason=retention, db=db)
            self.bump(db, bot, character)
        if retention != "normal":
            self._withdraw_legacy_scope(character, scope)

    def set_subject_retention(self, bot, character, scope, subject, retention):
        if retention not in {"normal", "no_store", "no_long_term"}:
            raise ValueError("invalid_retention")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR REPLACE INTO sm_subject_rules VALUES(?,?,?,?,?)", (bot, character, scope, subject, retention))
            if retention != "normal":
                for row in db.execute("SELECT id FROM sm_events WHERE bot=? AND character=? AND scope=? AND subject=?", (bot, character, scope, subject)).fetchall():
                    self.withdraw(row[0], reason=retention, db=db)
            self.bump(db, bot, character)
        if retention != "normal":
            self._withdraw_legacy_scope(character, scope, subject)

    def _withdraw_legacy_scope(self, character, scope, subject=None):
        with self.connection() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_items'").fetchone():
                return
            sql, args = "SELECT id FROM memory_items WHERE character_id=? AND owner_key=?", [character, scope]
            if subject:
                sql += " AND subject_key=?"
                args.append(subject)
            keys = [r[0] for r in db.execute(sql, args)]
        for key in keys:
            with self.connection() as db:
                exists = db.execute("SELECT 1 FROM memory_items WHERE id=?", (key,)).fetchone()
            if exists:
                self.govern_legacy(key, character=character)

    def correct_record(self, key, body, version, *, character, db=None):
        if not isinstance(body, str) or not 1 <= len(body.strip()) <= 4000:
            raise ValueError("invalid_correction")
        if db is None:
            with self.connection() as transaction:
                transaction.execute("BEGIN IMMEDIATE")
                return self.correct_record(key, body, version, character=character, db=transaction)
        if db is not None:
            row = db.execute("SELECT * FROM sm_records WHERE id=? AND character=? AND status='active'", (key, character)).fetchone()
            if not row or row["version"] != version:
                raise MemoryConflict("record_changed")
            children = [r[0] for r in db.execute("SELECT child FROM sm_edges WHERE parent=?", (key,))]
            for child in children:
                self.withdraw(child, reason="source_corrected", db=db)
            # A correction is governance of an existing identity, never an automatic fact ingress.
            db.execute("UPDATE sm_records SET body=?,hash=?,metadata='{}',version=version+1 WHERE id=?", (body, digest(body), key))
            self.bump(db, row["bot"], character)

    def govern_legacy(self, key, *, character, body=None, db=None):
        """Withdraw descendants and erase old bodies/caches, including the migration backup."""
        if body is not None and (not isinstance(body, str) or not 1 <= len(body.strip()) <= 4000):
            raise ValueError("invalid_correction")
        if db is None:
            with self.connection() as transaction:
                transaction.execute("BEGIN IMMEDIATE")
                self.govern_legacy(key, character=character, body=body, db=transaction)
            self.scrub_backups()
            return
        if db is not None:
            root = db.execute("SELECT * FROM memory_items WHERE id=? AND character_id=?", (key, character)).fetchone()
            diary = db.execute("SELECT * FROM diary_entries WHERE id=? AND character_id=?", (key, character)).fetchone()
            if not root and not diary:
                raise ValueError("unknown_legacy_record")
            descendants = {key, *[r[0] for r in db.execute("WITH RECURSIVE d(id) AS (SELECT child_id FROM memory_links WHERE parent_id=? "
                "UNION SELECT l.child_id FROM memory_links l JOIN d ON l.parent_id=d.id) SELECT id FROM d", (key,))]}
            for item in descendants:
                if item != key or body is None:
                    self.withdraw(item, reason="legacy_governance", db=db)
                ref = db.execute("SELECT source_ref FROM memory_items WHERE id=?", (item,)).fetchone()
                if ref and ref[0]:
                    db.execute("UPDATE memory_inbox SET payload='{}',status='withdrawn' WHERE character_id=? AND source_ref=?", (character, ref[0]))
                if item != key or body is None:
                    db.execute("DELETE FROM memory_items WHERE id=?", (item,))
                    db.execute("DELETE FROM diary_entries WHERE id=?", (item,))
                db.execute("DELETE FROM memory_evidence WHERE memory_id=?", (item,))
                db.execute("DELETE FROM memory_revisions WHERE memory_id=?", (item,))
            db.execute("DELETE FROM memory_compactions WHERE character_id=?", (character,))
            if body is not None:
                db.execute("UPDATE memory_items SET summary=?,search_text=?,updated_at=? WHERE id=?", (body, " ".join(search_terms(body)), self.clock(), key))
                db.execute("UPDATE diary_entries SET draft=?,public_candidate=NULL WHERE id=?", (body, key))
                # The original backup must not restore pre-correction text.
                db.execute("INSERT OR IGNORE INTO sm_tombstones VALUES(?,?,?)", (key, self.clock(), "legacy_corrected"))
                db.execute("INSERT OR REPLACE INTO sm_meta VALUES('backup_scrub_required','true')")

    def candidates(self, context):
        # ACL is applied by SQL before any body is read, ranked, tokenized or summarized.
        acl = "(visibility='shared' OR (visibility='session' AND scope=?))"
        args = [context.bot, context.character, context.scope]
        if context.internal:
            acl = "(" + acl + " OR internal_allowed=1)"
        if context.allowed_ids is not None:
            if context.allowed_ids:
                acl += " AND id IN (" + ",".join("?" for _ in context.allowed_ids) + ")"
                args.extend(context.allowed_ids)
            else:
                acl += " AND 0"
        with self.connection() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM sm_records WHERE bot=? AND character=? AND status='active' "
                "AND (short=1 OR long=1) AND " + acl + " ORDER BY occurred DESC LIMIT 1000", args)]
        terms = set(search_terms(context.query))
        return sorted(rows, key=lambda r: (len(terms.intersection(search_terms(r["body"]))),
            r["module"] == context.module, r["occurred"]), reverse=True)

    async def build_memory_view(self, context, *, count, available, tokenizer_key=""):
        config = self.config
        remaining = max(0, min(available, config["memory_tokens"]))
        items, attributions = [], []
        # Shared recent projection contains no bodies, IDs or private subjects.
        recent = []
        with self.connection() as db:
            for row in db.execute("SELECT module,count(*) AS n FROM sm_records WHERE bot=? AND character=? AND visibility='shared' "
                "AND status='active' AND experience=1 AND retention='normal' AND occurred>=? GROUP BY module", (context.bot, context.character, self.clock() - 86400)):
                recent.append({"module": row[0], "recent_experiences": row[1]})
        if context.allowed_ids is not None:
            recent = []  # Maintenance only consumes its versioned input set.
        recent_cost = await count(encoded(recent)) if recent else 0
        if recent_cost > min(remaining, config["recent_tokens"]):
            recent, recent_cost = [], 0
        remaining -= recent_cost
        seen = set()
        for row in self.candidates(context):
            if row["hash"] in seen:
                continue
            item = {"id": row["id"], "module": row["module"], "kind": row["kind"], "summary": row["body"],
                    "coverage": [row["start"], row["end"]], "data_only": True}
            raw = encoded(item)
            with self.connection() as db:
                cached = db.execute("SELECT tokens FROM sm_tokens WHERE hash=? AND model=?", (digest(raw), tokenizer_key)).fetchone() if tokenizer_key else None
            cost = cached[0] if cached else await count(raw)
            if tokenizer_key and not cached:
                with self.connection() as db:
                    db.execute("INSERT OR REPLACE INTO sm_tokens VALUES(?,?,?)", (digest(raw), tokenizer_key, cost))
            if cost > remaining:
                continue
            seen.add(row["hash"])
            items.append(item)
            attributions.append({"module": row["module"], "layer": "long" if row["long"] else "short", "tokens": cost})
            remaining -= cost
        return {"items": items, "recent": recent, "activity": context.activity, "attribution": attributions,
                "common_tokens": recent_cost, "available": max(0, min(available, config["memory_tokens"]))}

    def record_usage(self, context, **usage):
        # Only numerical metadata, never the request or memory bodies.
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO sm_usage VALUES(?,?,?,?,?,?)", (context.request_id, self.clock(),
                context.character, context.module, context.scope, encoded(usage)))
            db.execute("DELETE FROM sm_usage WHERE id NOT IN (SELECT id FROM sm_usage ORDER BY created DESC LIMIT 500)")

    def get_request_usage(self, request_id=None, *, character=None):
        with self.connection() as db:
            where, args = ("id=?", [request_id]) if request_id else ("character=?", [character])
            rows = db.execute("SELECT * FROM sm_usage WHERE " + where + " ORDER BY created DESC LIMIT 50", args).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def get_inventory_stats(self, character):
        with self.connection() as db:
            version = tuple(tuple(r) for r in db.execute("SELECT bot,version FROM sm_epochs WHERE character=? ORDER BY bot", (character,)))
            cached = self._stats_cache.get(character)
            if cached and cached[0] == version and self.clock() - cached[1] < 10:
                return cached[2]
            modules = []
            for descriptor in self.modules.values():
                value = {**descriptor.__dict__, "short": {}, "long": {}, "types": {}}
                for layer in ("short", "long"):
                    row = db.execute(f"SELECT count(*),coalesce(sum(length(body)),0),coalesce(sum(length(CAST(body AS BLOB))),0) "
                        f"FROM sm_records WHERE character=? AND module=? AND {layer}=1 AND status='active'", (character, descriptor.module_id)).fetchone()
                    value[layer] = dict(count=row[0], chars=row[1], bytes=row[2])
                value["types"] = {r[0]: r[1] for r in db.execute("SELECT kind,count(*) FROM sm_records WHERE character=? AND module=? AND long=1 AND status='active' GROUP BY kind", (character, descriptor.module_id))}
                value["batches"] = [dict(r) for r in db.execute("SELECT b.kind,count(DISTINCT b.id) AS batches,count(m.record_id) AS selected,max(b.generation) AS generation "
                    "FROM sm_batches b LEFT JOIN sm_members m ON m.batch=b.id WHERE b.character=? AND b.module=? AND b.active=1 GROUP BY b.kind", (character, descriptor.module_id))]
                value["coverage"] = list(db.execute("SELECT min(start),max(end) FROM sm_records WHERE character=? AND module=? AND long=1 AND status='active'", (character, descriptor.module_id)).fetchone())
                modules.append(value)
            pending = db.execute("SELECT count(*) FROM sm_consumers c JOIN sm_records r ON r.id=c.record_id WHERE r.character=? AND c.state='pending'", (character,)).fetchone()[0]
            pins = db.execute("SELECT count(DISTINCT p.record_id) FROM sm_pins p JOIN sm_jobs j ON p.job=j.id WHERE j.character=?", (character,)).fetchone()[0]
            recovery = list(db.execute("SELECT count(*),coalesce(sum(length(CAST(body AS BLOB))),0) FROM sm_records WHERE character=? AND status='superseded'", (character,)).fetchone())
            legacy = {}
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_items'").fetchone():
                legacy = dict(records=db.execute("SELECT count(*) FROM memory_items WHERE character_id=? AND deleted_at IS NULL", (character,)).fetchone()[0],
                    pending=db.execute("SELECT count(*) FROM memory_inbox WHERE character_id=? AND status='legacy_pending'", (character,)).fetchone()[0])
            jobs = [dict(r) for r in db.execute("SELECT id,module,operation,day,state,attempts,error,retry_at,updated FROM sm_jobs WHERE character=? ORDER BY updated DESC LIMIT 100", (character,))]
            days = [dict(r) for r in db.execute("SELECT * FROM sm_days WHERE character=? ORDER BY day DESC LIMIT 30", (character,))]
            cleanup = db.execute("SELECT value FROM sm_meta WHERE key='cleanup'").fetchone()
        result = dict(modules=modules, pending=pending, pins=pins, recovery=dict(count=recovery[0], bytes=recovery[1]), legacy=legacy,
            jobs=jobs, days=days, disk_bytes=sum(p.stat().st_size for p in self.path.parent.glob(self.path.name + '*') if p.is_file()),
            config=self.config, schema=PARAMETERS, cleanup=json.loads(cleanup[0]) if cleanup else None)
        self._stats_cache[character] = version, self.clock(), result
        return result

    def pressure(self):
        with self.connection() as db:
            count, size = db.execute("SELECT count(*),coalesce(sum(length(CAST(body AS BLOB))),0) FROM sm_records WHERE short=1 AND status='active'").fetchone()
            size += db.execute("SELECT coalesce(sum(length(CAST(payload AS BLOB))),0) FROM sm_events WHERE status='pending'").fetchone()[0]
            c = read_config(db)
        return count >= c["soft_records"] or size >= c["soft_bytes"] or shutil.disk_usage(self.path.parent).free < 64 * 1024 * 1024

    def cleanup(self, now=None):
        now = self.clock() if now is None else now
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            config = read_config(db)
            db.execute("DELETE FROM sm_pins WHERE job IN (SELECT id FROM sm_jobs WHERE lease<?)", (now,))
            exited = db.execute("UPDATE sm_records SET short=0 WHERE short=1 AND created<? "
                "AND NOT EXISTS(SELECT 1 FROM sm_consumers c WHERE c.record_id=sm_records.id AND c.state='pending') "
                "AND NOT EXISTS(SELECT 1 FROM sm_pins p WHERE p.record_id=sm_records.id)", (now - config["short_term_hours"] * 3600,)).rowcount
            cutoff = now - config["recovery_days"] * 86400
            # Keep identity/edges/tombstones; bound bodies, revisions, event payloads and job history.
            purged = db.execute("UPDATE sm_records SET body='',metadata='{}',status='purged' WHERE long=0 AND short=0 "
                "AND coalesce(retired,created)<? AND status NOT IN ('withdrawn','purged') "
                "AND NOT EXISTS(SELECT 1 FROM sm_pins p WHERE p.record_id=sm_records.id)", (cutoff,)).rowcount
            db.execute("UPDATE sm_events SET payload='',status='purged' WHERE status='done' AND created<? "
                "AND NOT EXISTS(SELECT 1 FROM sm_pins p WHERE p.record_id=sm_events.id) "
                "AND NOT EXISTS(SELECT 1 FROM sm_edges e JOIN sm_consumers c ON c.record_id=e.child "
                "JOIN sm_records r ON r.id=e.child WHERE e.parent=sm_events.id AND r.status='active' "
                "AND c.consumer='qq_special' AND c.state='pending')", (cutoff,))
            db.execute("DELETE FROM sm_jobs WHERE state='done' AND updated<?", (cutoff,))
            db.execute("DELETE FROM sm_members WHERE batch IN (SELECT id FROM sm_batches WHERE active=0 AND retired<?)", (cutoff,))
            db.execute("DELETE FROM sm_tokens WHERE hash NOT IN (SELECT hash FROM sm_records WHERE body!='')")
            result = dict(at=now, short_exited=exited, bodies_purged=purged)
            db.execute("INSERT OR REPLACE INTO sm_meta VALUES('cleanup',?)", (encoded(result),))
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_compactions'").fetchone():
                db.execute("DELETE FROM memory_compactions WHERE created_at<?", (cutoff,))
                db.execute("DELETE FROM memory_revisions WHERE created_at<?", (cutoff,))
        self._stats_cache.clear()
        self.scrub_backups()
        return result

    def scrub_backups(self):
        """Apply tombstones to our finite migration backup; never touch arbitrary user backups."""
        with self.connection() as db:
            needed = db.execute("SELECT value FROM sm_meta WHERE key='backup_scrub_required'").fetchone()
            tombstones = [r[0] for r in db.execute("SELECT id FROM sm_tombstones")]
            scopes = {r[0] for r in db.execute("SELECT scope FROM sm_records WHERE id IN (SELECT id FROM sm_tombstones) UNION "
                "SELECT scope FROM sm_events WHERE id IN (SELECT id FROM sm_tombstones)")}
        if needed and needed[0] == "true":
            from .model_debug import purge_memory_debug
            purge_memory_debug(self.path.parent / "logs/model_debug", scopes)
            control = self.path.parent / "web_control.sqlite3"
            if control.exists():
                with sqlite3.connect(control) as db:
                    db.execute("PRAGMA secure_delete=ON")
                    db.execute("DELETE FROM edits WHERE kind IN ('memory','diary')")
        backup = self.path.with_name(self.path.name + ".pre-shared-v1.bak")
        if backup.exists() and self.clock() - backup.stat().st_mtime > self.config["recovery_days"] * 86400:
            backup.unlink()
        elif backup.exists() and needed and needed[0] == "true":
            # Legacy backup has no new records. Deletions of legacy keys purge related caches too.
            with sqlite3.connect(backup) as db:
                db.execute("PRAGMA secure_delete=ON")
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for key in tombstones:
                    if "memory_items" in tables:
                        row = db.execute("SELECT source_ref FROM memory_items WHERE id=?", (key,)).fetchone()
                        if row and row[0]:
                            db.execute("UPDATE memory_inbox SET payload='{}',status='withdrawn' WHERE source_ref=?", (row[0],))
                        db.execute("DELETE FROM memory_items WHERE id=?", (key,))
                        db.execute("DELETE FROM diary_entries WHERE id=?", (key,))
                        db.execute("DELETE FROM memory_evidence WHERE memory_id=?", (key,))
                db.execute("DELETE FROM memory_revisions")
                db.execute("DELETE FROM memory_compactions")
        with self.connection() as db:
            db.execute("UPDATE sm_meta SET value='false' WHERE key='backup_scrub_required'")
