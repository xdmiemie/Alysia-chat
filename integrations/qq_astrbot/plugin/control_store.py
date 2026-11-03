"""Persistent UI telemetry, bounded public projections and edit revisions."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time


def revision(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def current_content(content):
    if isinstance(content, list):
        return [
            current_content(p.get("text", ""))
            if p.get("type") == "text"
            else "[图片或音视频附件]"
            for p in content
            if isinstance(p, dict)
        ]
    if not isinstance(content, str):
        return content
    try:
        value = json.loads(content)
    except ValueError:
        return content
    if isinstance(value, dict) and "current_message" in value:
        return value["current_message"]
    return content


def public_exchange(record, prompts):
    request = record.get("request") or {}
    response = record.get("response") or {}
    messages = request.get("messages", []) if isinstance(request, dict) else []
    # Only the latest user turn and the tool rounds belonging to it are shown.
    start = next(
        (
            i
            for i in range(len(messages) - 1, -1, -1)
            if messages[i].get("role") == "user"
        ),
        len(messages),
    )
    visible = [
        {
            k: current_content(v) if k == "content" else v
            for k, v in m.items()
            if k in {"role", "content", "tool_calls", "tool_call_id", "name"}
        }
        for m in messages[start:]
        if m.get("role") != "system"
    ]
    system = "\n".join(
        str(m.get("content", "")) for m in messages if m.get("role") == "system"
    )
    names = list(record.get("prompt_names") or [])
    for key, value in prompts.items():
        if not isinstance(value, str):
            continue
        fragments = [
            part.strip()
            for part in re.split(r"\$[a-zA-Z_]+", value)
            if len(part.strip()) >= 12
        ]
        if fragments and all(part in system for part in fragments) and key not in names:
            names.append(key)
    replies = (
        [
            {
                k: v
                for k, v in (choice.get("message") or {}).items()
                if k in {"role", "content", "tool_calls"}
            }
            for choice in response.get("choices", [])
        ]
        if isinstance(response, dict)
        else []
    )
    return {
        "id": record.get("request_id"),
        "time": record.get("started_at"),
        "phase": record.get("phase"),
        "session": record.get("session_id"),
        "status": record.get("status"),
        "duration_ms": record.get("duration_ms"),
        "input": visible,
        "output": replies,
        "prompt_names": names,
    }


class ControlStore:
    def __init__(self, directory):
        self.path = Path(directory) / "web_control.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS usage(id TEXT PRIMARY KEY, started TEXT, phase TEXT,
                    session TEXT, status TEXT, input INTEGER, output INTEGER, cached INTEGER,
                    reported INTEGER NOT NULL, duration REAL);
                CREATE TABLE IF NOT EXISTS edits(id INTEGER PRIMARY KEY, kind TEXT, target TEXT,
                    before_json TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS model_jobs(id TEXT PRIMARY KEY, state TEXT, payload TEXT,
                    error TEXT, created REAL, updated REAL);
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

    def record_usage(self, record):
        if not record.get("request_id"):
            return
        response = record.get("response")
        usage = response.get("usage") if isinstance(response, dict) else None
        reported = (
            isinstance(usage, dict)
            and isinstance(usage.get("prompt_tokens"), int)
            and isinstance(usage.get("completion_tokens"), int)
        )
        usage = usage or {}
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO usage VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    record["request_id"],
                    record.get("started_at"),
                    record.get("phase") or "agent",
                    record.get("session_id"),
                    record.get("status"),
                    usage.get("prompt_tokens") if reported else None,
                    usage.get("completion_tokens") if reported else None,
                    (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
                    if reported
                    else None,
                    int(reported),
                    record.get("duration_ms"),
                ),
            )

    def audit(self, kind, target, before):
        with self.connection() as db:
            db.execute(
                "INSERT INTO edits(kind,target,before_json,created) VALUES(?,?,?,?)",
                (kind, target, json.dumps(before, ensure_ascii=False), time.time()),
            )

    def usage(self, since=""):
        with self.connection() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT phase,count(*) calls,sum(reported) reported,"
                    "coalesce(sum(input),0) input,coalesce(sum(output),0) output,coalesce(sum(cached),0) cached "
                    "FROM usage WHERE started>=? GROUP BY phase ORDER BY phase",
                    (since,),
                )
            ]
            days = [
                dict(r)
                for r in db.execute(
                    "SELECT substr(started,1,10) day,count(*) calls,"
                    "coalesce(sum(input),0) input,coalesce(sum(output),0) output FROM usage WHERE started>=? "
                    "GROUP BY day ORDER BY day DESC LIMIT 90",
                    (since,),
                )
            ]
        return {
            "phases": rows,
            "days": days,
            "timezone": "UTC",
            "unreported": sum(r["calls"] - r["reported"] for r in rows),
        }


def validate_messages(messages):
    if not isinstance(messages, list) or len(messages) > 500:
        raise ValueError("上下文必须为最多 500 条消息的列表")
    pending, used = set(), set()
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {
            "user",
            "assistant",
            "tool",
        }:
            raise ValueError(
                "上下文只允许 user、assistant、tool 消息；提示词请在提示词栏编辑"
            )
        if set(message) - {"role", "content", "tool_calls", "tool_call_id", "name"}:
            raise ValueError("上下文含不支持的字段")
        if not isinstance(message.get("content"), (str, list, type(None))):
            raise ValueError("消息内容格式无效")
        if message["role"] == "tool":
            key = message.get("tool_call_id")
            if key not in pending:
                raise ValueError("工具结果必须对应前面的工具调用")
            pending.remove(key)
        else:
            if pending:
                raise ValueError("请保留完整的工具调用及结果，或一并删除")
            if message.get("tool_calls"):
                if message["role"] != "assistant" or not isinstance(
                    message["tool_calls"], list
                ):
                    raise ValueError("仅 assistant 可包含工具调用")
                for call in message["tool_calls"]:
                    key = call.get("id")
                    function = call.get("function") or {}
                    if (
                        not key
                        or key in used
                        or call.get("type") != "function"
                        or not function.get("name")
                    ):
                        raise ValueError("工具调用 ID 或函数无效")
                    args = json.loads(function.get("arguments", ""))
                    if not isinstance(args, dict):
                        raise ValueError("工具参数必须为 JSON 对象")
                    pending.add(key)
                    used.add(key)
    if pending:
        raise ValueError("缺少工具结果")
    if len(json.dumps(messages).encode()) > 2_000_000:
        raise ValueError("上下文超过 2 MB")
    return messages
