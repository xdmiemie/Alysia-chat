from __future__ import annotations

import hashlib
import importlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Protocol

from runtime.adapters.wechat_desktop.ui_snapshot import ChatMessageSnapshot, ChatSessionSnapshot, UISnapshot
from runtime.adapters.wechat_desktop.window_locator import WindowInfo


MIN_WECHAT_WINDOW_AREA = 50_000
WECHAT_WINDOW_CACHE_TTL_SECONDS = 5.0
WECHAT_WINDOW_TITLES = {"\u5fae\u4fe1", "Weixin", "WeChat", "MMUIRenderSubWindowHW"}
WECHAT_WINDOW_CLASSES = {"WeChatMainWndForPC", "Qt51514QWindowIcon", "MMUIRenderSubWindowHW"}
WECHAT_PROCESS_NAMES = {
    "weixin.exe",
    "wechat.exe",
    "wechatappex.exe",
    "wechat_starter_v1.0.0.exe",
    "wechat_starter.exe",
    "weixin",
    "wechat",
    "wechatappex",
    "wechat_starter_v1.0.0",
    "wechat_starter",
}
TEXT_TYPES = {
    "text",
    "txt",
    "1",
    "\u6587\u5b57",
    "\u6587\u672c",
}
VOICE_TYPES = {"voice", "audio", "\u8bed\u97f3"}
IMAGE_TYPES = {"image", "\u56fe\u7247"}
FILE_TYPES = {"file", "\u6587\u4ef6"}
SYSTEM_TYPES = {"system", "\u7cfb\u7edf"}
_VISIBLE_WINDOW_CACHE = None
_VISIBLE_WINDOW_CACHE_AT = 0.0


class WeChatDesktopClient(Protocol):
    def locate_main_window(self) -> WindowInfo | None:
        ...

    def snapshot(self) -> UISnapshot:
        ...

    def list_sessions(self, limit: int) -> tuple[ChatSessionSnapshot, ...]:
        ...

    def open_chat(self, contact_key: str) -> None:
        ...

    def current_chat_title(self) -> str:
        ...

    def recent_messages(self, limit: int = 8) -> tuple[ChatMessageSnapshot, ...]:
        ...

    def send_text(self, text: str, *, restore_clipboard: bool = True) -> None:
        ...

    def has_own_text_at_tail(self, text: str) -> bool:
        ...

    def play_voice_message(self, message) -> None:
        ...

    def list_call_windows(self) -> tuple[object, ...]:
        ...

    def locate_call_window(self, call_id: str) -> object | None:
        ...


class UnsupportedPyWeixinApi(RuntimeError):
    pass


