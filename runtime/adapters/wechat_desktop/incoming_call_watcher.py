from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from runtime.adapters.wechat_desktop.call_contact_resolver import CallContactResolver
from runtime.adapters.wechat_desktop.call_controller import CallStateTracker, WeChatCallController
from runtime.adapters.wechat_desktop.call_health import CallHealthChecker
from runtime.adapters.wechat_desktop.call_models import CallContact, CallDecision, CallResult, CallWindowSnapshot
from runtime.adapters.wechat_desktop.call_policy import WeChatCallPolicy
from runtime.adapters.wechat_desktop.call_window_locator import WeChatCallWindowLocator
from runtime.adapters.wechat_desktop.config import WeChatDesktopConfig
from runtime.adapters.wechat_desktop.takeover import TakeoverState


class IncomingCallWatcher:
    def __init__(
        self,
        *,
        config: WeChatDesktopConfig,
        locator: WeChatCallWindowLocator,
        resolver: CallContactResolver,
        controller: WeChatCallController,
        health_checker: CallHealthChecker,
        takeover: TakeoverState | None = None,
        state: CallStateTracker | None = None,
        time_fn=time.monotonic,
        event_sink: Callable[[str, dict[str, object]], None] | None = None,
        empty_poll_log_interval_seconds: float = 5.0,
    ) -> None:
        self.config = config
        self.locator = locator
        self.resolver = resolver
        self.controller = controller
        self.health_checker = health_checker
        self.takeover = takeover
        self.state = state or controller.state
        self.time_fn = time_fn
        self.event_sink = event_sink
        self.empty_poll_log_interval_seconds = empty_poll_log_interval_seconds
        self._handled_at: dict[str, float] = {}
        self._last_empty_poll_log_at = 0.0

    async def poll_once(self) -> tuple[CallResult, ...]:
        return await self.handle_detected_calls(self.locator.locate_incoming_calls())

    async def handle_detected_calls(self, calls: tuple[CallWindowSnapshot, ...]) -> tuple[CallResult, ...]:
        self._sync_closed_call()
        self._log_poll(calls)
        results = []
        for call in calls:
            result = await self._handle_call(call)
            if result is not None:
                results.append(result)
        return tuple(results)

    async def run_forever(self, *, poll_interval_seconds: float = 0.5) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(poll_interval_seconds)

    async def _handle_call(self, call: CallWindowSnapshot) -> CallResult | None:
        contact = self.resolver.resolve(call)
        health = self.health_checker.check()
        policy = WeChatCallPolicy(
            config=self.config,
            takeover=self.takeover,
            has_active_call=self.state.has_active_call and self.state.current_call_id != call.call_id,
        )
        decision = policy.decide(
            call=call,
            contact=contact,
            health=health,
            already_handled=self._already_handled(call.call_id),
        )
        self._emit(
            "call_decision",
            call_id=call.call_id,
            call_type=call.call_type,
            call_state=call.state,
            contact_key=contact.contact_key if contact else "",
            contact_display_name=contact.display_name if contact else "",
            action=decision.action,
            reason=decision.reason,
            health_ok=health.ok,
            health_details=list(health.details),
        )
        if decision.action == "answer":
            self.state.transition(call_id=call.call_id, state="ringing", contact=contact, reason="answer pending")
            if self.config.calls.ring_wait_ms > 0:
                await asyncio.sleep(self.config.calls.ring_wait_ms / 1000.0)
            result = await self.controller.answer_snapshot(call, contact)
            self._remember_result(result)
            self._emit_result(result)
            return result
        if decision.action == "reject":
            self.state.transition(call_id=call.call_id, state="ending", contact=contact, reason="reject pending")
            result = await self.controller.reject(call.call_id)
            self._remember_result(result)
            self._emit_result(result)
            return result
        if decision.action == "ignore":
            self._remember(call.call_id)
            result = _ignored_result(call, contact, decision)
            self._emit_result(result)
            return result
        return None

    def _already_handled(self, call_id: str) -> bool:
        handled_at = self._handled_at.get(call_id)
        if handled_at is None:
            return False
        return self.time_fn() - handled_at <= self.config.calls.duplicate_cooldown_seconds

    def _remember(self, call_id: str) -> None:
        self._handled_at[call_id] = self.time_fn()

    def _remember_result(self, result: CallResult) -> None:
        if result.action == "answer" and result.ok and not result.verified:
            return
        if result.ok or not result.retryable:
            self._remember(result.call_id)

    def _sync_closed_call(self) -> None:
        if not self.state.has_active_call:
            return
        current = self.locator.current_state(self.state.current_call_id)
        if current == "idle":
            self.state.transition(call_id=self.state.current_call_id, state="idle", reason="call window closed")

    def _log_poll(self, calls: tuple[CallWindowSnapshot, ...]) -> None:
        if calls:
            self._emit(
                "call_poll_detected",
                count=len(calls),
                call_ids=[call.call_id for call in calls],
                states=[call.state for call in calls],
                types=[call.call_type for call in calls],
                titles=[call.title for call in calls],
            )
            return
        now = self.time_fn()
        if now - self._last_empty_poll_log_at >= self.empty_poll_log_interval_seconds:
            self._last_empty_poll_log_at = now
            self._emit("call_poll_empty", count=0)

    def _emit_result(self, result: CallResult) -> None:
        self._emit(
            "call_result",
            call_id=result.call_id,
            action=result.action,
            ok=result.ok,
            state=result.state,
            contact_key=result.contact_key,
            error=result.error,
            retryable=result.retryable,
            verified=result.verified,
            metadata=dict(result.metadata),
        )

    def _emit(self, event: str, **fields: object) -> None:
        if self.event_sink is not None:
            self.event_sink(event, fields)


def _ignored_result(
    call: CallWindowSnapshot,
    contact: CallContact | None,
    decision: CallDecision,
) -> CallResult:
    return CallResult(
        ok=True,
        call_id=call.call_id,
        action="ignore",
        state=call.state,
        contact_key=contact.contact_key if contact else "",
        session_id=contact.session_id if contact else "",
        error=decision.reason,
        retryable=decision.retryable,
        verified=True,
    )
