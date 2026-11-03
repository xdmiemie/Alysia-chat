from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import psutil
import pytest

from scripts.qq_companion import supervisor
from scripts.qq_companion.owned_process import OwnedProcess


ROOT = Path(__file__).resolve().parents[1]


def wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for local process test")
        time.sleep(0.02)


def test_cleanup_continues_after_a_child_disappears_or_denies_termination(monkeypatch):
    calls = []

    class Process:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return 100

        def children(self, recursive):
            return [Process(2), Process(3)]

        def terminate(self):
            calls.append(self.pid)
            if self.pid == 3:
                raise psutil.NoSuchProcess(self.pid)
            if self.pid == 2:
                raise psutil.AccessDenied(self.pid)

    monkeypatch.setattr(supervisor.psutil, "Process", Process)
    monkeypatch.setattr(supervisor.psutil, "wait_procs", lambda processes, timeout: (processes, []))
    supervisor.stop_tree(1, 100)
    assert calls == [3, 2, 1]


def test_registry_keeps_failures_and_stops_other_services(tmp_path, monkeypatch):
    registry, flag = tmp_path / "supervisor.json", tmp_path / "stop.requested"
    children = [{"name": str(i), "pid": i, "created_at": 100} for i in (1, 2, 3)]
    registry.write_text(json.dumps({"children": children}), encoding="utf-8")
    monkeypatch.setattr(supervisor, "REGISTRY", registry)
    monkeypatch.setattr(supervisor, "STOP_FLAG", flag)
    calls = []

    def stop(pid, created):
        calls.append(pid)
        if pid == 2:
            raise psutil.AccessDenied(pid)

    monkeypatch.setattr(supervisor, "stop_tree", stop)
    with pytest.raises(RuntimeError, match="registry retained"):
        supervisor.stop_recorded()
    assert calls == [3, 2, 1]
    assert json.loads(registry.read_text())["children"] == [children[1]]
    assert flag.exists()
    monkeypatch.setattr(supervisor, "stop_tree", lambda *args: None)
    supervisor.stop_recorded()
    assert not registry.exists()


def test_shutdown_signal_marks_stop_once_and_cleanup_ignores_repeats(tmp_path, monkeypatch):
    handlers = {}
    flag = tmp_path / "stop.requested"
    monkeypatch.setattr(supervisor, "STOP_FLAG", flag)
    monkeypatch.setattr(supervisor.signal, "signal", lambda sig, handler: handlers.setdefault(sig, handler))
    with supervisor.shutdown_signals():
        handler = handlers[signal.SIGINT]
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGINT, None)
        assert flag.exists()
        handler(signal.SIGINT, None)


