from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from runtime.adapters.wechat_desktop.call_audio import (
    ApplicationLoopbackVadCallRecorder,
    DefaultDeviceCallSpeaker,
)
from runtime.adapters.wechat_desktop.call_controller import CallStateTracker
from runtime.adapters.wechat_desktop.call_models import CallContact, CallResult, CallWindowSnapshot
from runtime.adapters.wechat_desktop.call_session import HalfDuplexCallSession
from runtime.adapters.wechat_desktop.config import WeChatDesktopConfig
from runtime.scheduler.inference_queue import SingleGPUQueue


class HalfDuplexCallSupervisor:
    """Own the single active Stage 17 session and its background task."""

    def __init__(
        self,
        *,
        config: WeChatDesktopConfig,
        character_id: str,
        core,
        gpu_queue: SingleGPUQueue,
        state: CallStateTracker,
        event_sink: Callable[[str, dict[str, object]], None] | None = None,
        missing_window_grace_seconds: float = 8.0,
    ) -> None:
        self.config = config
        self.character_id = character_id
        self.core = core
        self.gpu_queue = gpu_queue
        self.state = state
        self.event_sink = event_sink or (lambda _event, _fields: None)
        self.missing_window_grace_seconds = missing_window_grace_seconds
        self.session: HalfDuplexCallSession | None = None
        self.task: asyncio.Task[None] | None = None
        self._last_window_seen = 0.0
        self._connected_window_seen = False

    @property
    def active(self) -> bool:
        return self.session is not None and not self.session.closed

    async def start(self, result: CallResult) -> None:
        if not self.config.calls.ai_enabled or not result.ok or result.action != "answer":
            return
        if self.task is not None and not self.task.done():
            return
        contact = CallContact(
            account_key=self.config.account.account_key,
            contact_key=result.contact_key,
            display_name=result.contact_key,
            source="stage16_answer",
        )
        recorder = ApplicationLoopbackVadCallRecorder(
            voice=self.config.voice,
            calls=self.config.calls,
            executable_names=self.config.client.executable_names,
            event_sink=self.event_sink,
        )
        self.session = HalfDuplexCallSession(
            call_id=result.call_id,
            contact=contact,
            character_id=self.character_id,
            core=self.core,
            recorder=recorder,
            speaker=DefaultDeviceCallSpeaker(
                self.config.calls.output_device,
                self.config.calls.output_device_name,
            ).set_event_sink(self.event_sink),
            state=self.state,
            calls=self.config.calls,
            scheduler=self.config.scheduler,
            gpu_queue=self.gpu_queue,
            event_sink=self.event_sink,
        )
        self._last_window_seen = time.monotonic()
        self._connected_window_seen = False
        # Enter exclusive call mode before yielding back to the desktop loops.
        # This closes the small gap in which a message poll could start after
        # answering but before the background call task first ran.
        self.session.start()
        self.task = asyncio.create_task(self._run(self.session), name=f"wechat-call:{result.call_id}")

    async def observe(self, calls: tuple[CallWindowSnapshot, ...]) -> None:
        if self.session is None or self.session.closed:
            return
        active = [call for call in calls if call.state in {"ringing", "answering", "connected", "ending"}]
        if active:
            self._last_window_seen = time.monotonic()
            if any(call.state in {"connected", "ending"} for call in active):
                self._connected_window_seen = True
            return
        # On this WeChat build the incoming window vanishes after a successful
        # click and the connected tray window is not exposed through UIA. An
        # empty detection frame therefore cannot prove hangup unless a connected
        # window was observed earlier. Without that proof, audio silence/error
        # limits close the session safely.
        if (
            self._connected_window_seen
            and time.monotonic() - self._last_window_seen >= self.missing_window_grace_seconds
        ):
            self.session.close("WeChat call window disappeared")

    async def shutdown(self) -> None:
        if self.session is not None:
            self.session.close("adapter shutdown")
        if self.task is not None:
            try:
                await asyncio.wait_for(self.task, timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self.task.cancel()

    async def _run(self, session: HalfDuplexCallSession) -> None:
        started = time.monotonic()
        try:
            while not session.closed:
                if time.monotonic() - started >= self.config.calls.max_call_seconds:
                    session.close("maximum call duration reached")
                    break
                result = await session.run_turn()
                self.event_sink("call_turn_result", result.to_dict())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.event_sink(
                "call_session_failed", {"call_id": session.call_id, "error": str(exc)}
            )
        finally:
            session.close("call session stopped")
