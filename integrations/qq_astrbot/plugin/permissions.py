"""Code-level capabilities. Chat text can never grant an action."""

from __future__ import annotations

from copy import copy
from datetime import datetime
import sqlite3
import re
from pathlib import Path
from typing import Callable


FORBIDDEN = frozenset({"DELETE", "PROFILE_CHANGE", "UPLOAD_ARBITRARY", "EXEC_SHELL",
                       "MODEL_ACTIVATE", "TRAIN", "WRITE_SYSTEM_FILES"})
# Trusted source registration, never inferred from a tool name/description/model output.
TOOL_ACTIONS = {"q2_echo": "READ_PUBLIC"}


class PermissionPolicy:
    def __init__(self, db_path: Path, *, dm_allowlist: tuple[str, ...] = (),
                 dm_daily_limit: int = 1, qzone_gate_passed: bool = False,
                 qzone_authors: tuple[str, ...] = ()) -> None:
        self.dm_allowlist = frozenset(dm_allowlist)
        self.dm_daily_limit = max(0, dm_daily_limit)
        self.qzone_gate_passed = qzone_gate_passed
        self.qzone_authors = frozenset(qzone_authors)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, timeout=5, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS outbound_quota ("
                        "scope TEXT, recipient TEXT, day TEXT, request_id TEXT, "
                        "PRIMARY KEY(scope, request_id))")

    def allowed(self, action: str, *, actor: str = "", recipient: str = "",
                public_candidate: bool = False) -> bool:
        if action in FORBIDDEN:
            return False
        if action == "READ_PUBLIC":
            return True
        if action == "SEND_DM_ALLOWLIST":
            return recipient in self.dm_allowlist and self.dm_daily_limit > 0
        if action == "PUBLISH_QZONE":
            return self.qzone_gate_passed and actor in self.qzone_authors and public_candidate
        return False

    def reserve_dm(self, scope: str, recipient: str, request_id: str, *, day: str | None = None) -> bool:
        """Atomic, persistent quota; failed sends retain reservations (fail closed)."""
        if not request_id or not self.allowed("SEND_DM_ALLOWLIST", recipient=recipient):
            return False
        match = re.fullmatch(r"qq:([0-9]{1,20}):(?:dm|group):[0-9]{1,20}", scope)
        if not match:
            return False
        # An actor cannot multiply the per-recipient quota by changing groups.
        scope = f"qq:{match[1]}"
        day = day or datetime.now().astimezone().date().isoformat()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            duplicate = self.db.execute("SELECT 1 FROM outbound_quota WHERE scope=? AND request_id=?",
                                        (scope, request_id)).fetchone()
            count = self.db.execute("SELECT count(*) FROM outbound_quota WHERE scope=? AND recipient=? AND day=?",
                                    (scope, recipient, day)).fetchone()[0]
            if duplicate or count >= self.dm_daily_limit:
                self.db.rollback()
                return False
            self.db.execute("INSERT INTO outbound_quota VALUES (?, ?, ?, ?)", (scope, recipient, day, request_id))
            self.db.commit()
            return True
        except BaseException:
            self.db.rollback()
            raise

    def restrict_tools(self, tool_set, active: Callable[[], bool], *, handlers: dict[str, Callable]):
        """Copy before filtering; wrap execution rather than relying on a hook veto.

        Only the audited, read-only Q2 echo adapter exists in Q3. Future side-effect
        adapters must reserve their capability at execution, then be registered here.
        """
        if tool_set is None:
            return None
        safe = copy(tool_set)
        safe.tools = []
        for tool in tool_set.tools:
            action = TOOL_ACTIONS.get(tool.name)
            if action != "READ_PUBLIC" or not self.allowed(action) or tool.name not in handlers:
                continue
            wrapped = copy(tool)
            # Bind the audited adapter supplied by our plugin. A tool from
            # another plugin using the same name cannot replace its handler.
            original = handlers[tool.name]

            def guard(handler):
                async def guarded(*args, **kwargs):
                    if not active():
                        raise PermissionError("QQ event is no longer authorized")
                    return await handler(*args, **kwargs)
                return guarded

            wrapped.handler = guard(original)
            safe.tools.append(wrapped)
        return safe

    def close(self) -> None:
        self.db.close()
