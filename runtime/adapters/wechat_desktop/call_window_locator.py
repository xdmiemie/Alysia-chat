from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from runtime.adapters.wechat_desktop.call_models import CallButton, CallWindowSnapshot
from runtime.adapters.wechat_desktop.config import WeChatDesktopConfig
from runtime.adapters.wechat_desktop.process_locator import find_wechat_processes


ANSWER_TEXT = ("接听", "接受", "answer", "accept")
REJECT_TEXT = ("拒绝", "挂断", "decline", "reject")
HANGUP_TEXT = ("挂断", "结束", "hang up", "end call")
VOICE_TEXT = ("语音", "voice", "audio")
VIDEO_TEXT = ("视频", "video")
CALL_TEXT = ("通话", "call")
WECHAT_CALL_WINDOW_CLASSES = {"ILinkVoipTrayWnd", "mmui::VOIPTrayWindow", "mmui::VOIPWindow"}


class WeChatCallWindowLocator:
    def __init__(self, *, config: WeChatDesktopConfig, client: object | None = None) -> None:
        self.config = config
        self.client = client

    def locate_incoming_calls(self) -> tuple[CallWindowSnapshot, ...]:
        custom = self._locate_with_client()
        if custom:
            return custom
        return self._scan_uia_top_level_windows()

    def find_call(self, call_id: str) -> CallWindowSnapshot | None:
        for call in self.locate_incoming_calls():
            if call.call_id == call_id:
                return call
        custom = getattr(self.client, "locate_call_window", None)
        if callable(custom):
            return _snapshot_from_any(custom(call_id), account_key=self.config.account.account_key)
        return None

    def current_state(self, call_id: str) -> str:
        call = self.find_call(call_id)
        if call is not None:
            return call.state
        if any(item.state == "connected" for item in self.locate_incoming_calls()):
            return "connected"
        return "idle"

    def _locate_with_client(self) -> tuple[CallWindowSnapshot, ...]:
        method = getattr(self.client, "list_call_windows", None)
        if not callable(method):
            return ()
        raw = method()
        snapshots = [
            snapshot
            for snapshot in (
                _snapshot_from_any(item, account_key=self.config.account.account_key) for item in _as_iter(raw)
            )
            if snapshot is not None and snapshot.state in {"ringing", "answering", "connected", "ending"}
        ]
        return tuple(snapshots)

    def _scan_uia_top_level_windows(self) -> tuple[CallWindowSnapshot, ...]:
        try:
            from pywinauto import Desktop
        except ImportError:
            return ()
        process_ids = {process.pid for process in find_wechat_processes(self.config.client.executable_names)}
        windows = _top_level_uia_windows(Desktop)
        snapshots: list[CallWindowSnapshot] = []
        for window in windows:
            process_id = _window_process_id(window)
            snapshot = _snapshot_from_uia_window(window, account_key=self.config.account.account_key)
            if snapshot is None or snapshot.state not in {"ringing", "answering", "connected", "ending"}:
                continue
            if _trusted_wechat_call_snapshot(snapshot, process_ids=process_ids, process_id=process_id):
                snapshots.append(snapshot)
        snapshots.sort(key=lambda item: _call_window_rank(item, process_ids=process_ids))
        return tuple(snapshots)


def _snapshot_from_any(value: object, *, account_key: str) -> CallWindowSnapshot | None:
    if value is None:
        return None
    if isinstance(value, CallWindowSnapshot):
        return value
    if isinstance(value, dict):
        buttons = value.get("buttons", {})
        return CallWindowSnapshot(
            account_key=str(value.get("account_key") or account_key),
            window_handle=str(value.get("window_handle") or value.get("handle") or ""),
            title=str(value.get("title") or ""),
            process_id=int(value.get("process_id") or 0),
            call_type=_call_type(str(value.get("call_type") or value.get("title") or "")),
            state=str(value.get("state") or "ringing"),  # type: ignore[arg-type]
            contact_display_text=str(value.get("contact_display_text") or value.get("contact") or ""),
            runtime_id=str(value.get("runtime_id") or ""),
            buttons={str(key): _button_from_any(key, button) for key, button in _dict_items(buttons)},
            controls=dict(value.get("controls") or {}),
            metadata=dict(value.get("metadata") or {}),
        )
    return None


