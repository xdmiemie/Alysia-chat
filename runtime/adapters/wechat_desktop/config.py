from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime.common.config import _parse_scalar


@dataclass(frozen=True)
class AccountConfig:
    account_key: str = "role_account_main"
    dedicated_account: bool = True
    expected_display_name: str = "set-after-validation"


@dataclass(frozen=True)
class ClientConfig:
    executable_names: tuple[str, ...] = ("Weixin.exe", "WeChat.exe", "WeChatAppEx.exe", "wechat_starter_v1.0.0.exe")
    launch_if_missing: bool = False
    require_logged_in: bool = True
    require_unlocked_desktop: bool = True
    verified_version: str = "set-after-validation"
    selector_profile: str = "set-after-calibration"
    pywechat_root: Path = Path("third_party/pywechat")


@dataclass(frozen=True)
class MonitorConfig:
    mode: str = "pinned_contacts"
    poll_interval_ms: int = 500
    buffer_seconds: float = 1.5
    max_visible_sessions: int = 20
    ignore_group_chats: bool = True
    ignore_system_messages: bool = True


@dataclass(frozen=True)
class ReplyConfig:
    default_mode: str = "text"
    min_reply_interval_seconds: float = 3.0
    fallback_to_text: bool = True
    max_text_chars: int = 1000


@dataclass(frozen=True)
class VoiceConfig:
    enabled: bool = True
    capture_backend: str = "application_loopback"
    captured_dir: Path = Path("storage/wechat/incoming/captured")
    normalized_dir: Path = Path("storage/wechat/incoming/normalized")
    failed_dir: Path = Path("storage/wechat/failed")
    ffmpeg_path: Path = Path("tools/ffmpeg/bin/ffmpeg.exe")
    application_loopback_helper_path: Path = Path(
        "tools/application_loopback_capture/application_loopback_capture.exe"
    )
    loopback_input_device: int | None = None
    capture_timeout_seconds: float = 20.0
    min_capture_ms: int = 300
    duration_tolerance_ms: int = 700
    silence_peak_threshold: float = 0.002
    max_capture_retries: int = 1
    relocation_scan_limit: int = 8
    delete_captured_after_success: bool = True


@dataclass(frozen=True)
class CallsConfig:
    enabled: bool = True
    auto_answer: bool = True
    allow_video_calls: bool = False
    default_policy: str = "deny"
    poll_interval_seconds: float = 0.2
    ring_wait_ms: int = 0
    max_call_seconds: int = 1800
    busy_behavior: str = "ignore"
    model_unhealthy_behavior: str = "ignore"
    duplicate_cooldown_seconds: float = 30.0
    post_call_failure_text: str = ""
    pause_message_polling_while_enabled: bool = True
    ai_enabled: bool = True
    recordings_dir: Path = Path("storage/wechat/calls")
    retain_audio: bool = False
    output_device: int | None = None
    output_device_name: str = ""
    listen_timeout_seconds: float = 30.0
    listen_guard_ms: int = 450
    max_consecutive_failures: int = 3
    vad_frame_ms: int = 30
    vad_pre_roll_ms: int = 450
    vad_speech_start_frames: int = 4
    vad_silence_end_ms: int = 750
    vad_min_speech_ms: int = 300
    vad_max_speech_ms: int = 25_000


@dataclass(frozen=True)
class UIConfig:
    operation_timeout_seconds: float = 15.0
    verify_chat_title_before_send: bool = True
    verify_own_message_after_send: bool = True
    allow_coordinate_fallback: bool = False
    restore_window_after_operation: bool = True
    max_send_retries: int = 2


@dataclass(frozen=True)
class SchedulerConfig:
    gpu_max_pending: int = 16
    ui_max_pending: int = 32
    ui_pause_max_pending: int = 8
    total_message_seconds: float = 240.0
    call_turn_seconds: float = 180.0
    cancel_pending_same_session: bool = True
    restart_max_attempts: int = 2
    restart_window_seconds: float = 300.0
    restart_cooldown_seconds: float = 10.0


