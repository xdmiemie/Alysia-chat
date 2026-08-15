from __future__ import annotations

import asyncio
from array import array
from dataclasses import replace
from pathlib import Path

from runtime.adapters.wechat_desktop.call_audio import CallAudioInput
from runtime.adapters.wechat_desktop.call_controller import CallStateTracker
from runtime.adapters.wechat_desktop.call_models import CallContact, CallWindowSnapshot
from runtime.adapters.wechat_desktop.call_session import HalfDuplexCallSession
from runtime.adapters.wechat_desktop.app import _run_call_watcher_loop
from runtime.adapters.wechat_desktop.call_supervisor import HalfDuplexCallSupervisor
from runtime.adapters.wechat_desktop.config import WeChatDesktopConfig, load_wechat_desktop_config
from runtime.audio import devices
from runtime.audio import player
from runtime.audio.audio_format import write_pcm16_wav
from runtime.core.models import AlysiaReply, CoreTiming
from runtime.scheduler.inference_queue import SingleGPUQueue


class FakeRecorder:
    def __init__(self, source: Path | None) -> None:
        self.source = source
        self.stopped = False
        self.events: list[str] = []

    def listen_once(self, recordings_dir) -> CallAudioInput | None:
        self.events.append("capture_start")
        if self.source is None:
            self.events.append("capture_timeout")
            return None
        target = Path(recordings_dir) / "captured.wav"
        target.write_bytes(self.source.read_bytes())
        self.events.append("capture_stop")
        return CallAudioInput(target, 500)

    def stop(self) -> None:
        self.stopped = True


class FakeSpeaker:
    def __init__(self, recorder: FakeRecorder) -> None:
        self.recorder = recorder
        self.played: list[Path] = []
        self.stopped = False

    def play(self, audio_path) -> None:
        assert self.recorder.events[-1] == "capture_stop"
        self.recorder.events.append("play")
        self.played.append(Path(audio_path))

    def stop(self) -> None:
        self.stopped = True


class FakeCore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.messages = []

    def process(self, message):
        self.messages.append(message)
        output = self.root / "generated.wav"
        write_pcm16_wav(output, array("h", [0, 800, -800, 200] * 1000).tobytes(), 16000, 1)
        return AlysiaReply(
            request_id=message.request_id,
            session_id=message.session_id,
            channel=message.channel,
            ok=True,
            text="你好呀",
            message_type="voice",
            audio_path=str(output),
            state="COMPLETED",
            states=("ACCEPTED", "TRANSCRIBING", "SYNTHESIZING", "COMPLETED"),
            error_code="NONE",
            error="",
            transcription={"text": "你好"},
            reply={"reply": "你好呀"},
            timings=CoreTiming(input_audio_ms=500),
        )


def _session(tmp_path: Path, recorder: FakeRecorder, *, retain_audio: bool = True, failures: int = 3):
    config = WeChatDesktopConfig()
    calls = replace(
        config.calls,
        recordings_dir=tmp_path / "calls",
        retain_audio=retain_audio,
        listen_guard_ms=0,
        max_consecutive_failures=failures,
    )
    contact = CallContact(account_key="role", contact_key="alice", display_name="Alice")
    state = CallStateTracker()
    state.transition(call_id="call-1", state="connected", contact=contact)
    speaker = FakeSpeaker(recorder)
    core = FakeCore(tmp_path)
    session = HalfDuplexCallSession(
        call_id="call-1",
        contact=contact,
        character_id="elysia",
        core=core,
        recorder=recorder,
        speaker=speaker,
        state=state,
        calls=calls,
        scheduler=config.scheduler,
    )
    return session, state, speaker, core


def test_stage17_config_uses_process_loopback_and_current_default_output():
    config = load_wechat_desktop_config("configs/runtime/wechat.yaml")

    assert config.calls.ai_enabled is True
    assert config.voice.capture_backend == "application_loopback"
    assert config.calls.output_device is None
    assert config.calls.output_device_name == "CABLE Input (VB-Audio Virtual Cable)"
    assert config.calls.vad_pre_roll_ms == 450
    assert config.calls.retain_audio is False


