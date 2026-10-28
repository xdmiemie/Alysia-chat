"""Own the Q1 process tree; Ctrl+C or --stop closes everything except NTQQ."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "storage" / "qq_companion"
REGISTRY = STATE / "supervisor.json"
STOP_FLAG = STATE / "stop.requested"
LOGS = STATE / "logs"
ASTRBOT = ROOT / "external" / "AstrBot"
SNOWLUMA = ROOT / "external" / "SnowLuma" / "v2.1.1"
ASTRBOT_PYTHON = ROOT / "envs" / "astrbot" / "Scripts" / "python.exe"
MODEL_PYTHON = ROOT / "runtime_env" / "python.exe"


def port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_port(name: str, port: int, process: subprocess.Popen, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{name} exited early ({process.returncode}); inspect ignored log under {LOGS}")
        if port_open(port):
            print(f"{name} listening on 127.0.0.1:{port}", flush=True)
            return
        time.sleep(1)
    raise RuntimeError(f"{name} did not open port {port}; inspect ignored log under {LOGS}")


def stop_tree(pid: int, started: float) -> None:
    try:
        parent = psutil.Process(pid)
        if abs(parent.create_time() - started) > 2:
            return  # PID was reused; never kill an unrelated process.
        descendants = parent.children(recursive=True)
        for child in reversed(descendants):
            child.terminate()
        parent.terminate()
        _, alive = psutil.wait_procs(descendants + [parent], timeout=8)
        for process in alive:
            process.kill()
        psutil.wait_procs(alive, timeout=5)
    except psutil.NoSuchProcess:
        pass


def stop_recorded() -> None:
    if not REGISTRY.exists():
        print("No Q1 supervisor registry found.")
        return
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    STOP_FLAG.touch()
    for item in reversed(data.get("children", [])):
        stop_tree(item["pid"], item["created_at"])
        print(f"Stopped {item['name']} if still owned.")
    REGISTRY.unlink(missing_ok=True)


def preflight(no_models: bool, with_tts: bool) -> None:
    if not MODEL_PYTHON.exists():
        raise RuntimeError("Stage 10 Python environment is missing; run quickstart/bootstrap.ps1")
    if not ASTRBOT_PYTHON.exists() or not (ASTRBOT / "data" / "cmd_config.json").exists():
        raise RuntimeError("AstrBot venv/config missing; run install_astrbot.ps1 and configure.py --apply")
    if not (SNOWLUMA / "index.mjs").exists() or not (SNOWLUMA / "node.exe").exists():
        raise RuntimeError("Pinned SnowLuma release is absent")
    for port in (5099, 3000, 6185, 6199):
        if port_open(port):
            raise RuntimeError(f"Port {port} is already occupied; stop the existing service first")
    if not no_models:
        if port_open(8080):
            raise RuntimeError("Port 8080 is already occupied; Qwen would not be owned by this supervisor")
        manifest = json.loads((ROOT / "model_store" / "active" / "qwen3.json").read_text(encoding="utf-8"))
        if not (ROOT / manifest["runtime"]["model_file"]).exists():
            raise RuntimeError("Active Qwen GGUF is missing")
    if with_tts:
        for port in (9880, 5002):
            if port_open(port):
                raise RuntimeError(f"Port {port} is already occupied")


def run(no_models: bool, with_tts: bool) -> None:
    preflight(no_models, with_tts)
    configure_args = [str(MODEL_PYTHON), str(ROOT / "scripts" / "qq_companion" / "configure.py"), "--apply"]
    snow_config = os.environ.get("QQ_COMPANION_SNOWLUMA_CONFIG")
    if snow_config:
        configure_args.extend(["--snowluma-config", snow_config])
    configured = subprocess.run(
        configure_args,
        cwd=ROOT,
        check=False,
    )
    if configured.returncode != 0:
        raise RuntimeError("OneBot configuration failed; inspect the error above")
    STATE.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    STOP_FLAG.unlink(missing_ok=True)
    children: list[dict] = []
    handles = []
    restart_specs: dict[str, tuple[list[str], Path, dict | None, int, int]] = {}
    restart_history: dict[str, list[float]] = {}

    def launch(name: str, argv: list[str], cwd: Path, env: dict | None = None) -> subprocess.Popen:
        log = (LOGS / f"{name}.log").open("ab", buffering=0)
        handles.append(log)
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
        children.append({"name": name, "pid": proc.pid, "created_at": psutil.Process(proc.pid).create_time()})
        REGISTRY.write_text(json.dumps({"supervisor_pid": os.getpid(), "children": children}, indent=2), encoding="utf-8")
        return proc

    def start_service(name: str, argv: list[str], cwd: Path, port: int, timeout: int,
                      env: dict | None = None) -> None:
        restart_specs[name] = (argv, cwd, env, port, timeout)
        process = launch(name, argv, cwd, env)
        wait_port(name, port, process, timeout)

    try:
        if not no_models:
            start_service("qwen3", ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                    str(ROOT / "scripts" / "start_qwen3_server.ps1"),
                                    "-UseActive"], ROOT, 8080, 180)
        if with_tts:
            start_service("gpt_sovits", ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                         str(ROOT / "scripts" / "start_gpt_sovits_upstream.ps1")], ROOT, 9880, 240)
            start_service("tts_wrapper", [str(MODEL_PYTHON), "-m", "uvicorn", "runtime.gpt_sovits.api:app",
                                          "--host", "127.0.0.1", "--port", "5002"], ROOT, 5002, 30)
        astr_env = os.environ.copy()
        astr_env["ASTRBOT_CHAT_STATE_DIR"] = str(STATE)
        astr_env["PYTHONIOENCODING"] = "utf-8"
        astr_env["PYTHONUTF8"] = "1"
        astr_env["ASTRBOT_DISABLE_METRICS"] = "1"
        start_service("astrbot", [str(ASTRBOT_PYTHON), "-m", "astrbot.cli", "run", "--port", "6185"],
                      ASTRBOT, 6199, 120, astr_env)
        snow_env = os.environ.copy()
        snow_env["PATH"] = str(SNOWLUMA) + os.pathsep + snow_env.get("PATH", "")
        start_service("snowluma", [str(SNOWLUMA / "node.exe"), "index.mjs"], SNOWLUMA, 5099, 45, snow_env)
        print("Q1 services started. NTQQ must be logged in. Ctrl+C stops managed services.", flush=True)
        while not STOP_FLAG.exists():
            for entry in list(children):
                if not psutil.pid_exists(entry["pid"]):
                    name = entry["name"]
                    if name not in ("astrbot", "snowluma"):
                        raise RuntimeError(f"Managed service {name} exited; see ignored logs")
                    now = time.monotonic()
                    recent = [moment for moment in restart_history.get(name, []) if now - moment < 300]
                    if len(recent) >= 3:
                        raise RuntimeError(f"{name} exited four times in five minutes; see ignored logs")
                    recent.append(now)
                    restart_history[name] = recent
                    delay = 2 ** len(recent)
                    print(f"{name} exited; restarting in {delay}s (attempt {len(recent)}/3)", flush=True)
                    children.remove(entry)
                    REGISTRY.write_text(json.dumps({"supervisor_pid": os.getpid(), "children": children}, indent=2), encoding="utf-8")
                    time.sleep(delay)
                    if STOP_FLAG.exists():
                        break
                    argv, cwd, env, port, timeout = restart_specs[name]
                    start_service(name, argv, cwd, port, timeout, env)
            time.sleep(1)
    finally:
        if REGISTRY.exists():
            stop_recorded()
        for handle in handles:
            handle.close()
        STOP_FLAG.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--no-models", action="store_true", help="Q1 protocol probe without Qwen")
    parser.add_argument("--with-tts", action="store_true", help="Also start existing GPT-SoVITS and wrapper")
    args = parser.parse_args()
    if args.stop:
        stop_recorded()
        return 0
    if args.with_tts and args.no_models:
        parser.error("--with-tts requires models")
    try:
        run(args.no_models, args.with_tts)
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        print(f"Q1 startup/runtime error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
