"""Explorer double-click entry: reuse/start the stack and open its native WebUI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

import psutil

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.qq_companion.desktop_webui import PROTOCOL, WEB_ROOT, WEB_URL, find_browser, read_json, write_json

STATE = ROOT / "storage/qq_companion"
SUPERVISOR = ROOT / "scripts/qq_companion/supervisor.py"
TIMEOUT = 900


@contextmanager
def launch_lock(timeout: float = TIMEOUT):
    """Serialize Explorer launches, including the interval before registry creation."""
    import msvcrt

    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "launcher.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        announced = False
        while True:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if not announced:
                    print("另一个启动窗口正在准备服务，等待完成……", flush=True)
                    announced = True
                if time.monotonic() >= deadline:
                    raise RuntimeError("等待已有启动窗口超时，请查看启动日志。")
                time.sleep(1)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def matches_supervisor(process: psutil.Process) -> bool:
    try:
        return process.is_running() and any(
            Path(arg).resolve() == SUPERVISOR
            for arg in process.cmdline()[1:]
            if Path(arg).name.lower() == "supervisor.py"
        )
    except (OSError, psutil.Error):
        return False


def existing_supervisor() -> psutil.Process | None:
    try:
        data = json.loads((STATE / "supervisor.json").read_text(encoding="utf-8"))
        process = psutil.Process(int(data["supervisor_pid"]))
        if abs(
            process.create_time() - float(data["supervisor_created_at"])
        ) < 0.01 and matches_supervisor(process):
            return process
    except (OSError, ValueError, KeyError, TypeError, psutil.Error):
        pass
    # A terminal launch can still be applying config before its first registry write.
    for process in psutil.process_iter(["name"]):
        if "python" in (process.info.get("name") or "").lower() and matches_supervisor(
            process
        ):
            return process
    return None


def web_ready() -> bool:
    try:
        with build_opener(ProxyHandler({})).open(WEB_ROOT, timeout=2) as response:
            return response.status == 200 and b"astrbot" in response.read(65536).lower()
    except (OSError, URLError):
        return False


def start_supervisor() -> psutil.Process:
    logs = STATE / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(QQ_COMPANION_DOUYIN_WORKER="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    with (logs / "launcher.log").open("ab", buffering=0) as log:
        child = subprocess.Popen(
            [str(ROOT / "runtime_env/python.exe"), "-X", "utf8", str(SUPERVISOR)],
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    return psutil.Process(child.pid)


def wait_for_web(process: psutil.Process, timeout: float = TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    next_notice = time.monotonic() + 20
    while True:
        if not process.is_running():
            raise RuntimeError("服务启动失败或已停止，请查看启动日志中的具体原因。")
        if web_ready():
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "等待网页端就绪超时，请查看日志后重新双击。"
            )
        if time.monotonic() >= next_notice:
            print("正在加载模型和服务，请稍候……", flush=True)
            next_notice = time.monotonic() + 20
        time.sleep(1)


def supports_desktop(process: psutil.Process) -> bool:
    data = read_json(STATE / "supervisor.json")
    return (data.get("supervisor_pid") == process.pid
            and data.get("supervisor_created_at") == process.create_time()
            and data.get("desktop_protocol") == PROTOCOL)


def stop_supervisor(process: psutil.Process) -> None:
    """Request normal cleanup; the supervisor's jobs also cover a stuck exit."""
    if not process.is_running():
        return
    (STATE / "stop.requested").touch()
    try:
        process.wait(timeout=45)
    except psutil.TimeoutExpired:
        process.terminate()  # psutil verifies PID identity; Windows closes owned jobs.
        process.wait(timeout=10)


def open_desktop(process: psutil.Process, timeout: float = TIMEOUT) -> None:
    identity = {"supervisor_pid": process.pid, "supervisor_created_at": process.create_time(),
                "request_id": uuid.uuid4().hex}
    write_json(STATE / "desktop.request.json", identity)
    deadline = time.monotonic() + timeout
    while process.is_running():
        response = read_json(STATE / "desktop.response.json")
        if all(response.get(key) == value for key, value in identity.items()):
            if response.get("status") == "ready":
                return
            raise RuntimeError(response.get("message") or "WebUI 窗口启动失败。")
        if time.monotonic() >= deadline:
            raise RuntimeError("等待独立 WebUI 窗口超时，请查看启动日志。")
        time.sleep(0.25)
    raise RuntimeError("服务已停止，WebUI 窗口未能打开。")


def main() -> int:
    started = None
    try:
        find_browser()  # Fail before allocating models if no supported browser exists.
        with launch_lock():
            try:
                process = existing_supervisor()
                if process is not None:
                    print("检测到项目已启动，等待网页端就绪……", flush=True)
                    wait_for_web(process)
                    if not supports_desktop(process):
                        print("正在更新窗口管理方式，重启已有服务……", flush=True)
                        stop_supervisor(process)
                        process = None
                    elif (STATE / "stop.requested").exists():
                        stop_supervisor(process)
                        process = None
                if process is None:
                    print("正在启动 astrbot-chat（模型、语音、QQ 和抖音）……", flush=True)
                    process = started = start_supervisor()
                    wait_for_web(process)
                print("正在打开独立 WebUI 窗口……", flush=True)
                open_desktop(process)
            except BaseException:
                if started is not None:
                    stop_supervisor(started)
                raise
        print("已打开 WebUI。关闭最后一个 WebUI 窗口将停止模型和全部配套服务。", flush=True)
        return 0
    except (OSError, RuntimeError, psutil.Error) as exc:
        print(f"启动未完成：{exc}", file=sys.stderr, flush=True)
        print(f"日志目录：{STATE / 'logs'}", file=sys.stderr, flush=True)
        print(f"网页地址：{WEB_URL}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
