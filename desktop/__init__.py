"""Alysia Studio desktop application."""

import ctypes
import os
import sys
from pathlib import Path


def _prepare_qt_dlls() -> None:
    """Keep conda's ICU from shadowing the Windows ICU used by Qt wheels."""
    global _windows_icu
    if sys.platform != "win32":
        return
    conda_icu = Path(sys.prefix) / "Library" / "bin" / "icuuc.dll"
    windows_root = os.environ.get("SystemRoot")
    if not conda_icu.is_file() or not windows_root:
        return
    system_icu = Path(windows_root) / "System32" / "icuuc.dll"
    if system_icu.is_file():
        _windows_icu = ctypes.WinDLL(str(system_icu))


_windows_icu = None
_prepare_qt_dlls()

__all__ = ["__version__"]

__version__ = "0.1.0"
