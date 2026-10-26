"""Repeatable Stage 11 Core regression without requiring model processes."""

from __future__ import annotations

import io
import json
import wave
from array import array
from pathlib import Path

from runtime.audio.audio_format import inspect_wav, write_pcm16_wav
from runtime.core import AlysiaCore, UnifiedMessage
from runtime.gpt_sovits.types import SpeechResult
from runtime.qwen3.config import Qwen3Config
from runtime.qwen3.service import Qwen3ChatSession
from runtime.sensevoice.types import TranscriptionResult


FIXED_INPUTS = tuple(
    line.strip()
    for line in (Path(__file__).resolve().parents[1] / "docs/qq_agent/fixed_text_inputs.txt").read_text(
        encoding="utf-8"
    ).splitlines()
    if line.strip()
)


class RecordingQwenClient:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def complete(self, messages):
        contents = [message.content for message in messages]
        self.calls.append(contents)
        latest = messages[-1].content.removeprefix("/no_think\n")
        return json.dumps(
            {"reply": f"收到：{latest}", "emotion": "neutral", "intensity": 0.5, "speed": 1.0},
            ensure_ascii=False,
        )


class FixedTranscriber:
    def transcribe(self, _audio_path):
        return TranscriptionResult(
            text="语音测试",
            language="zh",
            emotion="neutral",
            event="speech",
            duration_ms=500,
            inference_ms=1,
        )


class WavSynthesizer:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def synthesize(self, request):
        self.texts.append(request.text)
        audio = io.BytesIO()
        with wave.open(audio, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(array("h", [0, 1000, -1000, 500] * 400).tobytes())
        return SpeechResult(
            audio_bytes=audio.getvalue(),
            format="wav",
            sample_rate=16000,
            reference_audio=Path("reference.wav"),
            normalized_text=request.text,
            backend_ms=1,
        )


def make_core(output_dir: Path):
    clients: dict[str, RecordingQwenClient] = {}
    synthesizer = WavSynthesizer()

    def session_factory(session_id: str):
        client = RecordingQwenClient()
        clients[session_id] = client
        return Qwen3ChatSession(Qwen3Config(max_history_turns=8), client, "Stage 11 test prompt")

    return (
        AlysiaCore(
            transcriber=FixedTranscriber(),
            chat_session_factory=session_factory,
            synthesizer=synthesizer,
            output_dir=output_dir,
        ),
        clients,
        synthesizer,
    )


def test_twenty_fixed_text_turns_are_isolated_and_survive_core_restart(tmp_path):
    assert len(FIXED_INPUTS) == 20
    core, clients, synthesizer = make_core(tmp_path / "first")
    sessions = ("qq:private:a", "qq:private:b")

    for index, prompt in enumerate(FIXED_INPUTS):
        session_id = sessions[index % 2]
        result = core.process(UnifiedMessage.local_text(prompt, session_id=session_id))
        assert result.ok, result.error
        assert result.state == "COMPLETED"
        assert result.text == f"收到：{prompt}"
        assert result.session_id == session_id
        assert inspect_wav(result.audio_path).duration_seconds > 0

    assert len(synthesizer.texts) == 20
    for index, session_id in enumerate(sessions):
        own = FIXED_INPUTS[index::2]
        other = FIXED_INPUTS[1 - index :: 2]
        assert len(clients[session_id].calls) == 10
        for call in clients[session_id].calls:
            joined = "\n".join(call)
            assert not any(prompt in joined for prompt in other)
        assert own[0] in "\n".join(clients[session_id].calls[0])

    restarted_core, restarted_clients, _ = make_core(tmp_path / "restarted")
    restarted = restarted_core.process(UnifiedMessage.local_text("重启后继续", session_id=sessions[0]))
    assert restarted.ok, restarted.error
    assert restarted.text == "收到：重启后继续"
    assert len(restarted_clients[sessions[0]].calls) == 1
    assert not any(prompt in "\n".join(restarted_clients[sessions[0]].calls[0]) for prompt in FIXED_INPUTS)


def test_voice_flows_through_asr_qwen_and_wav_output(tmp_path):
    source = tmp_path / "speech.wav"
    write_pcm16_wav(source, array("h", [0, 1200, -1200, 600] * 4000).tobytes(), 16000, 1)
    core, clients, synthesizer = make_core(tmp_path / "reply")

    result = core.process(UnifiedMessage.local_audio(source, session_id="qq:private:voice"))

    assert result.ok, result.error
    assert result.transcription["text"] == "语音测试"
    assert result.text == "收到：语音测试"
    assert result.timings.asr_ms is not None
    assert result.timings.tts_ms is not None
    assert inspect_wav(result.audio_path).duration_seconds > 0
    assert len(clients["qq:private:voice"].calls) == 1
    assert synthesizer.texts == ["收到：语音测试"]
