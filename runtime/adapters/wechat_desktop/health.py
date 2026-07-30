from __future__ import annotations

from runtime.adapters.wechat_desktop.client import WeChatDesktopClient
from runtime.adapters.wechat_desktop.config import WeChatDesktopConfig
from runtime.adapters.wechat_desktop.models import HealthStatus
from runtime.adapters.wechat_desktop.process_locator import find_wechat_processes
from runtime.adapters.wechat_desktop.windows_session import is_desktop_unlocked


REQUIRED_CONTROLS = ("chat_list", "chat_title", "message_list")


def check_wechat_desktop_health(config: WeChatDesktopConfig, client: WeChatDesktopClient) -> HealthStatus:
    details: list[str] = []
    processes = find_wechat_processes(config.client.executable_names)
    if config.client.require_unlocked_desktop and not is_desktop_unlocked():
        details.append("Windows desktop appears to be locked or not interactive.")

    window = client.locate_main_window()
    if not processes and window is None:
        details.append("Windows WeChat process was not found.")
    if window is None or not window.visible:
        if processes:
            details.append(
                "Windows WeChat process is running, but no visible UI window was found. "
                "Open WeChat from the taskbar/tray, log in, and keep the main chat window visible."
            )
        else:
            details.append("Windows WeChat main window was not found or not visible.")

    try:
        snapshot = client.snapshot()
    except Exception as exc:
        return HealthStatus("degraded", tuple([*details, f"Could not read WeChat UI snapshot: {exc}"]))

    if window is not None and config.client.require_logged_in and not snapshot.logged_in:
        details.append("Windows WeChat is not logged in.")
    missing_controls = [
        name for name in REQUIRED_CONTROLS if window is not None and not snapshot.controls.get(name, False)
    ]
    if missing_controls:
        details.append("Missing required WeChat UI control(s): " + ", ".join(missing_controls))
    if snapshot.blocking_dialogs:
        details.append("Blocking WeChat dialog(s): " + ", ".join(snapshot.blocking_dialogs))
    if (
        config.client.verified_version != "set-after-validation"
        and snapshot.version
        and snapshot.version != config.client.verified_version
    ):
        details.append(
            f"WeChat version mismatch: expected {config.client.verified_version}, got {snapshot.version}."
        )
    if details:
        return HealthStatus("degraded", tuple(details))
    return HealthStatus("ok", ("Windows WeChat UI is ready.",))
