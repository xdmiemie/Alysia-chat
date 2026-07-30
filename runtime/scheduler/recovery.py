from __future__ import annotations

import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceCheckResult:
    name: str
    ok: bool
    detail: str = ""
    restarted: bool = False
    restart_blocked: bool = False


@dataclass
class RestartPolicy:
    max_attempts: int = 2
    window_seconds: float = 300.0
    cooldown_seconds: float = 10.0


class ManagedService:
    def __init__(
        self,
        *,
        name: str,
        health_check: Callable[[], ServiceCheckResult],
        restart: Callable[[], None],
        policy: RestartPolicy | None = None,
    ) -> None:
        self.name = name
        self.health_check = health_check
        self.restart = restart
        self.policy = policy or RestartPolicy()
        self._restart_times: deque[float] = deque()

    def ensure_healthy(self) -> ServiceCheckResult:
        current = self.health_check()
        if current.ok:
            return current
        if not self._restart_allowed():
            return ServiceCheckResult(
                name=self.name,
                ok=False,
                detail=current.detail,
                restart_blocked=True,
            )
        self.restart()
        self._restart_times.append(time.monotonic())
        after = self.health_check()
        return ServiceCheckResult(
            name=self.name,
            ok=after.ok,
            detail=after.detail,
            restarted=True,
        )

    def _restart_allowed(self) -> bool:
        now = time.monotonic()
        while self._restart_times and now - self._restart_times[0] > self.policy.window_seconds:
            self._restart_times.popleft()
        if len(self._restart_times) >= self.policy.max_attempts:
            return False
        if self._restart_times and now - self._restart_times[-1] < self.policy.cooldown_seconds:
            return False
        return True


def check_http_health(name: str, url: str, *, timeout_seconds: float = 2.0) -> ServiceCheckResult:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            if 200 <= response.status < 300:
                return ServiceCheckResult(name=name, ok=True, detail=f"HTTP {response.status}")
            return ServiceCheckResult(name=name, ok=False, detail=f"HTTP {response.status}")
    except (OSError, urllib.error.URLError) as exc:
        return ServiceCheckResult(name=name, ok=False, detail=str(exc))