def _snapshot_from_uia_window(window: object, *, account_key: str) -> CallWindowSnapshot | None:
    title = _visible_text(window)
    window_class = _class_name(window)
    descendants = _safe_descendants(window)
    texts = [title]
    buttons: dict[str, CallButton] = {}
    controls = {"answer_button": False, "reject_button": False, "hangup_button": False}
    for index, control in enumerate(descendants[:300]):
        text = _visible_text(control)
        if text:
            texts.append(text)
        role = _button_role(text, control_type=_control_type(control))
        if not role:
            continue
        button = CallButton(
            role=role,
            text=text,
            automation_id=_control_id(control),
            control_path=f"descendant:{index}",
            rect=_control_rect(control),
            enabled=_is_enabled(control),
            visible=_is_visible(control),
            invoke=lambda item=control: _invoke_control(item),
            raw_control=control,
        )
        buttons[role] = button
        controls[f"{role}_button"] = button.visible and button.enabled
    combined_text = "\n".join(text for text in texts if text)
    if not _looks_like_call_window(combined_text, buttons, class_name=window_class):
        return None
    return CallWindowSnapshot(
        account_key=account_key,
        window_handle=str(_window_handle(window)),
        title=title,
        process_id=_window_process_id(window),
        call_type=_call_type(combined_text),
        state=_call_state(buttons, combined_text),
        contact_display_text=_contact_text_from_window(combined_text),
        runtime_id=_control_id(window),
        buttons=buttons,
        controls=controls,
        metadata={"combined_text": combined_text, "class_name": window_class},
    )


def _button_from_any(role: object, value: object) -> CallButton:
    if isinstance(value, CallButton):
        return value
    if isinstance(value, dict):
        return CallButton(
            role=str(value.get("role") or role),
            text=str(value.get("text") or ""),
            automation_id=str(value.get("automation_id") or ""),
            control_path=str(value.get("control_path") or ""),
            rect=_rect_from_any(value.get("rect")),
            enabled=bool(value.get("enabled", True)),
            visible=bool(value.get("visible", True)),
            invoke=value.get("invoke") if callable(value.get("invoke")) else None,
        )
    return CallButton(role=str(role), text=str(value or ""))


def _call_state(buttons: dict[str, CallButton], text: str) -> str:
    if "answer" in buttons and ("reject" in buttons or "hangup" in buttons):
        return "ringing"
    if "hangup" in buttons:
        return "connected"
    lowered = text.lower()
    if "missed" in lowered or "未接" in text:
        return "missed"
    return "ringing"


def _call_type(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered or marker in text for marker in VIDEO_TEXT):
        return "video"
    if any(marker in lowered or marker in text for marker in VOICE_TEXT):
        return "voice"
    return "unknown"


def _button_role(text: str, *, control_type: str = "") -> str:
    if control_type and control_type != "Button":
        return ""
    lowered = text.lower()
    if any(marker in lowered or marker in text for marker in ANSWER_TEXT):
        return "answer"
    if any(marker in lowered or marker in text for marker in HANGUP_TEXT):
        return "hangup"
    if any(marker in lowered or marker in text for marker in REJECT_TEXT):
        return "reject"
    return ""


def _looks_like_call_window(text: str, buttons: dict[str, CallButton], *, class_name: str = "") -> bool:
    lowered = text.lower()
    has_call_text = any(marker in lowered or marker in text for marker in (*VOICE_TEXT, *VIDEO_TEXT, *CALL_TEXT))
    has_call_controls = bool({"answer", "reject", "hangup"} & set(buttons))
    if class_name in WECHAT_CALL_WINDOW_CLASSES and has_call_controls:
        return True
    return has_call_text and has_call_controls


def _trusted_wechat_call_snapshot(
    snapshot: CallWindowSnapshot,
    *,
    process_ids: set[int],
    process_id: int,
) -> bool:
    class_name = str(snapshot.metadata.get("class_name", ""))
    if class_name in WECHAT_CALL_WINDOW_CLASSES:
        return True
    return bool(process_ids and process_id in process_ids and snapshot.title in {"微信", "Weixin", "WeChat"})


def _call_window_rank(snapshot: CallWindowSnapshot, *, process_ids: set[int]) -> tuple[int, int, str]:
    class_name = str(snapshot.metadata.get("class_name", ""))
    class_rank = 0 if class_name in WECHAT_CALL_WINDOW_CLASSES else 1
    process_rank = 0 if snapshot.process_id in process_ids else 1
    return (class_rank, process_rank, snapshot.title)