@dataclass(frozen=True)
class ContactsConfig:
    default_policy: str = "deny"
    allow_contact_keys: tuple[str, ...] = ()
    allow_display_names: tuple[str, ...] = ()
    block_contact_keys: tuple[str, ...] = ()
    call_enabled_contact_keys: tuple[str, ...] = ()
    call_enabled_display_names: tuple[str, ...] = ()
    call_disabled_contact_keys: tuple[str, ...] = ()

    def is_allowed(self, contact_key: str, display_name: str = "") -> bool:
        if contact_key in self.block_contact_keys:
            return False
        if contact_key in self.allow_contact_keys:
            return True
        if contact_key in self.allow_display_names:
            return True
        if display_name and display_name in self.allow_display_names:
            return True
        return self.default_policy == "allow"

    def is_call_enabled(self, contact_key: str, display_name: str = "") -> bool:
        if contact_key in self.call_disabled_contact_keys:
            return False
        if contact_key in self.call_enabled_contact_keys:
            return True
        if contact_key in self.call_enabled_display_names:
            return True
        if display_name and display_name in self.call_enabled_display_names:
            return True
        has_explicit_call_allowlist = bool(self.call_enabled_contact_keys or self.call_enabled_display_names)
        return not has_explicit_call_allowlist and self.is_allowed(contact_key, display_name)


@dataclass(frozen=True)
class WeChatDesktopConfig:
    enabled: bool = False
    provider: str = "pyweixin"
    log_path: Path = Path("storage/logs/wechat_desktop/text.jsonl")
    dedup_path: Path = Path("storage/wechat_desktop/state/dedup.jsonl")
    account: AccountConfig = AccountConfig()
    client: ClientConfig = ClientConfig()
    monitor: MonitorConfig = MonitorConfig()
    reply: ReplyConfig = ReplyConfig()
    voice: VoiceConfig = VoiceConfig()
    calls: CallsConfig = CallsConfig()
    ui: UIConfig = UIConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    contacts: ContactsConfig = ContactsConfig()


