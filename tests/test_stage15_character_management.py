from __future__ import annotations

import wave
import json
from pathlib import Path

import pytest

from desktop.characters import CharacterDraft, CharacterManager


def _wav(path: Path, *, seconds: float = 3.5) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\0\0" * int(16_000 * seconds))
    return path


def test_stage15_template_saves_isolated_character_resources(tmp_path: Path) -> None:
    manager = CharacterManager(tmp_path)
    draft = CharacterDraft.template("violet", "Violet")
    reference = _wav(tmp_path / "incoming" / "voice.wav")
    manager.add_reference(draft, "neutral", reference, "你好，这是参考语音。")
    result = manager.save(draft)

    root = tmp_path / "configs" / "characters" / "violet"
    assert not result.errors
    assert result.warnings  # models have not been trained yet, so it is incomplete.
    assert (root / "character.yaml").is_file()
    assert (root / "system_prompt.md").is_file()
    assert (root / "system_prompt.txt").is_file()  # existing runtime compatibility
    assert (root / "references" / "neutral.wav").is_file()
    assert 'neutral: "neutral.wav"' in (root / "emotion_map.yaml").read_text(encoding="utf-8")
    assert dict(manager.list_profiles())["violet"] == "Violet"


def test_stage15_export_import_duplicate_and_delete_do_not_touch_models(tmp_path: Path) -> None:
    manager = CharacterManager(tmp_path)
    draft = CharacterDraft.template("violet", "Violet")
    manager.add_reference(draft, "neutral", _wav(tmp_path / "voice.wav"), "参考文本")
    manager.save(draft)
    model_file = tmp_path / "model_store" / "registry" / "qwen3" / "violet" / "models.json"
    model_file.parent.mkdir(parents=True)
    model_file.write_text('{"models": []}', encoding="utf-8")

    copied = manager.duplicate("violet", "violet_copy")
    assert copied.character_id == "violet_copy"
    assert (tmp_path / "configs" / "characters" / "violet_copy" / "references" / "neutral.wav").is_file()
    assert model_file.is_file()

    bundle = manager.export_bundle("violet_copy", tmp_path / "violet.zip")
    manager.delete("violet_copy")
    assert not (tmp_path / "configs" / "characters" / "violet_copy").exists()
    assert model_file.is_file()
    imported = manager.import_bundle(bundle)
    assert imported.character_id == "violet_copy"


def test_stage15_rejects_invalid_id_and_reference_without_text(tmp_path: Path) -> None:
    manager = CharacterManager(tmp_path)
    draft = CharacterDraft.template("Not valid", "Name")
    manager.add_reference(draft, "neutral", _wav(tmp_path / "voice.wav"))
    validation = manager.validate(draft)
    assert any("角色 ID" in issue for issue in validation.errors)
    assert any("缺少对应文本" in issue for issue in validation.errors)
    with pytest.raises(ValueError):
        manager.save(draft)


def test_stage15_legacy_character_uses_active_registry_models_in_editor(tmp_path: Path) -> None:
    character = tmp_path / "configs" / "characters" / "legacy"
    character.mkdir(parents=True)
    (character / "character.yaml").write_text("character_id: legacy\nname: Legacy\n", encoding="utf-8")
    for kind, model_id in (("qwen3", "legacy-chat-v1"), ("gpt_sovits", "legacy-voice-v1")):
        registry = tmp_path / "model_store" / "registry" / kind / "legacy"
        registry.mkdir(parents=True)
        (registry / "models.json").write_text(
            json.dumps({"active_model_id": model_id, "models": [{"id": model_id}]}),
            encoding="utf-8",
        )

    draft = CharacterManager(tmp_path).load("legacy")
    assert draft.qwen_model == "legacy-chat-v1"
    assert draft.tts_model == "legacy-voice-v1"
