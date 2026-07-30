from __future__ import annotations

import asyncio
import argparse
from array import array
from pathlib import Path
from types import SimpleNamespace

from scripts.wechat_voice_capture_acceptance import run_acceptance
import runtime.adapters.wechat_desktop.client as wechat_client
from runtime.adapters.wechat_desktop.app import _reply_to_batch
from runtime.adapters.wechat_desktop.config import WeChatDesktopConfig, load_wechat_desktop_config
from runtime.adapters.wechat_desktop.message_monitor import MessageMonitor
from runtime.adapters.wechat_desktop.models import BufferedDesktopBatch, DesktopMessage, SendResult
from runtime.adapters.wechat_desktop.ui_snapshot import ChatMessageSnapshot, ChatSessionSnapshot, UISnapshot
from runtime.adapters.wechat_desktop.voice_capture import CapturedAudio, WeChatVoiceCaptureService
from runtime.audio.audio_format import inspect_wav, write_pcm16_wav
from runtime.audio.normalize import normalize_wav_for_sensevoice


class FakeVoiceClient:
    def __init__(self) -> None:
        self.current_contact = ""
        self.opened: list[str] = []
        self.played: list[str] = []
        self.messages_by_contact = {
            "alice-key": (
                ChatMessageSnapshot(
                    direction="incoming",
                    message_type="voice",
                    text="5秒",
                    runtime_id="voice-1",
                    relative_order=1,
                    time_label="10:00",
                    metadata={"fingerprint": "voice-fp-1", "voice_duration_ms": 500},
                ),
            )
        }

    def locate_main_window(self):
        return None

    def snapshot(self):
        return UISnapshot(logged_in=True)

    def list_sessions(self, limit: int):
        return (
            ChatSessionSnapshot(
                contact_key="alice-key",
                display_name="Alice",
                title="Alice",
                unread=True,
                last_text="[语音]",
            ),
        )

    def open_chat(self, contact_key: str):
        self.current_contact = contact_key
        self.opened.append(contact_key)

    def current_chat_title(self):
        return self.current_contact

    def recent_messages(self, limit: int = 8):
        return self.messages_by_contact[self.current_contact][-limit:]

    def send_text(self, text: str, *, restore_clipboard: bool = True):
        pass

    def has_own_text_at_tail(self, text: str):
        return True

    def play_voice_message(self, message: DesktopMessage):
        self.played.append(message.fingerprint)


class FakeCaptureBackend:
    def __init__(self, source_wav: Path) -> None:
        self.source_wav = source_wav
        self.triggered = False

    def capture(self, *, request_id, output_path, expected_duration_ms, timeout_seconds, trigger_playback):
        trigger_playback()
        self.triggered = True
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(self.source_wav.read_bytes())
        return CapturedAudio(path=output_path, method="application_loopback")


class FakeCore:
    def __init__(self) -> None:
        self.messages = []

    def process(self, message):
        self.messages.append(message)
        return SimpleNamespace(ok=True, text="听到了，我在。", state="COMPLETED", error="")


class FakeSender:
    def __init__(self) -> None:
        self.sent = []

    async def send_text(self, contact_key, text, request_id):
        self.sent.append((contact_key, text, request_id))
        return SendResult(ok=True, request_id=request_id, contact_key=contact_key, verified=True)


