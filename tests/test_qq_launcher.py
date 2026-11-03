from __future__ import annotations

import json
from types import SimpleNamespace as NS

import psutil
import pytest

from scripts.qq_companion import open_app


@pytest.fixture(autouse=True)
def browser_available(monkeypatch):
    monkeypatch.setattr(open_app, "find_browser", lambda: None)


def test_reuses_running_supervisor_and_opens_console(monkeypatch, tmp_path):
    monkeypatch.setattr(open_app, "STATE", tmp_path)
    process = NS(is_running=lambda: True)
    monkeypatch.setattr(open_app, "existing_supervisor", lambda: process)
    monkeypatch.setattr(open_app, "web_ready", lambda: True)
    monkeypatch.setattr(open_app, "supports_desktop", lambda _: True)
    monkeypatch.setattr(
        open_app, "start_supervisor", lambda: pytest.fail("duplicate start")
    )
    opened = []
    monkeypatch.setattr(open_app, "open_desktop", opened.append)
    assert open_app.main() == 0
    assert opened == [process]


def test_launch_failure_does_not_open_browser(monkeypatch, tmp_path):
    monkeypatch.setattr(open_app, "STATE", tmp_path)
    monkeypatch.setattr(open_app, "existing_supervisor", lambda: None)
    monkeypatch.setattr(
        open_app, "start_supervisor", lambda: NS(is_running=lambda: False)
    )
    monkeypatch.setattr(
        open_app, "open_desktop", lambda _: pytest.fail("opened failed app")
    )
    assert open_app.main() == 1


def test_timeout_does_not_claim_ready(monkeypatch):
    monkeypatch.setattr(open_app, "web_ready", lambda: False)
    with pytest.raises(RuntimeError, match="超时"):
        open_app.wait_for_web(NS(is_running=lambda: True), timeout=0)


def test_launch_lock_prevents_concurrent_start_and_releases(tmp_path, monkeypatch):
    monkeypatch.setattr(open_app, "STATE", tmp_path)
    with open_app.launch_lock():
        with pytest.raises(RuntimeError, match="超时"):
            with open_app.launch_lock(timeout=0):
                pytest.fail("second launcher acquired lock")
    with open_app.launch_lock(timeout=0):
        pass


def test_registry_pid_reuse_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(open_app, "STATE", tmp_path)
    (tmp_path / "supervisor.json").write_text(
        json.dumps({"supervisor_pid": 123, "supervisor_created_at": 1}),
        encoding="utf-8",
    )
    monkeypatch.setattr(open_app.psutil, "Process", lambda _: NS(create_time=lambda: 2))
    monkeypatch.setattr(open_app.psutil, "process_iter", lambda _: [])
    assert open_app.existing_supervisor() is None


def test_unregistered_starting_supervisor_is_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(open_app, "STATE", tmp_path)
    process = NS(
        info={"name": "python.exe"},
        is_running=lambda: True,
        cmdline=lambda: ["python.exe", "-X", "utf8", str(open_app.SUPERVISOR)],
    )
    monkeypatch.setattr(open_app.psutil, "process_iter", lambda _: [process])
    assert open_app.existing_supervisor() is process
    process.cmdline = lambda: ["python.exe", str(tmp_path / "supervisor.py")]
    assert open_app.existing_supervisor() is None


def test_starts_hidden_with_douyin_and_handles_paths_with_spaces(tmp_path, monkeypatch):
    root = tmp_path / "astrbot chat 中文"
    monkeypatch.setattr(open_app, "ROOT", root)
    monkeypatch.setattr(open_app, "STATE", root / "storage/qq_companion")
    monkeypatch.setattr(
        open_app, "SUPERVISOR", root / "scripts/qq_companion/supervisor.py"
    )
    observed = {}

    def launch(argv, **kwargs):
        observed.update(argv=argv, **kwargs)
        return NS(pid=psutil.Process().pid)

    monkeypatch.setattr(open_app.subprocess, "Popen", launch)
    assert open_app.start_supervisor().pid == psutil.Process().pid
    assert observed["argv"][0] == str(root / "runtime_env/python.exe")
    assert observed["argv"][-1] == str(open_app.SUPERVISOR)
    assert observed["cwd"] == root
    assert observed["env"]["QQ_COMPANION_DOUYIN_WORKER"] == "1"
    assert observed["creationflags"] & open_app.subprocess.CREATE_NO_WINDOW
    assert observed["creationflags"] & open_app.subprocess.CREATE_NEW_PROCESS_GROUP


def test_legacy_supervisor_restarts_before_opening_managed_window(monkeypatch, tmp_path):
    monkeypatch.setattr(open_app, "STATE", tmp_path)
    old, new = object(), object()
    calls = []
    monkeypatch.setattr(open_app, "existing_supervisor", lambda: old)
    monkeypatch.setattr(open_app, "wait_for_web", lambda process: calls.append(("wait", process)))
    monkeypatch.setattr(open_app, "supports_desktop", lambda _: False)
    monkeypatch.setattr(open_app, "stop_supervisor", lambda process: calls.append(("stop", process)))
    monkeypatch.setattr(open_app, "start_supervisor", lambda: new)
    monkeypatch.setattr(open_app, "open_desktop", lambda process: calls.append(("open", process)))
    assert open_app.main() == 0
    assert calls == [("wait", old), ("stop", old), ("wait", new), ("open", new)]


def test_failed_window_cleans_up_newly_started_stack(monkeypatch, tmp_path):
    monkeypatch.setattr(open_app, "STATE", tmp_path)
    process = object()
    stopped = []
    monkeypatch.setattr(open_app, "existing_supervisor", lambda: None)
    monkeypatch.setattr(open_app, "start_supervisor", lambda: process)
    monkeypatch.setattr(open_app, "wait_for_web", lambda _: None)
    monkeypatch.setattr(open_app, "stop_supervisor", stopped.append)

    def fail(_):
        raise RuntimeError("browser failed")

    monkeypatch.setattr(open_app, "open_desktop", fail)
    assert open_app.main() == 1
    assert stopped == [process]


def test_missing_browser_does_not_start_models(monkeypatch):
    def fail():
        raise RuntimeError("browser missing")

    monkeypatch.setattr(open_app, "find_browser", fail)
    monkeypatch.setattr(open_app, "start_supervisor", lambda: pytest.fail("started models"))
    assert open_app.main() == 1


def test_open_desktop_rejects_stale_acknowledgement(monkeypatch, tmp_path):
    monkeypatch.setattr(open_app, "STATE", tmp_path)
    process = NS(pid=123, create_time=lambda: 1, is_running=lambda: True)
    open_app.write_json(tmp_path / "desktop.response.json", {
        "supervisor_pid": 123, "supervisor_created_at": 1,
        "request_id": "previous-launch", "status": "ready",
    })
    with pytest.raises(RuntimeError, match="超时"):
        open_app.open_desktop(process, timeout=0)


def test_open_desktop_waits_for_matching_window_acknowledgement(monkeypatch, tmp_path):
    monkeypatch.setattr(open_app, "STATE", tmp_path)
    process = NS(pid=123, create_time=lambda: 1, is_running=lambda: True)

    def acknowledge(_):
        request = open_app.read_json(tmp_path / "desktop.request.json")
        open_app.write_json(tmp_path / "desktop.response.json", {**request, "status": "ready"})

    monkeypatch.setattr(open_app.time, "sleep", acknowledge)
    open_app.open_desktop(process)