def test_default_audio_selection_follows_current_os_defaults(monkeypatch):
    class Default:
        device = (2, 1)

    class FakeSoundDevice:
        default = Default()

        @staticmethod
        def query_devices():
            return [
                {"name": "first", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 1, "default_samplerate": 48000},
                {"name": "default out", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 48000},
                {"name": "default in", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": 48000},
            ]

    monkeypatch.setattr(devices, "_sounddevice", lambda: FakeSoundDevice())

    assert devices.select_input_device().index == 2
    assert devices.select_output_device().index == 1


def test_virtual_cable_output_is_selected_by_stable_name():
    rows = [
        devices.AudioDevice(4, "CABLE Input (VB-Audio Virtual Cable)", 0, 0, 16, 44100),
        devices.AudioDevice(20, "CABLE Input (VB-Audio Virtual Cable)", 2, 0, 2, 48000),
        devices.AudioDevice(21, "Headset", 2, 0, 2, 48000),
    ]

    selected = devices.select_output_device_by_name(
        "CABLE Input (VB-Audio Virtual Cable)", rows
    )

    assert selected.index == 20


def test_player_resamples_tts_to_virtual_cable_default_rate(tmp_path, monkeypatch):
    wav_path = tmp_path / "tts-32k.wav"
    source_samples = array("h", [0, 1200, -1200, 300] * 320)
    write_pcm16_wav(wav_path, source_samples.tobytes(), 32000, 1)

    class FakeSoundDevice:
        played = None

        @staticmethod
        def check_output_settings(*, device, channels, dtype, samplerate):
            assert device == 20
            assert channels == 1
            assert dtype == "int16"
            if samplerate != 48000:
                raise RuntimeError("Invalid sample rate")

        @classmethod
        def play(cls, audio, *, samplerate, device, blocking):
            cls.played = (audio, samplerate, device, blocking)

    monkeypatch.setattr(player, "_sounddevice", lambda: FakeSoundDevice)
    monkeypatch.setattr(
        player,
        "select_output_device",
        lambda _preferred: devices.AudioDevice(
            20,
            "CABLE Input (VB-Audio Virtual Cable)",
            2,
            0,
            2,
            48000,
        ),
    )

    result = player.play_wav(wav_path, output_device=20)

    assert result.source_sample_rate == 32000
    assert result.playback_sample_rate == 48000
    assert FakeSoundDevice.played[1:] == (48000, 20, True)
    assert len(FakeSoundDevice.played[0]) > len(source_samples)


def test_half_duplex_turn_captures_then_infers_then_plays(tmp_path):
    source = tmp_path / "caller.wav"
    write_pcm16_wav(source, array("h", [0, 1200, -1200, 300] * 2000).tobytes(), 16000, 1)
    recorder = FakeRecorder(source)
    session, state, speaker, core = _session(tmp_path, recorder)
    session.start()

    result = asyncio.run(session.run_turn())

    assert result.ok is True
    assert result.transcript == "你好"
    assert result.reply_text == "你好呀"
    assert recorder.events == ["capture_start", "capture_stop", "play"]
    assert len(speaker.played) == 1
    assert core.messages[0].channel == "wechat_call"
    assert core.messages[0].message_type == "voice_call_turn"
    assert core.messages[0].metadata["half_duplex"] is True
    assert state.state == "listening"
    assert state.ordinary_messages_paused is True
    assert (tmp_path / "calls" / "call-1" / "manifest.jsonl").exists()


def test_half_duplex_closes_and_releases_audio_after_repeated_silence(tmp_path):
    recorder = FakeRecorder(None)
    session, state, speaker, _core = _session(tmp_path, recorder, failures=2)
    session.start()

    first = asyncio.run(session.run_turn())
    second = asyncio.run(session.run_turn())

    assert first.state == "silence_timeout"
    assert second.state == "silence_timeout"
    assert session.closed is True
    assert recorder.stopped is True
    assert speaker.stopped is True
    assert state.state == "idle"
    assert (tmp_path / "calls" / "call-1" / "summary.json").exists()


def test_stage17_powershell_launcher_owns_only_services_it_starts():
    wrapper = Path("scripts/start_wechat_desktop.ps1").read_text(encoding="utf-8-sig")
    launcher = Path("scripts/start_app.ps1").read_text(encoding="utf-8-sig")

    assert '"-WeChatDesktop", "-EnableCalls"' in wrapper
    assert "$NoServiceManagement" in wrapper
    assert "runtime.adapters.wechat_desktop.app" in launcher
    assert "$Started +=" in launcher
    assert "} finally {" in launcher
    assert "foreach ($Entry in $Started)" in launcher
    assert "Stop-StartedProcessTree" in launcher
    assert '[string]$GPTSoVITSModel = "base-v1"' in wrapper


def test_call_session_exclusively_pauses_ordinary_message_polling(tmp_path):
    source = tmp_path / "caller.wav"
    write_pcm16_wav(source, array("h", [0, 1200, -1200, 300] * 2000).tobytes(), 16000, 1)
    session, state, _speaker, _core = _session(tmp_path, FakeRecorder(source))

    session.start()
    assert state.call_session_active is True
    assert state.ordinary_messages_paused is True

    session.close("test complete")
    assert state.call_session_active is False
    assert state.ordinary_messages_paused is False


def test_call_watcher_pauses_detector_for_entire_ai_session(tmp_path):
    class Frame:
        sequence = 1
        calls = ()
        error = ""

    class FakeDetector:
        def __init__(self):
            self.paused = False
            self.pause_count = 0
            self.resume_count = 0

        def latest(self):
            return Frame()

        def pause(self, seconds=None):
            self.paused = True
            self.pause_count += 1

        def resume(self):
            self.paused = False
            self.resume_count += 1

    class FakeWatcher:
        async def handle_detected_calls(self, calls):
            return ()

    class FakeSessionState:
        call_id = "call-1"

    class FakeSupervisor:
        active = True
        session = FakeSessionState()

        async def observe(self, calls):
            return None

        async def start(self, result):
            return None

    async def scenario():
        detector = FakeDetector()
        supervisor = FakeSupervisor()
        config = replace(WeChatDesktopConfig(), log_path=tmp_path / "events.jsonl")
        task = asyncio.create_task(
            _run_call_watcher_loop(
                FakeWatcher(),
                detector=detector,
                config=config,
                json_output=False,
                call_supervisor=supervisor,
            )
        )
        await asyncio.sleep(0.12)
        assert detector.paused is True
        assert detector.pause_count == 1
        assert detector.resume_count == 0

        supervisor.active = False
        await asyncio.sleep(0.12)
        assert detector.paused is False
        assert detector.resume_count >= 1
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())


def test_supervisor_does_not_treat_unverified_empty_window_as_hangup():
    class FakeSession:
        closed = False

        def close(self, reason):
            self.closed = True
            self.reason = reason

    supervisor = HalfDuplexCallSupervisor(
        config=WeChatDesktopConfig(),
        character_id="elysia",
        core=None,
        gpu_queue=SingleGPUQueue(),
        state=CallStateTracker(),
        missing_window_grace_seconds=0,
    )
    first_session = FakeSession()
    supervisor.session = first_session

    asyncio.run(supervisor.observe(()))

    assert first_session.closed is False

    connected = CallWindowSnapshot(
        account_key="role",
        window_handle="connected-window",
        state="connected",
    )
    asyncio.run(supervisor.observe((connected,)))
    asyncio.run(supervisor.observe(()))

    assert first_session.closed is True
    assert first_session.reason == "WeChat call window disappeared"
