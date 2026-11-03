"""A supervised, isolated browser window whose lifetime controls the stack."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import threading
import time

import psutil

from scripts.qq_companion.owned_process import OwnedProcess

WEB_ROOT = "http://127.0.0.1:6185/"
WEB_URL = WEB_ROOT + "#/plugin-view/astrbot_chat_q1/console"
PROTOCOL = 1
WINDOW_TIMEOUT = 30
CLOSE_GRACE = 2


def write_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def find_browser() -> Path:
    for folder, relative in (
        ("ProgramFiles(x86)", "Microsoft/Edge/Application/msedge.exe"),
        ("ProgramFiles", "Microsoft/Edge/Application/msedge.exe"),
        ("LOCALAPPDATA", "Microsoft/Edge/Application/msedge.exe"),
        ("ProgramFiles", "Google/Chrome/Application/chrome.exe"),
        ("ProgramFiles(x86)", "Google/Chrome/Application/chrome.exe"),
        ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
    ):
        base = os.environ.get(folder)
        if base and (path := Path(base) / relative).is_file():
            return path
    raise RuntimeError("独立 WebUI 窗口需要 Microsoft Edge 或 Google Chrome，请先安装。")


def browser_command(directory: Path) -> list[str]:
    return [
        str(find_browser()),
        f"--user-data-dir={directory / 'webui-profile'}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-default-apps",
        "--disable-component-extensions-with-background-pages",
        "--disable-extensions",
        "--disable-features=msForceBrowserSignIn",
        f"--app={WEB_URL}",
    ]


def has_browser_window(owners: list[OwnedProcess]) -> bool:
    """Count actual windows, even if Edge keeps a background process alive.

    Minimized windows remain visible to IsWindowVisible. Job membership keeps
    ordinary browser windows and recycled PIDs outside this check.
    """
    pids = {process.pid for owner in owners for process in owner.job.processes()}
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = wintypes.LONG
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    found = False

    @callback_type
    def inspect_window(hwnd, _):
        nonlocal found
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd):
            name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, name, len(name))
            # Exclude owned popups and tool windows, without requiring a caption
            # (fullscreen browser frames remove theirs).
            extended_style = user32.GetWindowLongW(hwnd, -20)
            if (name.value == "Chrome_WidgetWin_1" and not extended_style & 0x80
                    and not user32.GetWindow(hwnd, 4)):  # WS_EX_TOOLWINDOW / GW_OWNER
                found = True
        return True

    user32.EnumWindows(inspect_window, 0)
    return found


class DesktopWebUI:
    def __init__(self, directory: Path, stop_flag: Path):
        self.directory = directory
        self.stop_flag = stop_flag
        self.identity = {"supervisor_pid": os.getpid(),
                         "supervisor_created_at": psutil.Process().create_time()}
        self.owners = []
        self.request_id = None
        self.pending = False
        self.opened = False
        self.missing_since = None
        self.open_deadline = 0
        self.finished = threading.Event()
        self.thread = threading.Thread(target=self.run, name="desktop-webui", daemon=True)

    def reply(self, status: str, message: str = "") -> None:
        write_json(self.directory / "desktop.response.json", {
            **self.identity, "request_id": self.request_id, "status": status, "message": message,
        })

    def step(self) -> bool:
        request = read_json(self.directory / "desktop.request.json")
        if (request.get("request_id") and request["request_id"] != self.request_id
                and all(request.get(key) == value for key, value in self.identity.items())):
            self.request_id = request["request_id"]
            self.pending = True
            self.open_deadline = time.monotonic() + WINDOW_TIMEOUT
            self.owners.append(OwnedProcess(browser_command(self.directory),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        if not self.owners:
            return True
        visible = has_browser_window(self.owners)
        now = time.monotonic()
        if visible:
            self.opened = True
            self.missing_since = None
            if self.pending:
                self.reply("ready")
                self.pending = False
        elif self.pending:
            if now >= self.open_deadline:
                raise RuntimeError("WebUI 窗口未能打开，请查看 Edge/Chrome 是否可正常运行。")
        elif self.opened:
            if self.missing_since is None:
                self.missing_since = now
            if now - self.missing_since >= CLOSE_GRACE:
                print("WebUI windows closed; stopping managed models and services.", flush=True)
                self.stop_flag.touch()
                return False
        return True

    def run(self) -> None:
        try:
            while not self.finished.is_set() and not self.stop_flag.exists():
                if not self.step():
                    break
                self.finished.wait(0.25)
        except Exception as exc:
            print(f"WebUI error: {exc}", flush=True)
            try:
                self.reply("error", str(exc))
            finally:
                self.stop_flag.touch()
        finally:
            for owner in reversed(self.owners):
                try:
                    owner.stop()
                except Exception as exc:
                    print(f"WebUI cleanup: {exc}", flush=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.finished.set()
        if self.thread.ident is not None:
            self.thread.join()
