from __future__ import annotations

from dataclasses import dataclass, field

from runtime.adapters.wechat_desktop.call_contact_resolver import CallContactResolver
from runtime.adapters.wechat_desktop.call_models import (
    CallAction,
    CallButton,
    CallContact,
    CallResult,
    CallState,
    CallStateEvent,
    CallWindowSnapshot,
)
from runtime.adapters.wechat_desktop.call_window_locator import WeChatCallWindowLocator
from runtime.adapters.wechat_desktop.config import UIConfig
from runtime.adapters.wechat_desktop.ui_queue import SingleUIQueue, get_global_ui_queue


CALL_UI_PRIORITY = 0


@dataclass
class CallStateTracker:
    current_call_id: str = ""
    state: CallState = "idle"
    contact_key: str = ""
    session_id: str = ""
    call_session_active: bool = False
    events: list[CallStateEvent] = field(default_factory=list)

    @property
    def has_active_call(self) -> bool:
        return self.call_session_active or (self.state not in {
            "idle", "rejected", "missed", "busy", "failed", "manual_takeover", "hangup"
        } and bool(self.current_call_id))

    @property
    def ordinary_messages_paused(self) -> bool:
        return self.call_session_active or (self.state in {
            "ringing", "answering", "ending", "waiting_for_first_speech", "listening",
            "recognizing", "thinking", "synthesizing", "speaking", "silence_timeout",
            "model_error", "audio_error",
        } and bool(self.current_call_id))

    def transition(
        self,
        *,
        call_id: str,
        state: CallState,
        contact: CallContact | None = None,
        reason: str = "",
    ) -> None:
        self.current_call_id = call_id if state != "idle" else ""
        self.state = state
        if contact is not None:
            self.contact_key = contact.contact_key
            self.session_id = contact.session_id
        if state == "idle":
            self.contact_key = ""
            self.session_id = ""
        self.events.append(
            CallStateEvent(call_id=call_id, state=state, contact_key=self.contact_key, reason=reason)
        )