def _contact_text_from_window(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    blocked = (*ANSWER_TEXT, *REJECT_TEXT, *HANGUP_TEXT, *VOICE_TEXT, *VIDEO_TEXT, *CALL_TEXT, "微信", "wechat")
    for line in lines:
        invited = _contact_from_invitation_text(line)
        if invited:
            return invited
        lowered = line.lower()
        if any(marker in lowered or marker in line for marker in blocked):
            continue
        return line
    return ""


def _contact_from_invitation_text(line: str) -> str:
    for marker in ("邀请你", "invites you", "invited you"):
        index = line.lower().find(marker.lower())
        if index > 0:
            return line[:index].strip()
    return ""


def _visible_text(control: object) -> str:
    values: list[str] = []
    for getter in (
        lambda: control.window_text(),  # type: ignore[attr-defined]
        lambda: getattr(getattr(control, "element_info", None), "name", ""),
    ):
        try:
            value = str(getter()).strip()
        except Exception:
            value = ""
        if value:
            values.append(value)
    return "\n".join(dict.fromkeys(values)).strip()


def _class_name(control: object) -> str:
    try:
        return str(control.class_name())
    except Exception:
        return ""


def _control_type(control: object) -> str:
    try:
        return str(getattr(control.element_info, "control_type", ""))
    except Exception:
        return ""


def _safe_descendants(window: object) -> list[object]:
    try:
        return list(window.descendants())  # type: ignore[attr-defined]
    except Exception:
        return []


def _window_process_id(window: object) -> int:
    for getter in (
        lambda: window.process_id(),  # type: ignore[attr-defined]
        lambda: getattr(getattr(window, "element_info", None), "process_id", 0),
    ):
        try:
            return int(getter() or 0)
        except Exception:
            continue
    return 0


def _window_handle(window: object) -> str:
    for getter in (
        lambda: getattr(window, "handle", ""),
        lambda: getattr(getattr(window, "element_info", None), "handle", ""),
    ):
        try:
            value = getter()
        except Exception:
            value = ""
        if value not in (None, ""):
            return str(value)
    return ""


def _control_id(control: object) -> str:
    element_info = getattr(control, "element_info", None)
    for name in ("runtime_id", "automation_id", "control_id", "handle"):
        try:
            value = getattr(element_info, name, "")
        except Exception:
            value = ""
        if value not in (None, "", ()):
            return str(value)
    return ""


def _control_rect(control: object) -> tuple[int, int, int, int]:
    try:
        rectangle = control.rectangle()  # type: ignore[attr-defined]
        return (
            int(getattr(rectangle, "left")),
            int(getattr(rectangle, "top")),
            int(getattr(rectangle, "right")),
            int(getattr(rectangle, "bottom")),
        )
    except Exception:
        return ()


def _rect_from_any(value: object) -> tuple[int, int, int, int]:
    if isinstance(value, (list, tuple)) and len(value) == 4:
        try:
            left, top, right, bottom = value
            return (int(left), int(top), int(right), int(bottom))
        except (TypeError, ValueError):
            return ()
    return ()


def _is_enabled(control: object) -> bool:
    try:
        return bool(control.is_enabled())  # type: ignore[attr-defined]
    except Exception:
        return True


def _is_visible(control: object) -> bool:
    try:
        return bool(control.is_visible())  # type: ignore[attr-defined]
    except Exception:
        return True


def _invoke_control(control: object) -> None:
    click = getattr(control, "click_input", None)
    if callable(click):
        click()
        return
    invoke = getattr(control, "invoke", None)
    if callable(invoke):
        invoke()
        return
    raise RuntimeError("call button control is not invokable")


def _as_iter(value: object) -> Iterable[object]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return value
    return (value,)


def _dict_items(value: object) -> Iterable[tuple[object, object]]:
    return value.items() if isinstance(value, dict) else ()


def _top_level_uia_windows(desktop_factory) -> list[object]:
    windows: list[object] = []
    seen_handles: set[str] = set()
    try:
        desktop = desktop_factory(backend="uia")
        for window in desktop.windows():
            handle = _window_handle(window)
            if handle in seen_handles:
                continue
            seen_handles.add(handle)
            windows.append(window)
    except Exception:
        desktop = None

    for hwnd in _enum_visible_top_level_hwnds():
        handle = str(hwnd)
        if handle in seen_handles:
            continue
        try:
            desktop = desktop or desktop_factory(backend="uia")
            window = desktop.window(handle=hwnd)
        except Exception:
            continue
        seen_handles.add(handle)
        windows.append(window)
    return windows


def _enum_visible_top_level_hwnds() -> list[int]:
    try:
        import win32gui
    except ImportError:
        return []
    hwnds: list[int] = []

    def collect(hwnd, _param):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            if max(0, right - left) * max(0, bottom - top) <= 0:
                return True
        except Exception:
            return True
        hwnds.append(int(hwnd))
        return True

    try:
        win32gui.EnumWindows(collect, None)
    except Exception:
        return []
    return hwnds
