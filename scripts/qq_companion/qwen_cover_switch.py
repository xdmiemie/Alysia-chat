"""Release only Qwen for RVC while the managed audio services remain resident."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time
from urllib.request import ProxyHandler, Request, build_opener

import psutil


RETAINED_AUDIO_SERVICES = frozenset({"sensevoice", "gpt_sovits", "tts_wrapper"})


class QwenCoverSwitch:
    def __init__(self, *, children, stop_entry, start_service, restart_specs, write_registry,
                 root, directory, stopping, port_open):
        self.children, self.stop_entry, self.start_service = children, stop_entry, start_service
        self.restart_specs, self.write_registry = restart_specs, write_registry
        self.root, self.directory = Path(root), Path(directory)
        self.stopping, self.port_open = stopping, port_open

    @staticmethod
    def _same_process(entry):
        try:
            return abs(psutil.Process(entry["pid"]).create_time() - entry["created_at"]) < 2
        except psutil.NoSuchProcess:
            return False

    def stop(self, stopped):
        entries = [entry for entry in self.children if entry["name"] == "qwen3"]
        if entries:
            # Cancellation may outlive its admission lease; wait for actual slots.
            opener = build_opener(ProxyHandler({}))
            deadline = time.monotonic() + 180
            while True:
                with opener.open("http://127.0.0.1:8080/slots", timeout=5) as response:
                    slots = json.load(response)
                if not isinstance(slots, list):
                    raise RuntimeError("Cannot verify Qwen slots")
                if all(slot.get("is_processing") is False for slot in slots):
                    break
                if time.monotonic() > deadline or self.stopping():
                    raise RuntimeError("Qwen slots failed to drain")
                time.sleep(0.25)
        for entry in reversed(entries):
            stopped.insert(0, "qwen3")  # Restore even after a partial stop failure.
            self.stop_entry(entry)
            if self._same_process(entry):
                raise RuntimeError("Managed Qwen process did not exit")
            self.children.remove(entry)
        self.write_registry()

    def _retained_process_ids(self):
        allowed = set()
        for entry in self.children:
            if entry["name"] not in RETAINED_AUDIO_SERVICES or not self._same_process(entry):
                continue
            try:
                process = psutil.Process(entry["pid"])
                allowed.add(process.pid)
                allowed.update(child.pid for child in process.children(recursive=True))
            except psutil.NoSuchProcess:
                continue
        return allowed

    def verify(self):
        if any(entry["name"] == "qwen3" for entry in self.children):
            raise RuntimeError("Managed Qwen remains loaded")
        if self.port_open(8080):
            raise RuntimeError("Qwen service port remains open")
        retained = self._retained_process_ids()
        for process in psutil.process_iter(["pid", "exe", "cmdline"]):
            try:
                if process.info["pid"] in retained:
                    continue
                command = " ".join(process.info["cmdline"] or []).lower()
                executable = (process.info["exe"] or "").lower()
                belongs = str(self.root).lower() in command or executable.startswith(str(self.root).lower() + os.sep)
                model = any(name in command for name in ("llama-server", "runtime.gpt_sovits.api",
                    "runtime.sensevoice.api", "api_v2.py", "feiyu_rvc_worker.py", "run_rvc.py", "audio-separator"))
                if belongs and model:
                    raise RuntimeError("An unregistered project GPU model is still running")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        result = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits", "--id=0"],
                                capture_output=True, text=True, timeout=15, check=True)
        free = int(result.stdout.strip())
        if free < 12288:
            raise RuntimeError("Insufficient free VRAM for cover with resident audio services")
        self._record("cover", free_mib=free, unloaded_services=["qwen3"])

    def restore(self, names):
        if self.stopping():
            raise RuntimeError("Shutdown requested")
        if set(names) - {"qwen3"}:
            raise ValueError("Cover restoration only manages Qwen")
        if "qwen3" in names:
            # A failed stop may have left Qwen alive; never spawn a duplicate.
            entries = [entry for entry in self.children if entry["name"] == "qwen3"]
            if not any(self._same_process(entry) for entry in entries):
                for entry in entries:
                    self.stop_entry(entry)
                    self.children.remove(entry)
                argv, cwd, env, port, timeout = self.restart_specs["qwen3"]
                self.start_service("qwen3", argv, cwd, port, timeout, env)
            opener = build_opener(ProxyHandler({}))
            deadline = time.monotonic() + 180
            while True:
                if self.stopping():
                    raise RuntimeError("Shutdown requested")
                try:
                    with opener.open("http://127.0.0.1:8080/health", timeout=3) as response:
                        if response.status == 200:
                            break
                except Exception:
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError("Qwen restore health failed")
                time.sleep(1)
            request = Request("http://127.0.0.1:8080/v1/chat/completions", data=json.dumps({
                "messages": [{"role": "user", "content": "Reply OK."}], "max_tokens": 1536,
                "temperature": 0, "stream": False}).encode(), headers={"Content-Type": "application/json"})
            with opener.open(request, timeout=90) as response:
                if not json.load(response).get("choices", [{}])[0].get("message", {}).get("content"):
                    raise RuntimeError("Qwen restore inference failed")
        self._record("restored", services=names)

    def _record(self, mode, **fields):
        retained = {entry["name"]: entry["pid"] for entry in self.children
                    if entry["name"] in RETAINED_AUDIO_SERVICES}
        with (self.directory / "gpu_transitions.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps({"mode": mode, **fields, "retained_services": retained, "time": time.time()}) + "\n")
