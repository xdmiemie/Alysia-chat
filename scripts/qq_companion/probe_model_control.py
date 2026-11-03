"""Exercise real supervised Qwen parameter restart and restore original settings."""

from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
import psutil

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from integrations.qq_astrbot.plugin.foreground_activity import ForegroundActivity
from integrations.qq_astrbot.plugin.media_state import MediaState
from scripts.qq_companion.gpu_broker import GPUResourceBroker
from scripts.qq_companion.model_control import ModelControl
from scripts.qq_companion.owned_process import OwnedProcess
from scripts.qq_companion.qwen_cover_switch import QwenCoverSwitch
from runtime.qwen3.config import load_qwen3_config


def main():
    directory = ROOT / "storage/temporary/web-model-restart"
    directory.mkdir(parents=True, exist_ok=True)
    path = ROOT / "configs/runtime/qwen3.yaml"
    original = path.read_bytes()
    initial = load_qwen3_config(path)
    children, owners, handles, specs = [], {}, [], {}

    def port_open(port):
        with socket.socket() as s:
            return s.connect_ex(("127.0.0.1", port)) == 0

    if port_open(8080):
        raise RuntimeError("Stop the managed model before this isolated probe")

    def start_service(name, argv, cwd, port, timeout, env=None):
        log = (directory / "model.log").open("ab")
        handles.append(log)
        owner = OwnedProcess(
            argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log
        )
        owners[owner.process.pid] = owner
        children.append(
            {"name": name, "pid": owner.process.pid, "created_at": owner.created_at}
        )
        specs[name] = (argv, cwd, env, port, timeout)
        deadline = time.monotonic() + timeout
        with httpx.Client(trust_env=False, timeout=3) as client:
            while True:
                try:
                    if client.get("http://127.0.0.1:8080/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if owner.process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Model startup failed")
                time.sleep(0.5)
        return owner.process

    def stop_entry(entry):
        owners[entry["pid"]].stop()

    switch = QwenCoverSwitch(
        children=children,
        stop_entry=stop_entry,
        start_service=start_service,
        restart_specs=specs,
        write_registry=lambda: None,
        root=ROOT,
        directory=directory,
        stopping=lambda: False,
        port_open=port_open,
    )
    broker = GPUResourceBroker()
    broker.acquire("web_model_acceptance")
    try:
        start_service(
            "qwen3",
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "scripts/start_qwen3_server.ps1"),
                "-UseActive",
                "-EnableSlots",
            ],
            ROOT,
            8080,
            180,
        )
        control, media, foreground = (
            ModelControl(ROOT, directory),
            MediaState(directory),
            ForegroundActivity(directory),
        )
        media.set_mode("ready")
        pids, jobs = [children[0]["pid"]], []
        for temperature in [
            0.65 if initial.temperature != 0.65 else 0.7,
            initial.temperature,
        ]:
            key = control.submit({"temperature": temperature})
            control.step(switch, media, foreground)
            with control.store.connection() as db:
                row = dict(
                    db.execute(
                        "SELECT id,state,error FROM model_jobs WHERE id=?", (key,)
                    ).fetchone()
                )
            assert row["state"] == "completed", row
            assert load_qwen3_config(path).temperature == temperature
            pids.append(children[0]["pid"])
            assert pids[-1] != pids[-2] and not psutil.pid_exists(pids[-2])
            jobs.append(row)
            print(
                "MODEL_CONTROL_RESTART_PASS: configuration applied; old owned process stopped; new model healthy",
                flush=True,
            )
        path.write_bytes(original)
        (directory / "report.json").write_text(
            json.dumps(
                {"jobs": jobs, "processes": pids, "restored_original": True}, indent=2
            ),
            encoding="utf-8",
        )
        if "--vision" in sys.argv:
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/qq_companion/probe_q7_model.py"),
                    "--use-running",
                    "--enable-passed",
                    "--output",
                    str(ROOT / "storage/temporary/web-q7-regression"),
                ],
                cwd=ROOT,
                check=True,
            )
    finally:
        path.write_bytes(original)
        for owner in owners.values():
            owner.stop()
        for handle in handles:
            handle.close()
        broker.release()


if __name__ == "__main__":
    main()
