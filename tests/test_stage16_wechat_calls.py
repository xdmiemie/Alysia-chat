from __future__ import annotations

import asyncio
from dataclasses import replace

from runtime.adapters.wechat_desktop.call_contact_resolver import CallContactResolver
from runtime.adapters.wechat_desktop.call_controller import CallStateTracker, WeChatCallController
from runtime.adapters.wechat_desktop.call_detection_thread import ReadOnlyCallDetectionThread, _read_only_snapshot
from runtime.adapters.wechat_desktop.call_health import CallHealthChecker, CallHealthStatus
from runtime.adapters.wechat_desktop.call_models import CallButton, CallContact, CallWindowSnapshot
from runtime.adapters.wechat_desktop.call_policy import WeChatCallPolicy
from runtime.adapters.wechat_desktop.call_window_locator import WeChatCallWindowLocator
from runtime.adapters.wechat_desktop.call_window_locator import _snapshot_from_uia_window, _trusted_wechat_call_snapshot
from runtime.adapters.wechat_desktop.config import WeChatDesktopConfig, load_wechat_desktop_config
from runtime.adapters.wechat_desktop.app import _should_pause_message_polling_for_calls
from runtime.adapters.wechat_desktop.incoming_call_watcher import IncomingCallWatcher
from runtime.adapters.wechat_desktop.takeover import TakeoverState
from runtime.adapters.wechat_desktop.ui_queue import SingleUIQueue


class FakeCallClient:
    def __init__(self, *, contact_key: str = "alice-key", display_name: str = "Alice") -> None:
        self.contact_key = contact_key
        self.display_name = display_name
        self.state = "ringing"
        self.call_type = "voice"
        self.clicked: list[str] = []
        self.window_handle = "call-window-1"

    def list_call_windows(self):
        if self.state == "idle":
            return ()
        return (
            CallWindowSnapshot(
                account_key="role",
                window_handle=self.window_handle,
                title=f"{self.display_name} voice call",
                call_type=self.call_type,
                state=self.state,
                contact_display_text=self.display_name,
                buttons=self._buttons(),
                controls={
                    "answer_button": self.state == "ringing",
                    "reject_button": self.state == "ringing",
                    "hangup_button": self.state == "connected",
                },
            ),
        )

    def resolve_call_contact(self, _call):
        return CallContact(
            account_key="role",
            contact_key=self.contact_key,
            display_name=self.display_name,
            source="fake",
        )

    def _buttons(self):
        if self.state == "ringing":
            return {
                "answer": CallButton("answer", invoke=lambda: self._click("answer")),
                "reject": CallButton("reject", invoke=lambda: self._click("reject")),
            }
        if self.state == "connected":
            return {"hangup": CallButton("hangup", invoke=lambda: self._click("hangup"))}
        return {}

    def _click(self, action: str) -> None:
        self.clicked.append(action)
        if action == "answer":
            self.state = "connected"
        if action in {"reject", "hangup"}:
            self.state = "idle"


class FakeElementInfo:
    def __init__(self, *, control_type: str, name: str = "", process_id: int = 0) -> None:
        self.control_type = control_type
        self.name = name
        self.process_id = process_id
        self.automation_id = ""


class FakeCallUIControl:
    handle = 0

    def __init__(
        self,
        *,
        control_type: str,
        text: str,
        class_name: str = "",
        process_id: int = 36180,
        children: tuple["FakeCallUIControl", ...] = (),
    ) -> None:
        self.element_info = FakeElementInfo(control_type=control_type, name=text, process_id=process_id)
        self._text = text
        self._class_name = class_name
        self._children = children
        self.clicked = False

    def class_name(self):
        return self._class_name

    def window_text(self):
        return self._text

    def descendants(self):
        return self._children

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def click_input(self):
        self.clicked = True


class FakeRawButton:
    def __init__(self, on_click) -> None:
        self.on_click = on_click
        self.clicked = False

    def click_input(self):
        self.clicked = True
        self.on_click()


class FallbackRawButton:
    def __init__(self, on_click) -> None:
        self.on_click = on_click
        self.methods: list[str] = []

    def click_input(self):
        self.methods.append("click_input")
        raise RuntimeError("uia click failed")

    def click(self):
        self.methods.append("click")
        self.on_click()


