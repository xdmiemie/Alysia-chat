from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path

from runtime.adapters.wechat_desktop.client import WeChatDesktopClient
from runtime.adapters.wechat_desktop.config import WeChatDesktopConfig
from runtime.adapters.wechat_desktop.deduplicator import MessageDeduplicator
from runtime.adapters.wechat_desktop.message_buffer import MessageBuffer
from runtime.adapters.wechat_desktop.message_parser import MessageParser
from runtime.adapters.wechat_desktop.models import BufferedDesktopBatch, DesktopMessage
from runtime.adapters.wechat_desktop.ui_snapshot import ChatSessionSnapshot
from runtime.adapters.wechat_desktop.ui_queue import SingleUIQueue


class MessageMonitor:
    def __init__(
        self,
        *,
        config: WeChatDesktopConfig,
        client: WeChatDesktopClient,
        deduplicator: MessageDeduplicator | None = None,
        buffer: MessageBuffer | None = None,
        ui_queue: SingleUIQueue | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.parser = MessageParser(account_key=config.account.account_key)
        self.deduplicator = deduplicator or MessageDeduplicator(state_path=config.dedup_path)
        self.buffer = buffer or MessageBuffer(
            buffer_seconds=config.monitor.buffer_seconds,
            max_text_chars=config.reply.max_text_chars,
        )
        self.ui_queue = ui_queue

    def poll_once(self) -> list[BufferedDesktopBatch]:
        return list(self.iter_ready_batches())

    async def iter_ready_batches_async(
        self,
        should_stop: Callable[[], bool] | None = None,
    ) -> AsyncIterator[BufferedDesktopBatch]:
        sessions = self._configured_contact_sessions()
        configured_allowlist = bool(sessions)
        if not sessions:
            sessions = self.client.list_sessions(limit=self.config.monitor.max_visible_sessions)
        ready_count = 0
        poll_elapsed_ms = 0
        stopped = False
        for session in sessions:
            if should_stop is not None and should_stop():
                stopped = True
                break
            if configured_allowlist:
                _terminal_log("开始检测一个白名单", contact=session.contact_key)
            started = time.perf_counter()
            if self.ui_queue is None:
                ready = self._poll_session(session)
            else:
                # Desktop scraping can spend several seconds inside UIA. Run it
                # off the event loop and serialize it with answer/hangup clicks.
                # A pending call job has priority 0 and runs before the next
                # ordinary contact poll (priority 90).
                ready = await self.ui_queue.run(
                    lambda current=session: asyncio.to_thread(self._poll_session, current),
                    request_id=f"poll:{session.contact_key}",
                    contact_key=session.contact_key,
                    priority=90,
                    timeout_seconds=self.config.ui.operation_timeout_seconds,
                )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            poll_elapsed_ms += elapsed_ms
            ready_count += len(ready)
            if configured_allowlist:
                _terminal_log(
                    "检测一个白名单结束",
                    contact=session.contact_key,
                    ready=len(ready),
                    elapsed_ms=elapsed_ms,
                )
            for batch in ready:
                yield batch
            await asyncio.sleep(0)
            if should_stop is not None and should_stop():
                stopped = True
                break

        if stopped:
            return

        started = time.perf_counter()
        expired = self.buffer.flush_expired()
        poll_elapsed_ms += int((time.perf_counter() - started) * 1000)
        ready_count += len(expired)
        for batch in expired:
            yield batch

        if poll_elapsed_ms >= 1000:
            self._log(
                "slow_poll",
                elapsed_ms=poll_elapsed_ms,
                session_count=len(sessions),
                ready_count=ready_count,
            )

    def iter_ready_batches(self) -> Iterator[BufferedDesktopBatch]:
        sessions = self._configured_contact_sessions()
        configured_allowlist = bool(sessions)
        if not sessions:
            sessions = self.client.list_sessions(limit=self.config.monitor.max_visible_sessions)
        ready_count = 0
        poll_elapsed_ms = 0
        for session in sessions:
            if configured_allowlist:
                _terminal_log("开始检测一个白名单", contact=session.contact_key)
            started = time.perf_counter()
            ready = self._poll_session(session)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            poll_elapsed_ms += elapsed_ms
            ready_count += len(ready)
            if configured_allowlist:
                _terminal_log(
                    "检测一个白名单结束",
                    contact=session.contact_key,
                    ready=len(ready),
                    elapsed_ms=elapsed_ms,
                )
            yield from ready

        started = time.perf_counter()
        expired = self.buffer.flush_expired()
        poll_elapsed_ms += int((time.perf_counter() - started) * 1000)
        ready_count += len(expired)
        yield from expired

        if poll_elapsed_ms >= 1000:
            self._log(
                "slow_poll",
                elapsed_ms=poll_elapsed_ms,
                session_count=len(sessions),
                ready_count=ready_count,
            )

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

        seen_newer_outgoing = False
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
            if message.direction == "outgoing":
                seen_newer_outgoing = True
                self._log("ignored_not_incoming", message=message)
                continue
            if not self._is_candidate(message):
                continue
            if seen_newer_outgoing:
                self._log("ignored_already_replied_tail", message=message)
                return []
            if self.deduplicator.is_seen(message):
                self._log("ignored_duplicate", message=message)
                self._log("no_unseen_text", session=session, latest_reason="duplicate")
                return []
            self.deduplicator.seen(message)
            self._log("accepted_text" if message.message_type == "text" else "accepted_voice", message=message)
            return self.buffer.add(message)
        return []

    def _is_candidate(self, message: DesktopMessage) -> bool:
        if message.direction != "incoming":
            self._log("ignored_not_incoming", message=message)
            return False
        if message.message_type == "text":
            if not message.text.strip():
                self._log("ignored_empty_text", message=message)
                return False
        elif message.message_type == "voice":
            if not message.is_bound_to_ui:
                self._log("ignored_unbound_voice", message=message)
                return False
        else:
            self._log("ignored_non_text", message=message)
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


def _terminal_log(message: str, **fields: object) -> None:
    suffix = " ".join(f"{key}={value}" for key, value in fields.items() if value not in (None, ""))
    print(f"[wechat] {message}{' ' + suffix if suffix else ''}", flush=True)
