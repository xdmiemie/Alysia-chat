from __future__ import annotations

import io
import json
from types import SimpleNamespace

import psutil
import pytest

from scripts.qq_companion import qwen_cover_switch as module


@pytest.fixture
def stack(tmp_path, monkeypatch):
    children = [{"name": name, "pid": pid, "created_at": 100}
                for name, pid in (("qwen3", 10), ("sensevoice", 20),
                                  ("gpt_sovits", 30), ("tts_wrapper", 40), ("media_api", 50))]
    alive = {entry["pid"]: 100 for entry in children}
    alive.update({11: 100, 31: 100})  # Native Qwen and upstream TTS descendants.
    commands = {10: "powershell", 11: "llama-server", 20: "runtime.sensevoice.api",
                30: "powershell", 31: "api_v2.py", 40: "runtime.gpt_sovits.api", 50: "media_api"}
    requests, events = [], []
    slots = [[{"is_processing": False}]]

    class Process:
        def __init__(self, pid):
            if pid not in alive:
                raise psutil.NoSuchProcess(pid)
            self.pid = pid
            self.info = {"pid": pid, "exe": str(tmp_path / "python.exe"),
                         "cmdline": [str(tmp_path), commands[pid]]}

        def create_time(self):
            return alive[self.pid]

        def children(self, recursive):
            assert recursive
            return [Process(pid) for pid in {10: [11], 30: [31]}.get(self.pid, []) if pid in alive]

    monkeypatch.setattr(module, "psutil", SimpleNamespace(Process=Process,
        process_iter=lambda attrs: [Process(pid) for pid in alive],
        NoSuchProcess=psutil.NoSuchProcess, AccessDenied=psutil.AccessDenied))

    class Opener:
        def open(self, request, timeout):
            url = request if isinstance(request, str) else request.full_url
            requests.append((url, None if isinstance(request, str) else json.loads(request.data)))
            events.append(url.rsplit("/", 1)[-1])
            body = slots.pop(0) if url.endswith("/slots") else (
                {"choices": [{"message": {"content": "OK"}}]} if url.endswith("/completions") else {})
            response = io.BytesIO(json.dumps(body).encode())
            response.status = 200
            return response

    monkeypatch.setattr(module, "build_opener", lambda *args: Opener())
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="13000\n"))

    def stop(entry):
        events.append(("stop", entry["name"]))
        for process in Process(entry["pid"]).children(recursive=True):
            alive.pop(process.pid)
        alive.pop(entry["pid"])

    def start(name, argv, cwd, port, timeout, env):
        events.append(("start", name))
        assert (name, argv, port, env) == ("qwen3", ["qwen"], 8080, {"TEST": "1"})
        children.append({"name": name, "pid": 60, "created_at": 200})
        commands[60], alive[60] = "llama-server", 200

    checked_ports = []

    def port_open(port):
        checked_ports.append(port)
        return port != 8080  # Audio ports deliberately stay open.

    switch = module.QwenCoverSwitch(children=children, stop_entry=stop, start_service=start,
        restart_specs={"qwen3": (["qwen"], tmp_path, {"TEST": "1"}, 8080, 180)},
        write_registry=lambda: events.append("registry"), root=tmp_path, directory=tmp_path,
        stopping=lambda: False, port_open=port_open)
    return SimpleNamespace(switch=switch, children=children, alive=alive, commands=commands,
        requests=requests, events=events, slots=slots, checked_ports=checked_ports, directory=tmp_path)


def test_cover_cycle_only_stops_and_reloads_qwen(stack):
    retained = [dict(entry) for entry in stack.children if entry["name"] != "qwen3"]
    stopped = []
    stack.switch.stop(stopped)
    assert stopped == ["qwen3"]
    assert stack.children == retained
    assert stack.events == ["slots", ("stop", "qwen3"), "registry"]
    assert 10 not in stack.alive and 11 not in stack.alive
    stack.switch.verify()
    assert stack.checked_ports == [8080]
    stack.switch.restore(stopped)
    assert [entry for entry in stack.children if entry["name"] != "qwen3"] == retained
    assert [event for event in stack.events if isinstance(event, tuple)] == [
        ("stop", "qwen3"), ("start", "qwen3")]
    assert [url for url, _ in stack.requests] == ["http://127.0.0.1:8080/slots",
        "http://127.0.0.1:8080/health", "http://127.0.0.1:8080/v1/chat/completions"]
    records = [json.loads(line) for line in (stack.directory / "gpu_transitions.jsonl").read_text().splitlines()]
    assert records[0]["unloaded_services"] == records[1]["services"] == ["qwen3"]
    assert records[0]["retained_services"] == records[1]["retained_services"] == {
        "sensevoice": 20, "gpt_sovits": 30, "tts_wrapper": 40}