class OneShotRawCallClient(FakeCallClient):
    def __init__(self) -> None:
        super().__init__()
        self.list_calls = 0

    def list_call_windows(self):
        self.list_calls += 1
        if self.list_calls > 1:
            return ()
        return (
            CallWindowSnapshot(
                account_key="role",
                window_handle=self.window_handle,
                title=f"{self.display_name} voice call",
                call_type=self.call_type,
                state=self.state,
                contact_display_text=self.display_name,
                buttons={"answer": CallButton("answer", raw_control=FakeRawButton(lambda: self._click("answer")))},
                controls={"answer_button": True, "reject_button": False, "hangup_button": False},
            ),
        )


class StickyRingingRawCallClient(FakeCallClient):
    def _buttons(self):
        return {
            "answer": CallButton("answer", raw_control=FakeRawButton(lambda: self.clicked.append("answer"))),
            "hangup": CallButton("hangup", invoke=lambda: self._click("hangup")),
        }


class FallbackRawCallClient(FakeCallClient):
    def __init__(self) -> None:
        super().__init__()
        self.raw_button: FallbackRawButton | None = None

    def _buttons(self):
        self.raw_button = FallbackRawButton(lambda: self._click("answer"))
        return {"answer": CallButton("answer", raw_control=self.raw_button)}


def _stage16_config(*, allow: tuple[str, ...] = ("alice-key",)) -> WeChatDesktopConfig:
    return WeChatDesktopConfig(
        account=WeChatDesktopConfig.account.__class__(account_key="role"),
        client=replace(WeChatDesktopConfig.client, selector_profile="stage16-test"),
        contacts=WeChatDesktopConfig.contacts.__class__(
            default_policy="deny",
            allow_contact_keys=allow,
        ),
        calls=replace(WeChatDesktopConfig.calls, ring_wait_ms=0, duplicate_cooldown_seconds=60),
    )


def _stack(config: WeChatDesktopConfig, client: FakeCallClient, health: CallHealthChecker | None = None):
    locator = WeChatCallWindowLocator(config=config, client=client)
    resolver = CallContactResolver(account_key=config.account.account_key, client=client)
    state = CallStateTracker()
    controller = WeChatCallController(
        locator=locator,
        resolver=resolver,
        ui=config.ui,
        queue=SingleUIQueue(operation_timeout_seconds=1),
        state=state,
    )
    watcher = IncomingCallWatcher(
        config=config,
        locator=locator,
        resolver=resolver,
        controller=controller,
        health_checker=health or CallHealthChecker(config=config),
        state=state,
        time_fn=lambda: 10.0,
    )
    return locator, resolver, controller, watcher, state


def test_stage16_config_loads_call_defaults():
    config = load_wechat_desktop_config("configs/runtime/wechat.yaml")

    assert config.calls.enabled is True
    assert config.calls.auto_answer is True
    assert config.calls.allow_video_calls is False
    assert config.calls.poll_interval_seconds == 0.2
    assert config.calls.ring_wait_ms == 0
    assert config.calls.pause_message_polling_while_enabled is True
    assert config.calls.model_unhealthy_behavior == "ignore"
    assert config.contacts.is_call_enabled("n", "n") is True


def test_stage16_policy_allows_whitelisted_voice_call_when_healthy():
    config = _stage16_config()
    client = FakeCallClient()
    locator, resolver, _controller, _watcher, _state = _stack(config, client)
    call = locator.locate_incoming_calls()[0]
    contact = resolver.resolve(call)

    decision = WeChatCallPolicy(config=config).decide(
        call=call,
        contact=contact,
        health=CallHealthStatus(ok=True),
    )

    assert decision.action == "answer"
    assert decision.auto_answer is True


def test_stage16_watcher_answers_whitelisted_voice_call_without_pausing_messages_after_connect():
    config = _stage16_config()
    client = FakeCallClient()
    _locator, _resolver, _controller, watcher, state = _stack(config, client)

    results = asyncio.run(watcher.poll_once())

    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].action == "answer"
    assert results[0].state == "connected"
    assert client.clicked == ["answer"]
    assert state.has_active_call is True
    assert state.ordinary_messages_paused is False


