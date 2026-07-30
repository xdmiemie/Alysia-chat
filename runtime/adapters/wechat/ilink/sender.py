from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any
from uuid import uuid4

from runtime.adapters.wechat.ilink.api import ILinkApiClient, ILinkApiError
from runtime.adapters.wechat.models import SendResult


class ILinkSender:
    def __init__(
        self,
        api: ILinkApiClient,
        *,
        max_text_length: int = 4096,
        max_retries: int = 2,
        retry_delay_seconds: float = 0.5,
        typing_enabled: bool = False,
    ) -> None:
        self.api = api
        self.max_text_length = max_text_length
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds
        self.typing_enabled = typing_enabled
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._idempotency: dict[tuple[str, str, str], SendResult] = {}

    async def send_text(self, to_user_id: str, context_token: str, text: str) -> SendResult:
        cleaned = " ".join(text.split()).strip()
        if not to_user_id.strip():
            return SendResult(ok=False, error="to_user_id must not be empty")
        if not context_token.strip():
            return SendResult(ok=False, error="context_token must not be empty")
        if not cleaned:
            return SendResult(ok=False, error="text must not be empty")
        if len(cleaned) > self.max_text_length:
            return SendResult(ok=False, error=f"text exceeds {self.max_text_length} characters")

        key = (to_user_id, context_token, cleaned)
        if key in self._idempotency:
            return self._idempotency[key]

        async with self._locks[to_user_id]:
            if key in self._idempotency:
                return self._idempotency[key]
            result = await self._send_text_with_retries(to_user_id, context_token, cleaned)
            self._idempotency[key] = result
            return result

    async def _send_text_with_retries(self, to_user_id: str, context_token: str, text: str) -> SendResult:
        if self.typing_enabled:
            await self._best_effort_typing(to_user_id, context_token, True)
        try:
            attempt = 0
            while True:
                try:
                    payload = {
                        "msg": {
                            "from_user_id": "",
                            "to_user_id": to_user_id,
                            "client_id": f"alysia-chat:{uuid4().hex}",
                            "message_type": 2,
                            "message_state": 2,
                            "context_token": context_token,
                            "item_list": [
                                {
                                    "type": 1,
                                    "text_item": {"text": text},
                                }
                            ],
                        },
                        "base_info": self.api.base_info(),
                    }
                    response = await self.api.send_message(payload)
                    return SendResult(
                        ok=True,
                        message_id=str(response.get("message_id", response.get("id", "")) or ""),
                        metadata={
                            **_safe_send_metadata(response),
                            "to_user_ref": _user_ref(to_user_id),
                        },
                    )
                except ILinkApiError as exc:
                    if not exc.retryable or attempt >= self.max_retries:
                        return SendResult(
                            ok=False,
                            error=str(exc),
                            retryable=exc.retryable,
                            status_code=exc.status_code,
                        )
                    attempt += 1
                    await asyncio.sleep(self.retry_delay_seconds * attempt)
        finally:
            if self.typing_enabled:
                await self._best_effort_typing(to_user_id, context_token, False)

    async def _best_effort_typing(self, to_user_id: str, context_token: str, typing: bool) -> None:
        try:
            await self.api.send_typing(to_user_id=to_user_id, context_token=context_token, typing=typing)
        except Exception:
            return


def _safe_send_metadata(response: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in response.items()
        if key not in {"context_token", "token", "bot_token", "authorization"}
    }


def _user_ref(user_id: str) -> str:
    if "@" not in user_id:
        return user_id[:4] + "..."
    prefix, suffix = user_id.split("@", 1)
    return f"{prefix[:4]}...@{suffix}"