class PyWeixinDesktopClient:
    def __init__(self, pywechat_root: str | Path = "third_party/pywechat") -> None:
        self.pywechat_root = Path(pywechat_root)
        self.module = self._import_pyweixin()
        self.tools = getattr(self.module, "Tools", None)
        self.navigator = getattr(self.module, "Navigator", None)
        self.messages = getattr(self.module, "Messages", None)
        if self.tools is None or self.navigator is None or self.messages is None:
            raise UnsupportedPyWeixinApi("pyweixin must expose Tools, Navigator, and Messages")
        self._original_open_weixin = self.navigator.open_weixin
        self.navigator.open_weixin = self._open_weixin
        self.current_contact = ""
        self.account_display_name = ""
        self._message_cache: dict[str, tuple[ChatMessageSnapshot, ...]] = {}
        self._use_current_chat_for_contact: set[str] = set()

    def _import_pyweixin(self):
        if self.pywechat_root.exists():
            for candidate in (self.pywechat_root / "src", self.pywechat_root):
                root = str(candidate.resolve())
                if root not in sys.path:
                    sys.path.insert(0, root)
        try:
            return importlib.import_module("pyweixin")
        except ImportError as exc:
            raise UnsupportedPyWeixinApi(
                "Could not import pyweixin. Put the pinned pywechat checkout under third_party/pywechat."
            ) from exc

    def locate_main_window(self) -> WindowInfo | None:
        try:
            window = self._open_weixin(is_maximize=False)
        except Exception:
            return None
        return _window_info(window)

    def _open_weixin(self, is_maximize: bool | None = None, window_size: tuple | None = None):
        try:
            return self._original_open_weixin(is_maximize=is_maximize, window_size=window_size)
        except Exception as original_exc:
            window = _open_weixin_by_visible_window()
            if window is not None:
                return window
            raise original_exc

    def snapshot(self) -> UISnapshot:
        raw_window = self._safe_open_window()
        window = _window_info(raw_window) if raw_window is not None else None
        running = bool(raw_window) or _safe_is_weixin_running(self.tools) or _is_known_wechat_process_running()
        controls = _inspect_wechat_controls(raw_window) if raw_window is not None else {}
        accessible = any(controls.values())
        about = self._safe_about_weixin()
        version = _first_string(about, ("version", "Version", "\u7248\u672c"))
        account_key = _first_string(about, ("account_display_name", "nickname", "\u6635\u79f0", "wxid"))
        self.account_display_name = account_key or self.account_display_name
        logged_in = bool(window and accessible)
        blocking_dialogs = ()
        if window is not None and not accessible:
            blocking_dialogs = (
                "WeChat is exposing only the MMUIRenderSubWindowHW render surface. "
                "Start Windows Narrator before logging in to WeChat, then restart/re-login WeChat.",
            )
        return UISnapshot(
            logged_in=logged_in,
            account_display_name=self.account_display_name,
            version=version,
            main_window_title=window.title if window else "",
            controls=controls,
            blocking_dialogs=blocking_dialogs,
        )

    def list_sessions(self, limit: int) -> tuple[ChatSessionSnapshot, ...]:
        raw_by_contact = self.messages.check_new_messages(
            is_maximize=False,
            search_pages=limit,
            close_weixin=False,
        )
        if not isinstance(raw_by_contact, dict):
            return ()

        sessions: list[ChatSessionSnapshot] = []
        self._message_cache = {}
        for contact, raw_messages in list(raw_by_contact.items())[:limit]:
            contact_key = str(contact)
            messages = tuple(
                _message_from_raw(
                    raw,
                    index,
                    contact_key=contact_key,
                    own_name=self.account_display_name,
                    source="pyweixin.check_new_messages",
                )
                for index, raw in enumerate(raw_messages or [])
            )
            self._message_cache[contact_key] = messages
            last_text = next((item.text for item in reversed(messages) if item.text.strip()), "")
            sessions.append(
                ChatSessionSnapshot(
                    contact_key=contact_key,
                    display_name=contact_key,
                    title=contact_key,
                    unread=bool(messages),
                    last_text=last_text,
                    is_group=False,
                    is_system=False,
                    metadata={"source": "pyweixin.check_new_messages"},
                )
            )
        return tuple(sessions)

    def open_chat(self, contact_key: str) -> None:
        if not contact_key.strip():
            raise ValueError("contact_key is empty")
        if self.current_contact == contact_key and contact_key in self._use_current_chat_for_contact:
            return
        if _current_chat_matches(contact_key):
            self._use_current_chat_for_contact.add(contact_key)
            self.current_contact = contact_key
            return
        if _select_visible_chat_session(contact_key):
            self._use_current_chat_for_contact.add(contact_key)
            self.current_contact = contact_key
            return
        if contact_key not in self._use_current_chat_for_contact:
            try:
                self.navigator.open_dialog_window(friend=contact_key, is_maximize=False, search_pages=5)
            except Exception:
                if self.locate_main_window() is None:
                    raise
                self._use_current_chat_for_contact.add(contact_key)
        self.current_contact = contact_key

    def current_chat_title(self) -> str:
        return self.current_contact

    def recent_messages(self, limit: int = 8) -> tuple[ChatMessageSnapshot, ...]:
        if not self.current_contact:
            return ()
        cached = self._message_cache.get(self.current_contact)
        if cached and _can_use_cached_messages(cached):
            return cached[-limit:]

        if self.current_contact in self._use_current_chat_for_contact:
            messages = _recent_messages_from_current_chat(
                contact_key=self.current_contact,
                own_name=self.account_display_name,
                limit=limit,
            )
            self._message_cache[self.current_contact] = messages
            return messages[-limit:]

        try:
            raw_messages = self.messages.pull_messages(
                friend=self.current_contact,
                number=limit,
                is_json=False,
                myName=self.account_display_name or None,
                search_pages=5,
                is_maximize=False,
                close_weixin=False,
            )
            messages = tuple(
                _message_from_raw(raw, index, contact_key=self.current_contact, own_name=self.account_display_name)
                for index, raw in enumerate(raw_messages or [])
            )
        except Exception:
            messages = _recent_messages_from_current_chat(
                contact_key=self.current_contact,
                own_name=self.account_display_name,
                limit=limit,
            )
        self._message_cache[self.current_contact] = messages
        return messages[-limit:]

    def send_text(self, text: str, *, restore_clipboard: bool = True) -> None:
        if not self.current_contact:
            raise RuntimeError("open_chat must be called before send_text")
        try:
            if self.current_contact in self._use_current_chat_for_contact:
                raise RuntimeError("current chat fallback mode is active")
            self.messages.send_messages_to_friend(
                friend=self.current_contact,
                messages=[text],
                search_pages=5,
                is_maximize=False,
                close_weixin=False,
            )
        except Exception:
            if not _send_text_to_current_chat(text):
                raise
        self._append_cached_message(
            self.current_contact,
            ChatMessageSnapshot(
                direction="outgoing",
                message_type="text",
                text=text,
                runtime_id=_stable_runtime_id(("sent", self.current_contact, text)),
                metadata={"source": "local_send_cache"},
            ),
        )

    def has_own_text_at_tail(self, text: str) -> bool:
        cached = self._message_cache.get(self.current_contact, ())
        if any(item.direction == "outgoing" and item.text.strip() == text.strip() for item in cached[-3:]):
            return True
        messages = self.recent_messages(limit=5)
        return any(item.direction == "outgoing" and item.text.strip() == text.strip() for item in messages[-3:])

    def play_voice_message(self, message) -> None:
        if not self.current_contact:
            raise RuntimeError("open_chat must be called before play_voice_message")
        if not _click_current_chat_voice_message(message):
            raise RuntimeError("could not click the bound WeChat voice bubble")

    def list_call_windows(self) -> tuple[object, ...]:
        return _list_pyweixin_call_windows(self.module, account_key="role_account_main")

    def locate_call_window(self, call_id: str) -> object | None:
        for call in self.list_call_windows():
            if getattr(call, "call_id", "") == call_id:
                return call
        connected = [call for call in self.list_call_windows() if getattr(call, "state", "") == "connected"]
        return connected[0] if connected else None

    def _safe_about_weixin(self) -> dict[str, Any]:
        try:
            about = self.tools.about_weixin()
        except Exception:
            return {}
        return about if isinstance(about, dict) else {}

    def _append_cached_message(self, contact_key: str, message: ChatMessageSnapshot) -> None:
        messages = self._message_cache.get(contact_key, ())
        self._message_cache[contact_key] = (*messages, message)

    def _safe_open_window(self):
        try:
            return self._open_weixin(is_maximize=False)
        except Exception:
            return None


