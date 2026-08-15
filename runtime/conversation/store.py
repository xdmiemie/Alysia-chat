from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True)
class Conversation:
    id: str
    character_id: str
    title: str
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class Message:
    id: str
    conversation_id: str
    role: str
    message_type: str
    text: str
    audio_path: str
    character_id: str
    model_version: str
    request_id: str
    parent_message_id: str
    created_at: int
    status: str


class ConversationStore:
    """Small SQLite repository used by every desktop chat session.

    Connections are deliberately short lived.  This keeps the repository safe
    when the Qt worker thread writes a reply while the UI thread reads history.
    """

    def __init__(self, path: str | Path = "storage/database/alysia.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    character_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    text TEXT,
                    audio_path TEXT,
                    character_id TEXT NOT NULL DEFAULT '',
                    model_version TEXT,
                    request_id TEXT,
                    parent_message_id TEXT,
                    created_at INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                );

                """
            )
            # Databases made by early development builds only had the columns
            # from the stage document. Add trace columns without losing history.
            existing = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(messages)").fetchall()
            }
            for name, declaration in (
                ("character_id", "TEXT NOT NULL DEFAULT ''"),
                ("request_id", "TEXT"),
                ("parent_message_id", "TEXT"),
            ):
                if name not in existing:
                    connection.execute(f"ALTER TABLE messages ADD COLUMN {name} {declaration}")
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_conversations_character_updated
                ON conversations(character_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_messages_conversation_created
                ON messages(conversation_id, created_at ASC);
                CREATE INDEX IF NOT EXISTS idx_messages_request
                ON messages(request_id);
                """
            )

    def create_conversation(self, character_id: str, title: str = "新对话") -> Conversation:
        character_id = character_id.strip()
        if not character_id:
            raise ValueError("character_id must not be empty")
        now = _now_ms()
        record = Conversation(uuid4().hex, character_id, title.strip() or "新对话", now, now)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO conversations(id, character_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (record.id, record.character_id, record.title, record.created_at, record.updated_at),
            )
        return record

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        return _conversation(row) if row else None

    def list_conversations(self, character_id: str | None = None) -> list[Conversation]:
        query = "SELECT * FROM conversations"
        params: tuple[object, ...] = ()
        if character_id is not None:
            query += " WHERE character_id = ?"
            params = (character_id,)
        query += " ORDER BY updated_at DESC, created_at DESC"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_conversation(row) for row in rows]

    def latest_or_create(self, character_id: str) -> Conversation:
        records = self.list_conversations(character_id)
        return records[0] if records else self.create_conversation(character_id)

    def rename_conversation(self, conversation_id: str, title: str) -> None:
        title = title.strip()
        if not title:
            raise ValueError("conversation title must not be empty")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (title, _now_ms(), conversation_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"conversation not found: {conversation_id}")

    def delete_conversation(self, conversation_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))

    def add_message(
        self,
        conversation_id: str,
        role: str,
        text: str = "",
        *,
        message_type: str = "text",
        audio_path: str = "",
        character_id: str = "",
        model_version: str = "",
        request_id: str = "",
        parent_message_id: str = "",
        status: str = "completed",
        message_id: str | None = None,
    ) -> Message:
        if role not in {"user", "assistant", "system"}:
            raise ValueError(f"unsupported message role: {role}")
        if status not in {"pending", "completed", "failed"}:
            raise ValueError(f"unsupported message status: {status}")
        now = _now_ms()
        record = Message(
            id=message_id or uuid4().hex,
            conversation_id=conversation_id,
            role=role,
            message_type=message_type,
            text=text,
            audio_path=audio_path,
            character_id=character_id,
            model_version=model_version,
            request_id=request_id,
            parent_message_id=parent_message_id,
            created_at=now,
            status=status,
        )
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone() is None:
                raise KeyError(f"conversation not found: {conversation_id}")
            connection.execute(
                """
                INSERT INTO messages(
                    id, conversation_id, role, message_type, text, audio_path,
                    character_id, model_version, request_id, parent_message_id,
                    created_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.conversation_id,
                    record.role,
                    record.message_type,
                    record.text,
                    record.audio_path,
                    record.character_id,
                    record.model_version,
                    record.request_id,
                    record.parent_message_id,
                    record.created_at,
                    record.status,
                ),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id)
            )
            if role == "user":
                self._set_automatic_title(connection, conversation_id, text)
        return record

    def get_message(self, message_id: str) -> Message | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return _message(row) if row else None

    def list_messages(self, conversation_id: str, limit: int | None = None) -> list[Message]:
        if limit is None:
            query = "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC, rowid ASC"
            params: tuple[object, ...] = (conversation_id,)
        else:
            query = """
                SELECT * FROM (
                    SELECT * FROM messages WHERE conversation_id = ?
                    ORDER BY created_at DESC, rowid DESC LIMIT ?
                ) ORDER BY created_at ASC
            """
            params = (conversation_id, max(0, limit))
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_message(row) for row in rows]

    def context_messages(self, conversation_id: str, max_turns: int = 8) -> list[Message]:
        records = self.list_messages(conversation_id)
        completed = [
            record for record in records if record.status == "completed" and record.role != "system"
        ]
        return completed[-max_turns * 2 :]

    @staticmethod
    def _set_automatic_title(connection: sqlite3.Connection, conversation_id: str, text: str) -> None:
        row = connection.execute(
            "SELECT title, (SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND role = 'user') AS count "
            "FROM conversations WHERE id = ?",
            (conversation_id, conversation_id),
        ).fetchone()
        if row and str(row["title"]) == "新对话" and int(row["count"]) == 1:
            compact = " ".join(text.split())
            title = compact[:24] + ("…" if len(compact) > 24 else "")
            if title:
                connection.execute(
                    "UPDATE conversations SET title = ? WHERE id = ?", (title, conversation_id)
                )


def _now_ms() -> int:
    return int(time.time() * 1000)


def _conversation(row: sqlite3.Row) -> Conversation:
    return Conversation(
        id=str(row["id"]),
        character_id=str(row["character_id"]),
        title=str(row["title"] or "新对话"),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


def _message(row: sqlite3.Row) -> Message:
    return Message(
        id=str(row["id"]),
        conversation_id=str(row["conversation_id"]),
        role=str(row["role"]),
        message_type=str(row["message_type"]),
        text=str(row["text"] or ""),
        audio_path=str(row["audio_path"] or ""),
        character_id=str(row["character_id"] or ""),
        model_version=str(row["model_version"] or ""),
        request_id=str(row["request_id"] or ""),
        parent_message_id=str(row["parent_message_id"] or ""),
        created_at=int(row["created_at"]),
        status=str(row["status"]),
    )
