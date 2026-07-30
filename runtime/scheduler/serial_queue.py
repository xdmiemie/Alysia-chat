from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar


T = TypeVar("T")


class QueueError(RuntimeError):
    pass


class QueueBackpressureError(QueueError):
    pass


class QueueCancelledError(QueueError):
    pass


class QueueClosedError(QueueError):
    pass


class QueueTimeoutError(QueueError):
    pass


@dataclass(frozen=True)
class QueueEvent:
    queue_name: str
    event: str
    request_id: str = ""
    session_hash: str = ""
    contact_hash: str = ""
    priority: int = 50
    pending: int = 0
    active: int = 0
    queue_wait_ms: int | None = None
    duration_ms: int | None = None
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "queue_name": self.queue_name,
            "queue_event": self.event,
            "request_id": self.request_id,
            "session_hash": self.session_hash,
            "contact_hash": self.contact_hash,
            "priority": self.priority,
            "pending": self.pending,
            "active": self.active,
            "queue_wait_ms": self.queue_wait_ms,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


@dataclass(frozen=True)
class QueueSnapshot:
    queue_name: str
    pending: int
    active: int
    completed: int
    failed: int
    cancelled: int
    timed_out: int
    max_pending_seen: int
    paused: bool


@dataclass
class _QueueJob(Generic[T]):
    sequence: int
    priority: int
    operation: Callable[[], T | Awaitable[T]]
    future: asyncio.Future[T]
    request_id: str
    session_key: str
    contact_key: str
    submitted_at: float
    timeout_seconds: float | None


@dataclass
class _QueueState:
    condition: asyncio.Condition
    pending: list[_QueueJob]
    worker_task: asyncio.Task | None = None
    closed: bool = False
    paused: bool = False
    active: int = 0
    sequence: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    timed_out: int = 0
    max_pending_seen: int = 0