def _window_info(value) -> WindowInfo | None:
    if value is None:
        return None
    title = _call_no_args(value, "window_text")
    handle = _get(value, "handle", "")
    visible = _call_no_args(value, "is_visible")
    return WindowInfo(
        title=str(title or _get(value, "title", "")),
        handle=str(handle or ""),
        visible=True if visible is None else bool(visible),
    )


def _open_weixin_by_visible_window():
    global _VISIBLE_WINDOW_CACHE
    global _VISIBLE_WINDOW_CACHE_AT
    cached = _cached_visible_window()
    if cached is not None:
        return cached

    hwnd = _find_visible_weixin_hwnd()
    if not hwnd:
        return None
    try:
        from pywinauto import Desktop
    except ImportError:
        return None
    try:
        window = Desktop(backend="uia").window(handle=hwnd)
        try:
            if not window.is_visible():
                window.restore()
        except Exception:
            pass
        _VISIBLE_WINDOW_CACHE = window
        _VISIBLE_WINDOW_CACHE_AT = time.monotonic()
        return window
    except Exception:
        try:
            return Desktop(backend="win32").window(handle=hwnd)
        except Exception:
            return None


def _cached_visible_window():
    if _VISIBLE_WINDOW_CACHE is None:
        return None
    if time.monotonic() - _VISIBLE_WINDOW_CACHE_AT > WECHAT_WINDOW_CACHE_TTL_SECONDS:
        return None
    try:
        if _VISIBLE_WINDOW_CACHE.is_visible():
            return _VISIBLE_WINDOW_CACHE
    except Exception:
        return None
    return None


def _find_visible_weixin_hwnd() -> int:
    try:
        import psutil
        import win32gui
        import win32process
    except ImportError:
        return 0

    weixin_pids = set()
    for process in psutil.process_iter(["pid", "name"]):
        try:
            name = str(process.info.get("name") or "").lower()
            pid = int(process.info.get("pid") or 0)
        except (psutil.Error, ValueError, TypeError):
            continue
        if name in WECHAT_PROCESS_NAMES and pid > 0:
            weixin_pids.add(pid)

    candidates: list[tuple[int, int, bool, bool]] = []

    def collect(hwnd, _param):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd).strip()
        class_name = win32gui.GetClassName(hwnd).strip()
        try:
            _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            pid = 0
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            area = max(0, right - left) * max(0, bottom - top)
        except Exception:
            area = 0
        title_matches = title in WECHAT_WINDOW_TITLES
        class_matches = class_name in WECHAT_WINDOW_CLASSES
        process_matches = pid in weixin_pids
        size_matches = area >= MIN_WECHAT_WINDOW_AREA
        if size_matches and (title_matches or class_matches or process_matches):
            candidates.append((hwnd, area, title_matches, class_matches))
        return True

    try:
        win32gui.EnumWindows(collect, None)
    except Exception:
        return _find_weixin_hwnd_with_powershell()
    if not candidates:
        return _find_weixin_hwnd_with_powershell()
    candidates.sort(key=lambda item: (item[2], item[3], item[1]), reverse=True)
    return int(candidates[0][0])


