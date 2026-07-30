from __future__ import annotations

import json
import time
from pathlib import Path

from runtime.adapters.wechat_desktop.client import WeChatDesktopClient
from runtime.adapters.wechat_desktop.config import WeChatDesktopConfig
from runtime.adapters.wechat_desktop.deduplicator import MessageDeduplicator
from runtime.adapters.wechat_desktop.message_buffer import MessageBuffer
from runtime.adapters.wechat_desktop.message_parser import MessageParser
from runtime.adapters.wechat_desktop.models import BufferedDesktopBatch, DesktopMessage
from runtime.adapters.wechat_desktop.ui_snapshot import ChatSessionSnapshot


class MessageMonitor:
    def __init__(
        self,
        *,
        config: WeChatDesktopConfig,
        client: WeChatDesktopClient,
        deduplicator: MessageDeduplicator | None = None,
        buffer: MessageBuffer | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.parser = MessageParser(account_key=config.account.account_key)
        self.deduplicator = deduplicator or MessageDeduplicator(state_path=config.dedup_path)
        self.buffer = buffer or MessageBuffer(
            buffer_seconds=config.monitor.buffer_seconds,
            max_text_chars=config.reply.max_text_chars,
        )

    def poll_once(self) -> list[BufferedDesktopBatch]:
        started = time.perf_counter()
        sessions = self._configured_contact_sessions()
        if not sessions:
            sessions = self.client.list_sessions(limit=self.config.monitor.max_visible_sessions)
        ready: list[BufferedDesktopBatch] = []
        for session in sessions:
            ready.extend(self._poll_session(session))
        ready.extend(self.buffer.flush_expired())
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if elapsed_ms >= 1000:
            self._log("slow_poll", elapsed_ms=elapsed_ms, session_count=len(sessions), ready_count=len(ready))
        return ready

    def flush(self) -> list[BufferedDesktopBatch]:
        return self.buffer.flush_all()

    def _configured_contact_sessions(self) -> tuple[ChatSessionSnapshot, ...]:
        contacts = tuple(
            dict.fromkeys(
                [
                    *self.config.contacts.allow_contact_keys,
                    *self.config.contacts.allow_display_names,
                ]
            )
        )
        if not contacts:
            return ()
        return tuple(
            ChatSessionSnapshot(
                contact_key=contact,
                display_name=contact,
                title=contact,
                unread=True,
                last_text="",
                metadata={"source": "configured_allowlist"},
            )
            for contact in contacts
            if contact.strip()
        )

    def _poll_session(self, session: ChatSessionSnapshot) -> list[BufferedDesktopBatch]:
        if self.config.monitor.ignore_group_chats and session.is_group:
            self._log("ignored_group_chat", session=session)
            return []
        if self.config.monitor.ignore_system_messages and session.is_system:
            self._log("ignored_system_chat", session=session)
            return []
        if not session.unread and not session.last_text:
            return []

        try:
            self.client.open_chat(session.contact_key)
            current_title = self.client.current_chat_title()
            if not current_title:
                self._log("ignored_unknown_chat_title", session=session)
                return []
            messages = self.client.recent_messages(limit=5)
            if not messages:
                return []
        except Exception as exc:
            self._log("session_poll_failed", session=session, error=str(exc))
            return []

        for snapshot in reversed(messages):
            message = self.parser.parse(
                ChatSessionSnapshot(
                    contact_key=session.contact_key,
                    display_name=session.display_name,
                    title=current_title,
                    unread=session.unread,
                    last_text=session.last_text,
                    is_group=session.is_group,
                    is_system=session.is_system,
                ),
                snapshot,
            )
            if not self._is_candidate(message):
                continue
            if self.deduplicator.is_seen(message):
                self._log("ignored_duplicate", message=message)
                self._log("no_unseen_text", session=session, latest_reason="duplicate")
                return []
            self.deduplicator.seen(message)
            self._log("accepted_text", message=message)
            return self.buffer.add(message)
        return []

    def _is_candidate(self, message: DesktopMessage) -> bool:
        if message.direction != "incoming":
            self._log("ignored_not_incoming", message=message)
            return False
        if message.message_type != "text":
            self._log("ignored_non_text", message=message)
            return False
        if not message.text.strip():
            self._log("ignored_empty_text", message=message)
            return False
        if not self.config.contacts.is_allowed(message.contact_key, message.contact_display_name):
            self._log("ignored_not_allowed", message=message)
            return False
        return True

    def _log(
        self,
        event: str,
        *,
        message: DesktopMessage | None = None,
        session: ChatSessionSnapshot | None = None,
        **fields: object,
    ) -> None:
        log_path = Path(self.config.log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {"ts": int(time.time()), "event": event, **fields}
        if message is not None:
            payload["message"] = message.to_log_dict()
        if session is not None:
            payload["session"] = {
                "contact_key": session.contact_key[:2] + "...",
                "display_name": session.display_name[:2] + "...",
                "title": session.title[:2] + "...",
                "unread": session.unread,
                "is_group": session.is_group,
                "is_system": session.is_system,
            }
        with log_path.open("a", encoding="utf-8") as log:
            log.write(json.dumps(payload, ensure_ascii=False) + "\n")