def load_wechat_desktop_config(path: str | Path = "configs/runtime/wechat.yaml") -> WeChatDesktopConfig:
    raw = load_nested_yaml(Path(path))
    account = raw.get("account", {})
    client = raw.get("client", {})
    monitor = raw.get("monitor", {})
    reply = raw.get("reply", {})
    voice = raw.get("voice", {})
    calls = raw.get("calls", {})
    ui = raw.get("ui", {})
    scheduler = raw.get("scheduler", {})
    contacts = raw.get("contacts", {})

    return WeChatDesktopConfig(
        enabled=bool(raw.get("enabled", WeChatDesktopConfig.enabled)),
        provider=str(raw.get("provider", WeChatDesktopConfig.provider)),
        log_path=Path(str(raw.get("log_path", WeChatDesktopConfig.log_path))),
        dedup_path=Path(str(raw.get("dedup_path", WeChatDesktopConfig.dedup_path))),
        account=AccountConfig(
            account_key=str(account.get("account_key", AccountConfig.account_key)),
            dedicated_account=bool(account.get("dedicated_account", AccountConfig.dedicated_account)),
            expected_display_name=str(account.get("expected_display_name", AccountConfig.expected_display_name)),
        ),
        client=ClientConfig(
            executable_names=_as_string_tuple(client.get("executable_names", ClientConfig.executable_names)),
            launch_if_missing=bool(client.get("launch_if_missing", ClientConfig.launch_if_missing)),
            require_logged_in=bool(client.get("require_logged_in", ClientConfig.require_logged_in)),
            require_unlocked_desktop=bool(
                client.get("require_unlocked_desktop", ClientConfig.require_unlocked_desktop)
            ),
            verified_version=str(client.get("verified_version", ClientConfig.verified_version)),
            selector_profile=str(client.get("selector_profile", ClientConfig.selector_profile)),
            pywechat_root=Path(str(client.get("pywechat_root", ClientConfig.pywechat_root))),
        ),
        monitor=MonitorConfig(
            mode=str(monitor.get("mode", MonitorConfig.mode)),
            poll_interval_ms=int(monitor.get("poll_interval_ms", MonitorConfig.poll_interval_ms)),
            buffer_seconds=float(monitor.get("buffer_seconds", MonitorConfig.buffer_seconds)),
            max_visible_sessions=int(monitor.get("max_visible_sessions", MonitorConfig.max_visible_sessions)),
            ignore_group_chats=bool(monitor.get("ignore_group_chats", MonitorConfig.ignore_group_chats)),
            ignore_system_messages=bool(monitor.get("ignore_system_messages", MonitorConfig.ignore_system_messages)),
        ),
        reply=ReplyConfig(
            default_mode=str(reply.get("default_mode", ReplyConfig.default_mode)),
            min_reply_interval_seconds=float(
                reply.get("min_reply_interval_seconds", ReplyConfig.min_reply_interval_seconds)
            ),
            fallback_to_text=bool(reply.get("fallback_to_text", ReplyConfig.fallback_to_text)),
            max_text_chars=int(reply.get("max_text_chars", ReplyConfig.max_text_chars)),
        ),
        voice=VoiceConfig(
            enabled=bool(voice.get("enabled", VoiceConfig.enabled)),
            capture_backend=str(voice.get("capture_backend", VoiceConfig.capture_backend)),
            captured_dir=Path(str(voice.get("captured_dir", VoiceConfig.captured_dir))),
            normalized_dir=Path(str(voice.get("normalized_dir", VoiceConfig.normalized_dir))),
            failed_dir=Path(str(voice.get("failed_dir", VoiceConfig.failed_dir))),
            ffmpeg_path=Path(str(voice.get("ffmpeg_path", VoiceConfig.ffmpeg_path))),
            application_loopback_helper_path=Path(
                str(
                    voice.get(
                        "application_loopback_helper_path",
                        VoiceConfig.application_loopback_helper_path,
                    )
                )
            ),
            loopback_input_device=(
                None
                if voice.get("loopback_input_device", VoiceConfig.loopback_input_device) in (None, "")
                else int(voice.get("loopback_input_device"))
            ),
            capture_timeout_seconds=float(
                voice.get("capture_timeout_seconds", VoiceConfig.capture_timeout_seconds)
            ),
            min_capture_ms=int(voice.get("min_capture_ms", VoiceConfig.min_capture_ms)),
            duration_tolerance_ms=int(
                voice.get("duration_tolerance_ms", VoiceConfig.duration_tolerance_ms)
            ),
            silence_peak_threshold=float(
                voice.get("silence_peak_threshold", VoiceConfig.silence_peak_threshold)
            ),
            max_capture_retries=int(voice.get("max_capture_retries", VoiceConfig.max_capture_retries)),
            relocation_scan_limit=int(
                voice.get("relocation_scan_limit", VoiceConfig.relocation_scan_limit)
            ),
            delete_captured_after_success=bool(
                voice.get("delete_captured_after_success", VoiceConfig.delete_captured_after_success)
            ),
        ),
        calls=CallsConfig(
            enabled=bool(calls.get("enabled", CallsConfig.enabled)),
            auto_answer=bool(calls.get("auto_answer", CallsConfig.auto_answer)),
            allow_video_calls=bool(calls.get("allow_video_calls", CallsConfig.allow_video_calls)),
            default_policy=str(calls.get("default_policy", CallsConfig.default_policy)),
            poll_interval_seconds=float(
                calls.get("poll_interval_seconds", CallsConfig.poll_interval_seconds)
            ),
            ring_wait_ms=int(calls.get("ring_wait_ms", CallsConfig.ring_wait_ms)),
            max_call_seconds=int(calls.get("max_call_seconds", CallsConfig.max_call_seconds)),
            busy_behavior=str(calls.get("busy_behavior", CallsConfig.busy_behavior)),
            model_unhealthy_behavior=str(
                calls.get("model_unhealthy_behavior", CallsConfig.model_unhealthy_behavior)
            ),
            duplicate_cooldown_seconds=float(
                calls.get("duplicate_cooldown_seconds", CallsConfig.duplicate_cooldown_seconds)
            ),
            post_call_failure_text=str(
                calls.get("post_call_failure_text", CallsConfig.post_call_failure_text)
            ),
            pause_message_polling_while_enabled=bool(
                calls.get(
                    "pause_message_polling_while_enabled",
                    CallsConfig.pause_message_polling_while_enabled,
                )
            ),
            ai_enabled=bool(calls.get("ai_enabled", CallsConfig.ai_enabled)),
            recordings_dir=Path(str(calls.get("recordings_dir", CallsConfig.recordings_dir))),
            retain_audio=bool(calls.get("retain_audio", CallsConfig.retain_audio)),
            output_device=(
                None
                if calls.get("output_device", CallsConfig.output_device) in (None, "")
                else int(calls.get("output_device"))
            ),
            output_device_name=str(
                calls.get("output_device_name", CallsConfig.output_device_name)
            ),
            listen_timeout_seconds=float(
                calls.get("listen_timeout_seconds", CallsConfig.listen_timeout_seconds)
            ),
            listen_guard_ms=int(calls.get("listen_guard_ms", CallsConfig.listen_guard_ms)),
            max_consecutive_failures=int(
                calls.get("max_consecutive_failures", CallsConfig.max_consecutive_failures)
            ),
            vad_frame_ms=int(calls.get("vad_frame_ms", CallsConfig.vad_frame_ms)),
            vad_pre_roll_ms=int(calls.get("vad_pre_roll_ms", CallsConfig.vad_pre_roll_ms)),
            vad_speech_start_frames=int(
                calls.get("vad_speech_start_frames", CallsConfig.vad_speech_start_frames)
            ),
            vad_silence_end_ms=int(
                calls.get("vad_silence_end_ms", CallsConfig.vad_silence_end_ms)
            ),
            vad_min_speech_ms=int(
                calls.get("vad_min_speech_ms", CallsConfig.vad_min_speech_ms)
            ),
            vad_max_speech_ms=int(
                calls.get("vad_max_speech_ms", CallsConfig.vad_max_speech_ms)
            ),
        ),
        ui=UIConfig(
            operation_timeout_seconds=float(
                ui.get("operation_timeout_seconds", UIConfig.operation_timeout_seconds)
            ),
            verify_chat_title_before_send=bool(
                ui.get("verify_chat_title_before_send", UIConfig.verify_chat_title_before_send)
            ),
            verify_own_message_after_send=bool(
                ui.get("verify_own_message_after_send", UIConfig.verify_own_message_after_send)
            ),
            allow_coordinate_fallback=bool(ui.get("allow_coordinate_fallback", UIConfig.allow_coordinate_fallback)),
            restore_window_after_operation=bool(
                ui.get("restore_window_after_operation", UIConfig.restore_window_after_operation)
            ),
            max_send_retries=int(ui.get("max_send_retries", UIConfig.max_send_retries)),
        ),
        scheduler=SchedulerConfig(
            gpu_max_pending=int(scheduler.get("gpu_max_pending", SchedulerConfig.gpu_max_pending)),
            ui_max_pending=int(scheduler.get("ui_max_pending", SchedulerConfig.ui_max_pending)),
            ui_pause_max_pending=int(
                scheduler.get("ui_pause_max_pending", SchedulerConfig.ui_pause_max_pending)
            ),
            total_message_seconds=float(
                scheduler.get("total_message_seconds", SchedulerConfig.total_message_seconds)
            ),
            call_turn_seconds=float(scheduler.get("call_turn_seconds", SchedulerConfig.call_turn_seconds)),
            cancel_pending_same_session=bool(
                scheduler.get("cancel_pending_same_session", SchedulerConfig.cancel_pending_same_session)
            ),
            restart_max_attempts=int(
                scheduler.get("restart_max_attempts", SchedulerConfig.restart_max_attempts)
            ),
            restart_window_seconds=float(
                scheduler.get("restart_window_seconds", SchedulerConfig.restart_window_seconds)
            ),
            restart_cooldown_seconds=float(
                scheduler.get("restart_cooldown_seconds", SchedulerConfig.restart_cooldown_seconds)
            ),
        ),
        contacts=ContactsConfig(
            default_policy=str(contacts.get("default_policy", ContactsConfig.default_policy)),
            allow_contact_keys=_as_string_tuple(contacts.get("allow_contact_keys", ())),
            allow_display_names=_as_string_tuple(contacts.get("allow_display_names", ())),
            block_contact_keys=_as_string_tuple(contacts.get("block_contact_keys", ())),
            call_enabled_contact_keys=_as_string_tuple(contacts.get("call_enabled_contact_keys", ())),
            call_enabled_display_names=_as_string_tuple(contacts.get("call_enabled_display_names", ())),
            call_disabled_contact_keys=_as_string_tuple(contacts.get("call_disabled_contact_keys", ())),
        ),
    )


