from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from runtime.adapters.wechat.ilink.api import ILinkApiClient, ILinkApiError
from runtime.adapters.wechat.ilink.cursor_store import CursorStore
from runtime.adapters.wechat.ilink.event_parser import ILinkEventParser
from runtime.adapters.wechat.models import ILinkTextMessage


class LoginExpiredError(RuntimeError):
    pass


class ILinkLongPoller:
    def __init__(
        self,
        *,
        api: ILinkApiClient,
        cursor_store: CursorStore,
        parser: ILinkEventParser,
        reconnect_seconds: float,
        max_reconnect_seconds: float,
    ) -> None:
        self.api = api
        self.cursor_store = cursor_store
        self.parser = parser
        self.reconnect_seconds = reconnect_seconds
        self.max_reconnect_seconds = max_reconnect_seconds
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def messages(self) -> AsyncIterator[list[ILinkTextMessage]]:
        delay = self.reconnect_seconds
        while not self._stopped.is_set():
            cursor = self.cursor_store.read()
            try:
                payload = await self.api.get_updates(cursor)
                batch = self.parser.parse_update_batch(payload)
                if batch.cursor:
                    self.cursor_store.commit(batch.cursor)
                delay = self.reconnect_seconds
                if batch.messages:
                    yield list(batch.messages)
            except ILinkApiError as exc:
                if not exc.retryable:
                    raise LoginExpiredError(str(exc)) from exc
                await asyncio.sleep(delay)
                delay = min(self.max_reconnect_seconds, max(delay * 2, self.reconnect_seconds))
