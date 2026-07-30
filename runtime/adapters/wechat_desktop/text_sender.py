from __future__ import annotations

import asyncio
from collections import defaultdict

from runtime.adapters.wechat_desktop.client import WeChatDesktopClient
from runtime.adapters.wechat_desktop.config import UIConfig
from runtime.adapters.wechat_desktop.models import SendResult
from runtime.adapters.wechat_desktop.ui_queue import SingleUIQueue, get_global_ui_queue


class WeChatDesktopSender:
    def __init__(self, *, client: WeChatDesktopClient, ui: UIConfig, queue: SingleUIQueue | None = None) -> None:
        self.client = client
        self.ui = ui
        self.queue = queue
        self._contact_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def send_text(self, contact_key: str, text: str, request_id: str) -> SendResult:
        cleaned = text.strip()
        if not contact_key.strip():
            return SendResult(ok=False, request_id=request_id, contact_key=contact_key, error="contact_key is empty")
        if not cleaned:
            return SendResult(ok=False, request_id=request_id, contact_key=contact_key, error="text is empty")

        async with self._contact_locks[contact_key]:
            queue = self.queue or get_global_ui_queue()
            return await queue.run(
                lambda: self._send_text_locked(contact_key, cleaned, request_id),
                request_id=request_id,
                contact_key=contact_key,
                priority=40,
                timeout_seconds=self.ui.operation_timeout_seconds,
            )

    def _send_text_locked(self, contact_key: str, text: str, request_id: str) -> SendResult:
        last_error = ""
        for attempt in range(1, self.ui.max_send_retries + 2):
            try:
                print(f"Opening WeChat chat {contact_key} (attempt {attempt})...", flush=True)
                self.client.open_chat(contact_key)
                if self.ui.verify_chat_title_before_send:
                    title_before = self.client.current_chat_title()
                    if not title_before:
                        raise RuntimeError("could not verify target chat title before send")
                self.client.open_chat(contact_key)
                if self.ui.verify_chat_title_before_send and not self.client.current_chat_title():
                    raise RuntimeError("could not verify target chat title before trigger")
                print("Writing reply into WeChat input and sending...", flush=True)
                self.client.send_text(text, restore_clipboard=True)
                verified = True
                if self.ui.verify_own_message_after_send:
                    print("Verifying sent reply at WeChat chat tail...", flush=True)
                    verified = self.client.has_own_text_at_tail(text)
                    if not verified:
                        raise RuntimeError("sent text was not visible at chat tail")
                return SendResult(
                    ok=True,
                    request_id=request_id,
                    contact_key=contact_key,
                    attempts=attempt,
                    verified=verified,
                )
            except Exception as exc:
                last_error = str(exc)
                print(f"WeChat send attempt {attempt} failed: {last_error}", flush=True)
        return SendResult(
            ok=False,
            request_id=request_id,
            contact_key=contact_key,
            error=last_error,
            retryable=False,
            attempts=self.ui.max_send_retries + 1,
            verified=False,
        )
