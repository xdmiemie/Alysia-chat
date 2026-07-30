from __future__ import annotations

import hashlib


def stable_hash(value: str, *, length: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def redact_secret(value: str, *, visible: int = 6) -> str:
    if not value:
        return ""
    if len(value) <= visible:
        return "***"
    return f"{value[:visible]}..."
