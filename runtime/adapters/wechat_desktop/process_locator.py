from __future__ import annotations

import csv
import subprocess
from dataclasses import dataclass
from io import StringIO
from pathlib import Path


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    name: str


def find_wechat_processes(executable_names: tuple[str, ...]) -> tuple[ProcessInfo, ...]:
    found = _find_with_psutil(executable_names)
    if found:
        return found
    return _find_with_tasklist(executable_names)


def _find_with_psutil(executable_names: tuple[str, ...]) -> tuple[ProcessInfo, ...]:
    try:
        import psutil
    except ImportError:
        return ()

    wanted = _wanted_names(executable_names)
    found: list[ProcessInfo] = []
    for process in psutil.process_iter(["pid", "name", "exe"]):
        try:
            name = str(process.info.get("name") or "")
            exe = str(process.info.get("exe") or "")
            pid = int(process.info.get("pid") or 0)
        except (psutil.Error, ValueError, TypeError):
            continue
        candidates = {name, Path(name).stem, Path(exe).name, Path(exe).stem}
        if pid > 0 and _matches_any(candidates, wanted):
            found.append(ProcessInfo(pid=pid, name=name or Path(exe).name))
    return tuple(found)


def _find_with_tasklist(executable_names: tuple[str, ...]) -> tuple[ProcessInfo, ...]:
    try:
        completed = subprocess.run(
            ["tasklist", "/FO", "CSV"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ()
    if completed.returncode != 0:
        return ()
    wanted = _wanted_names(executable_names)
    rows = csv.reader(StringIO(completed.stdout))
    found = []
    for row in rows:
        if len(row) < 2:
            continue
        name = str(row[0])
        if not _matches_any((name, Path(name).stem), wanted):
            continue
        try:
            pid = int(str(row[1]))
        except ValueError:
            pid = 0
        if pid > 0:
            found.append(ProcessInfo(pid=pid, name=name))
    return tuple(found)


def _wanted_names(executable_names: tuple[str, ...]) -> set[str]:
    wanted: set[str] = set()
    for executable in executable_names:
        name = str(executable).strip().lower()
        if not name:
            continue
        wanted.add(name)
        wanted.add(Path(name).stem.lower())
    return wanted


def _matches_any(candidates: tuple[str, ...] | set[str], wanted: set[str]) -> bool:
    return any(str(candidate).strip().lower() in wanted for candidate in candidates if candidate)
