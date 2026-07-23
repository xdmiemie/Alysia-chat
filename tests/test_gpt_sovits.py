from __future__ import annotations

from array import array

import pytest

from runtime.audio.audio_format import inspect_wav, write_pcm16_wav
from runtime.gpt_sovits.config import GPTSoVITSConfig
from runtime.gpt_sovits.references import resolve_reference_audio
from runtime.gpt_sovits.service import GPTSoVITSSynthesizer
from runtime.gpt_sovits.text import normalize_tts_text, split_tts_sentences
from runtime.gpt_sovits.types import ReferenceAudio, SpeechRequest


class FakeSpeechBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ReferenceAudio, float, str]] = []

    def synthesize(self, text: str, reference: ReferenceAudio, speed: float, audio_format: str):
        self.calls.append((text, reference, speed, audio_format))
        samples = array("h", [0, 1000, -1000, 500] * 200)
        wav_path = reference.path.parent / "_fake-output.wav"
        write_pcm16_wav(wav_path, samples.tobytes(), sample_rate=16000, channels=1)
        return wav_path.read_bytes(), 7


def _write_reference_set(tmp_path):
    reference_dir = tmp_path / "references"
    reference_dir.mkdir()
    for emotion in ["neutral", "happy", "gentle", "sad", "surprised", "serious"]:
        write_pcm16_wav(reference_dir / f"{emotion}.wav", array("h", [0, 200, -200] * 100).tobytes(), 16000, 1)

    emotion_map = tmp_path / "emotion_map.yaml"
    emotion_map.write_text(
        "\n".join(
            [
                "neutral: neutral.wav",
                "happy: happy.wav",
                "gentle: gentle.wav",
                "sad: sad.wav",
                "surprised: surprised.wav",
                "serious: serious.wav",
            ]
        ),
        encoding="utf-8",
    )
    reference_texts = tmp_path / "reference_texts.yaml"
    reference_texts.write_text("neutral: 你好呀\nhappy: 太好啦\n", encoding="utf-8")
    return reference_dir, emotion_map, reference_texts


def _config(tmp_path) -> GPTSoVITSConfig:
    reference_dir, emotion_map, reference_texts = _write_reference_set(tmp_path)
    return GPTSoVITSConfig(
        model_manifest=tmp_path / "manifest.json",
        emotion_map=emotion_map,
        reference_texts=reference_texts,
        reference_dir=reference_dir,
        generated_dir=tmp_path / "generated",
    )


def test_normalize_tts_text_removes_markdown_symbols_and_normalizes_dates():
    text = normalize_tts_text("**今天** 是 2026-07-23，[链接](https://example.com) @#$")

    assert "链接" in text
    assert "二零二六年七月二十三日" in text
    assert "@" not in text
    assert "*" not in text


def test_split_tts_sentences_filters_empty_and_splits_long_text():
    sentences = split_tts_sentences("第一句。第二句很长很长很长很长很长很长很长。", max_chars=8)

    assert sentences[0] == "第一句。"
    assert all(len(sentence) <= 8 for sentence in sentences[1:])


def test_resolve_reference_audio_uses_emotion_and_falls_back_to_neutral(tmp_path):
    config = _config(tmp_path)
    (config.reference_dir / "happy.txt").write_text("同名文本优先", encoding="utf-8")

    happy = resolve_reference_audio(config, "happy")
    unknown = resolve_reference_audio(config, "sleepy")

    assert happy.path.name == "happy.wav"
    assert happy.prompt_text == "同名文本优先"
    assert unknown.path.name == "neutral.wav"
    assert unknown.emotion == "neutral"


def test_synthesizer_returns_non_empty_wav_and_backend_metadata(tmp_path):
    config = _config(tmp_path)
    backend = FakeSpeechBackend()
    synthesizer = GPTSoVITSSynthesizer(config=config, backend=backend)

    result = synthesizer.synthesize(
        SpeechRequest(text="辛苦啦，今天先休息一下。", character_id="elysia", emotion="gentle", speed=0.95)
    )

    assert result.audio_bytes.startswith(b"RIFF")
    assert result.reference_audio.name == "gentle.wav"
    assert result.normalized_text == "辛苦啦，今天先休息一下。"
    assert result.backend_ms == 7
    assert backend.calls[0][2] == 0.95


def test_synthesizer_rejects_empty_text_after_preprocessing(tmp_path):
    synthesizer = GPTSoVITSSynthesizer(config=_config(tmp_path), backend=FakeSpeechBackend())

    with pytest.raises(ValueError, match="speakable"):
        synthesizer.synthesize(SpeechRequest(text="```code``` @#$", character_id="elysia"))


def test_speech_endpoint_returns_wav_bytes(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from runtime.gpt_sovits.api import create_app

    monkeypatch.chdir(tmp_path)
    synthesizer = GPTSoVITSSynthesizer(config=_config(tmp_path), backend=FakeSpeechBackend())
    client = TestClient(create_app(synthesizer=synthesizer))

    response = client.post(
        "/v1/audio/speech",
        json={"text": "辛苦啦", "character_id": "elysia", "emotion": "neutral", "speed": 1.0, "format": "wav"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content.startswith(b"RIFF")


def test_stage4_acceptance_fixture_has_twenty_items():
    fixture = "tests/fixtures/gpt_sovits_stage4_texts.txt"
    lines = [line for line in open(fixture, encoding="utf-8").read().splitlines() if line.strip()]

    assert len(lines) == 20


def test_fake_output_is_valid_wav(tmp_path):
    config = _config(tmp_path)
    backend = FakeSpeechBackend()
    synthesizer = GPTSoVITSSynthesizer(config=config, backend=backend)
    result = synthesizer.synthesize(SpeechRequest(text="测试音频。", character_id="elysia"))
    output = tmp_path / "out.wav"
    output.write_bytes(result.audio_bytes)

    metrics = inspect_wav(output)

    assert metrics.duration_seconds > 0
    assert metrics.sample_rate == 16000
