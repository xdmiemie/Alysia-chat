"""Local profile/token permissions; no credential values enter subprocesses."""
from __future__ import annotations
import getpass
import os
from pathlib import Path
import platform
import subprocess


def protect(path, *, directory=False):
    path = Path(path)
    if os.name != "nt":
        path.chmod(0o700 if directory else 0o600)
        return
    owner = os.environ.get("USERDOMAIN", platform.node()) + "\\" + getpass.getuser()
    suffix = ":(OI)(CI)F" if directory else ":F"
    result = subprocess.run(["icacls.exe", str(path), "/inheritance:r", "/grant:r",
        owner + suffix, "*S-1-5-18" + suffix, "*S-1-5-32-544" + suffix],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError("private_file_permissions_failed")