def test_stage16_enabled_call_watcher_does_not_pause_messages_when_idle():
    config = _stage16_config()
    client = FakeCallClient()
    client.state = "idle"
    _locator, _resolver, _controller, watcher, _state = _stack(config, client)

    results = asyncio.run(watcher.poll_once())

    assert results == ()
    assert _should_pause_message_polling_for_calls(config, watcher) is False


def test_stage16_call_watcher_pauses_messages_only_for_active_call_state():
    config = _stage16_config()
    client = FakeCallClient()
    _locator, _resolver, _controller, watcher, state = _stack(config, client)

    state.transition(
        call_id="active-call",
        state="ringing",
        contact=CallContact(account_key="role", contact_key="alice-key"),
    )

    assert _should_pause_message_polling_for_calls(config, watcher) is True


def test_stage16_watcher_answers_detected_snapshot_without_relocating_call():
    config = _stage16_config()
    client = OneShotRawCallClient()
    _locator, _resolver, _controller, watcher, _state = _stack(config, client)

    results = asyncio.run(watcher.poll_once())

    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].action == "answer"
    assert results[0].state == "answering"
    assert results[0].verified is False
    assert results[0].metadata["click_method"] == "raw_control.click_input"
    assert client.clicked == ["answer"]
    assert client.list_calls == 1


def test_stage16_answer_tries_fallback_click_when_uia_click_fails():
    config = _stage16_config()
    client = FallbackRawCallClient()
    _locator, _resolver, _controller, watcher, _state = _stack(config, client)

    results = asyncio.run(watcher.poll_once())

    assert results[0].ok is True
    assert results[0].state == "answering"
    assert results[0].metadata["click_method"] == "raw_control.click"
    assert client.clicked == ["answer"]
    assert client.raw_button is not None
    assert client.raw_button.methods == ["click_input", "click"]


def test_stage16_answer_refreshes_read_only_snapshot_before_click():
    config = _stage16_config()
    client = FakeCallClient()
    _locator, _resolver, controller, _watcher, _state = _stack(config, client)
    readonly = CallWindowSnapshot(
        account_key="role",
        window_handle=client.window_handle,
        title="Alice voice call",
        call_type="voice",
        state="ringing",
        contact_display_text="Alice",
        buttons={"answer": CallButton("answer", text="接受", rect=(10, 20, 30, 40))},
    )
    contact = CallContact(account_key="role", contact_key="alice-key", display_name="Alice")

    result = asyncio.run(controller.answer_snapshot(readonly, contact))

    assert result.ok is True
    assert client.clicked == ["answer"]


def test_stage16_read_only_detection_snapshot_strips_live_controls():
    raw = object()
    snapshot = CallWindowSnapshot(
        account_key="role",
        window_handle="call-window-1",
        contact_display_text="Alice",
        buttons={
            "answer": CallButton(
                "answer",
                text="接受",
                rect=(10, 20, 30, 40),
                invoke=lambda: None,
                raw_control=raw,
            )
        },
    )

    readonly = _read_only_snapshot(snapshot)

    assert readonly.buttons["answer"].rect == (10, 20, 30, 40)
    assert readonly.buttons["answer"].invoke is None
    assert readonly.buttons["answer"].raw_control is None


def test_stage16_read_only_detection_thread_pauses_after_detecting_call():
    config = _stage16_config()
    client = FakeCallClient()
    locator = WeChatCallWindowLocator(config=config, client=client)
    detector = ReadOnlyCallDetectionThread(config=config, interval_seconds=0.2, locator=locator)

    detector._detect_once()

    assert detector.paused is True
    assert len(detector.latest().calls) == 1


def test_stage16_unverified_answer_can_retry_while_call_is_still_ringing():
    config = _stage16_config()
    client = StickyRingingRawCallClient()
    _locator, _resolver, _controller, watcher, _state = _stack(config, client)

    first = asyncio.run(watcher.poll_once())
    second = asyncio.run(watcher.poll_once())

    assert first[0].action == "answer"
    assert second[0].action == "answer"
    assert client.clicked == ["answer", "answer"]


