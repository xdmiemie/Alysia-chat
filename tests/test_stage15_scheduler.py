from __future__ import annotations

import asyncio
import pytest

from runtime.adapters.wechat_desktop.app import _reply_to_batch
from runtime.adapters.wechat_desktop.models import BufferedDesktopBatch, DesktopMessage, SendResult
from runtime.adapters.wechat_desktop.ui_queue import SingleUIQueue
from runtime.core.models import AlysiaReply, CoreTiming, UnifiedMessage
from runtime.scheduler.inference_queue import SingleGPUQueue
from runtime.scheduler.recovery import ManagedService, RestartPolicy, ServiceCheckResult
from runtime.scheduler.serial_queue import QueueBackpressureError, QueueCancelledError, QueueEvent, QueueTimeoutError


def _reply(message: UnifiedMessage, text: str = "ok") -> AlysiaReply:
    return AlysiaReply(
        request_id=message.request_id,
        session_id=message.session_id,
        channel=message.channel,
        ok=True,
        text=text,
        message_type="text",
        audio_path="",
        state="COMPLETED",
        states=("ACCEPTED", "COMPLETED"),
        error_code="none",
        error="",
        transcription=None,
        reply={"reply": text},
        timings=CoreTiming(total_ms=1),
        metadata=dict(message.metadata),
    )


def _message(request_id: str, session_id: str, sender_id: str = "alice-key") -> UnifiedMessage:
    return UnifiedMessage.local_text(
        "hello",
        request_id=request_id,
        session_id=session_id,
        sender_id=sender_id,
    )


def test_stage15_gpu_queue_runs_one_job_at_a_time_in_priority_order():
    events: list[str] = []
    queue = SingleGPUQueue(max_pending=4, total_timeout_seconds=5, cancel_pending_same_session=False)

    async def work(label: str, delay: float) -> str:
        events.append(f"start:{label}")
        await asyncio.sleep(delay)
        events.append(f"end:{label}")
        return label

    async def run_all():
        first = asyncio.create_task(
            queue.run(lambda: work("low", 0.02), request_id="req-low", session_key="s1", priority=50)
        )
        await asyncio.sleep(0)
        second = asyncio.create_task(
            queue.run(lambda: work("high", 0.01), request_id="req-high", session_key="s2", priority=10)
        )
        results = await asyncio.gather(first, second)
        await queue.close()
        return results

    assert asyncio.run(run_all()) == ["low", "high"]
    assert events == ["start:low", "end:low", "start:high", "end:high"]


def test_stage15_gpu_queue_cancels_pending_same_session_request():
    queue = SingleGPUQueue(max_pending=4, total_timeout_seconds=5, cancel_pending_same_session=True)
    started = asyncio.Event()
    release = asyncio.Event()
    ran: list[str] = []

    async def blocking() -> str:
        started.set()
        await release.wait()
        ran.append("active")
        return "active"

    async def quick(label: str) -> str:
        ran.append(label)
        return label

    async def run_all():
        first = asyncio.create_task(queue.run(lambda: blocking(), request_id="req-1", session_key="s1"))
        await started.wait()
        second = asyncio.create_task(queue.run(lambda: quick("old"), request_id="req-2", session_key="s2"))
        await asyncio.sleep(0)
        third = asyncio.create_task(queue.run(lambda: quick("new"), request_id="req-3", session_key="s2"))
        release.set()
        first_result = await first
        with pytest.raises(QueueCancelledError):
            await second
        third_result = await third
        await queue.close()
        return first_result, third_result

    assert asyncio.run(run_all()) == ("active", "new")
    assert ran == ["active", "new"]
    assert queue.snapshot().cancelled == 1


