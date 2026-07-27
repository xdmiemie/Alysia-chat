from __future__ import annotations

from array import array
from pathlib import Path

from runtime.app.orchestrator import Stage5DialogueRunner
from runtime.audio.audio_format import inspect_wav, write_pcm16_wav
from runtime.gpt_sovits.types import ReferenceAudio, SpeechRequest, SpeechResult
from runtime.qwen3.types import CharacterReply
from runtime.sensevoice.types import TranscriptionResult


class FakeTranscriber:
    def __init__(self, results: list[TranscriptionResult]) -> None:
        self.results = list(results)
        self.calls: list[Path] = []

    def transcribe(self, audio_path):
        self.calls.append(Path(audio_path))
        if not self.results:
            raise AssertionError("fake transcriber had no more results")
        return self.results.pop(0)


class FakeChatSession:
    def __init__(self, replies: list[CharacterReply] | None = None, fail: bool = False) -> None:
        self.replies = list(replies or [CharacterReply("我听见啦。", "gentle", 0.6, 0.95)])
        self.fail = fail
        self.calls: list[str] = []

    def chat(self, user_text: str) -> CharacterReply:
        self.calls.append(user_text)
        if self.fail:
            raise RuntimeError("llm unavailable")
        return self.replies.pop(0)


class FakeSynthesizer:
    def __init__(self) -> None:
        self.calls: list[SpeechRequest] = []

    def synthesize(self, request: SpeechRequest) -> SpeechResult:
        self.calls.append(request)
        tmp_audio = request.text.encode("utf-8")
        samples = array("h", [0, 900, -900, 450] * max(200, len(tmp_audio)))
        wav_path = Path("unused.wav")
        temp = Path.cwd() / ".pytest_cache" / "stage5-fake.wav"
        write_pcm16_wav(temp, samples.tobytes(), sample_rate=16000, channels=1)
        return SpeechResult(
            audio_bytes=temp.read_bytes(),
            format="wav",
            sample_rate=16000,
            reference_audio=ReferenceAudio("gentle", wav_path).path,
            normalized_text=request.text,
            backend_ms=5,
        )


def _write_input(path: Path) -> Path:
    return write_pcm16_wav(path, array("h", [0, 1200, -1200, 600] * 2000).tobytes(), 16000, 1)


def _speech_result(text: str = "你好") -> TranscriptionResult:
    return TranscriptionResult(
        text=text,
        language="zh",
        emotion="neutral",
        event="speech",
        duration_ms=500,
        inference_ms=12,
    )


def test_stage5_reads_wavs_and_writes_generated_wav_manifest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    input_dir = tmp_path / "dialogue_questions_20"
    output_dir = tmp_path / "stage5_output"
    input_dir.mkdir()
    _write_input(input_dir / "02-second.wav")
    _write_input(input_dir / "01-first.wav")
    (input_dir / "manifest.jsonl").write_text("ignored", encoding="utf-8")

    runner = Stage5DialogueRunner(
        transcriber=FakeTranscriber([_speech_result("第一句"), _speech_result("第二句")]),
        chat_session=FakeChatSession(
            [
                CharacterReply("第一条回复。", "happy", 0.7, 1.02),
                CharacterReply("第二条回复。", "gentle", 0.6, 0.95),
            ]
        ),
        synthesizer=FakeSynthesizer(),
        input_dir=input_dir,
        output_dir=output_dir,
    )

    results = runner.run_all()

    assert [Path(result.input_audio).name for result in results] == ["01-first.wav", "02-second.wav"]
    assert all(result.output_audio.endswith(".wav") for result in results)
    assert all(inspect_wav(result.output_audio).duration_seconds > 0 for result in results)
    assert (output_dir / "manifest.jsonl").exists()
    assert "PLAYING" not in results[0].states
    assert "WAV_SAVED" in results[0].states


def test_stage5_asr_skip_generates_fallback_wav_without_calling_llm(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    input_dir = tmp_path / "dialogue_questions_20"
    output_dir = tmp_path / "stage5_output"
    input_dir.mkdir()
    _write_input(input_dir / "01-silent.wav")
    chat = FakeChatSession()
    synth = FakeSynthesizer()

    runner = Stage5DialogueRunner(
        transcriber=FakeTranscriber(
            [
                TranscriptionResult(
                    text="",
                    language="unknown",
                    emotion="unknown",
                    event="nospeech",
                    duration_ms=500,
                    inference_ms=0,
                    skipped=True,
                    reason="audio peak below silence threshold",
                )
            ]
        ),
        chat_session=chat,
        synthesizer=synth,
        input_dir=input_dir,
        output_dir=output_dir,
    )

    result = runner.run_all()[0]

    assert result.ok is False
    assert result.output_audio.endswith(".wav")
    assert result.error == "audio peak below silence threshold"
    assert chat.calls == []
    assert synth.calls[0].text == "我刚才没有听清，可以再说一遍吗？"


def test_stage5_llm_failure_generates_fallback_wav_and_continues(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    input_dir = tmp_path / "dialogue_questions_20"
    output_dir = tmp_path / "stage5_output"
    input_dir.mkdir()
    _write_input(input_dir / "01-question.wav")

    synth = FakeSynthesizer()
    runner = Stage5DialogueRunner(
        transcriber=FakeTranscriber([_speech_result("你是谁")]),
        chat_session=FakeChatSession(fail=True),
        synthesizer=synth,
        input_dir=input_dir,
        output_dir=output_dir,
    )

    result = runner.run_all()[0]

    assert result.ok is False
    assert result.output_audio.endswith(".wav")
    assert result.error.startswith("Qwen3 failed:")
    assert synth.calls[0].text == "我这里稍微卡了一下，但我还在听你说。"


def test_stage5_can_keep_tts_on_default_character(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    input_dir = tmp_path / "dialogue_questions_20"
    output_dir = tmp_path / "stage5_output"
    input_dir.mkdir()
    _write_input(input_dir / "01-question.wav")
    synth = FakeSynthesizer()

    runner = Stage5DialogueRunner(
        transcriber=FakeTranscriber([_speech_result("hello")]),
        chat_session=FakeChatSession([CharacterReply("reply", "neutral", 0.5, 1.0)]),
        synthesizer=synth,
        input_dir=input_dir,
        output_dir=output_dir,
        character_id="fish",
        tts_character_id="elysia",
    )

    runner.run_all()

    assert synth.calls[0].character_id == "elysia"


def test_stage5_bad_input_file_does_not_stop_later_turns(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    input_dir = tmp_path / "dialogue_questions_20"
    output_dir = tmp_path / "stage5_output"
    input_dir.mkdir()
    (input_dir / "01-broken.wav").write_bytes(b"not a wav")
    _write_input(input_dir / "02-good.wav")

    runner = Stage5DialogueRunner(
        transcriber=FakeTranscriber([_speech_result("继续")]),
        chat_session=FakeChatSession([CharacterReply("继续处理。", "neutral", 0.5, 1.0)]),
        synthesizer=FakeSynthesizer(),
        input_dir=input_dir,
        output_dir=output_dir,
    )

    results = runner.run_all()

    assert len(results) == 2
    assert results[0].ok is False
    assert results[0].output_audio == ""
    assert "FAILED" in results[0].states
    assert results[1].ok is True
    assert results[1].output_audio.endswith(".wav")
