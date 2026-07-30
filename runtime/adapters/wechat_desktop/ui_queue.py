from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar


T = TypeVar("T")


class SingleUIQueue:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def run(self, operation: Callable[[], T | Awaitable[T]]) -> T:
        async with self._lock:
            result = operation()
            if hasattr(result, "__await__"):
                return await result  # type: ignore[return-value]
            return result  # type: ignore[return-value]
