from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Literal


CallType = Literal["voice", "video", "unknown"]
CallState = Literal[
    "idle",
    "ringing",
    "answering",
    "connected",
    "ending",
    "rejected",
    "missed",
    "busy",
    "failed",
    "manual_takeover",
    "waiting_for_first_speech",
    "listening",
    "recognizing",
    "thinking",
    "synthesizing",
    "speaking",
    "silence_timeout",
    "model_error",
    "audio_error",
    "hangup",
]
CallAction = Literal["answer", "reject", "hangup", "ignore"]


@dataclass(frozen=True)
class CallContact:
    account_key: str
    contact_key: str
    display_name: str = ""
    source: str = ""

    @property
    def session_id(self) -> str:
        return f"wechat_desktop:{self.account_key}:{self.contact_key}"


@dataclass
class CallButton:
    role: str
    text: str = ""
    automation_id: str = ""
    control_path: str = ""
    rect: tuple[int, int, int, int] = ()
    enabled: bool = True
    visible: bool = True
    invoke: Callable[[], None] | None = None
    raw_control: object | None = None


@dataclass(frozen=True)
class CallWindowSnapshot:
    account_key: str
    window_handle: str
    title: str = ""
    process_id: int = 0
    call_type: CallType = "unknown"
    state: CallState = "ringing"
    contact_display_text: str = ""
    runtime_id: str = ""
    buttons: dict[str, CallButton] = field(default_factory=dict)
    controls: dict[str, bool] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)
    observed_at: float = field(default_factory=time.time)

    @property
    def call_id(self) -> str:
        return stable_call_id(
            account_key=self.account_key,
            window_handle=self.window_handle,
            runtime_id=self.runtime_id,
            contact_hint=self.contact_display_text,
            call_type=self.call_type,
        )

    @property
    def has_answer_button(self) -> bool:
        return _has_ready_button(self.buttons.get("answer"))

    @property
    def has_reject_button(self) -> bool:
        return _has_ready_button(self.buttons.get("reject"))

    @property
    def has_hangup_button(self) -> bool:
        return _has_ready_button(self.buttons.get("hangup"))


@dataclass(frozen=True)
class CallDecision:
    action: CallAction
    reason: str
    auto_answer: bool = False
    retryable: bool = False


@dataclass(frozen=True)
class CallResult:
    ok: bool
    call_id: str
    action: CallAction
    state: CallState
    contact_key: str = ""
    session_id: str = ""
    error: str = ""
    retryable: bool = False
    verified: bool = False
    metadata: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CallStateEvent:
    call_id: str
    state: CallState
    contact_key: str = ""
    reason: str = ""
    ts: float = field(default_factory=time.time)


def stable_call_id(
    *,
    account_key: str,
    window_handle: str,
    runtime_id: str,
    contact_hint: str,
    call_type: str,
) -> str:
    stable_window_key = runtime_id or window_handle or contact_hint
    parts = (account_key, stable_window_key, contact_hint, call_type)
    return hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:24]


def _has_ready_button(button: CallButton | None) -> bool:
    return bool(button and button.visible and button.enabled)
