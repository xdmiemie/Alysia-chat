from __future__ import annotations

import io
import json
import wave
from scripts.qq_companion.probe_q3_local import LocalLoop

import pytest
from fastapi import HTTPException

from scripts.qq_companion.configure_model import configure
from scripts.qq_companion.gpu_broker import GPUResourceBroker
from services.media_api.app import SpeechPayload, _token, create_app


@pytest.fixture(autouse=True)
def isolated_broker_state(tmp_path, monkeypatch):
    # Unit tests must never contend with the operator's running model stack.
    from scripts.qq_companion import gpu_broker
    monkeypatch.setattr(gpu_broker, "STATE", tmp_path / "broker")

def route(app, path):
    return next(item.endpoint for item in app.routes if item.path == path)


def run_immediate(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    raise AssertionError("The fake upload unexpectedly yielded")


class FakeUpload:
    def __init__(self, data, name):
        self.data = data
        self.filename = name

    async def read(self, limit):
        return self.data[:limit]


def test_one_runner_provider_and_persona_are_idempotent():
    config = {"provider": [{"id": "other"}], "agent_runner": {"config": {"model": {}}}}
    twice = configure(configure(config, "live-model", "elysia"), "live-model", "elysia")
    assert len(twice["provider"]) == 2
    assert twice["provider"][-1]["model"] == "live-model"
    assert twice["provider"][-1]["custom_extra_body"]["chat_template_kwargs"]["enable_thinking"] is True
    assert twice["provider"][-1]["custom_extra_body"]["reasoning_budget_tokens"] > 0
    assert twice["agent_runner"]["config"]["model"]["provider_id"] == "astrbot_chat_qwen3"
    assert twice["agent_runner"]["config"]["persona"]["persona_id"] == "elysia"


def test_media_auth_and_rejected_audio(monkeypatch):
    monkeypatch.setenv("QQ_COMPANION_MEDIA_TOKEN", "test-token")
    with pytest.raises(HTTPException) as denied:
        _token(None)
    assert denied.value.status_code == 401
    _token("Bearer test-token")
    app = create_app()
    with pytest.raises(HTTPException) as bad_audio:
        run_immediate(route(app, "/v1/audio/transcriptions")(
            FakeUpload(b"arbitrary", "bad.wav")))
    assert bad_audio.value.status_code == 415
    with pytest.raises(HTTPException) as bad_character:
        route(app, "/v1/audio/speech")(SpeechPayload(text="hi", character_id="unknown"))
    assert bad_character.value.status_code == 400


def test_media_transcription_contract(monkeypatch):
    import services.media_api.app as media

    monkeypatch.setenv("QQ_COMPANION_MEDIA_TOKEN", "test-token")
    monkeypatch.setattr(media, "_upstream", lambda *args, **kwargs: json.dumps(
        {"text": "你好", "language": "zh", "duration_ms": 1000, "emotion": "neutral"}).encode())
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 16000)
    loop = LocalLoop()
    try:
        response = loop.run_until_complete(route(create_app(), "/v1/audio/transcriptions")(
            FakeUpload(audio.getvalue(), "sample.wav")))
    finally:
        loop.close()
    assert response == {"text": "你好", "language": "zh", "duration_ms": 1000}


def test_broker_excludes_second_owner():
    first = GPUResourceBroker()
    second = GPUResourceBroker()
    first.acquire("test_a")
    try:
        with pytest.raises(RuntimeError):
            second.acquire("test_b")
    finally:
        first.release()


def test_activation_rolls_back_failed_fixed_smoke(tmp_path, monkeypatch):
    from scripts.qq_companion import model_activation

    active = tmp_path / "active.json"
    registry = tmp_path / "registry.json"
    active.write_text('{"model_id":"old"}', encoding="utf-8")
    registry.write_text('{"active_model_id":"old"}', encoding="utf-8")
    monkeypatch.setattr(model_activation, "BACKUPS", tmp_path / "backups")
    monkeypatch.setattr(model_activation, "paths", lambda kind, character: (active, registry))

    def fake_activate(character, model):
        active.write_text('{"model_id":"new"}', encoding="utf-8")
        registry.write_text('{"active_model_id":"new"}', encoding="utf-8")

    monkeypatch.setattr(model_activation, "activate_qwen", fake_activate)
    monkeypatch.setattr(model_activation, "run_active_probe", lambda: (_ for _ in ()).throw(RuntimeError("smoke failed")))
    with pytest.raises(RuntimeError, match="smoke failed"):
        model_activation.activate("qwen3", "elysia", "new")
    assert json.loads(active.read_text(encoding="utf-8"))["model_id"] == "old"
    assert json.loads(registry.read_text(encoding="utf-8"))["active_model_id"] == "old"


def test_activation_snapshot_can_be_rolled_back(tmp_path, monkeypatch):
    from scripts.qq_companion import model_activation

    active = tmp_path / "active.json"
    registry = tmp_path / "registry.json"
    active.write_text('{"model_id":"old"}', encoding="utf-8")
    registry.write_text('{"active_model_id":"old"}', encoding="utf-8")
    monkeypatch.setattr(model_activation, "BACKUPS", tmp_path / "backups")
    monkeypatch.setattr(model_activation, "paths", lambda kind, character: (active, registry))
    monkeypatch.setattr(model_activation, "activate_qwen", lambda character, model: (
        active.write_text('{"model_id":"new"}', encoding="utf-8"),
        registry.write_text('{"active_model_id":"new"}', encoding="utf-8")))
    monkeypatch.setattr(model_activation, "run_active_probe", lambda: "live-model")
    job_id = model_activation.activate("qwen3", "elysia", "new")
    assert json.loads(active.read_text(encoding="utf-8"))["model_id"] == "new"
    model_activation.rollback(job_id)
    assert json.loads(active.read_text(encoding="utf-8"))["model_id"] == "old"
    assert json.loads(registry.read_text(encoding="utf-8"))["active_model_id"] == "old"
