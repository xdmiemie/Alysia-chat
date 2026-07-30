from __future__ import annotations

import asyncio
import weakref
from collections.abc import Awaitable, Callable

from runtime.core.models import AlysiaReply, UnifiedMessage
from runtime.scheduler.serial_queue import QueueEvent, SerialAsyncQueue


class SingleGPUQueue(SerialAsyncQueue):
    def __init__(
        self,
        *,
        max_pending: int = 16,
        total_timeout_seconds: float = 240.0,
        cancel_pending_same_session: bool = True,
        event_sink: Callable[[QueueEvent], None] | None = None,
    ) -> None:
        super().__init__(
            name="gpu",
            max_pending=max_pending,
            default_timeout_seconds=total_timeout_seconds,
            cancel_pending_by_session=cancel_pending_same_session,
            event_sink=event_sink,
        )

    async def run_inference(
        self,
        message: UnifiedMessage,
        operation: Callable[[], AlysiaReply | Awaitable[AlysiaReply]],
        *,
        priority: int = 50,
        timeout_seconds: float | None = None,
    ) -> AlysiaReply:
        return await self.run(
            operation,
            request_id=message.request_id,
            session_key=message.session_id,
            contact_key=message.sender_id,
            priority=priority,
            timeout_seconds=timeout_seconds,
        )


_GLOBAL_GPU_QUEUES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, SingleGPUQueue] = weakref.WeakKeyDictionary()


def get_global_gpu_queue() -> SingleGPUQueue:
    loop = asyncio.get_running_loop()
    queue = _GLOBAL_GPU_QUEUES.get(loop)
    if queue is None:
        queue = SingleGPUQueue()
        _GLOBAL_GPU_QUEUES[loop] = queue
    return queue