def test_stage16_non_allowlisted_call_is_ignored_without_click():
    config = _stage16_config(allow=())
    client = FakeCallClient()
    _locator, _resolver, _controller, watcher, _state = _stack(config, client)

    results = asyncio.run(watcher.poll_once())

    assert results[0].ok is True
    assert results[0].action == "ignore"
    assert client.clicked == []


def test_stage16_video_call_is_rejected_by_default():
    config = _stage16_config()
    client = FakeCallClient()
    client.call_type = "video"
    _locator, _resolver, _controller, watcher, _state = _stack(config, client)

    results = asyncio.run(watcher.poll_once())

    assert results[0].action == "reject"
    assert client.clicked == ["reject"]


def test_stage16_unhealthy_models_prevent_auto_answer_without_click():
    config = _stage16_config()
    client = FakeCallClient()
    health = CallHealthChecker(
        config=config,
        qwen3_health_provider=lambda: type("Status", (), {"ok": False, "details": ("qwen busy",)})(),
    )
    _locator, _resolver, _controller, watcher, _state = _stack(config, client, health=health)

    results = asyncio.run(watcher.poll_once())

    assert results[0].action == "ignore"
    assert client.clicked == []


def test_stage16_same_call_is_not_clicked_twice():
    config = _stage16_config()
    client = FakeCallClient()
    _locator, _resolver, _controller, watcher, _state = _stack(config, client)

    first = asyncio.run(watcher.poll_once())
    client.state = "ringing"
    second = asyncio.run(watcher.poll_once())

    assert first[0].action == "answer"
    assert second[0].action == "ignore"
    assert client.clicked == ["answer"]


def test_stage16_manual_takeover_blocks_auto_answer():
    config = _stage16_config()
    client = FakeCallClient()
    takeover = TakeoverState()
    takeover.activate("operator")
    locator = WeChatCallWindowLocator(config=config, client=client)
    resolver = CallContactResolver(account_key=config.account.account_key, client=client)
    call = locator.locate_incoming_calls()[0]
    contact = resolver.resolve(call)

    decision = WeChatCallPolicy(config=config, takeover=takeover).decide(
        call=call,
        contact=contact,
        health=CallHealthStatus(ok=True),
    )

    assert decision.action == "ignore"


def test_stage16_hangup_releases_call_state():
    config = _stage16_config()
    client = FakeCallClient()
    _locator, _resolver, controller, watcher, state = _stack(config, client)

    answer = asyncio.run(watcher.poll_once())[0]
    hangup = asyncio.run(controller.hangup(answer.call_id, "test complete"))

    assert hangup.ok is True
    assert client.clicked == ["answer", "hangup"]
    assert state.state == "idle"
    assert state.ordinary_messages_paused is False


def test_stage16_real_wechat_voip_tray_shape_is_trusted():
    window = FakeCallUIControl(
        control_type="Pane",
        text="微信",
        class_name="ILinkVoipTrayWnd",
        process_id=36180,
        children=(
            FakeCallUIControl(control_type="Button", text="n邀请你语音通话", process_id=36180),
            FakeCallUIControl(control_type="Button", text="挂断", process_id=36180),
            FakeCallUIControl(control_type="Button", text="接受", process_id=36180),
        ),
    )

    snapshot = _snapshot_from_uia_window(window, account_key="role")

    assert snapshot is not None
    assert snapshot.state == "ringing"
    assert snapshot.contact_display_text == "n"
    assert set(snapshot.buttons) == {"answer", "hangup"}
    assert _trusted_wechat_call_snapshot(snapshot, process_ids={36180}, process_id=36180) is True


def test_stage16_shell_window_voice_text_is_not_trusted():
    window = FakeCallUIControl(
        control_type="Window",
        text="Windows PowerShell",
        class_name="CASCADIA_HOSTING_WINDOW_CLASS",
        process_id=20464,
        children=(
            FakeCallUIControl(
                control_type="Text",
                text="powershell -File scripts/start_wechat_desktop.ps1 -EnableCalls",
                process_id=20464,
            ),
            FakeCallUIControl(control_type="Button", text="Answer from docs", process_id=20464),
        ),
    )

    snapshot = _snapshot_from_uia_window(window, account_key="role")

    assert snapshot is not None
    assert _trusted_wechat_call_snapshot(snapshot, process_ids={36180}, process_id=20464) is False