def test_busy_qwen_is_drained_before_stopping(stack):
    stack.slots.insert(0, [{"is_processing": True}])
    stack.switch.stop([])
    assert stack.events[:3] == ["slots", "slots", ("stop", "qwen3")]


@pytest.mark.parametrize("command", ["llama-server", "api_v2.py", "run_rvc.py", "audio-separator"])
def test_unregistered_model_is_not_allowed_even_while_audio_is_retained(stack, command):
    stack.switch.stop([])
    stack.alive[90], stack.commands[90] = 100, command
    with pytest.raises(RuntimeError, match="unregistered"):
        stack.switch.verify()


def test_pid_reuse_does_not_whitelist_an_unowned_audio_process(stack):
    stack.switch.stop([])
    stack.alive[20] = 200
    with pytest.raises(RuntimeError, match="unregistered"):
        stack.switch.verify()


def test_low_vram_does_not_stop_resident_audio_and_qwen_can_be_restored(stack, monkeypatch):
    stopped = []
    stack.switch.stop(stopped)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="12000\n"))
    with pytest.raises(RuntimeError, match="VRAM"):
        stack.switch.verify()
    stack.switch.restore(stopped)
    assert [event for event in stack.events if isinstance(event, tuple)] == [
        ("stop", "qwen3"), ("start", "qwen3")]


def test_qwen_port_must_close_but_audio_ports_may_remain_open(stack):
    stack.switch.stop([])
    stack.switch.port_open = lambda port: True
    with pytest.raises(RuntimeError, match="Qwen service port"):
        stack.switch.verify()


def test_partial_stop_failure_does_not_spawn_a_second_qwen(stack):
    stack.switch.stop_entry = lambda entry: None
    stopped = []
    with pytest.raises(RuntimeError, match="did not exit"):
        stack.switch.stop(stopped)
    assert stopped == ["qwen3"]
    stack.switch.restore(stopped)
    assert not any(isinstance(event, tuple) and event[0] == "start" for event in stack.events)
    assert len([entry for entry in stack.children if entry["name"] == "qwen3"]) == 1


def test_restore_rejects_audio_restart_requests(stack):
    with pytest.raises(ValueError, match="only manages Qwen"):
        stack.switch.restore(["qwen3", "sensevoice"])
    assert stack.events == []


def test_restore_non_ok_health_has_a_deadline(stack, monkeypatch):
    stack.switch.stop([])

    class Unhealthy:
        def open(self, *args, **kwargs):
            response = io.BytesIO(b"{}")
            response.status = 503
            return response

    monkeypatch.setattr(module, "build_opener", lambda *args: Unhealthy())
    ticks = iter([0, 181])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    with pytest.raises(RuntimeError, match="restore health failed"):
        stack.switch.restore(["qwen3"])


@pytest.mark.parametrize("outcome", ["completed", "failed", "canceled"])
def test_dispatcher_restores_only_qwen_after_a_cover_outcome(stack, outcome):
    from scripts.qq_companion.cover_dispatcher import CoverDispatcher
    from services.media_api.song_source import Song

    retained = [dict(entry) for entry in stack.children if entry["name"] != "qwen3"]
    key, scope = "a" * 32, "qq:999:dm:101"

    def worker(job_id):
        assert dispatcher.state.mode() == "cover"
        assert stack.children == retained
        if outcome == "failed":
            raise RuntimeError("simulated worker failure")
        if outcome == "canceled":
            dispatcher.jobs.cancel(job_id, scope)
            return
        output = dispatcher.jobs.output(job_id)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"worker output")

    dispatcher = CoverDispatcher(stack.directory, broker=SimpleNamespace(file=True),
        stop_models=stack.switch.stop, restore_models=stack.switch.restore,
        verify_gpu=stack.switch.verify, run_worker=worker,
        source=SimpleNamespace(search=lambda query: [Song("netease", 1, "song", "singer", 10, True)]))
    dispatcher.jobs.submit(key, scope, {"mode": "cover", "query": "song", "artist": ""})
    dispatcher.step()
    assert stack.events == []  # Searching/selection never switches models.
    dispatcher.jobs.choose(key, scope, 1)
    dispatcher.step()
    assert dispatcher.jobs.get(key)["status"] == outcome
    assert dispatcher.state.mode() == "ready"
    assert [entry for entry in stack.children if entry["name"] != "qwen3"] == retained
    assert [event for event in stack.events if isinstance(event, tuple)] == [
        ("stop", "qwen3"), ("start", "qwen3")]
