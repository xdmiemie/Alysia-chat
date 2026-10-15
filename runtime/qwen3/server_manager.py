from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from threading import Event

from runtime.qwen3.model_registry import Qwen3ModelEntry
from runtime.common.http import urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SELECTION_PATH = PROJECT_ROOT / "storage" / "temporary" / "qwen3-selection.json"
PID_PATH = PROJECT_ROOT / "storage" / "temporary" / "qwen3-llama-server.pid"
START_SCRIPT = PROJECT_ROOT / "scripts" / "start_qwen3_server.ps1"


def start_qwen_server(
    model: Qwen3ModelEntry,
    *,
    base_url: str = "http://127.0.0.1:8080",
    timeout_seconds: float = 180.0,
    cancelled: Event | None = None,
    skip_activation: bool = False,
) -> None:
    """Start the project llama-server and verify its selected model."""
    props = _get_json(f"{base_url}/props", timeout=1.0)
    if props is not None:
        try:
            _verify_running_selection(model, props)
            return
        except RuntimeError:
            if not stop_qwen_server(base_url=base_url):
                raise
    if cancelled is not None and cancelled.is_set():
        raise RuntimeError("模型启动已取消")

    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(START_SCRIPT),
        "-Background",
        "-Character",
        model.character_id,
        "-ModelVersion",
        model.model_id,
    ]
    if skip_activation:
        command.append("-SkipActivation")
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            # Never pipe a launcher that creates a background process. On
            # Windows the child can retain the pipe handle, causing run() to
            # wait forever for EOF even though PowerShell itself has exited.
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=45,
            creationflags=creationflags,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        stop_qwen_server(base_url=base_url)
        raise RuntimeError(f"无法启动 Qwen3 llama-server：{exc}") from exc
    if completed.returncode != 0:
        stop_qwen_server(base_url=base_url)
        raise RuntimeError(
            "Qwen3 llama-server 启动失败，请查看 storage/logs/qwen3-llama-server.err.log"
        )

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if cancelled is not None and cancelled.is_set():
            stop_qwen_server(base_url=base_url)
            raise RuntimeError("模型启动已取消")
        props = _get_json(f"{base_url}/props", timeout=1.0)
        if props is not None:
            try:
                _verify_running_selection(model, props)
            except RuntimeError:
                stop_qwen_server(base_url=base_url)
                raise
            return
        time.sleep(0.2)
    stop_qwen_server(base_url=base_url)
    raise TimeoutError("Qwen3 llama-server 启动健康检查超时")


def qwen_server_matches(
    model: Qwen3ModelEntry,
    *,
    base_url: str = "http://127.0.0.1:8080",
) -> bool:
    props = _get_json(f"{base_url}/props", timeout=1.0)
    if props is None:
        return False
    try:
        _verify_running_selection(model, props)
    except RuntimeError:
        return False
    return True


def qwen_server_running(*, base_url: str = "http://127.0.0.1:8080") -> bool:
    return _get_json(f"{base_url}/props", timeout=0.8) is not None


def _verify_running_selection(model: Qwen3ModelEntry, props: dict[str, object]) -> None:
    running_path = Path(str(props.get("model_path") or ""))
    if not running_path.name or running_path.name.lower() != model.model_file.name.lower():
        raise RuntimeError(
            "正在运行的 Qwen3 服务不是所选模型；请先运行 scripts/stop_runtime.ps1 后重试"
        )
    selection = _load_selection()
    # A LoRA shares its base GGUF path. Prefer launch metadata for every model
    # when present; legacy base-only servers can still be identified by path.
    if selection or model.lora_adapter is not None:
        if (
            selection.get("character_id") != model.character_id
            or selection.get("model_id") != model.model_id
        ):
            raise RuntimeError(
                "正在运行的 Qwen3 版本与所选版本不一致；请先运行 scripts/stop_runtime.ps1 后重试"
            )


def _load_selection() -> dict[str, object]:
    try:
        data = json.loads(SELECTION_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def stop_qwen_server(*, base_url: str = "http://127.0.0.1:8080") -> bool:
    """Stop only a server carrying the PID metadata written by our launcher."""
    selection = _load_selection()
    try:
        selected_pid = int(selection["pid"])
        pid_file = int(PID_PATH.read_text(encoding="utf-8-sig").strip())
    except (OSError, KeyError, TypeError, ValueError):
        return False
    if selected_pid <= 0 or selected_pid != pid_file:
        return False
    try:
        os.kill(selected_pid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _get_json(f"{base_url}/props", timeout=0.4) is None:
            PID_PATH.unlink(missing_ok=True)
            SELECTION_PATH.unlink(missing_ok=True)
            return True
        time.sleep(0.15)
    return False


def _get_json(url: str, timeout: float) -> dict[str, object] | None:
    try:
        with urlopen(url, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return data if isinstance(data, dict) else None
