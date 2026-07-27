from __future__ import annotations

from array import array
from pathlib import Path

from runtime.audio.audio_format import write_pcm16_wav
from runtime.core import AlysiaCore, UnifiedMessage
from runtime.gpt_sovits.types import ReferenceAudio, SpeechRequest, SpeechResult
from runtime.qwen3.types import CharacterReply
from runtime.sensevoice.types import TranscriptionResult


class FakeTranscriber:
    def __init__(self) -> None:
        self.calls: list[Path] = []

    def transcribe(self, audio_path):
        self.calls.append(Path(audio_path))
        return TranscriptionResult(
            text="voice text",
            language="en",
            emotion="neutral",
            event="speech",
            duration_ms=400,
            inference_ms=9,
        )


class StatefulChatSession:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.calls: list[str] = []

    def chat(self, user_text: str) -> CharacterReply:
        self.calls.append(user_text)
        return CharacterReply(
            reply=f"{self.session_id}:{len(self.calls)}:{user_text}",
            emotion="neutral",
            intensity=0.5,
            speed=1.0,
        )


class FakeSynthesizer:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.calls: list[SpeechRequest] = []

    def synthesize(self, request: SpeechRequest) -> SpeechResult:
        self.calls.append(request)
        wav_path = self.tmp_path / f"reply-{len(self.calls)}.wav"
        write_pcm16_wav(wav_path, array("h", [0, 1000, -1000, 500] * 400).tobytes(), 16000, 1)
        return SpeechResult(
            audio_bytes=wav_path.read_bytes(),
            format="wav",
            sample_rate=16000,
            reference_audio=ReferenceAudio("neutral", wav_path).path,
            normalized_text=request.text,
            backend_ms=3,
        )


def test_core_processes_text_message_and_preserves_session_context(tmp_path):
    sessions: dict[str, StatefulChatSession] = {}
    synth = FakeSynthesizer(tmp_path)
    core = AlysiaCore(
        transcriber=FakeTranscriber(),
        chat_session_factory=lambda session_id: sessions.setdefault(session_id, StatefulChatSession(session_id)),
        synthesizer=synth,
        output_dir=tmp_path / "out",
    )

    first = core.process(UnifiedMessage.local_text("hello", session_id="local:desktop:a"))
    second = core.process(UnifiedMessage.local_text("again", session_id="local:desktop:a"))
    other = core.process(UnifiedMessage.local_text("fresh", session_id="local:desktop:b"))

    assert first.ok is True
    assert second.text.startswith("local:desktop:a:2:")
    assert other.text.startswith("local:desktop:b:1:")
    assert sessions["local:desktop:a"].calls == ["hello", "again"]
    assert sessions["local:desktop:b"].calls == ["fresh"]
    assert first.request_id != second.request_id
    assert Path(first.audio_path).exists()


def test_core_processes_audio_message_through_same_pipeline(tmp_path):
    wav_path = tmp_path / "input.wav"
    write_pcm16_wav(wav_path, array("h", [0, 1200, -1200, 600] * 1000).tobytes(), 16000, 1)
    transcriber = FakeTranscriber()
    core = AlysiaCore(
        transcriber=transcriber,
        chat_session_factory=lambda session_id: StatefulChatSession(session_id),
        synthesizer=FakeSynthesizer(tmp_path),
        output_dir=tmp_path / "out",
    )

    result = core.process(UnifiedMessage.local_audio(wav_path, session_id="local:file:default"))

    assert result.ok is True
    assert result.transcription is not None
    assert result.transcription["text"] == "voice text"
    assert transcriber.calls == [wav_path]
    assert "TRANSCRIBING" in result.states
    assert "WAV_SAVED" in result.states


def test_core_package_has_no_wechat_imports():
    core_files = list((Path("runtime") / "core").glob("*.py"))
    assert core_files
    combined = "\n".join(path.read_text(encoding="utf-8") for path in core_files)

    forbidden = ["wechat", "weflow", "uia", "weemessenger"]
    assert all(term not in combined.lower() for term in forbidden)