@pytest.mark.skipif(os.name != "nt", reason="Windows job ownership")
def test_job_stops_grandchild_after_launcher_exits(tmp_path):
    marker = tmp_path / "child.json"
    script = ("import json, subprocess, sys; from pathlib import Path; "
              "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
              f"Path({str(marker)!r}).write_text(json.dumps(p.pid))")
    owner = OwnedProcess([sys.executable, "-c", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_until(marker.exists)
        pid = json.loads(marker.read_text())
        owner.process.wait(timeout=10)
        assert psutil.pid_exists(pid)
        owner.stop()
        wait_until(lambda: not psutil.pid_exists(pid))
    finally:
        owner.stop()


DRIVER = '''
import json, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from scripts.qq_companion import supervisor, owned_process
directory = Path(sys.argv[2])
scenario = sys.argv[3]
real_owner = owned_process.OwnedProcess
count = 0

def fake_service(argv, **kwargs):
    global count
    if scenario.startswith('desktop') and any(arg.startswith('--app=') for arg in argv):
        owner = real_owner(argv, **kwargs)
        (directory / ('browser-' + str(owner.process.pid) + '.json')).write_text(
            json.dumps(owner.process.pid))
        return owner
    count += 1
    marker = directory / (str(count) + '.json')
    script = ("import json, os, subprocess, sys, time; from pathlib import Path; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "Path(%r).write_text(json.dumps([os.getpid(),p.pid])); time.sleep(60)" % str(marker))
    kwargs['cwd'] = directory
    command = [sys.executable, '-c', script]
    if count == 1:
        # Match the production Qwen/TTS PowerShell -> Python/native hierarchy.
        quote = lambda value: "'" + value.replace("'", "''") + "'"
        command = ['powershell', '-NoProfile', '-Command',
            '& ' + quote(sys.executable) + ' -c ' + quote(script)]
    return real_owner(command, **kwargs)

def wait_port(name, port, process, timeout):
    while not (directory / (str(count) + '.json')).exists():
        time.sleep(.02)
    if scenario == 'startup':
        (directory / 'ready').touch()
        while True:
            time.sleep(.02)

class Broker:
    def acquire(self, owner): pass
    def release(self): pass

original_stop = real_owner.stop
def delayed_stop(self):
    (directory / 'cleaning').touch()
    time.sleep(.3)
    original_stop(self)

real_owner.stop = delayed_stop
owned_process.OwnedProcess = fake_service
supervisor.STATE = directory
supervisor.REGISTRY = directory / 'supervisor.json'
supervisor.STOP_FLAG = directory / 'stop.requested'
supervisor.LOGS = directory / 'logs'
supervisor.preflight = lambda *args, **kwargs: None
supervisor.wait_port = wait_port
supervisor.wait_onebot = lambda *args, **kwargs: None
supervisor.GPUResourceBroker = Broker
supervisor.subprocess.run = lambda *args, **kwargs: type('Result', (), {'returncode': 0})()
if scenario.startswith('desktop'):
    from scripts.qq_companion import desktop_webui
    desktop_webui.WEB_URL = sys.argv[4]
if scenario != 'startup':
    original_port = wait_port
    def wait_port(name, *args):
        original_port(name, *args)
        if name == 'snowluma':
            (directory / 'ready').touch()
    supervisor.wait_port = wait_port
sys.argv = ['supervisor.py'] + ([] if scenario == 'startup' else ['--no-models'])
raise SystemExit(supervisor.main())
'''


@pytest.mark.skipif(os.name != "nt", reason="Windows console signals and job close")
@pytest.mark.parametrize("scenario", ["interrupt", "startup", "force"])
def test_real_supervisor_shutdown_with_local_substitutes(tmp_path, scenario):
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with (tmp_path / "driver.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, str(driver), str(ROOT), str(tmp_path), scenario],
            cwd=ROOT, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP, stdout=log, stderr=subprocess.STDOUT)
        try:
            wait_until(lambda: (tmp_path / "ready").exists() or process.poll() is not None)
            assert process.poll() is None, (tmp_path / "driver.log").read_text(encoding="utf-8")
            markers = list(tmp_path.glob("[12].json"))
            assert len(markers) == (1 if scenario == "startup" else 2)
            pids = [pid for marker in markers for pid in json.loads(marker.read_text())]
            registry = json.loads((tmp_path / "supervisor.json").read_text())
            pids += [entry["pid"] for entry in registry["children"]]
            assert all(psutil.pid_exists(pid) for pid in pids)
            if scenario == "force":
                process.kill()
            else:
                process.send_signal(signal.CTRL_BREAK_EVENT)
                wait_until(lambda: (tmp_path / "cleaning").exists())
                process.send_signal(signal.CTRL_BREAK_EVENT)
            process.wait(timeout=15)
            if scenario != "force":
                assert process.returncode == 0, (tmp_path / "driver.log").read_text(encoding="utf-8")
                assert not (tmp_path / "supervisor.json").exists()
                assert not (tmp_path / "stop.requested").exists()
            wait_until(lambda: all(not psutil.pid_exists(pid) for pid in pids))
            assert sentinel.poll() is None, "Shutdown affected an unrelated process"
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            sentinel.kill()
            sentinel.wait(timeout=10)


@pytest.mark.skipif(os.name != "nt" or os.environ.get("QQ_TEST_DESKTOP") != "1",
                    reason="Opt-in real Edge/Chrome windows: set QQ_TEST_DESKTOP=1")
@pytest.mark.parametrize("scenario", ["desktop-close", "desktop-force"])
def test_real_webui_controls_supervisor_and_service_trees(tmp_path, scenario):
    """Exercise actual browser windows with local service substitutes and no accounts."""
    import ctypes
    from ctypes import wintypes
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    from scripts.qq_companion import desktop_webui

    desktop_webui.find_browser()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = wintypes.LONG
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
    user32.SetWindowLongW.restype = wintypes.LONG
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]

    def windows():
        pids = {json.loads(path.read_text()) for path in tmp_path.glob("browser-*.json")}
        handles = []

        @callback_type
        def collect(hwnd, _):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and user32.IsWindowVisible(hwnd):
                name = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, name, len(name))
                style = user32.GetWindowLongW(hwnd, -16)
                if name.value == "Chrome_WidgetWin_1" and style & 0x00C80000 == 0x00C80000:
                    handles.append(hwnd)
            return True

        user32.EnumWindows(collect, 0)
        return handles

    class Page(BaseHTTPRequestHandler):
        loads = 0

        def do_GET(self):
            if self.path == "/":
                Page.loads += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b'<title>WebUI lifecycle test</title>'
                             + (b'<meta http-equiv="refresh" content="1">' if Page.loads < 4 else b'')
                             + b'<h1>Temporary WebUI lifecycle test</h1><p>No models or accounts connected.</p>')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
        creationflags=subprocess.CREATE_NO_WINDOW)
    with (tmp_path / "driver.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, str(driver), str(ROOT), str(tmp_path), scenario,
                                   f"http://127.0.0.1:{server.server_port}/"],
            cwd=ROOT, creationflags=subprocess.CREATE_NO_WINDOW, stdout=log, stderr=subprocess.STDOUT)
        try:
            wait_until(lambda: (tmp_path / "ready").exists() or process.poll() is not None)
            assert process.poll() is None, (tmp_path / "driver.log").read_text(encoding="utf-8")
            registry = desktop_webui.read_json(tmp_path / "supervisor.json")
            identity = {key: registry[key] for key in ("supervisor_pid", "supervisor_created_at")}

            def open_window(request_id):
                desktop_webui.write_json(tmp_path / "desktop.request.json", {
                    **identity, "request_id": request_id,
                })
                wait_until(lambda: desktop_webui.read_json(tmp_path / "desktop.response.json").get(
                    "request_id") == request_id, timeout=40)
                response = desktop_webui.read_json(tmp_path / "desktop.response.json")
                assert response["status"] == "ready", response

            open_window("one")
            wait_until(lambda: len(windows()) == 1)
            wait_until(lambda: Page.loads >= 4, timeout=15)
            handles = windows()
            user32.ShowWindow(handles[0], 6)  # Minimize after verifying actual page refreshes.
            time.sleep(desktop_webui.CLOSE_GRACE + 0.5)
            assert process.poll() is None
            # Fullscreen removes the native caption; this must not look like exit.
            original_style = user32.GetWindowLongW(handles[0], -16)
            user32.SetWindowLongW(handles[0], -16, original_style & ~0x00C80000)
            try:
                time.sleep(desktop_webui.CLOSE_GRACE + 0.5)
                assert process.poll() is None
            finally:
                user32.SetWindowLongW(handles[0], -16, original_style)
            open_window("two")
            wait_until(lambda: len(windows()) == 2)
            pids = [pid for marker in tmp_path.glob("[12].json") for pid in json.loads(marker.read_text())]
            pids += [child.pid for child in psutil.Process(process.pid).children(recursive=True)]
            if scenario == "desktop-force":
                process.kill()
            else:
                first = windows()[0]
                user32.ShowWindow(first, 9)
                assert user32.PostMessageW(first, 0x0112, 0xF060, 0)  # WM_SYSCOMMAND / SC_CLOSE.
                wait_until(lambda: len(windows()) == 1)
                time.sleep(desktop_webui.CLOSE_GRACE + 0.5)
                assert process.poll() is None
                last = windows()[0]
                user32.ShowWindow(last, 9)
                assert user32.PostMessageW(last, 0x0112, 0xF060, 0)
                try:
                    wait_until(lambda: not windows())
                except AssertionError:
                    for hwnd in windows():
                        title = ctypes.create_unicode_buffer(256)
                        user32.GetWindowTextW(hwnd, title, len(title))
                        print("Remaining test window", hwnd, "closed", last, repr(title.value),
                              hex(user32.GetWindowLongW(hwnd, -16) & 0xffffffff))
                    raise
            process.wait(timeout=20)
            if scenario == "desktop-close":
                assert process.returncode == 0, (tmp_path / "driver.log").read_text(encoding="utf-8")
                assert not (tmp_path / "supervisor.json").exists()
                assert not (tmp_path / "stop.requested").exists()
            wait_until(lambda: all(not psutil.pid_exists(pid) for pid in pids))
            assert not windows()
            assert sentinel.poll() is None, "Shutdown affected an unrelated process"
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            sentinel.kill()
            sentinel.wait(timeout=10)
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)
