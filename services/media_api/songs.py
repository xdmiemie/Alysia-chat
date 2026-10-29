"""Persistent Q4 song jobs. Only the owning supervisor runs GPU work."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import time
import unicodedata
import uuid

from .song_source import NetEaseSource, SongSourceError, classify_search_error

ROOT = Path(__file__).resolve().parents[2]
TERMINAL = {"completed", "failed", "canceled", "needs_clarification"}
SEARCH_MAX_ATTEMPTS = 3


class SongStore:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "songs.sqlite3"
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, scope TEXT, payload TEXT, "
                       "status TEXT, song TEXT, error TEXT, updated REAL)")
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            if "search_attempts" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN search_attempts INTEGER NOT NULL DEFAULT 0")
            if "search_error" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN search_error TEXT NOT NULL DEFAULT ''")
            for column, default in (("user_request", "''"), ("request_context", "'[]'"),
                                    ("search_results", "'[]'"), ("selection", "'{}'")):
                if column not in columns:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT NOT NULL DEFAULT {default}")
            for column in ("initial_song_id", "retry_revision"):
                if column not in columns:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
            db.execute("CREATE TABLE IF NOT EXISTS song_search_attempts (job_id TEXT, attempt INTEGER, "
                       "error TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(job_id,attempt))")
            db.execute("CREATE TABLE IF NOT EXISTS song_candidates (receipt TEXT PRIMARY KEY, scope TEXT, "
                       "song_id INTEGER, data TEXT, created REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS song_download_attempts (job_id TEXT, song_id INTEGER, "
                       "status TEXT NOT NULL, error TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(job_id,song_id))")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def submit(self, request_id: str, scope: str, music: dict, *, user_request="", request_context=None,
               selected_song: dict | None = None, await_commit: bool = False, candidates: list[dict] | None = None) -> dict:
        if not re.fullmatch(r"[a-f0-9]{32}", request_id) or not re.fullmatch(
                r"qq:[0-9]{1,20}:(?:dm|group):[0-9]{1,20}", scope):
            raise ValueError("Invalid song request identity")
        if (music.get("mode") not in {"original", "cover"} or
                not isinstance(music.get("query"), str) or not 1 <= len(music["query"].strip()) <= 120 or
                not isinstance(music.get("artist", ""), str) or len(music.get("artist", "")) > 100):
            raise ValueError("Invalid song decision")
        music = {"mode": music["mode"], "query": music["query"].strip(), "artist": music.get("artist", "").strip()}
        request_context = [] if request_context is None else request_context
        if (not isinstance(user_request, str) or len(user_request) > 2000 or
                not isinstance(request_context, list) or len(request_context) > 20):
            raise ValueError("Invalid song request context")
        context_json = json.dumps(request_context, ensure_ascii=False, sort_keys=True)
        if len(context_json.encode("utf-8")) > 24000:
            raise ValueError("Song request context too large")
        payload = json.dumps(music, sort_keys=True, ensure_ascii=False)
        if selected_song is not None and (type(selected_song.get("song_id")) is not int or selected_song["song_id"] <= 0):
            raise ValueError("Invalid selected song")
        candidates = candidates if candidates is not None else ([selected_song] if selected_song else [])
        if len(candidates) > 200 or len({item["song_id"] for item in candidates}) != len(candidates):
            raise ValueError("Invalid candidate pool")
        if selected_song and selected_song["song_id"] not in {item["song_id"] for item in candidates}:
            raise ValueError("Selected song is not in candidate pool")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM jobs WHERE id=?", (request_id,)).fetchone()
            if old:
                if (old["scope"] != scope or old["payload"] != payload or
                        old["user_request"] != user_request or old["request_context"] != context_json):
                    raise ValueError("Conflicting song request")
                if selected_song is not None and (old["initial_song_id"] or json.loads(old["selection"]).get("song_id")) != selected_song["song_id"]:
                    raise ValueError("Conflicting selected song")
            else:
                active = db.execute("SELECT count(*) FROM jobs WHERE status NOT IN ('completed','failed','canceled','needs_clarification')").fetchone()[0]
                if active >= 3:
                    raise ValueError("Song queue is full")
                # A native music tool already chose a concrete candidate. Insert
                # its selection atomically; the worker must never search/choose again.
                db.execute("INSERT INTO jobs (id,scope,payload,status,song,error,updated,user_request,request_context,selection,search_results,initial_song_id) "
                           "VALUES (?,?,?,?,?,'',?,?,?,?,?,?)", (request_id, scope, payload,
                           ("awaiting_commit" if await_commit else "waiting_gpu") if selected_song else "queued",
                           json.dumps(selected_song or {}, ensure_ascii=False), time.time(), user_request, context_json,
                           json.dumps({"song_id": selected_song["song_id"]} if selected_song else {}),
                           json.dumps(candidates, ensure_ascii=False), selected_song["song_id"] if selected_song else 0))
        return self.get(request_id, scope)

    def get(self, job_id: str, scope: str | None = None) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            history = db.execute("SELECT attempt,error FROM song_search_attempts WHERE job_id=? ORDER BY attempt",
                                 (job_id,)).fetchall() if row else []
            downloads = db.execute("SELECT song_id,status,error FROM song_download_attempts WHERE job_id=? ORDER BY updated,song_id",
                                   (job_id,)).fetchall() if row else []
        if not row or (scope is not None and row["scope"] != scope):
            raise KeyError("Song job not found")
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        value["song"] = json.loads(value["song"])
        for column in ("request_context", "search_results", "selection"):
            value[column] = json.loads(value[column])
        value["search_history"] = [dict(item) for item in history]
        value["download_attempts"] = [dict(item) for item in downloads]
        excluded = {item["song_id"] for item in downloads if item["status"] == "forbidden"}
        value["remaining_candidates"] = [item for item in value["search_results"] if item["song_id"] not in excluded]
        return value

    def candidate_pool(self, receipts: list[str], scope: str) -> list[dict]:
        candidates = []
        with self.connection() as db:
            for receipt in dict.fromkeys(receipts):
                row = db.execute("SELECT data FROM song_candidates WHERE receipt=? AND scope=? AND created>?",
                                 (receipt, scope, time.time() - 3600)).fetchone()
                if row is None:
                    raise ValueError("Candidate receipt is missing or expired")
                song = json.loads(row[0])
                if not any(item["song_id"] == song["song_id"] for item in candidates):
                    candidates.append(song)
        return candidates

    def start_download(self, job_id: str, song_id: int):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,song FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["status"] not in {"waiting_gpu", "running"} or json.loads(row["song"]).get("song_id") != song_id:
                raise ValueError("song_selection_changed")
            attempt = db.execute("SELECT status FROM song_download_attempts WHERE job_id=? AND song_id=?", (job_id, song_id)).fetchone()
            if attempt and attempt[0] == "forbidden":
                raise SongSourceError("source_http_403")
            db.execute("INSERT INTO song_download_attempts VALUES (?,?,'downloading','',?) "
                       "ON CONFLICT(job_id,song_id) DO UPDATE SET status='downloading',updated=excluded.updated",
                       (job_id, song_id, time.time()))

    def downloaded(self, job_id: str, song_id: int):
        with self.connection() as db:
            db.execute("UPDATE song_download_attempts SET status='downloaded',error='',updated=? WHERE job_id=? AND song_id=?",
                       (time.time(), job_id, song_id))

    def reject_download(self, job_id: str, song_id: int):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,song,search_results FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["status"] in TERMINAL or row["status"] == "awaiting_reselection":
                return
            if json.loads(row["song"]).get("song_id") != song_id:
                raise ValueError("song_selection_changed")
            db.execute("INSERT INTO song_download_attempts VALUES (?,?,'forbidden','source_http_403',?) "
                       "ON CONFLICT(job_id,song_id) DO UPDATE SET status='forbidden',error='source_http_403',updated=excluded.updated",
                       (job_id, song_id, time.time()))
            failed = {item[0] for item in db.execute("SELECT song_id FROM song_download_attempts WHERE job_id=? AND status='forbidden'", (job_id,))}
            remains = any(item["song_id"] not in failed for item in json.loads(row["search_results"]))
            db.execute("UPDATE jobs SET status=?,error=?,retry_revision=retry_revision+1,updated=? WHERE id=?",
                       ("awaiting_reselection" if remains else "failed",
                        "source_http_403" if remains else "source_candidates_exhausted", time.time(), job_id))

    def reselect(self, job_id: str, scope: str, song_id: int | None, revision: int, *, selection_error=False):
        self.get(job_id, scope)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=? AND scope=?", (job_id, scope)).fetchone()
            if row["retry_revision"] != revision:
                raise ValueError("stale_reselection")
            selection = {"song_id": song_id, "retry_revision": revision}
            if row["status"] != "awaiting_reselection":
                if row["status"] != "canceled" and json.loads(row["selection"]) == selection:
                    return
                raise ValueError("Song is no longer awaiting reselection")
            failed = {item[0] for item in db.execute("SELECT song_id FROM song_download_attempts WHERE job_id=? AND status='forbidden'", (job_id,))}
            song = next((item for item in json.loads(row["search_results"]) if item["song_id"] == song_id and song_id not in failed), None)
            if song_id is not None and song is None:
                raise ValueError("selected_candidate_was_eliminated_or_unknown")
            error = "" if song else ("reselection_model_failed" if selection_error else "no_suitable_remaining_candidate")
            db.execute("UPDATE jobs SET selection=?,song=?,status=?,error=?,updated=? WHERE id=?",
                       (json.dumps(selection), json.dumps(song or json.loads(row["song"]), ensure_ascii=False),
                        "waiting_gpu" if song else "failed", error, time.time(), job_id))

    def remember_candidate(self, scope: str, song: dict) -> str:
        if not re.fullmatch(r"qq:[0-9]{1,20}:(?:dm|group):[0-9]{1,20}", scope):
            raise ValueError("Invalid candidate scope")
        receipt = uuid.uuid4().hex
        with self.connection() as db:
            db.execute("DELETE FROM song_candidates WHERE created<?", (time.time() - 3600,))
            db.execute("INSERT INTO song_candidates VALUES (?,?,?,?,?)",
                       (receipt, scope, song["song_id"], json.dumps(song, ensure_ascii=False), time.time()))
        return receipt

    def candidate(self, receipt: str, scope: str, song_id: int) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT data FROM song_candidates WHERE receipt=? AND scope=? AND song_id=? AND created>?",
                             (receipt, scope, song_id, time.time() - 3600)).fetchone()
        if row is None:
            raise ValueError("Candidate receipt is missing or expired")
        return json.loads(row[0])

    def update(self, job_id: str, status: str, *, song: dict | None = None, error=""):
        if status not in TERMINAL | {"queued", "searching", "awaiting_selection", "awaiting_reselection", "awaiting_commit", "waiting_gpu", "running"}:
            raise ValueError("Invalid song job status")
        with self.connection() as db:
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row[0] == "canceled":
                return
            if song is None:
                db.execute("UPDATE jobs SET status=?,error=?,updated=? WHERE id=?", (status, error, time.time(), job_id))
            else:
                db.execute("UPDATE jobs SET status=?,song=?,error=?,updated=? WHERE id=?",
                           (status, json.dumps(song, ensure_ascii=False), error, time.time(), job_id))

    def cancel(self, job_id: str, scope: str):
        self.get(job_id, scope)
        self.update(job_id, "canceled")

    def commit(self, job_id: str, scope: str):
        self.get(job_id, scope)
        with self.connection() as db:
            db.execute("UPDATE jobs SET status='waiting_gpu',updated=? WHERE id=? AND scope=? AND status='awaiting_commit'",
                       (time.time(), job_id, scope))

    def record_candidates(self, job_id: str, candidates: list[dict]):
        if len(candidates) > 20:
            raise ValueError("Too many song candidates")
        with self.connection() as db:
            db.execute("UPDATE jobs SET search_results=?,status=?,error=?,updated=? WHERE id=? AND status='searching'",
                (json.dumps(candidates, ensure_ascii=False), "awaiting_selection" if candidates else "needs_clarification",
                 "" if candidates else "song_not_found", time.time(), job_id))

    def choose(self, job_id: str, scope: str, song_id: int | None):
        if song_id is not None and (type(song_id) is not int or song_id <= 0):
            raise ValueError("Invalid selected song ID")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=? AND scope=?", (job_id, scope)).fetchone()
            if not row:
                raise KeyError("Song job not found")
            selection = {"song_id": song_id}
            if row["status"] != "awaiting_selection":
                if json.loads(row["selection"]) == selection:
                    return
                raise ValueError("Song is no longer awaiting selection")
            candidates = json.loads(row["search_results"])
            song = next((item for item in candidates if item["song_id"] == song_id), None)
            if song_id is not None and song is None:
                raise ValueError("Selected song is not a search candidate")
            db.execute("UPDATE jobs SET selection=?,song=?,status=?,error=?,updated=? WHERE id=?",
                (json.dumps(selection), json.dumps(song or {}, ensure_ascii=False),
                 "waiting_gpu" if song else "needs_clarification", "" if song else "no_matching_candidate", time.time(), job_id))

    def record_search_attempt(self, job_id: str, attempt: int, error: str | None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row[0] != "searching":
                return
            if error is None:
                db.execute("UPDATE jobs SET search_attempts=?,updated=? WHERE id=? AND status='searching'",
                           (attempt, time.time(), job_id))
                db.execute("INSERT OR IGNORE INTO song_search_attempts VALUES (?,?,?,?)",
                           (job_id, attempt, "in_progress", time.time()))
            else:
                db.execute("UPDATE jobs SET search_attempts=?,search_error=?,updated=? WHERE id=? AND status='searching'",
                           (attempt, error, time.time(), job_id))
                db.execute("UPDATE song_search_attempts SET error=?,updated=? WHERE job_id=? AND attempt=?",
                           (error, time.time(), job_id, attempt))

    def next_job(self) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT id FROM jobs WHERE status IN ('queued','waiting_gpu') ORDER BY updated LIMIT 1").fetchone()
        return self.get(row[0]) if row else None

    def recover(self):
        with self.connection() as db:
            db.execute("UPDATE jobs SET status=CASE WHEN selection!='{}' THEN 'waiting_gpu' ELSE 'queued' END,error='' "
                       "WHERE status IN ('searching','running')")

    def output(self, job_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise ValueError("Invalid job ID")
        return self.directory / "song_jobs" / job_id / "result.wav"

    def public(self, job_id: str, scope: str) -> dict:
        job = self.get(job_id, scope)
        song = job["song"]
        return {"job_id": job_id, "status": job["status"], "mode": job["payload"]["mode"],
                "song": song, "error": job["error"],
                "search_attempts": job["search_attempts"], "search_error": job["search_error"],
                "search_history": job["search_history"],
                "search_results": job["search_results"], "selection": job["selection"],
                "remaining_candidates": job["remaining_candidates"], "download_attempts": job["download_attempts"],
                "retry_revision": job["retry_revision"],
                "has_audio": job["status"] == "completed" and self.output(job_id).is_file()}


def normalize(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def select_song(source: NetEaseSource, music: dict) -> dict:
    results = source.search((music["query"] + (" " + music["artist"] if music.get("artist") else ""))[:120])
    candidates = [s for s in results if normalize(s.title) == normalize(music["query"]) and
                  (not music.get("artist") or normalize(music["artist"]) in normalize(s.artists))]
    if not candidates:
        raise ValueError("song_not_found")
    available = [s for s in candidates if s.available]
    song = next((s for s in available if s.duration_seconds >= 180), None) or (available or candidates)[0]
    result = song.public()
    result["link"] = f"https://music.163.com/song?id={song.song_id}"
    return result


def _search_with_retry(operation, *, attempts_used: int = 0,
                           last_error: str = "", on_attempt=None, should_stop=lambda: False,
                           sleep=None) -> dict:
    """At most three complete searches per job, including attempts before restart."""
    sleep = sleep or time.sleep
    if attempts_used >= SEARCH_MAX_ATTEMPTS:
        raise SongSourceError(last_error or "source_attempts_exhausted")
    for attempt in range(attempts_used + 1, SEARCH_MAX_ATTEMPTS + 1):
        if should_stop():
            raise SongSourceError("song_search_canceled")
        if on_attempt:
            on_attempt(attempt, None)  # Reserve before network I/O, including crash cases.
        try:
            song = operation()
        except Exception as exc:
            failure = classify_search_error(exc)
            if failure is None:
                raise
            if on_attempt:
                on_attempt(attempt, failure.code)
            if not failure.retryable or attempt == SEARCH_MAX_ATTEMPTS:
                if failure is exc:
                    raise
                raise failure from exc
            if should_stop():
                raise SongSourceError("song_search_canceled")
            sleep(2 ** (attempt - 1))  # 1 s, then 2 s; no additional provider-layer retries.
        else:
            if on_attempt:
                on_attempt(attempt, "")
            return song


def select_song_with_retry(source: NetEaseSource, music: dict, **kwargs) -> dict:
    """Legacy offline checks only; production selection is made by the Agent."""
    return _search_with_retry(lambda: select_song(source, music), **kwargs)


def search_candidates_with_retry(source: NetEaseSource, music: dict, **kwargs) -> list[dict]:
    def search():
        return [{**song.public(), "link": f"https://music.163.com/song?id={song.song_id}"}
                for song in source.search(music["query"])[:20]]
    return _search_with_retry(search, **kwargs)


def cleanup(directory: Path, *, max_age=86400):
    """Only remove old, terminal job caches and bounded generated media files."""
    import shutil
    store = SongStore(directory)
    with store.connection() as db:
        rows = db.execute("SELECT id FROM jobs WHERE status IN ('completed','failed','canceled','needs_clarification') AND updated<?",
                          (time.time() - max_age,)).fetchall()
    base = (directory / "song_jobs").resolve()
    for row in rows:
        target = (base / row[0]).resolve()
        if target.parent == base and re.fullmatch(r"[a-f0-9]{32}", target.name) and target.is_dir():
            shutil.rmtree(target)
    cache = directory / "media_cache"
    if cache.is_dir():
        for item in cache.glob("*.wav"):
            if re.fullmatch(r"[a-f0-9]{32}\.wav", item.name) and time.time() - item.stat().st_mtime > max_age:
                item.unlink()
