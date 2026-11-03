"""Durable facts, exact-owner retrieval and deliberately lossy public projections.

No model, QQ transport or tool executor is available in this layer.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import unicodedata


def identity(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def owner_bot(owner: str) -> str:
    match = re.fullmatch(r"qq:([0-9]{1,20}):(?:dm|group):[0-9]{1,20}", owner)
    if not match:
        raise ValueError("Invalid memory owner")
    return match[1]


def shared_owner(bot: str, character: str) -> str:
    if not re.fullmatch(r"[0-9]{1,20}", bot):
        raise ValueError("Invalid bot identity")
    return f"qq:{bot}:shared:{character}"


def summary_identity(text: str) -> str:
    """Ignore typography, retaining words, negation, numbers and word boundaries."""
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(text.split()).rstrip("。.!！?？")


def search_terms(text: str) -> list[str]:
    """FTS5 unicode61 with explicit Chinese bigrams; query syntax is never input."""
    words = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", text.lower())
    terms = []
    for word in words:
        if re.fullmatch(r"[\u3400-\u9fff]+", word):
            terms.extend(word[i:i + 2] for i in range(max(1, len(word) - 1)))
        else:
            terms.append(word)
    return list(dict.fromkeys(terms))


class MemoryRepository:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS memory_items (
                  id TEXT PRIMARY KEY, character_id TEXT NOT NULL, scope TEXT NOT NULL,
                  owner_key TEXT NOT NULL, subject_key TEXT NOT NULL, fact_key TEXT NOT NULL,
                  source_type TEXT NOT NULL, source_ref TEXT, summary TEXT NOT NULL,
                  tags_json TEXT NOT NULL, salience REAL NOT NULL, confidence REAL NOT NULL,
                  sensitive INTEGER NOT NULL DEFAULT 0, visibility TEXT NOT NULL DEFAULT 'private',
                  created_at REAL NOT NULL, updated_at REAL NOT NULL,
                  expires_at REAL, deleted_at REAL, search_text TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS memory_owner ON memory_items(character_id,owner_key,visibility);
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(search_text, content='memory_items', content_rowid='rowid');
                CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory_items BEGIN
                  INSERT INTO memory_fts(rowid,search_text) VALUES(new.rowid,new.search_text); END;
                CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory_items BEGIN
                  INSERT INTO memory_fts(memory_fts,rowid,search_text) VALUES('delete',old.rowid,old.search_text); END;
                CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory_items BEGIN
                  INSERT INTO memory_fts(memory_fts,rowid,search_text) VALUES('delete',old.rowid,old.search_text);
                  INSERT INTO memory_fts(rowid,search_text) VALUES(new.rowid,new.search_text); END;
                CREATE TABLE IF NOT EXISTS memory_revisions (
                  id INTEGER PRIMARY KEY, memory_id TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_links (
                  child_id TEXT NOT NULL, parent_id TEXT NOT NULL, PRIMARY KEY(child_id,parent_id)
                );
                CREATE TABLE IF NOT EXISTS memory_evidence (
                  memory_id TEXT NOT NULL, source_ref TEXT NOT NULL, evidence TEXT NOT NULL,
                  created_at REAL NOT NULL, PRIMARY KEY(memory_id,source_ref)
                );
                CREATE INDEX IF NOT EXISTS memory_evidence_time ON memory_evidence(memory_id,created_at);
                CREATE TABLE IF NOT EXISTS memory_inbox (
                  id TEXT PRIMARY KEY, character_id TEXT NOT NULL, owner_key TEXT NOT NULL,
                  sender_key TEXT NOT NULL, source_ref TEXT NOT NULL, payload TEXT NOT NULL,
                  created_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                  attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
                  error TEXT, lease_until REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS memory_accounts (
                  bot TEXT NOT NULL, character_id TEXT NOT NULL, first_seen REAL NOT NULL,
                  PRIMARY KEY(bot,character_id)
                );
                CREATE TABLE IF NOT EXISTS memory_compactions (
                  id TEXT PRIMARY KEY, owner_key TEXT NOT NULL, character_id TEXT NOT NULL,
                  payload TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS diary_entries (
                  id TEXT PRIMARY KEY, bot TEXT NOT NULL, character_id TEXT NOT NULL,
                  entry_date TEXT NOT NULL, draft TEXT NOT NULL, privacy_level TEXT NOT NULL,
                  public_candidate TEXT, sources_json TEXT NOT NULL, published_qzone_id TEXT,
                  created_at REAL NOT NULL, UNIQUE(bot,character_id,entry_date)
                );
                CREATE TABLE IF NOT EXISTS memory_jobs (
                  id TEXT PRIMARY KEY, lease_until REAL NOT NULL, retry_at REAL NOT NULL DEFAULT 0,
                  attempts INTEGER NOT NULL DEFAULT 0, error TEXT
                );
                CREATE VIEW IF NOT EXISTS diary_public_candidates AS
                  SELECT id,bot,character_id,entry_date,public_candidate AS draft,
                    'public_candidate' AS privacy_level,created_at
                  FROM diary_entries WHERE public_candidate IS NOT NULL;
            """)
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='sm_meta'").fetchone():
                for table in ("memory_items", "diary_entries"):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS sm_no_insert_{table} BEFORE INSERT ON {table} "
                               "BEGIN SELECT RAISE(ABORT,'legacy_long_term_write_disabled'); END")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, owner, sender, source_ref, text, *, character="elysia", now=None):
        bot = owner_bot(owner)
        if not re.fullmatch(r"[0-9]{1,20}", sender):
            raise ValueError("Invalid sender identity")
        if owner.split(":")[2] == "dm" and owner.split(":")[3] != sender:
            raise ValueError("Private sender/owner mismatch")
        if not isinstance(text, str) or not text.strip():
            return False
        now = time.time() if now is None else now
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO memory_accounts VALUES(?,?,?)", (bot, character, now))
            return db.execute("INSERT OR IGNORE INTO memory_inbox"
                "(id,character_id,owner_key,sender_key,source_ref,payload,created_at) VALUES(?,?,?,?,?,?,?)",
                (identity(character, owner, source_ref), character, owner, sender, source_ref,
                 json.dumps({"text": text[:8000]}, ensure_ascii=False), now)).rowcount == 1

    def claim_source(self, *, character, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM memory_inbox WHERE character_id=? AND status='pending' "
                "AND retry_at<=? AND lease_until<=? ORDER BY created_at,id LIMIT 1", (character, now, now)).fetchone()
            if row:
                db.execute("UPDATE memory_inbox SET lease_until=? WHERE id=?", (now + 180, row["id"]))
                return dict(row)

    def fail_source(self, key, error, *, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            row = db.execute("SELECT attempts FROM memory_inbox WHERE id=?", (key,)).fetchone()
            if row:
                attempts = row[0] + 1
                db.execute("UPDATE memory_inbox SET attempts=?,error=?,retry_at=?,lease_until=0 WHERE id=?",
                    (attempts, error, now + min(3600, 30 * 2 ** min(attempts, 7)), key))

    @staticmethod
    def _put(db, *, key, character, scope, owner, subject, fact_key, source_type, source_ref,
             summary, tags, salience, confidence, sensitive=False, visibility="private", now):
        old = db.execute("SELECT * FROM memory_items WHERE id=?", (key,)).fetchone()
        if old and old["updated_at"] > now:
            # An older source may finish retrying after a newer correction.
            # Preserve its evidence, but never roll the active fact backward.
            db.execute("INSERT INTO memory_revisions(memory_id,payload,created_at) VALUES(?,?,?)",
                (key, json.dumps({"summary": summary, "source_ref": source_ref, "updated_at": now,
                                  "superseded_on_arrival": True}, ensure_ascii=False), now))
            return
        if old and old["summary"] != summary:
            db.execute("INSERT INTO memory_revisions(memory_id,payload,created_at) VALUES(?,?,?)",
                (key, json.dumps(dict(old), ensure_ascii=False), now))
        search_text = " ".join(search_terms(summary + " " + " ".join(tags)))
        db.execute("INSERT INTO memory_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET summary=excluded.summary,tags_json=excluded.tags_json,"
            "salience=excluded.salience,confidence=excluded.confidence,sensitive=excluded.sensitive,"
            "source_ref=excluded.source_ref,updated_at=excluded.updated_at,search_text=excluded.search_text,"
            "deleted_at=NULL", (key, character, scope, owner, subject, fact_key, source_type, source_ref,
                summary, json.dumps(tags, ensure_ascii=False), salience, confidence, int(sensitive), visibility,
                now, now, None, None, search_text))

    def save_extraction(self, source, facts, topics):
        """Facts cannot choose owner/subject/visibility. Public data is a fixed topic projection."""
        owner, char = source["owner_key"], source["character_id"]
        bot = owner_bot(owner)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for fact in facts:
                fact_key = fact["key"] + (":" + source["source_ref"] if fact["kind"] == "event" else "")
                key = identity(char, owner, source["sender_key"], fact_key)
                if fact["kind"] != "event":
                    existing = db.execute("SELECT id,fact_key,summary FROM memory_items WHERE character_id=? "
                        "AND owner_key=? AND subject_key=? AND source_type='qq_message' AND deleted_at IS NULL "
                        "AND instr(fact_key,':')=0 ORDER BY created_at,id",
                        (char, owner, source["sender_key"])).fetchall()
                    duplicate = next((r for r in existing
                        if summary_identity(r["summary"]) == summary_identity(fact["summary"])), None)
                    if duplicate:
                        key, fact_key = duplicate["id"], duplicate["fact_key"]
                self._put(db, key=key, character=char, scope="group" if ":group:" in owner else "user",
                    owner=owner, subject=source["sender_key"], fact_key=fact_key, source_type="qq_message",
                    source_ref=source["source_ref"], summary=fact["summary"], tags=fact["tags"],
                    salience=fact["salience"], confidence=fact["confidence"], sensitive=fact["sensitive"],
                    now=source["created_at"])
                db.execute("INSERT OR IGNORE INTO memory_evidence VALUES(?,?,?,?)",
                    (key, source["source_ref"], fact.get("evidence", ""), source["created_at"]))
                topic = fact.get("shared_topic")
                if topic in topics and not fact["sensitive"]:
                    # One generic topic, with separate dated observations for
                    # diaries, rather than identical active copies every day.
                    prior = db.execute("SELECT id FROM memory_items WHERE character_id=? AND owner_key=? "
                        "AND scope='persona' AND source_type='processed_summary' AND fact_key=? "
                        "AND deleted_at IS NULL ORDER BY created_at,id LIMIT 1",
                        (char, shared_owner(bot, char), topic)).fetchone()
                    shared_key = prior["id"] if prior else identity(char, bot, "topic", topic)
                    self._put(db, key=shared_key, character=char, scope="persona", owner=shared_owner(bot, char),
                        subject="character", fact_key=topic, source_type="processed_summary", source_ref=None,
                        summary=topics[topic], tags=[topic], salience=0.5, confidence=1,
                        visibility="shared", now=source["created_at"])
                    db.execute("INSERT OR IGNORE INTO memory_links VALUES(?,?)", (shared_key, key))
                    db.execute("INSERT OR IGNORE INTO memory_evidence VALUES(?,?,?,?)",
                        (shared_key, identity(owner, source["source_ref"]), "", source["created_at"]))
            # Source text is a durable processing queue, not a second chat history.
            db.execute("UPDATE memory_inbox SET status='done',payload='{}',lease_until=0,error=NULL WHERE id=?", (source["id"],))

    def known(self, owner, sender, character, limit=40):
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT fact_key,summary FROM memory_items WHERE "
                "owner_key=? AND subject_key=? AND character_id=? AND deleted_at IS NULL "
                "AND scope IN ('user','group') ORDER BY updated_at DESC LIMIT ?", (owner, sender, character, limit))]

    def retrieve(self, owner, query, *, character="elysia", limit=64, now=None):
        bot = owner_bot(owner)
        now = time.time() if now is None else now
        # All text loaded into Python is already in the permitted SQL scope. In
        # particular, global FTS hits are never fetched and filtered afterward.
        eligible = "character_id=? AND (owner_key=? OR (owner_key=? AND visibility='shared')) " \
                   "AND deleted_at IS NULL AND (expires_at IS NULL OR expires_at>?) AND confidence>=0.65"
        params = (character, owner, shared_owner(bot, character), now)
        with self.connection() as db:
            recent = db.execute(f"SELECT * FROM memory_items WHERE {eligible} "
                "ORDER BY salience DESC,updated_at DESC LIMIT ?", (*params, limit)).fetchall()
            terms = search_terms(query)[:48]
            hits = []
            if terms:
                # A materialized authorized set establishes the ACL before FTS.
                hits = db.execute(f"WITH allowed AS MATERIALIZED (SELECT rowid AS rid,* FROM memory_items WHERE {eligible}) "
                    "SELECT a.* FROM allowed a WHERE a.rid IN (SELECT rowid FROM memory_fts WHERE memory_fts MATCH ?) "
                    "ORDER BY a.salience DESC,a.updated_at DESC LIMIT ?",
                    (*params, " OR ".join('"' + word + '"' for word in terms), limit)).fetchall()
        rows = {r["id"]: dict(r) for r in [*hits, *recent]}
        matched = {r["id"] for r in hits}
        ranked = sorted(rows.values(), key=lambda row: (
            row["id"] in matched, row["salience"] * row["confidence"], row["updated_at"]), reverse=True)
        video_count, result, seen = 0, [], set()
        for row in ranked:
            signature = (row["owner_key"], row["subject_key"], row["scope"], row["source_type"],
                         row["visibility"], summary_identity(row["summary"]))
            # Distinct events and diaries retain their dates and identities.
            deduplicate = row["source_type"] == "processed_summary" or (
                row["source_type"] == "qq_message" and ":" not in row["fact_key"])
            if deduplicate and signature in seen:
                continue
            if deduplicate:
                seen.add(signature)
            if row["source_type"] == "douyin":
                video_count += 1
                if video_count > 3:
                    continue
            result.append(row)
        return result[:limit]

    def deduplicate(self, *, character="elysia", now=None):
        """Archive exact duplicate active facts; retain originals and dated evidence."""
        now = time.time() if now is None else now
        merged = []
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM memory_items WHERE character_id=? AND deleted_at IS NULL "
                "AND (source_type='processed_summary' OR (source_type='qq_message' AND instr(fact_key,':')=0)) "
                "ORDER BY created_at,id", (character,)).fetchall()
            seen = {}
            for row in rows:
                db.execute("INSERT OR IGNORE INTO memory_evidence VALUES(?,?,?,?)",
                    (row["id"], row["source_ref"] or 'legacy:' + row["id"], "", row["updated_at"]))
                if row["created_at"] != row["updated_at"]:
                    db.execute("INSERT OR IGNORE INTO memory_evidence VALUES(?,?,?,?)",
                        (row["id"], 'legacy-created:' + row["id"], "", row["created_at"]))
                signature = (row["owner_key"], row["subject_key"], row["scope"], row["source_type"],
                             row["visibility"], summary_identity(row["summary"]))
                keep = seen.setdefault(signature, row["id"])
                if keep == row["id"]:
                    continue
                db.execute("INSERT INTO memory_revisions(memory_id,payload,created_at) VALUES(?,?,?)",
                    (row["id"], json.dumps({"reason": "duplicate", "canonical_id": keep,
                        "original": dict(row)}, ensure_ascii=False), now))
                db.execute("INSERT OR IGNORE INTO memory_evidence SELECT ?,source_ref,evidence,created_at "
                    "FROM memory_evidence WHERE memory_id=?", (keep, row["id"]))
                db.execute("INSERT OR IGNORE INTO memory_links SELECT ?,parent_id FROM memory_links WHERE child_id=?",
                    (keep, row["id"]))
                db.execute("INSERT OR IGNORE INTO memory_links SELECT child_id,? FROM memory_links WHERE parent_id=?",
                    (keep, row["id"]))
                db.execute("UPDATE memory_items SET sensitive=max(sensitive,?),updated_at=max(updated_at,?) WHERE id=?",
                    (row["sensitive"], row["updated_at"], keep))
                db.execute("UPDATE memory_items SET deleted_at=? WHERE id=?", (now, row["id"]))
                merged.append({"archived_id": row["id"], "canonical_id": keep})
        return merged

    def compacted(self, owner, character, rows, budget):
        key = identity(owner, character, [(r["id"], r["summary"], r["updated_at"]) for r in rows], budget)
        with self.connection() as db:
            row = db.execute("SELECT payload FROM memory_compactions WHERE id=? AND owner_key=? AND character_id=?",
                (key, owner, character)).fetchone()
        return key, json.loads(row[0]) if row else None

    def save_compaction(self, key, owner, character, payload, source_ids=()):
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO memory_compactions VALUES(?,?,?,?,?)",
                (key, owner, character, json.dumps(payload, ensure_ascii=False), time.time()))
            db.executemany("INSERT OR IGNORE INTO memory_links VALUES(?,?)", [(key, item) for item in source_ids])

    def accounts(self, character):
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM memory_accounts WHERE character_id=?", (character,))]

    def diary_material(self, bot, character, start, end, *, shared=False):
        owner = shared_owner(bot, character)
        with self.connection() as db:
            if shared:
                rows = db.execute("SELECT * FROM memory_items WHERE owner_key=? AND character_id=? "
                    "AND visibility='shared' AND source_type!='diary' AND deleted_at IS NULL "
                    "AND ((updated_at>=? AND updated_at<?) OR EXISTS (SELECT 1 FROM memory_evidence e "
                    "WHERE e.memory_id=memory_items.id AND e.created_at>=? AND e.created_at<?)) "
                    "ORDER BY salience DESC,updated_at DESC LIMIT 8",
                    (owner, character, start, end, start, end))
            else:
                # Bot prefix has a colon boundary; it cannot match another bot.
                rows = db.execute("SELECT * FROM memory_items WHERE owner_key LIKE ? AND character_id=? "
                    "AND source_type!='diary' AND deleted_at IS NULL AND confidence>=0.65 "
                    "AND updated_at>=? AND updated_at<? ORDER BY salience DESC,updated_at DESC LIMIT 8",
                    (f"qq:{bot}:%", character, start, end))
            return [dict(r) for r in rows]

    def diary(self, bot, character, day):
        with self.connection() as db:
            row = db.execute("SELECT * FROM diary_entries WHERE bot=? AND character_id=? AND entry_date=?",
                (bot, character, day)).fetchone()
        return dict(row) if row else None

    def sources_pending(self, bot, character, before):
        with self.connection() as db:
            return bool(db.execute("SELECT 1 FROM memory_inbox WHERE owner_key LIKE ? AND character_id=? "
                "AND status!='done' AND created_at<? LIMIT 1", (f"qq:{bot}:%", character, before)).fetchone())

    def claim_job(self, key, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO memory_jobs(id,lease_until) VALUES(?,0)", (key,))
            return db.execute("UPDATE memory_jobs SET lease_until=? WHERE id=? AND lease_until<=? AND retry_at<=?",
                (now + 600, key, now, now)).rowcount == 1

    def finish_job(self, key, error=None, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            db.execute("UPDATE memory_jobs SET lease_until=0,attempts=attempts+1,error=?,retry_at=? WHERE id=?",
                (error, now + (300 if error else 0), key))

    def save_diary(self, bot, character, day, draft, public, sources, *, now=None):
        now = time.time() if now is None else now
        key = identity(bot, character, "diary", day)
        with self.connection() as db:
            created = db.execute("INSERT OR IGNORE INTO diary_entries VALUES(?,?,?,?,?,?,?,?,?,?)",
                (key, bot, character, day, "[私人]\n" + draft, "private", public,
                 json.dumps([r["id"] for r in sources]), None, now)).rowcount == 1
            if not created:
                return False
            # A private mixed-owner diary is never put into any DM/group scope.
            self._put(db, key=key, character=character, scope="diary", owner=f"qq:{bot}:diary:{character}",
                subject="character", fact_key=day, source_type="diary", source_ref=key,
                summary=draft, tags=["diary", day], salience=0.7, confidence=1, now=now)
            for row in sources:
                db.execute("INSERT OR IGNORE INTO memory_links VALUES(?,?)", (key, row["id"]))
            # Only a separately generated text whose INPUT was public projections
            # can become a shared memory/publication candidate.
            if public:
                public_key = identity(key, "public")
                self._put(db, key=public_key, character=character, scope="diary", owner=shared_owner(bot, character),
                    subject="character", fact_key=day, source_type="diary", source_ref=None,
                    summary=public, tags=["diary", day], salience=0.6, confidence=1, visibility="shared", now=now)
                db.execute("INSERT OR IGNORE INTO memory_links VALUES(?,?)", (public_key, key))
            return True

    def add_video(self, owner, source_ref, summary, tags, *, character="elysia", confidence=0.8, now=None):
        """Q7/Q8 ingress contract; permission is the trusted caller's owner, never model output."""
        owner_bot(owner)
        now = time.time() if now is None else now
        if not isinstance(summary, str) or not summary.strip() or not 0.65 <= confidence <= 1:
            raise ValueError("Invalid video memory")
        with self.connection() as db:
            self._put(db, key=identity(character, owner, source_ref), character=character, scope="video", owner=owner,
                subject="video", fact_key=source_ref, source_type="douyin", source_ref=source_ref,
                summary=summary[:2000], tags=tags[:12], salience=0.5, confidence=confidence, now=now)

    def add_public_video(self, bot, source_ref, summary, tags, *, character="elysia", confidence=0.8, now=None):
        """Q8 vetted public video knowledge, never a user's private fact."""
        owner = shared_owner(bot, character)
        now = time.time() if now is None else now
        if not isinstance(summary, str) or not summary.strip() or not 0.7 <= confidence <= 1:
            raise ValueError("Invalid public video memory")
        with self.connection() as db:
            self._put(db, key=identity(character, owner, source_ref), character=character, scope="video", owner=owner,
                subject="video", fact_key=source_ref, source_type="douyin", source_ref=source_ref,
                summary=summary[:2000], tags=tags[:12], salience=0.5, confidence=confidence,
                visibility="shared", now=now)