class WeChatCallController:
    def __init__(
        self,
        *,
        locator: WeChatCallWindowLocator,
        resolver: CallContactResolver,
        ui: UIConfig,
        queue: SingleUIQueue | None = None,
        state: CallStateTracker | None = None,
    ) -> None:
        self.locator = locator
        self.resolver = resolver
        self.ui = ui
        self.queue = queue
        self.state = state or CallStateTracker()

    async def answer(self, call_id: str) -> CallResult:
        return await self._run_call_ui(lambda: self._answer_locked(call_id), request_id=f"answer:{call_id}")

    async def answer_snapshot(
        self,
        call: CallWindowSnapshot,
        contact: CallContact | None = None,
    ) -> CallResult:
        return await self._run_call_ui(
            lambda: self._answer_snapshot_locked(call, contact or self.resolver.resolve(call)),
            request_id=f"answer:{call.call_id}",
        )

    async def reject(self, call_id: str) -> CallResult:
        return await self._run_call_ui(lambda: self._reject_locked(call_id), request_id=f"reject:{call_id}")

    async def hangup(self, call_id: str, reason: str = "") -> CallResult:
        return await self._run_call_ui(lambda: self._hangup_locked(call_id, reason), request_id=f"hangup:{call_id}")

    async def _run_call_ui(self, operation, *, request_id: str) -> CallResult:
        queue = self.queue or get_global_ui_queue()
        return await queue.run(
            operation,
            request_id=request_id,
            priority=CALL_UI_PRIORITY,
            timeout_seconds=self.ui.operation_timeout_seconds,
        )

    def _answer_locked(self, call_id: str) -> CallResult:
        call, contact, error = self._load_call(call_id)
        if call is None:
            return _result(False, call_id, "answer", "failed", error=error, retryable=True)
        return self._answer_call_locked(call, contact)

    def _answer_snapshot_locked(self, call: CallWindowSnapshot, contact: CallContact | None) -> CallResult:
        if _requires_live_refresh(call):
            refreshed = self._refresh_answer_call(call, contact)
            if refreshed is None:
                return _result(
                    False,
                    call.call_id,
                    "answer",
                    "failed",
                    contact=contact,
                    error="fresh call window was not found before answer click",
                    retryable=True,
                )
            call = refreshed
        elif not _has_live_button(call):
            refreshed = self._refresh_answer_call(call, contact)
            if refreshed is not None:
                call = refreshed
        return self._answer_call_locked(call, contact)

    def _answer_call_locked(self, call: CallWindowSnapshot, contact: CallContact | None) -> CallResult:
        call_id = call.call_id
        if call.state != "ringing":
            self.state.transition(call_id=call_id, state="manual_takeover", contact=contact, reason="not ringing")
            return _result(
                False,
                call_id,
                "answer",
                "manual_takeover",
                contact=contact,
                error="call is no longer ringing",
            )
        button = call.buttons.get("answer")
        if not _button_ready(button):
            return _result(False, call_id, "answer", "failed", contact=contact, error="answer button is not ready")
        self.state.transition(call_id=call_id, state="answering", contact=contact)
        try:
            click_method = _invoke_button(button)
        except Exception as exc:
            self.state.transition(call_id=call_id, state="idle", contact=contact, reason="answer click failed")
            return _result(
                False,
                call_id,
                "answer",
                "failed",
                contact=contact,
                error=f"answer button click failed: {exc}",
                retryable=True,
            )
        if _is_live_uia_button(button):
            self.state.transition(
                call_id=call_id,
                state="idle",
                contact=contact,
                reason="answer click sent; awaiting WeChat confirmation",
            )
            return _result(
                True,
                call_id,
                "answer",
                "answering",
                contact=contact,
                verified=False,
                metadata={
                    "button_text": button.text,
                    "click_method": click_method,
                    "verification": "click_sent_without_state_confirmation",
                },
            )
        state = self.locator.current_state(call_id)
        if state == "connected":
            self.state.transition(call_id=call_id, state="connected", contact=contact)
            return _result(True, call_id, "answer", "connected", contact=contact, verified=True)
        if state == "idle":
            self.state.transition(
                call_id=call_id,
                state="connected",
                contact=contact,
                reason="incoming call window closed after answer click",
            )
            return _result(
                True,
                call_id,
                "answer",
                "connected",
                contact=contact,
                verified=False,
                metadata={"verification": "incoming_window_closed_after_click"},
            )
        return _result(
            False,
            call_id,
            "answer",
            "failed",
            contact=contact,
            error=f"answer click was not confirmed; current state is {state}",
            retryable=True,
        )

    def _reject_locked(self, call_id: str) -> CallResult:
        call, contact, error = self._load_call(call_id)
        if call is None:
            return _result(False, call_id, "reject", "failed", error=error, retryable=True)
        button = call.buttons.get("reject")
        if not _button_ready(button):
            return _result(False, call_id, "reject", "failed", contact=contact, error="reject button is not ready")
        _invoke_button(button)
        state = self.locator.current_state(call_id)
        verified = state in {"idle", "rejected", "missed"}
        self.state.transition(
            call_id=call_id,
            state="idle" if verified else "failed",
            contact=contact,
            reason="rejected" if verified else f"current state is {state}",
        )
        return _result(
            verified,
            call_id,
            "reject",
            "rejected" if verified else "failed",
            contact=contact,
            error="" if verified else f"reject click was not confirmed; current state is {state}",
            verified=verified,
            retryable=not verified,
        )

    def _hangup_locked(self, call_id: str, reason: str) -> CallResult:
        call, contact, error = self._load_call(call_id)
        if call is None:
            self.state.transition(call_id=call_id, state="idle", reason="call window already closed")
            return _result(True, call_id, "hangup", "ending", error=error, verified=True)
        button = call.buttons.get("hangup") or call.buttons.get("reject")
        if not _button_ready(button):
            return _result(False, call_id, "hangup", "failed", contact=contact, error="hangup button is not ready")
        self.state.transition(call_id=call_id, state="ending", contact=contact, reason=reason)
        _invoke_button(button)
        state = self.locator.current_state(call_id)
        verified = state in {"idle", "rejected", "missed"}
        self.state.transition(
            call_id=call_id,
            state="idle" if verified else "failed",
            contact=contact,
            reason=reason if verified else f"current state is {state}",
        )
        return _result(
            verified,
            call_id,
            "hangup",
            "ending" if verified else "failed",
            contact=contact,
            error="" if verified else f"hangup click was not confirmed; current state is {state}",
            verified=verified,
            retryable=not verified,
        )

    def _load_call(self, call_id: str) -> tuple[CallWindowSnapshot | None, CallContact | None, str]:
        call = self.locator.find_call(call_id)
        if call is None:
            return None, None, "call window was not found"
        contact = self.resolver.resolve(call)
        return call, contact, ""

    def _refresh_answer_call(
        self,
        call: CallWindowSnapshot,
        contact: CallContact | None,
    ) -> CallWindowSnapshot | None:
        candidates = [
            item
            for item in self.locator.locate_incoming_calls()
            if item.state == "ringing" and _button_ready(item.buttons.get("answer"))
        ]
        if not candidates:
            return None
        for candidate in candidates:
            if candidate.call_id == call.call_id:
                return candidate
        scored = [
            (_call_match_score(candidate, call, contact), candidate)
            for candidate in candidates
        ]
        scored.sort(key=lambda item: item[0], reverse=True)
        score, candidate = scored[0]
        return candidate if score > 0 else None


