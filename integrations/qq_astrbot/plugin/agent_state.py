"""Durable run budgets, conversation revisions, action receipts and send provenance."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Callable


MAX_FOLLOWUPS = 1


class Superseded(RuntimeError):
    pass


class BudgetExhausted(RuntimeError):
    pass


class VersionedBot:
    """Per-event proxy: recheck at OneBot dispatch, after async media encoding."""
    def __init__(self, bot, store, scope, version):
        self.bot, self.store, self.scope, self.version = bot, store, scope, version

    def __getattr__(self, name):
        target = getattr(self.bot, name)
        if name not in {"send", "send_group_msg", "send_private_msg", "call_action"}:
            return target

        async def guarded(*args, **kwargs):
            is_send = name != "call_action" or bool(args and str(args[0]).startswith("send_"))
            if is_send and not self.store.current(self.scope, self.version):
                raise Superseded("conversation_changed_at_onebot_dispatch")
            return await target(*args, **kwargs)
        return guarded


@dataclass
class AgentRun:
    run_id: str
    scope: str
    version: int
    started: float
    followups: int = 0
    requests: int = 0
    tool_calls: int = 0
    reply_retries: int = 0
    max_requests: int = 16
    max_tools: int = 16
    max_seconds: float = 180
    closing: bool = False
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    stop: Callable[[], None] | None = None
    speech: dict = field(default_factory=dict)
    candidates: dict = field(default_factory=dict)
    candidate_refs: dict[int, int] = field(default_factory=dict)

    def remember_song(self, song):
        song_id = song["song_id"]
        self.candidates[song_id] = song
        if song_id not in self.candidate_refs.values():
            self.candidate_refs[len(self.candidate_refs) + 1] = song_id

    def song_id(self, candidate):
        if candidate not in self.candidate_refs:
            raise ValueError("unknown_candidate_use_weapi_first")
        return self.candidate_refs[candidate]


class AgentStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.active: dict[str, AgentRun] = {}
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS versions(scope TEXT PRIMARY KEY, version INTEGER, message_id TEXT);
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, scope TEXT, version INTEGER,
                    started REAL, followups INTEGER DEFAULT 0, requests INTEGER DEFAULT 0,
                    tool_calls INTEGER DEFAULT 0, reply_retries INTEGER DEFAULT 0, status TEXT);
                CREATE TABLE IF NOT EXISTS actions(run_id TEXT, action_key TEXT, tool TEXT,
                    arguments TEXT, status TEXT, result TEXT, PRIMARY KEY(run_id,action_key));
                CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, run_id TEXT, kind TEXT,
                    payload TEXT, created REAL);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
            if "followups" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN followups INTEGER DEFAULT 0")
            if "reply_retries" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN reply_retries INTEGER DEFAULT 0")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def observe(self, scope: str, message_id: str) -> int:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT version,message_id FROM versions WHERE scope=?", (scope,)).fetchone()
            if old and old["message_id"] == message_id:
                return old["version"]
            version = old["version"] + 1 if old else 1
            db.execute("INSERT OR REPLACE INTO versions VALUES (?,?,?)", (scope, version, message_id))
        previous = self.active.pop(scope, None)
        if previous:
            previous.cancel.set()
            if previous.stop:
                previous.stop()
            self.status(previous, "superseded")
        return version

    def current(self, scope: str, version: int) -> bool:
        with self.connection() as db:
            row = db.execute("SELECT version FROM versions WHERE scope=?", (scope,)).fetchone()
        return bool(row and row[0] == version)

    def guard_sender(self, event, scope: str, version: int):
        original = event.send
        if hasattr(event, "bot"):
            event.bot = VersionedBot(event.bot, self, scope, version)

        async def send(chain):
            # Framework diagnostic chains are internal; only tool effects and
            # actual dialogue belong on the QQ transport.
            if getattr(chain, "type", None) in {"tool_call", "reasoning"}:
                return None
            if not self.current(scope, version):
                raise Superseded("conversation_changed_before_send")
            try:
                return await original(chain)
            except BaseException as exc:
                run = event.get_extra("qq_agent_run")
                if run:
                    superseded = isinstance(exc, Superseded)
                    self.record(run, "transport_rejected" if superseded else "transport_uncertain", {"version": version})
                    self.status(run, "superseded" if superseded else "delivery_uncertain")
                    if self.active.get(scope) is run:
                        self.active.pop(scope)
                cleanup = event.get_extra("qq_agent_cleanup")
                if cleanup:
                    cleanup()
                raise

        event.send = send

    def begin(self, scope: str, version: int, message_id: str, **limits) -> AgentRun:
        key = hashlib.sha256(f"{scope}:{message_id}".encode()).hexdigest()[:32]
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO runs(id,scope,version,started,status) VALUES (?,?,?,?,?)",
                       (key, scope, version, time.time(), "running"))
            row = db.execute("SELECT * FROM runs WHERE id=?", (key,)).fetchone()
        run = AgentRun(key, scope, version, row["started"], row["followups"], row["requests"], row["tool_calls"],
                       reply_retries=row["reply_retries"], **limits)
        self.active[scope] = run
        return run

    def ensure_current(self, run: AgentRun):
        if run.cancel.is_set() or not self.current(run.scope, run.version):
            raise Superseded("conversation_changed")

    def work(self, run: AgentRun):
        self.ensure_current(run)
        if time.time() - run.started >= run.max_seconds:
            raise BudgetExhausted("run_deadline")

    def consume(self, run: AgentRun, kind: str):
        self.work(run)
        field_name, maximum = {"followups": ("followups", getattr(self, "max_followups", MAX_FOLLOWUPS)), "reply_retries": ("reply_retries", 1),
                               "requests": ("requests", run.max_requests),
                               "tool_calls": ("tool_calls", run.max_tools)}[kind]
        if getattr(run, field_name) >= maximum:
            raise BudgetExhausted(kind)
        setattr(run, field_name, getattr(run, field_name) + 1)
        with self.connection() as db:
            db.execute(f"UPDATE runs SET {field_name}=? WHERE id=?", (getattr(run, field_name), run.run_id))

    async def wait_delivery(self, run: AgentRun, seconds: float):
        """Pace ready messages even after the work budget expires; new input still cancels."""
        self.ensure_current(run)
        if seconds > 0:
            try:
                await asyncio.wait_for(run.cancel.wait(), timeout=seconds)
            except TimeoutError:
                pass
        self.ensure_current(run)

    async def wait(self, run: AgentRun, operation, *, timeout: float | None = None):
        """Cancellation wins even when the old result finishes in the same loop turn."""
        try:
            self.work(run)
        except BaseException:
            if hasattr(operation, "close"):
                operation.close()
            raise
        task = asyncio.ensure_future(operation)
        canceled = asyncio.create_task(run.cancel.wait())
        remaining = max(0.001, run.max_seconds - (time.time() - run.started))
        try:
            done, _ = await asyncio.wait({task, canceled}, timeout=min(timeout or remaining, remaining),
                                         return_when=asyncio.FIRST_COMPLETED)
            self.ensure_current(run)
            if task not in done:
                raise TimeoutError("agent_operation_timeout")
            result = task.result()
            self.work(run)
            return result
        finally:
            for pending in (task, canceled):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, canceled, return_exceptions=True)

    def status(self, run: AgentRun, status: str):
        with self.connection() as db:
            db.execute("UPDATE runs SET status=? WHERE id=?", (status, run.run_id))

    def run_status(self, run_id: str) -> str | None:
        with self.connection() as db:
            row = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        return row[0] if row else None

    def record_id(self, run_id: str, kind: str, payload):
        encoded = json.dumps(payload, ensure_ascii=False, default=str)
        if len(encoded) > 64000:
            encoded = json.dumps({"truncated": True, "preview": encoded[:63000]}, ensure_ascii=False)
        with self.connection() as db:
            db.execute("INSERT INTO audit(run_id,kind,payload,created) VALUES (?,?,?,?)",
                       (run_id, kind, encoded, time.time()))

    def record(self, run: AgentRun, kind: str, payload):
        self.record_id(run.run_id, kind, payload)

    def reserve_action(self, run: AgentRun, tool: str, arguments: dict) -> tuple[str, dict | None]:
        self.work(run)
        value = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        key = hashlib.sha256(f"{tool}:{value}".encode()).hexdigest()[:32]
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,result FROM actions WHERE run_id=? AND action_key=?", (run.run_id, key)).fetchone()
            if row:
                # An interrupted/ambiguous action is never executed again automatically.
                result = json.loads(row["result"]) if row["result"] else {"ok": False, "status": "uncertain", "action_id": key}
                return key, {**result, "replayed": True}
            db.execute("INSERT INTO actions VALUES (?,?,?,?,?,?)", (run.run_id, key, tool, value, "running", ""))
        return key, None

    def action_result(self, run: AgentRun, key: str, result: dict):
        with self.connection() as db:
            db.execute("UPDATE actions SET status=?,result=? WHERE run_id=? AND action_key=?",
                       (result.get("status", "completed"), json.dumps(result, ensure_ascii=False), run.run_id, key))
        self.record(run, "tool_result", result)
