from __future__ import annotations

from runtime.adapters.wechat_desktop.call_health import CallHealthStatus
from runtime.adapters.wechat_desktop.call_models import CallContact, CallDecision, CallWindowSnapshot
from runtime.adapters.wechat_desktop.config import WeChatDesktopConfig
from runtime.adapters.wechat_desktop.takeover import TakeoverState


class WeChatCallPolicy:
    def __init__(
        self,
        *,
        config: WeChatDesktopConfig,
        takeover: TakeoverState | None = None,
        has_active_call: bool | None = None,
    ) -> None:
        self.config = config
        self.takeover = takeover
        self.has_active_call = bool(has_active_call)

    def decide(
        self,
        *,
        call: CallWindowSnapshot,
        contact: CallContact | None,
        health: CallHealthStatus,
        already_handled: bool = False,
    ) -> CallDecision:
        calls = self.config.calls
        if already_handled:
            return CallDecision("ignore", "call was already handled")
        if not calls.enabled:
            return CallDecision("ignore", "wechat call automation is disabled")
        if not calls.auto_answer:
            return CallDecision("ignore", "auto_answer is disabled")
        if call.call_type == "video" and not calls.allow_video_calls:
            return CallDecision("reject", "video calls are disabled")
        if contact is None or not contact.contact_key:
            return CallDecision(_deny_action(calls.default_policy), "could not resolve caller contact")
        if not self.config.contacts.is_allowed(contact.contact_key, contact.display_name):
            return CallDecision(_deny_action(calls.default_policy), "caller is not in the allowlist")
        if not self.config.contacts.is_call_enabled(contact.contact_key, contact.display_name):
            return CallDecision(_deny_action(calls.default_policy), "caller does not have call_enabled permission")
        if self.has_active_call:
            return CallDecision(_busy_action(calls.busy_behavior), "another call is active", retryable=True)
        if self.takeover is not None and self.takeover.enabled:
            return CallDecision("ignore", f"manual takeover is active: {self.takeover.reason}")
        if not health.ok:
            return CallDecision(_unhealthy_action(calls.model_unhealthy_behavior), "; ".join(health.details))
        if not call.has_answer_button:
            return CallDecision("ignore", "answer button was not found or is not actionable", retryable=True)
        return CallDecision("answer", "allowed", auto_answer=True)


def _deny_action(default_policy: str) -> str:
    return "reject" if default_policy == "reject" else "ignore"


def _busy_action(value: str) -> str:
    return "reject" if value == "reject" else "ignore"


def _unhealthy_action(value: str) -> str:
    return "ignore" if value == "ignore" else "reject"