def test_stage15_ui_queue_has_single_active_worker_and_timeout_releases_queue():
    events: list[str] = []
    queue = SingleUIQueue(max_pending=4, operation_timeout_seconds=0.05)

    async def slow() -> str:
        events.append("slow-start")
        await asyncio.sleep(0.2)
        return "slow"

    async def fast() -> str:
        events.append("fast-start")
        return "fast"

    async def run_all():
        first = asyncio.create_task(queue.run(lambda: slow(), request_id="ui-1", contact_key="n"))
        await asyncio.sleep(0)
        second = asyncio.create_task(queue.run(lambda: fast(), request_id="ui-2", contact_key="L"))
        with pytest.raises(QueueTimeoutError):
            await first
        second_result = await second
        await queue.close()
        return second_result

    assert asyncio.run(run_all()) == "fast"
    assert events == ["slow-start", "fast-start"]
    snapshot = queue.snapshot()
    assert snapshot.active == 0
    assert snapshot.timed_out == 1
    assert snapshot.completed == 1


def test_stage15_ui_pause_applies_bounded_backpressure():
    queue = SingleUIQueue(max_pending=4, operation_timeout_seconds=5, pause_max_pending=1)

    async def run_all():
        await queue.pause_for_takeover()
        pending = asyncio.create_task(queue.run(lambda: "kept", request_id="ui-1", contact_key="n"))
        await asyncio.sleep(0)
        with pytest.raises(QueueBackpressureError):
            await queue.run(lambda: "blocked", request_id="ui-2", contact_key="L")
        await queue.resume_from_takeover()
        result = await pending
        await queue.close()
        return result

    assert asyncio.run(run_all()) == "kept"


def test_stage15_wechat_reply_uses_gpu_queue_and_logs_queue_events(tmp_path):
    class FakeCore:
        def __init__(self) -> None:
            self.messages: list[UnifiedMessage] = []

        def process(self, message: UnifiedMessage):
            self.messages.append(message)
            return _reply(message, text=f"reply:{message.sender_id}")

    class FakeSender:
        def __init__(self) -> None:
            self.sent: list[tuple[str, str, str]] = []

        async def send_text(self, contact_key, text, request_id):
            self.sent.append((contact_key, text, request_id))
            return SendResult(ok=True, request_id=request_id, contact_key=contact_key, verified=True)

    log_path = tmp_path / "wechat.jsonl"
    queue_events: list[QueueEvent] = []
    gpu_queue = SingleGPUQueue(
        max_pending=4,
        total_timeout_seconds=5,
        cancel_pending_same_session=False,
        event_sink=queue_events.append,
    )
    message = DesktopMessage(
        account_key="role",
        contact_key="alice-key",
        contact_display_name="Alice",
        chat_title="Alice",
        direction="incoming",
        message_type="text",
        text="hello",
        runtime_id="m1",
    )
    batch = BufferedDesktopBatch(
        account_key="role",
        contact_key="alice-key",
        contact_display_name="Alice",
        chat_title="Alice",
        messages=(message,),
    )
    core = FakeCore()
    sender = FakeSender()

    result = asyncio.run(
        _reply_to_batch(
            batch,
            core=core,
            sender=sender,
            character_id="elysia",
            log_path=log_path,
            gpu_queue=gpu_queue,
        )
    )

    assert result.ok is True
    assert core.messages[0].session_id == "wechat_desktop:role:alice-key"
    assert sender.sent[0][0:2] == ("alice-key", "reply:alice-key")
    assert [event.event for event in queue_events] == ["enqueued", "started", "completed"]


def test_stage15_restart_policy_blocks_restart_storms():
    checks = [
        ServiceCheckResult(name="llama", ok=False, detail="down"),
        ServiceCheckResult(name="llama", ok=False, detail="still down"),
        ServiceCheckResult(name="llama", ok=False, detail="down again"),
    ]
    restarted: list[str] = []

    def health_check():
        return checks.pop(0) if checks else ServiceCheckResult(name="llama", ok=False, detail="down")

    service = ManagedService(
        name="llama",
        health_check=health_check,
        restart=lambda: restarted.append("restart"),
        policy=RestartPolicy(max_attempts=1, window_seconds=60, cooldown_seconds=0),
    )

    first = service.ensure_healthy()
    second = service.ensure_healthy()

    assert first.restarted is True
    assert second.restart_blocked is True
    assert restarted == ["restart"]
