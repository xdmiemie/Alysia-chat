"""Windows job ownership survives launcher exit and supervisor termination.

Only processes launched here enter the job. Handles are not inherited, so an
owner crash closes its last handle and Windows terminates the entire job tree.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import subprocess

import psutil


class BasicLimits(ctypes.Structure):
    _fields_ = [("ProcessTime", ctypes.c_int64), ("JobTime", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD), ("MinWorkingSet", ctypes.c_size_t),
        ("MaxWorkingSet", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD)]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [("Basic", BasicLimits), ("IoCounters", ctypes.c_uint64 * 6),
        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class WindowsJob:
    def __init__(self):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                            wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, pid):
        handle = self.api.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self.api.AssignProcessToJobObject(self.handle, handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.api.CloseHandle(handle)

    def processes(self):
        size = 4096
        while True:
            buffer = ctypes.create_string_buffer(size)
            if self.api.QueryInformationJobObject(self.handle, 3, buffer, size, None):
                count = wintypes.DWORD.from_buffer(buffer, 4).value
                pids = (ctypes.c_size_t * count).from_buffer(buffer, 8)
                result = []
                for pid in pids:
                    try:
                        result.append(psutil.Process(pid))
                    except psutil.NoSuchProcess:
                        pass
                return result
            if ctypes.get_last_error() != 234:  # ERROR_MORE_DATA
                raise ctypes.WinError(ctypes.get_last_error())
            size *= 2

    def close(self):
        if self.handle:
            if not self.api.CloseHandle(self.handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle = None


class OwnedProcess:
    def __init__(self, argv, **kwargs):
        self.job = WindowsJob() if os.name == "nt" else None
        self.process = None
        try:
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004 if self.job else 0  # CREATE_SUSPENDED
            self.process = subprocess.Popen(argv, creationflags=flags, **kwargs)
            self.created_at = psutil.Process(self.process.pid).create_time()
            if self.job:
                # Assign before any launcher code can create grandchildren.
                self.job.assign(self.process.pid)
                psutil.Process(self.process.pid).resume()
        except BaseException:
            if self.process is not None:
                try:
                    self.process.kill()
                    self.process.wait(timeout=5)
                except (ProcessLookupError, psutil.NoSuchProcess):
                    pass
            if self.job:
                self.job.close()
            raise

    def stop(self):
        if self.job and self.job.handle:
            try:
                processes = self.job.processes()
            finally:
                self.job.close()
            _, alive = psutil.wait_procs(processes, timeout=10)
            if alive:
                raise RuntimeError(f"Owned job processes did not exit: {[p.pid for p in alive]}")
        elif not self.job:
            from scripts.qq_companion.supervisor import stop_tree
            stop_tree(self.process.pid, self.created_at)
        self.process.wait(timeout=5)
