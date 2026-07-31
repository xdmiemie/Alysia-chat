from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from runtime.adapters.wechat_desktop.config import WeChatDesktopConfig
from runtime.adapters.wechat_desktop.models import HealthStatus


HealthProvider = Callable[[], HealthStatus]
SensitiveScreenProvider = Callable[[], bool]


@dataclass(frozen=True)
class CallHealthStatus:
    ok: bool
    details: tuple[str, ...] = ()

    @property
    def state(self) -> str:
        return "ok" if self.ok else "degraded"


class CallHealthChecker:
    def __init__(
        self,
        *,
        config: WeChatDesktopConfig,
        desktop_health_provider: HealthProvider | None = None,
        sensevoice_health_provider: HealthProvider | None = None,
        qwen3_health_provider: HealthProvider | None = None,
        gpt_sovits_health_provider: HealthProvider | None = None,
        audio_health_provider: HealthProvider | None = None,
        sensitive_screen_provider: SensitiveScreenProvider | None = None,
    ) -> None:
        self.config = config
        self.desktop_health_provider = desktop_health_provider
        self.sensevoice_health_provider = sensevoice_health_provider
        self.qwen3_health_provider = qwen3_health_provider
        self.gpt_sovits_health_provider = gpt_sovits_health_provider
        self.audio_health_provider = audio_health_provider
        self.sensitive_screen_provider = sensitive_screen_provider

    def check(self) -> CallHealthStatus:
        details: list[str] = []
        providers = (
            ("wechat_desktop", self.desktop_health_provider),
            ("sensevoice", self.sensevoice_health_provider),
            ("qwen3", self.qwen3_health_provider),
            ("gpt_sovits", self.gpt_sovits_health_provider),
            ("audio_devices", self.audio_health_provider),
        )
        for name, provider in providers:
            if provider is None:
                continue
            try:
                status = provider()
            except Exception as exc:
                details.append(f"{name} health check failed: {exc}")
                continue
            if not status.ok:
                if status.details:
                    details.extend(f"{name}: {detail}" for detail in status.details)
                else:
                    details.append(f"{name} is not healthy")
        if self.sensitive_screen_provider is not None and self.sensitive_screen_provider():
            details.append("WeChat appears to be on a sensitive screen.")
        return CallHealthStatus(ok=not details, details=tuple(details))