class SerialAsyncQueue:
    def __init__(
        self,
        *,
        name: str,
        max_pending: int = 32,
        default_timeout_seconds: float | None = None,
        cancel_pending_by_session: bool = False,
        pause_max_pending: int | None = None,
        timeout_includes_queue_wait: bool = True,
        event_sink: Callable[[QueueEvent], None] | None = None,
    ) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be at least 1")
        self.name = name
        self.max_pending = max_pending
        self.default_timeout_seconds = default_timeout_seconds
        self.cancel_pending_by_session = cancel_pending_by_session
        self.pause_max_pending = pause_max_pending
        self.timeout_includes_queue_wait = timeout_includes_queue_wait
        self.event_sink = event_sink
        self._states: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _QueueState] = weakref.WeakKeyDictionary()

    async def run(
        self,
        operation: Callable[[], T | Awaitable[T]],
        *,
        request_id: str = "",
        session_key: str = "",
        contact_key: str = "",
        priority: int = 50,
        timeout_seconds: float | None = None,
        cancel_pending_by_session: bool | None = None,
    ) -> T:
        loop = asyncio.get_running_loop()
        state = self._state_for(loop)
        future: asyncio.Future[T] = loop.create_future()
        async with state.condition:
            if state.closed:
                raise QueueClosedError(f"{self.name} is closed")
            pending_limit = self._effective_pending_limit(state)
            if len(state.pending) >= pending_limit:
                raise QueueBackpressureError(f"{self.name} pending queue is full ({pending_limit})")
            state.sequence += 1
            job = _QueueJob(
                sequence=state.sequence,
                priority=priority,
                operation=operation,
                future=future,
                request_id=request_id,
                session_key=session_key,
                contact_key=contact_key,
                submitted_at=time.perf_counter(),
                timeout_seconds=timeout_seconds if timeout_seconds is not None else self.default_timeout_seconds,
            )
            if (self.cancel_pending_by_session if cancel_pending_by_session is None else cancel_pending_by_session):
                self._cancel_pending_session(state, job.session_key, replacement_request_id=job.request_id)
            state.pending.append(job)
            state.pending.sort(key=lambda item: (item.priority, item.sequence))
            state.max_pending_seen = max(state.max_pending_seen, len(state.pending))
            self._emit(state, "enqueued", job)
            if state.worker_task is None or state.worker_task.done():
                state.worker_task = loop.create_task(self._worker(state))
            state.condition.notify()
        return await future

    async def pause(self) -> None:
        state = self._state_for(asyncio.get_running_loop())
        async with state.condition:
            state.paused = True
            self._emit_state(state, "paused")
            await self._trim_paused_pending(state)

    async def resume(self) -> None:
        state = self._state_for(asyncio.get_running_loop())
        async with state.condition:
            state.paused = False
            self._emit_state(state, "resumed")
            state.condition.notify_all()

    async def close(self, *, cancel_pending: bool = True) -> None:
        state = self._state_for(asyncio.get_running_loop())
        async with state.condition:
            state.closed = True
            if cancel_pending:
                for job in state.pending:
                    self._cancel_job(state, job, "queue closed")
                state.pending.clear()
            state.condition.notify_all()
        if state.worker_task is not None:
            await state.worker_task

    def snapshot(self) -> QueueSnapshot:
        totals = QueueSnapshot(self.name, 0, 0, 0, 0, 0, 0, 0, False)
        pending = active = completed = failed = cancelled = timed_out = max_pending_seen = 0
        paused = False
        for state in self._states.values():
            pending += len(state.pending)
            active += state.active
            completed += state.completed
            failed += state.failed
            cancelled += state.cancelled
            timed_out += state.timed_out
            max_pending_seen = max(max_pending_seen, state.max_pending_seen)
            paused = paused or state.paused
        return QueueSnapshot(
            queue_name=totals.queue_name,
            pending=pending,
            active=active,
            completed=completed,
            failed=failed,
            cancelled=cancelled,
            timed_out=timed_out,
            max_pending_seen=max_pending_seen,
            paused=paused,
        )

    def _state_for(self, loop: asyncio.AbstractEventLoop) -> _QueueState:
        state = self._states.get(loop)
        if state is None:
            state = _QueueState(condition=asyncio.Condition(), pending=[])
            self._states[loop] = state
        return state

    def _effective_pending_limit(self, state: _QueueState) -> int:
        if state.paused and self.pause_max_pending is not None:
            return max(0, self.pause_max_pending)
        return self.max_pending

    async def _trim_paused_pending(self, state: _QueueState) -> None:
        if self.pause_max_pending is None:
            return
        keep = max(0, self.pause_max_pending)
        while len(state.pending) > keep:
            job = state.pending.pop(0)
            self._cancel_job(state, job, "queue paused")

    def _cancel_pending_session(self, state: _QueueState, session_key: str, *, replacement_request_id: str) -> None:
        if not session_key:
            return
        kept: list[_QueueJob] = []
        for job in state.pending:
            if job.session_key == session_key:
                self._cancel_job(state, job, f"replaced by newer request {replacement_request_id}")
            else:
                kept.append(job)
        state.pending = kept

    async def _worker(self, state: _QueueState) -> None:
        while True:
            async with state.condition:
                await state.condition.wait_for(lambda: state.closed or (state.pending and not state.paused))
                if state.closed and not state.pending:
                    return
                job = state.pending.pop(0)
                state.active = 1
                queue_wait_ms = _elapsed_ms(job.submitted_at)
                self._emit(state, "started", job, queue_wait_ms=queue_wait_ms)
            try:
                if job.future.done():
                    continue
                result = await self._run_job(job)
                if not job.future.done():
                    job.future.set_result(result)
                state.completed += 1
                self._emit(state, "completed", job, duration_ms=_elapsed_ms(job.submitted_at))
            except asyncio.TimeoutError as exc:
                state.timed_out += 1
                error = f"{self.name} job timed out"
                if not job.future.done():
                    job.future.set_exception(QueueTimeoutError(error))
                self._emit(state, "timed_out", job, duration_ms=_elapsed_ms(job.submitted_at), error=error)
            except Exception as exc:
                state.failed += 1
                if not job.future.done():
                    job.future.set_exception(exc)
                self._emit(state, "failed", job, duration_ms=_elapsed_ms(job.submitted_at), error=str(exc))
            finally:
                async with state.condition:
                    state.active = 0
                    state.condition.notify_all()

    async def _run_job(self, job: _QueueJob[T]) -> T:
        timeout = job.timeout_seconds
        elapsed = time.perf_counter() - job.submitted_at if self.timeout_includes_queue_wait else 0.0
        if timeout is not None and elapsed >= timeout:
            raise asyncio.TimeoutError()
        remaining = None if timeout is None else max(0.001, timeout - elapsed)
        result = job.operation()
        if inspect.isawaitable(result):
            if remaining is None:
                return await result  # type: ignore[return-value]
            return await asyncio.wait_for(result, timeout=remaining)  # type: ignore[arg-type,return-value]
        return result  # type: ignore[return-value]

    def _cancel_job(self, state: _QueueState, job: _QueueJob, reason: str) -> None:
        state.cancelled += 1
        if not job.future.done():
            job.future.set_exception(QueueCancelledError(reason))
        self._emit(state, "cancelled", job, error=reason)

    def _emit(
        self,
        state: _QueueState,
        event: str,
        job: _QueueJob | None = None,
        *,
        queue_wait_ms: int | None = None,
        duration_ms: int | None = None,
        error: str = "",
    ) -> None:
        if self.event_sink is None:
            return
        self.event_sink(
            QueueEvent(
                queue_name=self.name,
                event=event,
                request_id=job.request_id if job else "",
                session_hash=_hash_key(job.session_key) if job else "",
                contact_hash=_hash_key(job.contact_key) if job else "",
                priority=job.priority if job else 50,
                pending=len(state.pending),
                active=state.active,
                queue_wait_ms=queue_wait_ms,
                duration_ms=duration_ms,
                error=error,
            )
        )

    def _emit_state(self, state: _QueueState, event: str) -> None:
        self._emit(state, event, None)


def _hash_key(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
