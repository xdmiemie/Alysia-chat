"""Own the Q1 process tree; Ctrl+C or --stop closes everything except NTQQ."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import psutil

try:
    from scripts.qq_companion.gpu_broker import GPUResourceBroker
except ModuleNotFoundError:  # direct script execution
    from gpu_broker import GPUResourceBroker

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
STATE = ROOT / "storage" / "qq_companion"
REGISTRY = STATE / "supervisor.json"
STOP_FLAG = STATE / "stop.requested"
LOGS = STATE / "logs"
ASTRBOT = ROOT / "external" / "AstrBot"
SNOWLUMA = ROOT / "external" / "SnowLuma" / "v2.1.1"
ASTRBOT_PYTHON = ROOT / "envs" / "astrbot" / "Scripts" / "python.exe"
MODEL_PYTHON = ROOT / "runtime_env" / "python.exe"


def ignore_interrupts():
    for sig in (signal.SIGINT, getattr(signal, "SIGBREAK", signal.SIGINT)):
        signal.signal(sig, signal.SIG_IGN)


@contextmanager
def shutdown_signals():
    requested = False

    def request_shutdown(signum, frame):
        nonlocal requested
        STOP_FLAG.parent.mkdir(parents=True, exist_ok=True)
        STOP_FLAG.touch()
        if not requested:
            requested = True
            raise KeyboardInterrupt

    signals = {signal.SIGINT, getattr(signal, "SIGBREAK", signal.SIGINT)}
    previous = {sig: signal.signal(sig, request_shutdown) for sig in signals}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_port(name: str, port: int, process: subprocess.Popen, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if STOP_FLAG.exists():
            raise KeyboardInterrupt
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
        for process in list(reversed(descendants)) + [parent]:
            try:
                process.terminate()
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied:
                # Windows can report AccessDenied for a process already exiting.
                # Still attempt every sibling and the parent, then verify exit.
                pass
        _, alive = psutil.wait_procs(descendants + [parent], timeout=8)
        for process in alive:
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied:
                pass
        _, alive = psutil.wait_procs(alive, timeout=5)
        if alive:
            raise RuntimeError(f"Owned processes did not exit: {[process.pid for process in alive]}")
    except psutil.NoSuchProcess:
        pass


def stop_recorded() -> None:
    if not REGISTRY.exists():
        print("No Q1 supervisor registry found.")
        return
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    STOP_FLAG.touch()
    failed = []
    for item in reversed(data.get("children", [])):
        try:
            stop_tree(item["pid"], item["created_at"])
            print(f"Stopped {item['name']} if still owned.", flush=True)
        except Exception as exc:
            failed.append(item)
            print(f"Could not stop {item['name']}: {exc}", file=sys.stderr, flush=True)
    if failed:
        data["children"] = list(reversed(failed))
        REGISTRY.write_text(json.dumps(data, indent=2), encoding="utf-8")
        raise RuntimeError("Some owned services remain; registry retained for --stop retry")
    REGISTRY.unlink(missing_ok=True)


def preflight(no_models: bool, with_tts: bool, *, local_q4: bool = False) -> None:
    if not MODEL_PYTHON.exists():
        raise RuntimeError("Stage 10 Python environment is missing; run quickstart/bootstrap.ps1")
    if not ASTRBOT_PYTHON.exists() or not (ASTRBOT / "data" / "cmd_config.json").exists():
        raise RuntimeError("AstrBot venv/config missing; run install_astrbot.ps1 and configure.py --apply")
    if not (SNOWLUMA / "index.mjs").exists() or not (SNOWLUMA / "node.exe").exists():
        raise RuntimeError("Pinned SnowLuma release is absent")
    for port in (() if local_q4 else (5099, 3000, 6185, 6199)):
        if port_open(port):
            raise RuntimeError(f"Port {port} is already occupied; stop the existing service first")
    if not no_models:
        for port in (8080, 5001, 8765):
            if port_open(port):
                raise RuntimeError(f"Port {port} is already occupied; model/media service would not be owned by this supervisor")
        manifest = json.loads((ROOT / "model_store" / "active" / "qwen3.json").read_text(encoding="utf-8"))
        if not (ROOT / manifest["runtime"]["model_file"]).exists():
            raise RuntimeError("Active Qwen GGUF is missing")
    if with_tts:
        for port in (9880, 5002):
            if port_open(port):
                raise RuntimeError(f"Port {port} is already occupied")


def run(no_models: bool, with_tts: bool, *, local_q4: bool = False) -> None:
    global STATE, REGISTRY, STOP_FLAG, LOGS
    if local_q4:
        STATE = ROOT / "storage/temporary/q4-stack-acceptance"
        REGISTRY, STOP_FLAG, LOGS = STATE / "supervisor.json", STATE / "stop.requested", STATE / "logs"
    preflight(no_models, with_tts, local_q4=local_q4)
    configure_args = [str(MODEL_PYTHON), str(ROOT / "scripts" / "qq_companion" / "configure.py"), "--apply"]
    snow_config = os.environ.get("QQ_COMPANION_SNOWLUMA_CONFIG")
    if snow_config:
        configure_args.extend(["--snowluma-config", snow_config])
    configured = None if local_q4 else subprocess.run(
        configure_args,
        cwd=ROOT,
        check=False,
    )
    if configured is not None and configured.returncode != 0:
        raise RuntimeError("OneBot configuration failed; inspect the error above")
    STATE.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    STOP_FLAG.unlink(missing_ok=True)
    children: list[dict] = []
    handles = []
    owned = {}
    restart_specs: dict[str, tuple[list[str], Path, dict | None, int, int]] = {}
    restart_history: dict[str, list[float]] = {}
    broker = GPUResourceBroker()
    dispatcher = None
    last_cache_cleanup = time.monotonic()

    def write_registry():
        temporary = REGISTRY.with_suffix(".tmp")
        temporary.write_text(json.dumps({"supervisor_pid": os.getpid(),
            "supervisor_created_at": psutil.Process().create_time(), "children": children}, indent=2), encoding="utf-8")
        temporary.replace(REGISTRY)

    def stop_entry(entry):
        owner = owned.get(entry["pid"])
        if owner:
            owner.stop()
            owned.pop(entry["pid"], None)
        else:
            stop_tree(entry["pid"], entry["created_at"])

    def launch(name: str, argv: list[str], cwd: Path, env: dict | None = None) -> subprocess.Popen:
        log = (LOGS / f"{name}.log").open("ab", buffering=0)
        handles.append(log)
        from scripts.qq_companion.owned_process import OwnedProcess
        owner = OwnedProcess(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                             stdout=log, stderr=subprocess.STDOUT)
        proc = owner.process
        owned[proc.pid] = owner
        children.append({"name": name, "pid": proc.pid, "created_at": owner.created_at})
        write_registry()
        return proc

    def start_service(name: str, argv: list[str], cwd: Path, port: int, timeout: int,
                      env: dict | None = None) -> None:
        restart_specs[name] = (argv, cwd, env, port, timeout)
        process = launch(name, argv, cwd, env)
        wait_port(name, port, process, timeout)

    def stop_models(stopped):
        # An HTTP cancellation can leave inference running after its caller has
        # released admission. Confirm the actual server slots have drained too.
        if any(entry["name"] == "qwen3" for entry in children):
            from urllib.request import ProxyHandler, build_opener
            opener = build_opener(ProxyHandler({}))
            deadline = time.monotonic() + 180
            while True:
                with opener.open("http://127.0.0.1:8080/slots", timeout=5) as response:
                    slots = json.loads(response.read())
                if not isinstance(slots, list):
                    raise RuntimeError("Cannot verify Qwen slots")
                if all(slot.get("is_processing") is False for slot in slots):
                    break
                if time.monotonic() > deadline or STOP_FLAG.exists():
                    raise RuntimeError("Qwen slots failed to drain")
                time.sleep(0.25)
        gpu_names = {"qwen3", "sensevoice", "gpt_sovits", "tts_wrapper"}
        for entry in list(reversed(children)):
            if entry["name"] in gpu_names:
                stopped.insert(0, entry["name"])
                stop_entry(entry)
                if psutil.pid_exists(entry["pid"]):
                    try:
                        if abs(psutil.Process(entry["pid"]).create_time() - entry["created_at"]) < 2:
                            raise RuntimeError("Managed GPU process did not exit")
                    except psutil.NoSuchProcess:
                        pass
                children.remove(entry)
        write_registry()

    def verify_gpu():
        if any(entry["name"] in {"qwen3", "sensevoice", "gpt_sovits", "tts_wrapper"} for entry in children):
            raise RuntimeError("Managed GPU model remains loaded")
        for port in (8080, 5001, 9880, 5002):
            if port_open(port):
                raise RuntimeError("GPU service port remains open")
        # Refuse unknown project model processes rather than terminating them.
        for process in psutil.process_iter(["pid", "exe", "cmdline"]):
            try:
                command = " ".join(process.info["cmdline"] or []).lower()
                executable = (process.info["exe"] or "").lower()
                belongs = str(ROOT).lower() in command or executable.startswith(str(ROOT).lower() + os.sep)
                model = any(name in command for name in ("llama-server", "runtime.gpt_sovits.api", "runtime.sensevoice.api",
                    "api_v2.py", "feiyu_rvc_worker.py", "run_rvc.py", "audio-separator"))
                if belongs and model:
                    raise RuntimeError("An unregistered project GPU model is still running")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        result = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits", "--id=0"],
                                capture_output=True, text=True, timeout=15, check=True)
        free = int(result.stdout.strip())
        if free < 12288:
            raise RuntimeError("Insufficient free VRAM for exclusive cover")
        with (STATE / "gpu_transitions.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps({"mode": "cover", "free_mib": free, "time": time.time()}) + "\n")

    def restore_models(names):
        if STOP_FLAG.exists():
            raise RuntimeError("Shutdown requested")
        for name in names:
            argv, cwd, env, port, timeout = restart_specs[name]
            start_service(name, argv, cwd, port, timeout, env)
        # HTTP health, followed by one actual text request, proves the restored
        # model can infer instead of just having opened its listening socket.
        from urllib.request import ProxyHandler, Request, build_opener
        opener = build_opener(ProxyHandler({}))
        if "qwen3" in names:
            deadline = time.monotonic() + 180
            while True:
                try:
                    with opener.open("http://127.0.0.1:8080/health", timeout=3) as response:
                        if response.status == 200:
                            break
                except Exception:
                    if time.monotonic() > deadline:
                        raise RuntimeError("Qwen restore health failed")
                    time.sleep(1)
            request = Request("http://127.0.0.1:8080/v1/chat/completions", data=json.dumps({
                "messages": [{"role": "user", "content": "Reply OK."}], "max_tokens": 1536,
                "temperature": 0, "stream": False}).encode(), headers={"Content-Type": "application/json"})
            with opener.open(request, timeout=90) as response:
                if not json.loads(response.read()).get("choices", [{}])[0].get("message", {}).get("content"):
                    raise RuntimeError("Qwen restore inference failed")
        for name, port in (("sensevoice", 5001), ("tts_wrapper", 5002)):
            if name in names:
                with opener.open(f"http://127.0.0.1:{port}/health", timeout=10) as response:
                    if response.status != 200:
                        raise RuntimeError("Audio service restore health failed")
        if "tts_wrapper" in names:
            active = json.loads((ROOT / "model_store/active/qwen3.json").read_text(encoding="utf-8-sig"))
            request = Request("http://127.0.0.1:5002/v1/audio/speech", data=json.dumps({
                "text": "嗯。", "character_id": active["character_id"], "emotion": "neutral", "format": "wav"}).encode(),
                headers={"Content-Type": "application/json"})
            with opener.open(request, timeout=150) as response:
                if not response.read(12).startswith(b"RIFF"):
                    raise RuntimeError("TTS restore inference failed")
        with (STATE / "gpu_transitions.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps({"mode": "restored", "services": names, "time": time.time()}) + "\n")

    def run_song_worker(key):
        from services.media_api.songs import SongStore
        process = launch("song_worker", [str(MODEL_PYTHON), "-B", str(ROOT / "scripts/qq_companion/run_cover_job.py"),
                                          "--job", key, "--state-dir", str(STATE)], ROOT)
        entry = children[-1]
        deadline = time.monotonic() + 1800
        try:
            while process.poll() is None:
                if STOP_FLAG.exists() or time.monotonic() > deadline or SongStore(STATE).get(key)["status"] == "canceled":
                    raise RuntimeError("Song worker canceled or timed out")
                time.sleep(0.25)
            if process.returncode:
                raise RuntimeError("Song synthesis failed; inspect local song_worker.log")
        finally:
            stop_entry(entry)
            if entry in children:
                children.remove(entry)
            write_registry()

    try:
        if not no_models:
            broker.acquire("qq_runtime")
            start_service("qwen3", ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                    str(ROOT / "scripts" / "start_qwen3_server.ps1"),
                                    "-UseActive", "-EnableSlots"], ROOT, 8080, 180)
            model_config = None if local_q4 else subprocess.run([str(MODEL_PYTHON), str(ROOT / "scripts/qq_companion/configure_model.py"), "--apply"],
                                          cwd=ROOT, check=False)
            if model_config is not None and model_config.returncode:
                raise RuntimeError("Qwen3 chat/tool probe or AstrBot provider configuration failed")
            start_service("sensevoice", [str(MODEL_PYTHON), "-m", "uvicorn", "runtime.sensevoice.api:app",
                                         "--host", "127.0.0.1", "--port", "5001"], ROOT, 5001, 30)
        if with_tts:
            start_service("gpt_sovits", ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                         str(ROOT / "scripts" / "start_gpt_sovits_upstream.ps1")], ROOT, 9880, 240)
            start_service("tts_wrapper", [str(MODEL_PYTHON), "-m", "uvicorn", "runtime.gpt_sovits.api:app",
                                          "--host", "127.0.0.1", "--port", "5002"], ROOT, 5002, 30)
        if not no_models:
            token_path = STATE / "media_token"
            if not token_path.exists():
                token_path.write_text(secrets.token_urlsafe(32), encoding="ascii")
            media_env = os.environ.copy()
            media_env["QQ_COMPANION_MEDIA_TOKEN"] = token_path.read_text(encoding="ascii").strip()
            media_env["ASTRBOT_CHAT_STATE_DIR"] = str(STATE)
            start_service("media_api", [str(MODEL_PYTHON), "-m", "uvicorn", "services.media_api.app:app",
                                        "--host", "127.0.0.1", "--port", "8765"], ROOT, 8765, 30, media_env)
            from scripts.qq_companion.cover_dispatcher import CoverDispatcher
            from services.media_api.songs import cleanup
            from scripts.qq_companion.init_stickers import initialize
            initialize(STATE / "stickers")
            cleanup(STATE)
            dispatcher = CoverDispatcher(STATE, broker=broker, stop_models=stop_models,
                restore_models=restore_models, verify_gpu=verify_gpu, run_worker=run_song_worker,
                stopping=STOP_FLAG.exists)
            dispatcher.jobs.recover()
            dispatcher.state.set_mode("ready")
        if local_q4:
            # Same production stop/VRAM/worker/restore callbacks; never launch
            # AstrBot/SnowLuma or apply an account's OneBot configuration.
            probe_env = os.environ.copy()
            probe_env["QQ_COMPANION_MEDIA_TOKEN"] = token_path.read_text(encoding="ascii").strip()
            checked = subprocess.run([str(MODEL_PYTHON), "-m", "scripts.qq_companion.probe_media", "--count", "20"],
                                     cwd=ROOT, env=probe_env, check=False)
            if checked.returncode:
                raise RuntimeError("Q4 live audio API acceptance failed")
            from services.media_api.song_source import Song
            from services.media_api.songs import SongStore
            report = json.loads((ROOT / "storage/temporary/q4-source-acceptance/report.json").read_text(encoding="utf-8"))
            song = report["downloads"][0]["song"]
            class Source:
                def search(self, query):
                    return [Song("netease", song["song_id"], song["title"], song["artists"], song["duration_seconds"], True)]
            dispatcher.source = Source()
            key = secrets.token_hex(16)
            dispatcher.jobs.submit(key, "qq:999:dm:101", {"mode": "cover", "query": song["title"], "artist": ""})
            dispatcher.step()
            # This GPU-stack probe uses its single known fixture; semantic
            # model selection is checked separately with no QQ transport.
            dispatcher.jobs.choose(key, "qq:999:dm:101", song["song_id"])
            dispatcher.step()
            if dispatcher.jobs.get(key)["status"] != "completed" or dispatcher.state.mode() != "ready":
                raise RuntimeError("Q4 real GPU unload/cover/restore acceptance failed")
            (STATE / "report.json").write_text(json.dumps({"asr_count": 20, "tts_count": 20,
                "switch_cycles": 1, "models_restored": True, "qq_connections": 0, "job_id": key}, indent=2), encoding="utf-8")
            print("Q4_STACK_PASS: ASR 20/20, TTS 20/20, actual GPU unload/whole cover/restore; QQ connections: 0.", flush=True)
            return
        astr_env = os.environ.copy()
        astr_env["ASTRBOT_CHAT_STATE_DIR"] = str(STATE)
        if not no_models:
            active = json.loads((ROOT / "model_store/active/qwen3.json").read_text(encoding="utf-8"))
            astr_env["ASTRBOT_CHAT_CHARACTER_ID"] = active["character_id"]
        astr_env["PYTHONIOENCODING"] = "utf-8"
        astr_env["PYTHONUTF8"] = "1"
        astr_env["ASTRBOT_DISABLE_METRICS"] = "1"
        start_service("astrbot", [str(ASTRBOT_PYTHON), "-m", "astrbot.cli", "run", "--port", "6185"],
                      ASTRBOT, 6199, 120, astr_env)
        snow_env = os.environ.copy()
        snow_env["PATH"] = str(SNOWLUMA) + os.pathsep + snow_env.get("PATH", "")
        start_service("snowluma", [str(SNOWLUMA / "node.exe"), "index.mjs"], SNOWLUMA, 5099, 45, snow_env)
        print("Q2 services started. NTQQ must be logged in. Ctrl+C stops managed services.", flush=True)
        while not STOP_FLAG.exists():
            if dispatcher is not None and dispatcher.state.mode() == "ready":
                dispatcher.step()
            if dispatcher is not None and time.monotonic() - last_cache_cleanup >= 3600:
                cleanup(STATE)
                last_cache_cleanup = time.monotonic()
            for entry in list(children):
                if not psutil.pid_exists(entry["pid"]):
                    stop_entry(entry)  # Also closes any surviving grandchildren.
                    name = entry["name"]
                    optional = name in ("gpt_sovits", "tts_wrapper", "sensevoice", "media_api")
                    if name not in ("astrbot", "snowluma") and not optional:
                        raise RuntimeError(f"Managed service {name} exited; see ignored logs")
                    now = time.monotonic()
                    recent = [moment for moment in restart_history.get(name, []) if now - moment < 300]
                    if len(recent) >= 3:
                        if not optional:
                            raise RuntimeError(f"{name} exited four times in five minutes; see ignored logs")
                        print(f"{name} unavailable after three restarts; text chat remains active", flush=True)
                        children.remove(entry)
                        write_registry()
                        continue
                    recent.append(now)
                    restart_history[name] = recent
                    delay = 2 ** len(recent)
                    print(f"{name} exited; restarting in {delay}s (attempt {len(recent)}/3)", flush=True)
                    children.remove(entry)
                    write_registry()
                    time.sleep(delay)
                    if STOP_FLAG.exists():
                        break
                    argv, cwd, env, port, timeout = restart_specs[name]
                    start_service(name, argv, cwd, port, timeout, env)
            time.sleep(1)
    finally:
        # Repeated Ctrl+C must not interrupt cleanup halfway through the list.
        ignore_interrupts()
        STOP_FLAG.touch()
        try:
            for entry in list(reversed(children)):
                try:
                    stop_entry(entry)
                except Exception as exc:
                    print(f"Job cleanup for {entry['name']}: {exc}", file=sys.stderr, flush=True)
            if REGISTRY.exists():
                stop_recorded()
        finally:
            broker.release()
            for handle in handles:
                handle.close()
            if not REGISTRY.exists():
                STOP_FLAG.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--no-models", action="store_true", help="Q1 protocol probe without Qwen")
    parser.add_argument("--with-tts", action="store_true", help="Also start existing GPT-SoVITS and wrapper")
    parser.add_argument("--without-tts", action="store_true", help="Explicit text-only degradation; Q4 normally starts TTS")
    parser.add_argument("--local-q4-acceptance", action="store_true", help="Model/media acceptance only; never starts QQ components")
    args = parser.parse_args()
    if args.stop:
        ignore_interrupts()
        stop_recorded()
        return 0
    if args.with_tts and args.no_models:
        parser.error("--with-tts requires models")
    if args.with_tts and args.without_tts:
        parser.error("--with-tts conflicts with --without-tts")
    try:
        if args.local_q4_acceptance and (args.no_models or args.without_tts):
            parser.error("Local Q4 acceptance requires model and TTS services")
        with shutdown_signals():
            run(args.no_models, not args.no_models and not args.without_tts, local_q4=args.local_q4_acceptance)
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        print(f"Q1 startup/runtime error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
