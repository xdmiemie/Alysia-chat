"""Windows WDDM dedicated/shared GPU memory, sampled by process ID via PDH.

English counters work on localized Windows. Missing/invalid telemetry raises;
it must never be reported as zero shared memory.
"""
from __future__ import annotations

import ctypes as ct
from ctypes import wintypes as wt
import re


class _ValueUnion(ct.Union):
    _fields_ = [("large", ct.c_longlong), ("double", ct.c_double), ("long", wt.LONG), ("text", ct.c_void_p)]


class _Value(ct.Structure):
    _fields_ = [("status", wt.DWORD), ("value", _ValueUnion)]


class _Item(ct.Structure):
    _fields_ = [("name", wt.LPWSTR), ("value", _Value)]


class GPUMemoryCounters:
    def __init__(self):
        self.dll = ct.WinDLL("pdh")
        self.query = wt.HANDLE()
        signatures = {
            "PdhOpenQueryW": [wt.LPCWSTR, ct.c_size_t, ct.POINTER(wt.HANDLE)],
            "PdhAddEnglishCounterW": [wt.HANDLE, wt.LPCWSTR, ct.c_size_t, ct.POINTER(wt.HANDLE)],
            "PdhCollectQueryData": [wt.HANDLE],
            "PdhGetFormattedCounterArrayW": [wt.HANDLE, wt.DWORD, ct.POINTER(wt.DWORD), ct.POINTER(wt.DWORD), ct.c_void_p],
            "PdhCloseQuery": [wt.HANDLE],
        }
        for name, args in signatures.items():
            function = getattr(self.dll, name)
            function.argtypes, function.restype = args, wt.DWORD
        self._check(self.dll.PdhOpenQueryW(None, 0, ct.byref(self.query)))
        self.handles = {}
        try:
            for key, name in (("dedicated_bytes", "Dedicated Usage"), ("shared_bytes", "Shared Usage")):
                handle = wt.HANDLE()
                self._check(self.dll.PdhAddEnglishCounterW(self.query,
                    "\\GPU Process Memory(*)\\" + name, 0, ct.byref(handle)))
                self.handles[key] = handle
            self._check(self.dll.PdhCollectQueryData(self.query))
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _check(status):
        if status:
            raise OSError(f"GPU PDH counter unavailable: 0x{status:08x}")

    def sample(self):
        self._check(self.dll.PdhCollectQueryData(self.query))
        result = {}
        for key, handle in self.handles.items():
            size, count = wt.DWORD(), wt.DWORD()
            status = self.dll.PdhGetFormattedCounterArrayW(handle, 0x400, ct.byref(size), ct.byref(count), None)
            if status != 0x800007D2:
                self._check(status)
            buffer = ct.create_string_buffer(size.value)
            self._check(self.dll.PdhGetFormattedCounterArrayW(handle, 0x400, ct.byref(size), ct.byref(count), buffer))
            items = ct.cast(buffer, ct.POINTER(_Item))
            for index in range(count.value):
                item = items[index]
                match = re.match(r"pid_(\d+)_", item.name)
                if not match:
                    continue
                if item.value.status not in {0, 1}:
                    raise OSError(f"Invalid GPU counter data for {item.name}: {item.value.status}")
                process = result.setdefault(int(match[1]), {"dedicated_bytes": 0, "shared_bytes": 0})
                process[key] += item.value.value.large
        return result

    def close(self):
        if self.query:
            self.dll.PdhCloseQuery(self.query)
            self.query = wt.HANDLE()