def _button_ready(button: CallButton | None) -> bool:
    return bool(button and button.enabled and button.visible)


def _requires_live_refresh(call: CallWindowSnapshot) -> bool:
    button = call.buttons.get("answer")
    return bool(button and button.rect and button.raw_control is None and button.invoke is None)


def _has_live_button(call: CallWindowSnapshot) -> bool:
    button = call.buttons.get("answer")
    return bool(button and (button.raw_control is not None or button.invoke is not None))


def _call_match_score(
    candidate: CallWindowSnapshot,
    original: CallWindowSnapshot,
    contact: CallContact | None,
) -> int:
    score = 0
    if candidate.window_handle and candidate.window_handle == original.window_handle:
        score += 4
    if candidate.contact_display_text and candidate.contact_display_text == original.contact_display_text:
        score += 3
    if contact is not None:
        if candidate.contact_display_text == contact.contact_key:
            score += 3
        if candidate.contact_display_text == contact.display_name:
            score += 3
    if candidate.title and candidate.title == original.title:
        score += 1
    if candidate.call_type == original.call_type:
        score += 1
    return score


def _invoke_button(button: CallButton | None) -> str:
    if button is None:
        raise RuntimeError("call button is missing")
    errors: list[str] = []
    control = button.raw_control
    if control is not None:
        for method_name in ("click_input", "click"):
            method = getattr(control, method_name, None)
            if not callable(method):
                continue
            try:
                method()
                return f"raw_control.{method_name}"
            except Exception as exc:
                errors.append(f"raw_control.{method_name}: {exc}")
        try:
            _click_control_center(control)
            return "raw_control.center_click"
        except Exception as exc:
            errors.append(f"raw_control.center_click: {exc}")
    try:
        _click_rect_center(button.rect)
        return "button.rect_center_click"
    except Exception as exc:
        errors.append(f"button.rect_center_click: {exc}")
    if button.invoke is not None:
        try:
            button.invoke()
            return "button.invoke"
        except Exception as exc:
            errors.append(f"button.invoke: {exc}")
    invoke = getattr(control, "invoke", None)
    if callable(invoke):
        try:
            invoke()
            return "raw_control.invoke"
        except Exception as exc:
            errors.append(f"raw_control.invoke: {exc}")
    detail = "; ".join(errors)
    raise RuntimeError(f"call button cannot be invoked{': ' + detail if detail else ''}")


def _click_control_center(control: object) -> None:
    rectangle = control.rectangle()  # type: ignore[attr-defined]
    _click_rect_center(
        (
            int(getattr(rectangle, "left")),
            int(getattr(rectangle, "top")),
            int(getattr(rectangle, "right")),
            int(getattr(rectangle, "bottom")),
        )
    )


def _click_rect_center(rect: tuple[int, int, int, int]) -> None:
    if len(rect) != 4:
        raise RuntimeError("button rectangle is missing")
    left, top, right, bottom = rect
    if right <= left or bottom <= top:
        raise RuntimeError(f"invalid control rectangle: {(left, top, right, bottom)}")
    from pywinauto import mouse

    mouse.click(button="left", coords=((left + right) // 2, (top + bottom) // 2))


def _is_live_uia_button(button: CallButton | None) -> bool:
    return bool(button and button.raw_control is not None)


def _result(
    ok: bool,
    call_id: str,
    action: CallAction,
    state: CallState,
    *,
    contact: CallContact | None = None,
    error: str = "",
    retryable: bool = False,
    verified: bool = False,
    metadata: dict[str, object] | None = None,
) -> CallResult:
    return CallResult(
        ok=ok,
        call_id=call_id,
        action=action,
        state=state,
        contact_key=contact.contact_key if contact else "",
        session_id=contact.session_id if contact else "",
        error=error,
        retryable=retryable,
        verified=verified,
        metadata=metadata or {},
    )
