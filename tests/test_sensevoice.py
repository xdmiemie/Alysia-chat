from __future__ import annotations

from array import array

import pytest

from runtime.audio.audio_format import write_pcm16_wav
from runtime.sensevoice.config import SenseVoiceConfig
from runtime.sensevoice.service import SenseVoiceRecognizer
from runtime.sensevoice.text import normalize_transcribed_text, parse_sensevoice_text
from runtime.sensevoice.types import RawTranscription


class FakeSenseVoiceBackend:
    def __init__(self, text: str = "<|zh|><|NEUTRAL|><|Speech|> 今天 感觉 有一点 累 ") -> None:
        self.text = text
        self.calls = 0

    def transcribe(self, _audio_path):
        self.calls += 1
        return RawTranscription(text=self.text, inference_ms=12)


def _write_test_wav(path, samples):
    return write_pcm16_wav(path, array("h", samples).tobytes(), sample_rate=16000, channels=1)


def test_parse_sensevoice_tags_and_normalize_text():
    parsed = parse_sensevoice_text("<|zh|><|SAD|><|Speech|> 今天 感觉 有一点 累 ? ")

    assert parsed.text == "今天感觉有一点累？"
    assert parsed.language == "zh"
    assert parsed.emotion == "sad"
    assert parsed.event == "speech"


def test_normalize_transcribed_text_filters_empty_values():
    assert normalize_transcribed_text("   \n\t ") == ""


def test_recognizer_transcribes_wav_with_backend(tmp_path):
    wav_path = _write_test_wav(tmp_path / "speech.wav", [0, 1200, -1200, 800] * 2000)
    backend = FakeSenseVoiceBackend()
    recognizer = SenseVoiceRecognizer(config=SenseVoiceConfig(), backend=backend)

    result = recognizer.transcribe(wav_path)

    assert result.text == "今天感觉有一点累"
    assert result.language == "zh"
    assert result.emotion == "neutral"
    assert result.event == "speech"
    assert result.duration_ms == 500
    assert result.inference_ms == 12
    assert result.skipped is False
    assert backend.calls == 1


def test_recognizer_skips_too_short_audio(tmp_path):
    wav_path = _write_test_wav(tmp_path / "short.wav", [0, 1200, -1200])
    backend = FakeSenseVoiceBackend()
    recognizer = SenseVoiceRecognizer(config=SenseVoiceConfig(min_duration_ms=300), backend=backend)

    result = recognizer.transcribe(wav_path)

    assert result.text == ""
    assert result.event == "too_short"
    assert result.skipped is True
    assert backend.calls == 0


def test_recognizer_skips_silent_audio(tmp_path):
    wav_path = _write_test_wav(tmp_path / "silent.wav", [0] * 8000)
    backend = FakeSenseVoiceBackend()
    recognizer = SenseVoiceRecognizer(config=SenseVoiceConfig(silence_peak_threshold=0.002), backend=backend)

    result = recognizer.transcribe(wav_path)

    assert result.text == ""
    assert result.event == "nospeech"
    assert result.skipped is True
    assert backend.calls == 0


def test_recognizer_handles_twenty_segments_without_state_leak(tmp_path):
    wav_path = _write_test_wav(tmp_path / "speech.wav", [0, 1500, -1500, 400] * 2000)
    backend = FakeSenseVoiceBackend()
    recognizer = SenseVoiceRecognizer(config=SenseVoiceConfig(), backend=backend)

    results = [recognizer.transcribe(wav_path) for _ in range(20)]

    assert backend.calls == 20
    assert all(result.text == "今天感觉有一点累" for result in results)
    assert all(result.inference_ms == 12 for result in results)


def test_transcription_endpoint_accepts_uploaded_wav(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from runtime.sensevoice.api import create_app

    monkeypatch.chdir(tmp_path)
    wav_path = _write_test_wav(tmp_path / "speech.wav", [0, 1200, -1200, 800] * 2000)
    recognizer = SenseVoiceRecognizer(config=SenseVoiceConfig(), backend=FakeSenseVoiceBackend())
    client = TestClient(create_app(recognizer=recognizer))

    response = client.post(
        "/v1/audio/transcriptions",
        files={"file": ("speech.wav", wav_path.read_bytes(), "audio/wav")},
    )

    assert response.status_code == 200
    assert response.json()["text"] == "今天感觉有一点累"
    assert response.json()["duration_ms"] == 500