def load_nested_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    for index, raw_line in enumerate(lines):
        line_without_comment = raw_line.split("#", 1)[0].rstrip()
        if not line_without_comment.strip():
            continue
        indent = len(line_without_comment) - len(line_without_comment.lstrip(" "))
        stripped = line_without_comment.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if stripped.startswith("- "):
            if not isinstance(parent, list):
                raise ValueError(f"YAML list item without list parent: {raw_line}")
            parent.append(_parse_scalar(stripped[2:]))
            continue
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value:
            if not isinstance(parent, dict):
                raise ValueError(f"YAML mapping under non-mapping parent: {raw_line}")
            parent[key] = _parse_scalar(value)
            continue
        container: Any = {}
        if not isinstance(parent, dict):
            raise ValueError(f"YAML mapping under non-mapping parent: {raw_line}")
        parent[key] = container
        stack.append((indent, container))
        next_line = _peek_next_content(lines, index, indent)
        if next_line and next_line.strip().startswith("- "):
            parent[key] = []
            stack[-1] = (indent, parent[key])
    return root


def _peek_next_content(lines: list[str], index: int, current_indent: int) -> str:
    for next_line in lines[index + 1 :]:
        if not next_line.strip() or next_line.strip().startswith("#"):
            continue
        next_indent = len(next_line) - len(next_line.lstrip(" "))
        if next_indent > current_indent:
            return next_line
        return ""
    return ""


def _as_string_tuple(value: Any) -> tuple[str, ...]:
    if value in (None, "", "[]"):
        return ()
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    if isinstance(value, tuple):
        return tuple(str(item) for item in value)
    return (str(value),)
