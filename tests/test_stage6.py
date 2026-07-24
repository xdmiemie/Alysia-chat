from __future__ import annotations

from array import array
from dataclasses import dataclass
from pathlib import Path

from runtime.app.orchestrator import RealtimeDialogueRunner
from runtime.audio.audio_format import WaveMetrics, inspect_wav, write_pcm16_wav
from runtime.audio.vad_recorder import EnergyVad, UtteranceSegmenter, VadConfig
from runtime.gpt_sovits.types import ReferenceAudio, SpeechRequest, SpeechResult
from runtime.qwen3.types import CharacterReply
from runtime.sensevoice.types import TranscriptionResult


def _frame(value: int, samples: int = 480) -> bytes:
    return array("h", [value] * samples).tobytes()


def test_stage6_vad_segments_speech_with_pre_roll_and_silence_end():
    config = VadConfig(speech_start_frames=3, silence_end_ms=90, min_speech_ms=300)
    segmenter = UtteranceSegmenter(config, EnergyVad(config))
    frames = [_frame(0)] * 5 + [_frame(2200)] * 10 + [_frame(0)] * 3
    utterance = None

    for index, frame in enumerate(frames):
        utterance = segmenter.feed(frame, index)
        if utterance is not None:
            break

    assert utterance is not None
    assert utterance.discarded is False
    assert len(utterance.pcm_bytes) >= config.min_speech_frames * config.frame_bytes
    assert [event.event for event in utterance.events] == ["speech_start", "speech_end"]
    assert utterance.events[-1].reason == "silence"


def test_stage6_vad_discards_short_noise():
    config = VadConfig(speech_start_frames=3, silence_end_ms=90, min_speech_ms=300)
    segmenter = UtteranceSegmenter(config, EnergyVad(config))
    frames = [_frame(0)] * 5 + [_frame(2200)] * 3 + [_frame(0)] * 3
    utterance = None

    for index, frame in enumerate(frames):
        utterance = segmenter.feed(frame, index)
        if utterance is not None:
            break

    assert utterance is not None
    assert utterance.discarded is True
    assert utterance.pcm_bytes == b""
    assert utterance.reason == "too_short"


@dataclass(frozen=True)
class FakeRecording:
    wav_path: Path
    metrics: WaveMetrics


class FakeRecorder:
    def __init__(self, wav_path: Path) -> None:
        self.wav_path = wav_path
        self.calls: list[Path] = []

    def listen_once(self, recordings_dir):
        self.calls.append(Path(recordings_dir))
        metrics = inspect_wav(self.wav_path)
        return FakeRecording(wav_path=self.wav_path, metrics=metrics)


class FailingRecorder:
    def listen_once(self, recordings_dir):
        raise RuntimeError("microphone unavailable")


class FakeTranscriber:
    def transcribe(self, audio_path):
        return TranscriptionResult(
            text="hello",
            language="en",
            emotion="neutral",
            event="speech",
            duration_ms=500,
            inference_ms=7,
        )


class FakeChatSession:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def chat(self, user_text: str) -> CharacterReply:
        self.calls.append(user_text)
        return CharacterReply("hi there", "gentle", 0.6, 1.0)


class FakeSynthesizer:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.calls: list[SpeechRequest] = []

    def synthesize(self, request: SpeechRequest) -> SpeechResult:
        self.calls.append(request)
        wav_path = self.tmp_path / "reply-source.wav"
        write_pcm16_wav(wav_path, array("h", [0, 900, -900, 300] * 400).tobytes(), 16000, 1)
        return SpeechResult(
            audio_bytes=wav_path.read_bytes(),
            format="wav",
            sample_rate=16000,
            reference_audio=ReferenceAudio("gentle", wav_path).path,
            normalized_text=request.text,
            backend_ms=11,
        )


class FakeSpeaker:
    def __init__(self) -> None:
        self.played: list[Path] = []

    def play(self, audio_path):
        self.played.append(Path(audio_path))


def test_stage6_realtime_runner_records_transcribes_synthesizes_and_plays(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    input_wav = tmp_path / "input.wav"
    write_pcm16_wav(input_wav, array("h", [0, 1200, -1200, 600] * 2000).tobytes(), 16000, 1)
    chat = FakeChatSession()
    synth = FakeSynthesizer(tmp_path)
    speaker = FakeSpeaker()

    runner = RealtimeDialogueRunner(
        transcriber=FakeTranscriber(),
        chat_session=chat,
        synthesizer=synth,
        recorder=FakeRecorder(input_wav),
        speaker=speaker,
        recordings_dir=tmp_path / "recordings",
        output_dir=tmp_path / "stage6",
    )

    result = runner.run(1)[0]

    assert result.ok is True
    assert "LISTENING" in result.states
    assert "PLAYING" in result.states
    assert result.output_audio.endswith(".wav")
    assert speaker.played == [Path(result.output_audio)]
    assert chat.calls == ["hello"]
    assert synth.calls[0].text == "hi there"
    assert (tmp_path / "stage6" / "manifest.jsonl").exists()


def test_stage6_manifest_is_written_even_when_turn_fails_before_audio_output(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = RealtimeDialogueRunner(
        transcriber=FakeTranscriber(),
        chat_session=FakeChatSession(),
        synthesizer=FakeSynthesizer(tmp_path),
        recorder=FailingRecorder(),
        speaker=FakeSpeaker(),
        recordings_dir=tmp_path / "recordings",
        output_dir=tmp_path / "missing-parent" / "stage6",
    )

    result = runner.run_one(1)
    runner.append_manifest(result)

    assert result.ok is False
    assert result.error == "microphone unavailable"
    assert (tmp_path / "missing-parent" / "stage6" / "manifest.jsonl").exists()
