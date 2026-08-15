from __future__ import annotations

import os
import ctypes
from ctypes import wintypes
from pathlib import Path
import signal
import socket
import subprocess
import time
from threading import Event

from runtime.gpt_sovits.model_registry import GPTSoVITSModelEntry


PROJECT_ROOT = Path(__file__).resolve().parents[2]
START_SCRIPT = PROJECT_ROOT / "scripts" / "start_gpt_sovits_upstream.ps1"
PID_PATH = PROJECT_ROOT / "storage" / "temporary" / "gpt-sovits-desktop.pid"
CHARACTER_PATH = PROJECT_ROOT / "storage" / "temporary" / "gpt-sovits-upstream.character"
MODEL_PATH = PROJECT_ROOT / "storage" / "temporary" / "gpt-sovits-upstream.model"
STDOUT_PATH = PROJECT_ROOT / "storage" / "logs" / "gpt-sovits-upstream.out.log"
STDERR_PATH = PROJECT_ROOT / "storage" / "logs" / "gpt-sovits-upstream.err.log"
_JOB_HANDLES: dict[int, int] = {}


def start_gpt_sovits_server(
    model: GPTSoVITSModelEntry,
    *,
    host: str = "127.0.0.1",
    port: int = 9880,
    timeout_seconds: float = 240.0,
    cancelled: Event | None = None,
) -> None:
    """Start the selected character voice model for the desktop app."""
    if gpt_sovits_server_running(host=host, port=port):
        if gpt_sovits_server_matches(model, host=host, port=port):
            return
        raise RuntimeError("正在运行的 GPT-SoVITS 服务不是当前角色/模型，请先关闭模型")
    _clear_stale_pid()
    if cancelled is not None and cancelled.is_set():
        raise RuntimeError("模型启动已取消")

    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(START_SCRIPT),
        "-BindAddr",
        host,
        "-Port",
        str(port),
        "-Character",
        model.character_id,
        "-Model",
        model.model_id,
    ]
    STDOUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        with STDOUT_PATH.open("ab") as stdout, STDERR_PATH.open("ab") as stderr:
            process = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                creationflags=creationflags,
            )
    except OSError as exc:
        raise RuntimeError(f"无法启动 GPT-SoVITS：{exc}") from exc
    PID_PATH.write_text(str(process.pid), encoding="utf-8")
    job_handle = _assign_kill_on_close_job(process)
    if job_handle:
        _JOB_HANDLES[process.pid] = job_handle

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if cancelled is not None and cancelled.is_set():
            stop_gpt_sovits_server(host=host, port=port)
            raise RuntimeError("模型启动已取消")
        if process.poll() is not None:
            PID_PATH.unlink(missing_ok=True)
            raise RuntimeError(
                "GPT-SoVITS 启动失败，请查看 storage/logs/gpt-sovits-upstream.err.log"
            )
        if gpt_sovits_server_running(host=host, port=port):
            if not gpt_sovits_server_matches(model, host=host, port=port):
                stop_gpt_sovits_server(host=host, port=port)
                raise RuntimeError("GPT-SoVITS 已启动，但角色或模型校验失败")
            return
        time.sleep(0.25)
    stop_gpt_sovits_server(host=host, port=port)
    raise TimeoutError("GPT-SoVITS 启动健康检查超时")


def gpt_sovits_server_running(*, host: str = "127.0.0.1", port: int = 9880) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.6):
            return True
    except OSError:
        return False


def gpt_sovits_server_matches(
    model: GPTSoVITSModelEntry,
    *,
    host: str = "127.0.0.1",
    port: int = 9880,
) -> bool:
    if not gpt_sovits_server_running(host=host, port=port):
        return False
    return (
        _read_marker(CHARACTER_PATH) == model.character_id
        and _read_marker(MODEL_PATH) == model.model_id
    )


def stop_gpt_sovits_server(*, host: str = "127.0.0.1", port: int = 9880) -> bool:
    """Stop only the process tree launched by the desktop manager."""
    try:
        pid = int(PID_PATH.read_text(encoding="utf-8-sig").strip())
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            job_handle = _JOB_HANDLES.pop(pid, 0)
            if job_handle:
                ctypes.windll.kernel32.CloseHandle(job_handle)
            else:
                try:
                    subprocess.run(
                        ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                        check=False,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                except subprocess.TimeoutExpired:
                    # The tree termination request has already been issued.
                    # Terminate the launcher itself so shutdown never hangs.
                    os.kill(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    # CUDA teardown can keep the listener visible briefly after taskkill has
    # terminated the PowerShell process tree. Allow the model process enough
    # time to release the socket before deciding shutdown failed.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and gpt_sovits_server_running(host=host, port=port):
        time.sleep(0.15)
    stopped = not gpt_sovits_server_running(host=host, port=port)
    if stopped:
        PID_PATH.unlink(missing_ok=True)
        CHARACTER_PATH.unlink(missing_ok=True)
        MODEL_PATH.unlink(missing_ok=True)
    return stopped


def _clear_stale_pid() -> None:
    try:
        pid = int(PID_PATH.read_text(encoding="utf-8-sig").strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        PID_PATH.unlink(missing_ok=True)


def _read_marker(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""


def _assign_kill_on_close_job(process: subprocess.Popen) -> int:
    """Put a Windows launcher and all descendants in one owned process job."""
    if os.name != "nt":
        return 0

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class BASIC_LIMITS(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED_LIMITS(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMITS),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return 0
    limits = EXTENDED_LIMITS()
    limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    configured = kernel32.SetInformationJobObject(
        job,
        9,  # JobObjectExtendedLimitInformation
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    )
    assigned = configured and kernel32.AssignProcessToJobObject(job, int(process._handle))
    if not assigned:
        kernel32.CloseHandle(job)
        return 0
    return int(job)
