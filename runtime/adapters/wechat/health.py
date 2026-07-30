from __future__ import annotations

from pathlib import Path

from runtime.adapters.wechat.config import WeChatConfig
from runtime.adapters.wechat.ilink.credentials import load_credentials
from runtime.adapters.wechat.models import HealthStatus


def check_wechat_health(config: WeChatConfig) -> HealthStatus:
    if config.provider != "ilink":
        return HealthStatus("error", f"unsupported provider: {config.provider}")
    try:
        credentials = load_credentials(config.ilink.credentials_path)
    except FileNotFoundError:
        return HealthStatus("needs_login", f"credentials missing: {config.ilink.credentials_path}")
    except Exception as exc:
        return HealthStatus("error", str(exc))

    cursor_parent = Path(config.ilink.cursor_path).parent
    try:
        cursor_parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return HealthStatus("error", f"cursor directory is not writable: {exc}")

    credentials.validate()
    return HealthStatus("ok", credentials.base_url)
