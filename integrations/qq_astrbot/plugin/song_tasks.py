"""Durable music lifetimes, separate from foreground conversation revisions."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time


class SongCanceled(RuntimeError):
    pass


class SongHeld(RuntimeError):
    pass


class SongFinished(RuntimeError):
    pass


class SongDeliveryUncertain(RuntimeError):
    pass


class SongTasks:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, scope TEXT, owner TEXT,
                    mode TEXT, run_id TEXT, title TEXT, revision INTEGER DEFAULT 1,
                    state TEXT DEFAULT 'accepted', reason TEXT DEFAULT '', created REAL);
                CREATE TABLE IF NOT EXISTS holds(scope TEXT, owner TEXT, version INTEGER,
                    PRIMARY KEY(scope,owner));
                CREATE TABLE IF NOT EXISTS parts(task_id TEXT, part TEXT, state TEXT,
                    PRIMARY KEY(task_id,part));
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

    def hold(self, scope, owner, version):
        with self.connection() as db:
            db.execute("INSERT INTO holds VALUES (?,?,?) ON CONFLICT(scope,owner) DO UPDATE SET version=excluded.version",
                       (scope, owner, version))

    def release(self, scope, owner, version):
        with self.connection() as db:
            db.execute("DELETE FROM holds WHERE scope=? AND owner=? AND version=?", (scope, owner, version))

    def reset_holds(self):
        # Foreground runs do not resume after plugin startup. Their temporary
        # control holds must not strand independently accepted music tasks.
        with self.connection() as db:
            db.execute("DELETE FROM holds")

    def register(self, key, scope, owner, mode, run_id, title):
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO tasks(id,scope,owner,mode,run_id,title,created) VALUES (?,?,?,?,?,?,?)",
                       (key, scope, owner, mode, run_id, title, time.time()))
            row = db.execute("SELECT * FROM tasks WHERE id=?", (key,)).fetchone()
        if (row['scope'], row['owner'], row['mode'], row['run_id']) != (scope, owner, mode, run_id):
            raise ValueError("conflicting_song_task")
        return dict(row)

    def pending(self, scope, owner=None, mode=None):
        query, values = "SELECT * FROM tasks WHERE scope=? AND state='accepted'", [scope]
        if owner is not None:
            query += " AND owner=?"
            values.append(owner)
        if mode is not None:
            query += " AND mode=?"
            values.append(mode)
        with self.connection() as db:
            return [dict(row) for row in db.execute(query + " ORDER BY created", values)]

    def cancel(self, scope, *, owner=None, mode=None, except_id=None, reason="explicit_cancel"):
        rows = self.pending(scope, owner, mode)
        changed = []
        with self.connection() as db:
            for row in rows:
                if row['id'] == except_id:
                    continue
                if db.execute("UPDATE tasks SET state='canceled',revision=revision+1,reason=? WHERE id=? AND state='accepted'",
                              (reason, row['id'])).rowcount:
                    changed.append(row)
        return changed

    def finish(self, key, state):
        with self.connection() as db:
            db.execute("UPDATE tasks SET state=? WHERE id=? AND state='accepted'", (state, key))

    def guard(self, payload, *, ready=True):
        with self.connection() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (payload['task_id'],)).fetchone()
            if row is not None and row['state'] == 'uncertain':
                raise SongDeliveryUncertain('song_delivery_is_uncertain')
            if row is not None and row['state'] in {'sent', 'failed'}:
                raise SongFinished('song_already_delivered')
            if (row is None or row['state'] != 'accepted' or row['revision'] != payload['task_revision'] or
                    row['scope'] != payload['scope'] or row['owner'] != payload['owner']):
                raise SongCanceled("song_task_changed")
            if ready and db.execute("SELECT 1 FROM holds WHERE scope=? AND owner=?", (row['scope'], row['owner'])).fetchone():
                raise SongHeld("song_control_request_pending")

    def part_sent(self, key, part):
        with self.connection() as db:
            row = db.execute("SELECT state FROM parts WHERE task_id=? AND part=?", (key, part)).fetchone()
        return bool(row and row[0] == 'sent')

    def begin_part(self, key, part):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM parts WHERE task_id=? AND part=?", (key, part)).fetchone()
            if row:
                if row[0] == 'sent':
                    return False
                raise SongDeliveryUncertain("song_part_may_have_been_sent")
            db.execute("INSERT INTO parts VALUES (?,?,'sending')", (key, part))
        return True

    def finish_part(self, key, part):
        with self.connection() as db:
            db.execute("UPDATE parts SET state='sent' WHERE task_id=? AND part=?", (key, part))


class SongDeliveryBot:
    """Check task lifetime after AstrBot finishes asynchronous media encoding."""
    def __init__(self, bot, tasks, payload, part, guard):
        self.bot, self.tasks, self.payload, self.part, self.guard = bot, tasks, payload, part, guard

    def __getattr__(self, name):
        target = getattr(self.bot, name)
        if name not in {'send', 'send_group_msg', 'send_private_msg', 'call_action'}:
            return target
        async def guarded(*args, **kwargs):
            if name == 'call_action' and not (args and str(args[0]).startswith('send_')):
                return await target(*args, **kwargs)
            self.guard(self.payload)
            if not self.tasks.begin_part(self.payload['task_id'], self.part):
                return None
            result = await target(*args, **kwargs)
            self.tasks.finish_part(self.payload['task_id'], self.part)
            return result
        return guarded
