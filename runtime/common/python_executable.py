"""Resolve the project's unified Python for model and training subprocesses."""
from __future__ import annotations

import os
from pathlib import Path


def resolve_runtime_python(project_root: Path) -> Path:
    root = Path(project_root)
    candidates = (
        root / "runtime_env" / "python.exe",
        root / "runtime_env" / "Scripts" / "python.exe",
        root / "runtime_env" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    for name in ("ALYSIA_PYTHON", "GPT_SOVITS_PYTHON"):
        configured = os.environ.get(name, "").strip()
        if configured:
            candidate = Path(configured).expanduser()
            if candidate.is_file():
                return candidate.resolve()
            raise FileNotFoundError(f"{name} 指向不存在的 Python：{candidate}")
    raise FileNotFoundError(
        "找不到项目运行环境。请先运行 quickstart/bootstrap.ps1，"
        "或设置 ALYSIA_PYTHON / GPT_SOVITS_PYTHON。"
    )