def _find_weixin_hwnd_with_powershell() -> int:
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                (
                    "Get-Process Weixin,WeChat,wechat_starter_v1.0.0,wechat_starter -ErrorAction SilentlyContinue | "
                    "Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle } | "
                    "Select-Object -First 1 MainWindowHandle,MainWindowTitle | "
                    "ConvertTo-Json -Compress"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0
    if completed.returncode != 0 or not completed.stdout.strip():
        return 0
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return 0
    if isinstance(data, list):
        data = data[0] if data else {}
    try:
        return int(data.get("MainWindowHandle") or 0)
    except (AttributeError, TypeError, ValueError):
        return 0


def _find_render_child_window(window):
    try:
        children = window.children()
    except Exception:
        return None
    best = None
    best_area = 0
    for child in children:
        try:
            class_name = str(child.class_name())
            rectangle = child.rectangle()
            area = max(0, rectangle.right - rectangle.left) * max(0, rectangle.bottom - rectangle.top)
        except Exception:
            continue
        if class_name == "MMUIRenderSubWindowHW" and area > best_area:
            best = child
            best_area = area
    return best if best_area >= MIN_WECHAT_WINDOW_AREA else None


def _has_accessible_weixin_children(window) -> bool:
    return any(_inspect_wechat_controls(window).values())


def _inspect_wechat_controls(window) -> dict[str, bool]:
    controls = {
        "chat_list": False,
        "chat_title": False,
        "message_list": False,
        "input_box": False,
        "send_button": False,
    }
    if window is None:
        return controls
    try:
        descendants = window.descendants()
    except Exception:
        return controls
    try:
        window_rect = window.rectangle()
        left = window_rect.left
        top = window_rect.top
        width = max(1, window_rect.right - window_rect.left)
        height = max(1, window_rect.bottom - window_rect.top)
    except Exception:
        left = 0
        top = 0
        width = 1
        height = 1
    session_right = left + int(width * 0.40)
    chat_left = left + int(width * 0.30)
    header_bottom = top + int(height * 0.15)
    fallback_input_top = top + int(height * 0.85)

    meaningful = False
    rows = []
    for item in descendants[:300]:
        try:
            class_name = str(item.class_name())
            control_type = str(getattr(item.element_info, "control_type", ""))
            text = _visible_text_from_control(item)
            rectangle = item.rectangle()
        except Exception:
            continue
        rows.append((class_name, control_type, text, rectangle))

    chat_edit_tops = [
        rectangle.top
        for _class_name, control_type, _text, rectangle in rows
        if control_type == "Edit" and rectangle.left >= chat_left and rectangle.top > header_bottom
    ]
    input_top = min(chat_edit_tops) if chat_edit_tops else fallback_input_top

    for class_name, control_type, text, rectangle in rows:
        if class_name == "MMUIRenderSubWindowHW":
            continue
        if control_type in {"Button", "Edit", "List", "ListItem", "Text", "ToolBar"}:
            meaningful = True
        if control_type == "List" and rectangle.right <= session_right:
            controls["chat_list"] = True
        if control_type == "List" and rectangle.left >= chat_left and rectangle.bottom <= input_top:
            controls["message_list"] = True
        if control_type == "Edit" and rectangle.left >= chat_left and rectangle.top >= input_top:
            controls["input_box"] = True
        if control_type == "Button" and rectangle.left >= chat_left and rectangle.top >= input_top:
            controls["send_button"] = True
        if control_type in {"Text", "Button"} and text and rectangle.left >= chat_left and rectangle.top <= header_bottom:
            controls["chat_title"] = True
    if meaningful and not any(controls.values()):
        return dict.fromkeys(controls, True)
    return controls


def _safe_is_weixin_running(tools) -> bool:
    try:
        return bool(tools.is_weixin_running())
    except Exception:
        return False


def _is_known_wechat_process_running() -> bool:
    try:
        import psutil
    except ImportError:
        return False
    for process in psutil.process_iter(["name"]):
        try:
            name = str(process.info.get("name") or "").lower()
        except (psutil.Error, TypeError):
            continue
        if name in WECHAT_PROCESS_NAMES:
            return True
    return False


def _recent_messages_from_current_chat(
    *,
    contact_key: str,
    own_name: str = "",
    limit: int = 8,
) -> tuple[ChatMessageSnapshot, ...]:
    window = _open_weixin_by_visible_window()
    if window is None:
        return ()
    return _scrape_current_chat_messages(contact_key=contact_key, own_name=own_name, limit=limit)


def _current_chat_matches(contact_key: str) -> bool:
    window = _open_weixin_by_visible_window()
    if window is None:
        return False
    try:
        window_rect = window.rectangle()
        chat_left = window_rect.left + int((window_rect.right - window_rect.left) * 0.30)
        header_bottom = window_rect.top + int((window_rect.bottom - window_rect.top) * 0.15)
    except Exception:
        chat_left = 0
        header_bottom = 0
    for item in _safe_descendants(window):
        try:
            control_type = str(getattr(item.element_info, "control_type", ""))
            text = _visible_text_from_control(item)
            rectangle = item.rectangle()
        except Exception:
            continue
        if control_type not in {"Text", "Button"} or text.strip() != contact_key.strip():
            continue
        if rectangle.left >= chat_left and (not header_bottom or rectangle.top <= header_bottom):
            return True
    return False


def _select_visible_chat_session(contact_key: str) -> bool:
    window = _open_weixin_by_visible_window()
    if window is None:
        return False
    try:
        window_rect = window.rectangle()
        session_left = window_rect.left + int((window_rect.right - window_rect.left) * 0.05)
        session_right = window_rect.left + int((window_rect.right - window_rect.left) * 0.40)
        session_top = window_rect.top + int((window_rect.bottom - window_rect.top) * 0.10)
    except Exception:
        session_left = 0
        session_right = 0
        session_top = 0

    candidates = []
    for item in _safe_descendants(window):
        try:
            control_type = str(getattr(item.element_info, "control_type", ""))
            text = _visible_text_from_control(item)
            rectangle = item.rectangle()
        except Exception:
            continue
        if control_type not in {"ListItem", "Button", "Text"} or text.strip() != contact_key.strip():
            continue
        if session_right and not (session_left <= rectangle.left <= session_right):
            continue
        if session_top and rectangle.top < session_top:
            continue
        area = max(0, rectangle.right - rectangle.left) * max(0, rectangle.bottom - rectangle.top)
        candidates.append((0 if control_type == "ListItem" else 1, -area, item))

    for _priority, _area, item in sorted(candidates, key=lambda value: (value[0], value[1])):
        try:
            item.click_input()
            time.sleep(0.2)
            return _current_chat_matches(contact_key)
        except Exception:
            continue
    return False


def _scrape_current_chat_messages(
    *,
    contact_key: str,
    own_name: str = "",
    limit: int = 8,
) -> tuple[ChatMessageSnapshot, ...]:
    window = _open_weixin_by_visible_window()
    if window is None:
        return ()
    try:
        message_list = _find_current_message_list(window)
        message_rect = message_list.rectangle() if message_list is not None else None
        window_mid_x = message_rect.mid_point().x if message_rect is not None else window.rectangle().mid_point().x
        if message_list is not None:
            _move_message_list_to_tail(message_list)
    except Exception:
        message_list = None
        message_rect = None
        window_mid_x = 0
    descendants = _message_descendants(message_list) if message_list is not None else _safe_descendants(window)

    messages: list[ChatMessageSnapshot] = []
    visible_occurrences: dict[tuple[str, str, str], int] = {}
    for item in descendants:
        try:
            class_name = str(item.class_name())
            control_type = str(getattr(item.element_info, "control_type", ""))
            text = _visible_text_from_control(item)
            rectangle = item.rectangle()
        except Exception:
            continue
        if message_rect is not None and not _rect_inside(rectangle, message_rect):
            continue
        if not _looks_like_message_control(class_name, control_type, text):
            continue
        sender, content = _split_visible_chat_text(text, contact_key=contact_key)
        content = _strip_sender_lines(content, contact_key=contact_key, own_name=own_name)
        direction = _direction_from_message_control(
            item,
            item_mid_x=rectangle.mid_point().x,
            window_mid_x=window_mid_x,
            sender=sender,
            contact_key=contact_key,
            own_name=own_name,
        )
        message_type = _message_type_from_class_name(class_name, content)
        duration_ms = _voice_duration_ms(content)
        if duration_ms and message_type == "unknown":
            message_type = "voice"
        fingerprint_content = _fingerprint_content(content, message_type=message_type, duration_ms=duration_ms)
        occurrence_key = (direction, sender, fingerprint_content)
        occurrence = visible_occurrences.get(occurrence_key, 0)
        visible_occurrences[occurrence_key] = occurrence + 1
        fingerprint = _stable_runtime_id(
            (
                "uia",
                contact_key,
                direction,
                fingerprint_content,
                _control_runtime_identity(item)
                or f"visible:{occurrence}:{rectangle.left}:{rectangle.top}:{rectangle.right}:{rectangle.bottom}",
            )
        )
        metadata = {
            "fingerprint": fingerprint,
            "sender": sender,
            "raw_type": class_name,
            "control_type": control_type,
            "source": "uia.current_chat_scrape",
            "raw_text": text[:200],
        }
        if duration_ms:
            metadata["voice_duration_ms"] = duration_ms
        messages.append(
            ChatMessageSnapshot(
                direction=direction,
                message_type=message_type,
                text=content,
                runtime_id=_stable_runtime_id(
                    (contact_key, sender, fingerprint_content, class_name, str(len(messages)))
                ),
                relative_order=len(messages),
                metadata=metadata,
            )
        )
    return tuple(messages[-limit:])


def _click_current_chat_voice_message(message) -> bool:
    window = _open_weixin_by_visible_window()
    if window is None:
        return False
    try:
        message_list = _find_current_message_list(window)
        message_rect = message_list.rectangle() if message_list is not None else None
        window_mid_x = message_rect.mid_point().x if message_rect is not None else window.rectangle().mid_point().x
        if message_list is not None:
            _move_message_list_to_tail(message_list)
    except Exception:
        message_list = None
        message_rect = None
        window_mid_x = 0

    descendants = _message_descendants(message_list) if message_list is not None else _safe_descendants(window)
    contact_key = str(getattr(message, "contact_key", ""))
    own_name = ""
    target_fingerprint = str(getattr(message, "raw_fingerprint", "") or getattr(message, "fingerprint", ""))
    target_runtime_id = str(getattr(message, "runtime_id", ""))
    target_order = int(getattr(message, "relative_order", -1))
    target_text = str(getattr(message, "text", ""))

    visible_occurrences: dict[tuple[str, str, str], int] = {}
    index = 0
    for item in descendants:
        try:
            class_name = str(item.class_name())
            control_type = str(getattr(item.element_info, "control_type", ""))
            text = _visible_text_from_control(item)
            rectangle = item.rectangle()
        except Exception:
            continue
        if message_rect is not None and not _rect_inside(rectangle, message_rect):
            continue
        if not _looks_like_message_control(class_name, control_type, text):
            continue
        sender, content = _split_visible_chat_text(text, contact_key=contact_key)
        content = _strip_sender_lines(content, contact_key=contact_key, own_name=own_name)
        message_type = _message_type_from_class_name(class_name, content)
        if message_type != "voice":
            index += 1
            continue
        direction = _direction_from_message_control(
            item,
            item_mid_x=rectangle.mid_point().x,
            window_mid_x=window_mid_x,
            sender=sender,
            contact_key=contact_key,
            own_name=own_name,
        )
        duration_ms = _voice_duration_ms(content)
        fingerprint_content = _fingerprint_content(content, message_type=message_type, duration_ms=duration_ms)
        occurrence_key = (direction, sender, fingerprint_content)
        occurrence = visible_occurrences.get(occurrence_key, 0)
        visible_occurrences[occurrence_key] = occurrence + 1
        fingerprint = _stable_runtime_id(
            (
                "uia",
                contact_key,
                direction,
                fingerprint_content,
                _control_runtime_identity(item)
                or f"visible:{occurrence}:{rectangle.left}:{rectangle.top}:{rectangle.right}:{rectangle.bottom}",
            )
        )
        runtime_id = _stable_runtime_id((contact_key, sender, fingerprint_content, class_name, str(index)))
        if (
            (target_fingerprint and target_fingerprint == fingerprint)
            or (target_runtime_id and target_runtime_id == runtime_id)
            or (target_order == index and (not target_text or target_text == content))
        ):
            return _click_voice_play_hotspot(item, direction=direction)
        index += 1
    return False


def _click_voice_play_hotspot(item, *, direction: str) -> bool:
    try:
        rectangle = item.rectangle()
        width = max(1, rectangle.right - rectangle.left)
        height = max(1, rectangle.bottom - rectangle.top)
        y = rectangle.top + height // 2
        if width >= 260:
            offset = int(min(220, max(100, width * 0.18)))
            x = rectangle.left + offset if direction == "incoming" else rectangle.right - offset
        else:
            x = rectangle.left + width // 2
        item.click_input(coords=(int(x), int(y)), absolute=True)
        return True
    except Exception:
        try:
            item.click_input()
            return True
        except Exception:
            return False


def _control_runtime_identity(item) -> str:
    element_info = getattr(item, "element_info", None)
    for name in ("runtime_id", "automation_id", "control_id", "handle"):
        try:
            value = getattr(element_info, name, "")
        except Exception:
            value = ""
        if value not in (None, "", ()):
            return str(value)
    return ""


def _move_message_list_to_tail(message_list) -> None:
    for action in (
        lambda: message_list.type_keys("{END}"),
        lambda: message_list.type_keys("^{END}"),
        lambda: message_list.wheel_mouse_input(wheel_dist=-5),
    ):
        try:
            action()
            time.sleep(0.03)
            return
        except Exception:
            continue


def _find_current_message_list(window):
    try:
        window_rect = window.rectangle()
        chat_left = window_rect.left + int((window_rect.right - window_rect.left) * 0.30)
        header_bottom = window_rect.top + int((window_rect.bottom - window_rect.top) * 0.15)
        fallback_input_top = window_rect.top + int((window_rect.bottom - window_rect.top) * 0.85)
    except Exception:
        chat_left = 0
        header_bottom = 0
        fallback_input_top = 0

    edit_rows = _controls_with_rect(window, "Edit")
    list_rows = _controls_with_rect(window, "List")
    if edit_rows or list_rows:
        chat_edit_tops = [
            rectangle.top
            for _item, control_type, rectangle in edit_rows
            if control_type == "Edit" and rectangle.left >= chat_left and rectangle.top > header_bottom
        ]
        input_top = min(chat_edit_tops) if chat_edit_tops else fallback_input_top
        return _best_message_list(list_rows, chat_left=chat_left, input_top=input_top)

    rows = []
    for item in _safe_descendants(window):
        try:
            control_type = str(getattr(item.element_info, "control_type", ""))
            rectangle = item.rectangle()
        except Exception:
            continue
        rows.append((item, control_type, rectangle))
    chat_edit_tops = [
        rectangle.top
        for _item, control_type, rectangle in rows
        if control_type == "Edit" and rectangle.left >= chat_left and rectangle.top > header_bottom
    ]
    input_top = min(chat_edit_tops) if chat_edit_tops else fallback_input_top
    best = None
    best_area = 0
    for item, control_type, rectangle in rows:
        if control_type != "List":
            continue
        area = max(0, rectangle.right - rectangle.left) * max(0, rectangle.bottom - rectangle.top)
        if rectangle.left >= chat_left and rectangle.bottom <= input_top and area > best_area:
            best = item
            best_area = area
    return best


def _controls_with_rect(window, control_type: str):
    rows = []
    try:
        controls = window.descendants(control_type=control_type)
    except Exception:
        return rows
    for item in controls:
        try:
            item_control_type = str(getattr(item.element_info, "control_type", ""))
            rectangle = item.rectangle()
        except Exception:
            continue
        if item_control_type == control_type:
            rows.append((item, item_control_type, rectangle))
    return rows


def _best_message_list(rows, *, chat_left: int, input_top: int):
    best = None
    best_area = 0
    for item, control_type, rectangle in rows:
        if control_type != "List":
            continue
        area = max(0, rectangle.right - rectangle.left) * max(0, rectangle.bottom - rectangle.top)
        if rectangle.left >= chat_left and rectangle.bottom <= input_top and area > best_area:
            best = item
            best_area = area
    return best


def _message_descendants(message_list):
    items = []
    for getter in (
        lambda: message_list.children(),
        lambda: message_list.descendants(),
    ):
        try:
            items = list(getter())
        except Exception:
            items = []
        if items:
            return items
    return ()


def _rect_inside(inner, outer) -> bool:
    try:
        return (
            inner.left >= outer.left
            and inner.right <= outer.right
            and inner.top >= outer.top
            and inner.bottom <= outer.bottom
        )
    except Exception:
        return False


def _list_pyweixin_call_windows(module, *, account_key: str) -> tuple[object, ...]:
    snapshots = []
    for window in _pyweixin_call_window_candidates(module):
        try:
            from runtime.adapters.wechat_desktop.call_window_locator import _snapshot_from_uia_window

            snapshot = _snapshot_from_uia_window(window, account_key=account_key)
        except Exception:
            snapshot = None
        if snapshot is not None and snapshot.state in {"ringing", "connected", "answering", "ending"}:
            snapshots.append(snapshot)
    return tuple(snapshots)


def _pyweixin_call_window_candidates(module) -> tuple[object, ...]:
    windows = []
    seen: set[str] = set()
    for selector in _pyweixin_call_window_selectors(module):
        window = _pyweixin_window_from_selector(selector)
        if window is None:
            continue
        handle = str(_get(window, "handle", "") or _control_runtime_identity(window))
        if handle and handle in seen:
            continue
        if handle:
            seen.add(handle)
        windows.append(window)
    return tuple(windows)


def _pyweixin_call_window_selectors(module) -> tuple[dict[str, str], ...]:
    selectors: list[dict[str, str]] = []
    try:
        from pyweixin.Uielements import Windows

        for name in ("IncomingCallWindow", "VoipCallWindow"):
            selector = getattr(Windows, name, None)
            if isinstance(selector, dict):
                selectors.append({str(key): str(value) for key, value in selector.items()})
    except Exception:
        pass
    selectors.extend(
        [
            {"title": "微信", "class_name": "mmui::VOIPTrayWindow"},
            {"title": "Weixin", "class_name": "mmui::VOIPTrayWindow"},
            {"title": "WeChat", "class_name": "mmui::VOIPTrayWindow"},
            {"class_name": "mmui::VOIPTrayWindow"},
            {"class_name": "ILinkVoipTrayWnd"},
            {"title": "微信音视频通话", "class_name": "mmui::VOIPWindow"},
            {"title": "Weixin Voice & Video Calls", "class_name": "mmui::VOIPWindow"},
            {"class_name": "mmui::VOIPWindow"},
        ]
    )
    unique: list[dict[str, str]] = []
    seen = set()
    for selector in selectors:
        key = tuple(sorted(selector.items()))
        if key in seen:
            continue
        seen.add(key)
        unique.append(selector)
    return tuple(unique)


def _pyweixin_window_from_selector(selector: dict[str, str]):
    window = _pywinauto_window_from_selector(selector)
    if window is not None:
        return window
    return _win32_window_from_selector(selector)


def _pywinauto_window_from_selector(selector: dict[str, str]):
    try:
        from pywinauto import Desktop
    except ImportError:
        return None
    try:
        window = Desktop(backend="uia").window(**selector)
        if window.exists(timeout=0.05):
            return window
    except Exception:
        return None
    return None


def _win32_window_from_selector(selector: dict[str, str]):
    try:
        import win32gui
        from pywinauto import Desktop
    except ImportError:
        return None
    class_name = selector.get("class_name", "")
    title = selector.get("title", "")
    handles = []
    if class_name or title:
        try:
            hwnd = win32gui.FindWindow(class_name or None, title or None)
        except Exception:
            hwnd = 0
        if hwnd:
            handles.append(hwnd)
    if not handles and class_name:
        found = []

        def collect(hwnd, _param):
            try:
                if win32gui.GetClassName(hwnd) == class_name:
                    found.append(hwnd)
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(collect, None)
        except Exception:
            pass
        handles.extend(found)
    for hwnd in handles:
        try:
            return Desktop(backend="uia").window(handle=hwnd)
        except Exception:
            continue
    return None


def _safe_descendants(window):
    try:
        return window.descendants()
    except Exception:
        return ()


def _visible_text_from_control(item) -> str:
    values: list[str] = []
    for getter in (
        lambda: item.window_text(),
        lambda: getattr(item.element_info, "name", ""),
    ):
        try:
            value = str(getter()).strip()
        except Exception:
            value = ""
        if value:
            values.append(value)
    try:
        for child in item.descendants(control_type="Text"):
            value = str(child.window_text()).strip()
            if value:
                values.append(value)
    except Exception:
        pass
    return "\n".join(dict.fromkeys(values)).strip()


def _looks_like_message_control(class_name: str, control_type: str, text: str) -> bool:
    if not text:
        return False
    if class_name.startswith("mmui::Chat") and "ItemView" in class_name:
        return True
    if class_name in {"mmui::ChatTextItemView", "mmui::ChatBubbleItemView", "mmui::ChatBubbleReferItemView"}:
        return True
    if control_type in {"ListItem", "Text", "Custom", "Button"} and (
        _looks_like_human_message_text(text) or _voice_duration_ms(text) > 0
    ):
        return True
    return False


def _looks_like_human_message_text(text: str) -> bool:
    cleaned = " ".join(text.split())
    if not cleaned or len(cleaned) > 1000:
        return False
    blocked = {
        "\u641c\u7d22",
        "\u6d88\u606f",
        "\u901a\u8baf\u5f55",
        "\u6536\u85cf",
        "\u670b\u53cb\u5708",
        "\u89c6\u9891\u53f7",
        "\u6587\u4ef6\u4f20\u8f93\u52a9\u624b",
        "\u53d1\u9001",
        "\u8868\u60c5",
    }
    if cleaned in blocked:
        return False
    if cleaned.startswith(("http://", "https://")):
        return False
    return any("\u4e00" <= char <= "\u9fff" for char in cleaned) or any(char.isalnum() for char in cleaned)


def _send_text_to_current_chat(text: str) -> bool:
    window = _open_weixin_by_visible_window()
    if window is None:
        return False
    edit = _find_current_chat_edit(window)
    if edit is None:
        return False
    try:
        from pyweixin.WinSettings import SystemSettings

        edit.click_input()
        SystemSettings.copy_text_to_clipboard(text)
        time.sleep(0.05)
        edit.type_keys("^v")
        time.sleep(0.05)
        edit.type_keys("%s")
        return True
    except Exception:
        return False


def _find_current_chat_edit(window):
    candidates = (
        {"control_type": "Edit", "auto_id": "chat_input_field"},
        {"control_type": "Edit", "class_name": "mmui::ChatInputField"},
        {"control_type": "Edit"},
    )
    for selector in candidates:
        try:
            edits = window.descendants(**selector)
        except Exception:
            continue
        if edits:
            return edits[-1]
    return None


def _split_visible_chat_text(text: str, *, contact_key: str) -> tuple[str, str]:
    for separator in ("\n", " "):
        prefix = f"{contact_key}{separator}"
        if text.startswith(prefix):
            return contact_key, text[len(prefix) :].strip()
    return "", text.strip()


def _strip_sender_lines(text: str, *, contact_key: str, own_name: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) <= 1:
        return text.strip()
    sender_names = {contact_key.strip()}
    if own_name.strip():
        sender_names.add(own_name.strip())
    cleaned = [line for line in lines if line not in sender_names]
    return "\n".join(cleaned).strip() or text.strip()


def _direction_from_geometry(
    *,
    item_mid_x: int,
    window_mid_x: int,
    sender: str,
    contact_key: str,
    own_name: str,
) -> str:
    if window_mid_x:
        return "incoming" if item_mid_x < window_mid_x else "outgoing"
    return _direction_from_sender(sender, contact_key=contact_key, own_name=own_name)


def _direction_from_message_control(
    item,
    *,
    item_mid_x: int,
    window_mid_x: int,
    sender: str,
    contact_key: str,
    own_name: str,
) -> str:
    child_direction = _direction_from_message_children(item, contact_key=contact_key, own_name=own_name)
    if child_direction != "unknown":
        return child_direction
    return _direction_from_geometry(
        item_mid_x=item_mid_x,
        window_mid_x=window_mid_x,
        sender=sender,
        contact_key=contact_key,
        own_name=own_name,
    )


def _direction_from_message_children(item, *, contact_key: str, own_name: str) -> str:
    candidates: list[tuple[int, str]] = []
    try:
        children = item.descendants()
    except Exception:
        children = ()
    for child in children:
        try:
            text = _visible_text_from_control(child).strip()
            control_type = str(getattr(child.element_info, "control_type", ""))
            rectangle = child.rectangle()
        except Exception:
            continue
        if control_type not in {"Button", "Text", "Image", "Custom"}:
            continue
        direction = _direction_from_exact_sender_text(text, contact_key=contact_key, own_name=own_name)
        if direction in {"incoming", "outgoing"}:
            candidates.append((rectangle.left, direction))
    if not candidates:
        return "unknown"
    candidates.sort(key=lambda value: value[0])
    return candidates[0][1]


def _direction_from_exact_sender_text(text: str, *, contact_key: str, own_name: str) -> str:
    cleaned = text.strip()
    if cleaned == contact_key.strip():
        return "incoming"
    if own_name and cleaned == own_name.strip():
        return "outgoing"
    return "unknown"


def _message_type_from_class_name(class_name: str, content: str) -> str:
    if class_name == "mmui::ChatTextItemView":
        return "text"
    if "Voice" in class_name or "Audio" in class_name or _voice_duration_ms(content) > 0:
        return "voice"
    if not content.strip():
        return "unknown"
    non_text_markers = (
        "\u56fe\u7247",
        "\u89c6\u9891",
        "\u6587\u4ef6",
        "\u8bed\u97f3",
        "\u5fae\u4fe1\u7ea2\u5305",
        "\u5fae\u4fe1\u8f6c\u8d26",
    )
    if any(marker in content for marker in non_text_markers):
        return "unknown"
    return "text"


def _fingerprint_content(content: str, *, message_type: str, duration_ms: int = 0) -> str:
    if message_type == "voice" or duration_ms > 0:
        return f"voice:{duration_ms or _voice_duration_ms(content)}"
    return content


def _voice_duration_ms(text: str) -> int:
    cleaned = _canonical_voice_text(text)
    if not cleaned:
        return 0
    has_voice_unit = any(unit in cleaned for unit in ("秒", "''", '"', "s"))
    if not has_voice_unit:
        return 0
    digits = ""
    for char in cleaned:
        if char.isdigit() or char == ".":
            digits += char
        elif digits:
            break
    if not digits:
        return 0
    try:
        return int(float(digits) * 1000)
    except ValueError:
        return 0


def _canonical_voice_text(text: str) -> str:
    cleaned = text.strip().lower().replace(" ", "")
    cleaned = cleaned.replace("未播放", "").replace("已播放", "")
    cleaned = cleaned.replace("播放", "")
    cleaned = re.sub(r"[,，;；:：]", "", cleaned)
    return cleaned


def _message_from_raw(
    value,
    index: int,
    *,
    contact_key: str,
    own_name: str = "",
    source: str = "pyweixin.pull_messages",
) -> ChatMessageSnapshot:
    text = _first_string(value, ("text", "content", "message", "\u6d88\u606f\u5185\u5bb9"))
    sender = _first_string(value, ("sender", "from", "name", "\u6d88\u606f\u53d1\u9001\u4eba"))
    raw_type = _first_string(value, ("message_type", "type", "\u6d88\u606f\u7c7b\u578b"))
    time_label = _first_string(value, ("time", "time_label", "timestamp", "\u6d88\u606f\u53d1\u9001\u65f6\u95f4"))
    runtime_id = _first_string(value, ("runtime_id", "id", "message_id"))
    message_type = _message_type_from_raw(raw_type)
    metadata = {"sender": sender, "raw_type": raw_type, "source": source}
    duration_ms = _voice_duration_ms(text)
    if duration_ms:
        metadata["voice_duration_ms"] = duration_ms
        if message_type == "unknown":
            message_type = "voice"
    if not runtime_id:
        runtime_id = _stable_runtime_id((contact_key, sender, text, raw_type, time_label, str(index)))

    return ChatMessageSnapshot(
        direction=_direction_from_sender(sender, contact_key=contact_key, own_name=own_name),
        message_type=message_type,
        text=text,
        runtime_id=runtime_id,
        relative_order=index,
        time_label=time_label,
        metadata=metadata,
    )


def _can_use_cached_messages(messages: tuple[ChatMessageSnapshot, ...]) -> bool:
    if not messages:
        return False
    source = str(messages[-1].metadata.get("source", ""))
    return source == "pyweixin.check_new_messages"


def _direction_from_sender(sender: str, *, contact_key: str, own_name: str) -> str:
    cleaned = sender.strip()
    if not cleaned:
        return "unknown"
    if cleaned == contact_key:
        return "incoming"
    if own_name and cleaned == own_name:
        return "outgoing"
    return "outgoing"


def _message_type_from_raw(value: str) -> str:
    lowered = value.strip().lower()
    if lowered in TEXT_TYPES:
        return "text"
    if lowered in VOICE_TYPES:
        return "voice"
    if lowered in IMAGE_TYPES:
        return "image"
    if lowered in FILE_TYPES:
        return "file"
    if lowered in SYSTEM_TYPES:
        return "system"
    return "unknown"


def _first_string(value, names: tuple[str, ...]) -> str:
    for name in names:
        item = _get(value, name, None)
        if item not in (None, ""):
            return str(item)
    return ""


def _call_no_args(target, name: str):
    func = getattr(target, name, None)
    if callable(func):
        try:
            return func()
        except Exception:
            return None
    return None


def _get(value, name: str, default):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _stable_runtime_id(parts: tuple[str, ...]) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
