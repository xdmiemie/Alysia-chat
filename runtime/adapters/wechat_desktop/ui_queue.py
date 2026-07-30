from __future__ import annotations

import asyncio
import weakref
from collections.abc import Awaitable, Callable
from typing import TypeVar

from runtime.scheduler.serial_queue import QueueEvent, QueueSnapshot, SerialAsyncQueue


T = TypeVar("T")


class SingleUIQueue(SerialAsyncQueue):
    def __init__(
        self,
        *,
        max_pending: int = 32,
        operation_timeout_seconds: float = 20.0,
        pause_max_pending: int = 8,
        event_sink: Callable[[QueueEvent], None] | None = None,
    ) -> None:
        super().__init__(
            name="wechat_ui",
            max_pending=max_pending,
            default_timeout_seconds=operation_timeout_seconds,
            pause_max_pending=pause_max_pending,
            timeout_includes_queue_wait=False,
            event_sink=event_sink,
        )

    async def run(
        self,
        operation: Callable[[], T | Awaitable[T]],
        *,
        request_id: str = "",
        contact_key: str = "",
        priority: int = 50,
        timeout_seconds: float | None = None,
    ) -> T:
        return await super().run(
            operation,
            request_id=request_id,
            session_key=contact_key,
            contact_key=contact_key,
            priority=priority,
            timeout_seconds=timeout_seconds,
            cancel_pending_by_session=False,
        )

    async def pause_for_takeover(self) -> None:
        await self.pause()

    async def resume_from_takeover(self) -> None:
        await self.resume()

    def worker_snapshot(self) -> QueueSnapshot:
        return self.snapshot()


_GLOBAL_UI_QUEUES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, SingleUIQueue] = weakref.WeakKeyDictionary()


def get_global_ui_queue() -> SingleUIQueue:
    loop = asyncio.get_running_loop()
    queue = _GLOBAL_UI_QUEUES.get(loop)
    if queue is None:
        queue = SingleUIQueue()
        _GLOBAL_UI_QUEUES[loop] = queue
    return queue
