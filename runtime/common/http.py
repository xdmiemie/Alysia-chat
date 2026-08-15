from __future__ import annotations

import urllib.request
from urllib.parse import urlparse


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def urlopen(request: str | urllib.request.Request, *, timeout: float):
    """Open local model URLs directly while preserving proxies for remote URLs."""
    url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
    if (urlparse(url).hostname or "").lower() in _LOOPBACK_HOSTS:
        return _DIRECT_OPENER.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)
