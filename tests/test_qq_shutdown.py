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
supervisor.GPUResourceBroker = Broker
supervisor.subprocess.run = lambda *args, **kwargs: type('Result', (), {'returncode': 0})()
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
