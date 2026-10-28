"""QQ trigger priorities, per-group controls and bounded automatic participation."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import random
import re
import sqlite3
import time

from .memory_context import ContextMessage, MemoryContext, session_key


@dataclass(frozen=True)
class Incoming:
    bot_uin: str
    sender_uin: str
    message_id: str
    text: str
    group_id: str = ""
    display_name: str = ""
    at_bot: bool = False
    reply_to: str | None = None
    reply_sender: str = ""
    post_type: str = "message"
    member_count: int | None = None
    is_group_admin: bool = False

    @property
    def scope(self) -> str:
        return session_key(self.bot_uin, self.sender_uin, self.group_id)


@dataclass(frozen=True)
class RouteDecision:
    scope: str
    trigger: str
    should_reply: bool
    revision: int = 0


class MessageRouter:
    def __init__(self, db_path: Path, *, context: MemoryContext | None = None,
                 wake_words: tuple[str, ...] = ("爱莉希雅", "elysia"),
                 auto_probability: float = 0, auto_hourly_limit: int = 3,
                 auto_cooldown: float = 60, max_group_members: int = 2000) -> None:
        if not (0 <= auto_probability <= 1 and 0 <= auto_hourly_limit <= 3 and
                auto_cooldown >= 60 and max_group_members > 0):
            raise ValueError("Invalid QQ routing limits")
        self.context = context or MemoryContext()
        self.wake_words = tuple(w for w in wake_words if w)
        self.auto_probability, self.auto_hourly_limit = auto_probability, auto_hourly_limit
        self.auto_cooldown, self.max_group_members = auto_cooldown, max_group_members
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, timeout=5)
        self.db.execute("CREATE TABLE IF NOT EXISTS group_control (scope TEXT PRIMARY KEY, settings TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS auto_reply (scope TEXT, created_at REAL)")
        self.db.commit()
        self.revisions: OrderedDict[tuple[str, str], int] = OrderedDict()

    def settings(self, scope: str) -> dict:
        row = self.db.execute("SELECT settings FROM group_control WHERE scope=?", (scope,)).fetchone()
        return json.loads(row[0]) if row else {"enabled": True, "quiet": None}

    @staticmethod
    def _minutes(value: str) -> int:
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
            raise ValueError("Invalid quiet hours")
        hour, minute = map(int, value.split(":"))
        return hour * 60 + minute

    def is_active(self, scope: str, *, local_now: datetime | None = None) -> bool:
        settings = self.settings(scope)
        if not settings["enabled"]:
            return False
        if quiet := settings.get("quiet"):
            start, end = map(self._minutes, quiet)
            local_now = local_now or datetime.now().astimezone()
            minute = local_now.hour * 60 + local_now.minute
            if start == end or (start <= minute < end if start < end else minute >= start or minute < end):
                return False
        return True

    def control(self, msg: Incoming) -> str | None:
        """Deterministic command; role comes from OneBot, never the message text."""
        parts = msg.text.strip().split()
        if not parts or parts[0] != "/q3":
            return None
        if not msg.group_id or not msg.is_group_admin:
            return "此命令仅限本群群主或管理员使用。"
        settings = self.settings(msg.scope)
        if parts[1:] == ["on"]:
            settings["enabled"] = True
        elif parts[1:] == ["off"]:
            settings["enabled"] = False
        elif parts[1:] == ["quiet", "off"]:
            settings["quiet"] = None
        elif len(parts) == 4 and parts[1] == "quiet":
            try:
                self._minutes(parts[2])
                self._minutes(parts[3])
            except ValueError:
                return "时间格式为 HH:MM。"
            settings["quiet"] = parts[2:]
        elif parts[1:] == ["status"]:
            return f"本群状态：{'开启' if settings['enabled'] else '关闭'}；静默时段：{settings['quiet'] or '无'}"
        else:
            return "用法：/q3 on|off|status；/q3 quiet HH:MM HH:MM；/q3 quiet off"
        settings["generation"] = settings.get("generation", 0) + 1
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO group_control VALUES (?, ?)",
                            (msg.scope, json.dumps(settings)))
        return "本群设置已更新。"

    def generation(self, scope: str) -> int:
        return self.settings(scope).get("generation", 0)

    def route(self, msg: Incoming, *, now: float | None = None,
              local_now: datetime | None = None, roll: float | None = None) -> RouteDecision:
        now = time.time() if now is None else now
        scope = msg.scope
        if msg.post_type != "message" or msg.sender_uin == msg.bot_uin:
            return RouteDecision(scope, "ignored", False)
        if msg.group_id and (not self.is_active(scope, local_now=local_now) or
                             (msg.member_count is not None and msg.member_count > self.max_group_members)):
            return RouteDecision(scope, "disabled", False)
        merged = self.context.add(scope, ContextMessage(msg.sender_uin, msg.display_name, msg.text,
                                                       [msg.message_id], msg.at_bot, msg.reply_to, now))
        key = (scope, msg.sender_uin)
        revision = self.revisions.get(key, 0) + 1
        self.revisions[key] = revision
        self.revisions.move_to_end(key)
        while len(self.revisions) > self.context.max_sessions * 20:
            self.revisions.popitem(last=False)
        if not msg.group_id:
            trigger = "dm"
        elif msg.at_bot:
            trigger = "at"
        elif msg.reply_to and msg.reply_sender == msg.bot_uin:
            trigger = "reply"
        elif any(re.search(r"(?<![a-zA-Z0-9_])" + re.escape(w) + r"(?![a-zA-Z0-9_])", merged.text, re.I)
                 if w.isascii() else w in merged.text for w in self.wake_words):
            trigger = "wake"
        else:
            trigger = "passive"
            if (self.auto_probability > 0 and msg.member_count is not None and
                    (random.random() if roll is None else roll) < self.auto_probability):
                with self.db:
                    self.db.execute("DELETE FROM auto_reply WHERE created_at <= ?", (now - 3600,))
                    count, last = self.db.execute("SELECT count(*), max(created_at) FROM auto_reply WHERE scope=?",
                                                  (scope,)).fetchone()
                    if count < self.auto_hourly_limit and (last is None or now - last >= self.auto_cooldown):
                        self.db.execute("INSERT INTO auto_reply VALUES (?, ?)", (scope, now))
                        trigger = "auto"
        return RouteDecision(scope, trigger, trigger != "passive", revision)

    def is_latest(self, msg: Incoming, decision: RouteDecision) -> bool:
        return self.revisions.get((decision.scope, msg.sender_uin)) == decision.revision

    def close(self) -> None:
        self.db.close()
