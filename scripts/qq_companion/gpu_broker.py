"""Q2 cross-process GPU ownership gate for managed QQ and training jobs.

Q7 will extend this gate with drain/unload/VRAM checks. The Q2 gate holds one
Windows file lock for the entire lifetime of a managed GPU workload; it never
terminates a running training job to make room for another model.
"""

from __future__ import annotations

import json
import msvcrt
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "storage/qq_companion"


class GPUResourceBroker:
    def __init__(self) -> None:
        STATE.mkdir(parents=True, exist_ok=True)
        self.file = None

    def acquire(self, owner: str) -> None:
        if self.file is not None:
            raise RuntimeError("GPU broker is already acquired by this process")
        handle = (STATE / "gpu.lock").open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            handle.close()
            raise RuntimeError("GPU is reserved by another managed QQ or training process") from exc
        self.file = handle
        (STATE / "gpu_owner.json").write_text(json.dumps({
            "owner": owner, "pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat()
        }), encoding="utf-8")

    def release(self) -> None:
        if self.file is None:
            return
        (STATE / "gpu_owner.json").unlink(missing_ok=True)
        self.file.seek(0)
        msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        self.file.close()
        self.file = None