class FakeRect:
    def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom

    def mid_point(self):
        return SimpleNamespace(x=(self.left + self.right) // 2, y=(self.top + self.bottom) // 2)


class FakeVoiceUIControl:
    def __init__(self, control_type: str, text: str, rect: FakeRect, *, class_name: str = "") -> None:
        self.element_info = SimpleNamespace(control_type=control_type, name=text)
        self._text = text
        self._rect = rect
        self._class_name = class_name
        self.clicked: list[tuple[tuple[int | None, int | None], bool]] = []
        self._children: tuple[FakeVoiceUIControl, ...] = ()

    def class_name(self):
        return self._class_name

    def window_text(self):
        return self._text

    def rectangle(self):
        return self._rect

    def descendants(self, **_kwargs):
        return self._children

    def children(self):
        return self._children

    def click_input(self, **kwargs):
        self.clicked.append((kwargs.get("coords", (None, None)), bool(kwargs.get("absolute", False))))


class FakeVoiceRow(FakeVoiceUIControl):
    def __init__(self, text: str) -> None:
        super().__init__("ListItem", text, FakeRect(420, 300, 990, 353))
        self._children = (
            FakeVoiceUIControl("Button", "n", FakeRect(450, 306, 484, 340)),
            FakeVoiceUIControl("Text", text, FakeRect(505, 313, 650, 333)),
        )


class FakeVoiceMessageList(FakeVoiceUIControl):
    def __init__(self, row: FakeVoiceRow) -> None:
        super().__init__("List", "messages", FakeRect(420, 200, 990, 600))
        self._children = (row,)

    def type_keys(self, *_args, **_kwargs):
        pass


class FakeVoiceWindow(FakeVoiceUIControl):
    def __init__(self, row: FakeVoiceRow) -> None:
        super().__init__("Window", "微信", FakeRect(100, 100, 1000, 800))
        self._list = FakeVoiceMessageList(row)
        self._children = (
            FakeVoiceUIControl("Text", "n", FakeRect(450, 130, 470, 160)),
            self._list,
            FakeVoiceUIControl("Edit", "", FakeRect(420, 650, 990, 700)),
        )


def _write_wav(path: Path, *, sample_rate: int, channels: int) -> Path:
    samples = array("h")
    for index in range(sample_rate // 2):
        value = 1400 if index % 2 == 0 else -1400
        for _channel in range(channels):
            samples.append(value)
    return write_pcm16_wav(path, samples.tobytes(), sample_rate, channels)


def test_stage13_wechat_config_loads_voice_defaults():
    config = load_wechat_desktop_config("configs/runtime/wechat.yaml")

    assert config.voice.enabled is True
    assert config.voice.capture_backend == "application_loopback"
    assert config.voice.application_loopback_helper_path.as_posix().endswith(
        "tools/application_loopback_capture/application_loopback_capture.exe"
    )
    assert config.voice.normalized_dir.as_posix() == "storage/wechat/incoming/normalized"
    assert config.voice.max_capture_retries == 1


def test_stage13_normalizes_wechat_capture_to_sensevoice_wav(tmp_path):
    source = _write_wav(tmp_path / "captured.wav", sample_rate=48000, channels=2)
    output = tmp_path / "normalized.wav"

    normalized = normalize_wav_for_sensevoice(source, output)
    metrics = inspect_wav(output)

    assert normalized.method == "python"
    assert metrics.sample_rate == 16000
    assert metrics.channels == 1
    assert metrics.sample_width_bits == 16
    assert 0.45 <= metrics.duration_seconds <= 0.55


def test_stage13_manual_acceptance_accepts_existing_capture_wav(tmp_path):
    source = _write_wav(tmp_path / "manual-captured.wav", sample_rate=48000, channels=2)
    config_path = tmp_path / "wechat.yaml"
    config_path.write_text(
        "\n".join(
            [
                "voice:",
                f"  captured_dir: {tmp_path.as_posix()}/captured",
                f"  normalized_dir: {tmp_path.as_posix()}/normalized",
                f"  failed_dir: {tmp_path.as_posix()}/failed",
                "  min_capture_ms: 200",
                "  silence_peak_threshold: 0.002",
            ]
        ),
        encoding="utf-8",
    )

    result = run_acceptance(
        argparse.Namespace(
            config=str(config_path),
            sensevoice_config="configs/runtime/sensevoice.yaml",
            request_id="manual-test",
            capture_seconds=1.0,
            captured_wav=str(source),
            skip_transcribe=True,
            json=True,
        )
    )
    metrics = inspect_wav(result.normalized_path)

    assert result.ok is True
    assert result.capture_method == "provided_wav"
    assert Path(result.report_path).exists()
    assert metrics.sample_rate == 16000
    assert metrics.channels == 1


def test_stage13_monitor_accepts_bound_incoming_voice_immediately(tmp_path):
    client = FakeVoiceClient()
    config = WeChatDesktopConfig(
        log_path=tmp_path / "wechat.jsonl",
        dedup_path=tmp_path / "dedup.jsonl",
        contacts=WeChatDesktopConfig.contacts.__class__(
            default_policy="deny",
            allow_contact_keys=("alice-key",),
        ),
        monitor=WeChatDesktopConfig.monitor.__class__(buffer_seconds=10),
    )
    monitor = MessageMonitor(config=config, client=client)

    ready = monitor.poll_once()

    assert len(ready) == 1
    assert ready[0].messages[0].message_type == "voice"
    assert ready[0].messages[0].voice_duration_ms == 500
    assert "accepted_voice" in (tmp_path / "wechat.jsonl").read_text(encoding="utf-8")


def test_stage13_voice_fingerprint_ignores_playback_state(monkeypatch):
    first_row = FakeVoiceRow('[语音]3秒,未播放 3"')
    monkeypatch.setattr(wechat_client, "_open_weixin_by_visible_window", lambda: FakeVoiceWindow(first_row))
    first = wechat_client._scrape_current_chat_messages(contact_key="n", own_name="role", limit=3)[0]

    second_row = FakeVoiceRow('[语音]3秒 3"')
    monkeypatch.setattr(wechat_client, "_open_weixin_by_visible_window", lambda: FakeVoiceWindow(second_row))
    second = wechat_client._scrape_current_chat_messages(contact_key="n", own_name="role", limit=3)[0]

    assert first.message_type == "voice"
    assert first.metadata["voice_duration_ms"] == 3000
    assert first.metadata["fingerprint"] == second.metadata["fingerprint"]
    assert first.runtime_id == second.runtime_id


def test_stage13_voice_click_uses_incoming_bubble_hotspot(monkeypatch):
    row = FakeVoiceRow('[语音]3秒,未播放 3"')
    monkeypatch.setattr(wechat_client, "_open_weixin_by_visible_window", lambda: FakeVoiceWindow(row))
    message = DesktopMessage(
        account_key="role",
        contact_key="n",
        contact_display_name="n",
        chat_title="n",
        direction="incoming",
        message_type="voice",
        text='[语音]3秒,未播放 3"',
        relative_order=0,
        raw_fingerprint=wechat_client._scrape_current_chat_messages(contact_key="n", own_name="role", limit=3)[
            0
        ].metadata["fingerprint"],
    )

    assert wechat_client._click_current_chat_voice_message(message) is True

    (x, y), absolute = row.clicked[-1]
    assert absolute is True
    assert 500 <= x <= 560
    assert y == 326


def test_stage13_capture_binds_target_voice_and_outputs_unified_message(tmp_path):
    source = _write_wav(tmp_path / "source.wav", sample_rate=48000, channels=2)
    client = FakeVoiceClient()
    config = WeChatDesktopConfig.voice.__class__(
        captured_dir=tmp_path / "captured",
        normalized_dir=tmp_path / "normalized",
        failed_dir=tmp_path / "failed",
        min_capture_ms=200,
        delete_captured_after_success=False,
    )
    service = WeChatVoiceCaptureService(
        client=client,
        voice=config,
        ui=WeChatDesktopConfig.ui,
        backend=FakeCaptureBackend(source),
    )
    message = DesktopMessage(
        account_key="role",
        contact_key="alice-key",
        contact_display_name="Alice",
        chat_title="Alice",
        direction="incoming",
        message_type="voice",
        text="5秒",
        runtime_id="voice-1",
        raw_fingerprint="voice-fp-1",
        metadata={"voice_duration_ms": 500},
    )

    result = asyncio.run(service.capture(message, "req-voice"))
    unified = result.to_message(message, character_id="elysia")
    metrics = inspect_wav(unified.audio_path or "")

    assert result.ok is True
    assert client.played == ["voice-fp-1"]
    assert unified.message_type == "voice"
    assert unified.metadata["capture_method"] == "application_loopback"
    assert metrics.sample_rate == 16000
    assert metrics.channels == 1


def test_stage13_app_voice_branch_transcribes_then_sends_text_reply(tmp_path):
    source = _write_wav(tmp_path / "source.wav", sample_rate=48000, channels=2)
    client = FakeVoiceClient()
    service = WeChatVoiceCaptureService(
        client=client,
        voice=WeChatDesktopConfig.voice.__class__(
            captured_dir=tmp_path / "captured",
            normalized_dir=tmp_path / "normalized",
            failed_dir=tmp_path / "failed",
            min_capture_ms=200,
            delete_captured_after_success=False,
        ),
        ui=WeChatDesktopConfig.ui,
        backend=FakeCaptureBackend(source),
    )
    core = FakeCore()
    sender = FakeSender()
    message = DesktopMessage(
        account_key="role",
        contact_key="alice-key",
        contact_display_name="Alice",
        chat_title="Alice",
        direction="incoming",
        message_type="voice",
        text="5秒",
        runtime_id="voice-1",
        raw_fingerprint="voice-fp-1",
        metadata={"voice_duration_ms": 500},
    )
    batch = BufferedDesktopBatch(
        account_key="role",
        contact_key="alice-key",
        contact_display_name="Alice",
        chat_title="Alice",
        messages=(message,),
    )

    result = asyncio.run(
        _reply_to_batch(
            batch,
            core=core,
            sender=sender,
            voice_capture=service,
            character_id="elysia",
            log_path=tmp_path / "wechat.jsonl",
        )
    )

    assert result.ok is True
    assert core.messages[0].message_type == "voice"
    assert Path(core.messages[0].audio_path or "").exists()
    assert sender.sent[0][0:2] == ("alice-key", "听到了，我在。")
