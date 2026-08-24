from __future__ import annotations

import json
from pathlib import Path

from desktop.characters import CharacterDraft, CharacterManager
from training.qwen3.config import load_training_config


def _make_character(manager: CharacterManager, character_id: str) -> None:
    manager.save(CharacterDraft.template(character_id, character_id.title()))


def test_shared_qwen_model_is_retained_until_its_last_role_is_removed(tmp_path: Path) -> None:
    manager = CharacterManager(tmp_path)
    _make_character(manager, "violet")
    _make_character(manager, "iris")
    incoming = tmp_path / "incoming.gguf"
    incoming.write_bytes(b"GGUF")

    model = manager.add_shared_model(
        "qwen3", "summer-chat-v1", "Summer chat", ["violet", "iris"], [incoming], model_format="gguf"
    )
    owned_file = model.required_files[0]
    assert owned_file.is_file()
    assert set(model.character_ids) == {"violet", "iris"}

    assert manager.remove_model_association("qwen3", model.registry_key, "violet") is False
    assert owned_file.is_file()
    remaining = manager.list_shared_models("qwen3")
    assert remaining[0].character_ids == ("iris",)

    assert manager.remove_model_association("qwen3", model.registry_key, "iris") is True
    assert not owned_file.exists()


def test_training_config_reads_multi_role_assignment(tmp_path: Path) -> None:
    config = tmp_path / "job.yaml"
    config.write_text(
        'character_id: violet\nrun_name: run\nmodel_name: "Named model"\nassigned_character_ids: "violet,iris,violet"\n',
        encoding="utf-8",
    )

    loaded = load_training_config(config)

    assert loaded.model_name == "Named model"
    assert loaded.assigned_character_ids == ("violet", "iris")


def test_legacy_models_with_the_same_id_but_different_files_are_not_merged(tmp_path: Path) -> None:
    manager = CharacterManager(tmp_path)
    _make_character(manager, "violet")
    _make_character(manager, "iris")
    for role_id in ("violet", "iris"):
        weights = tmp_path / "model_store" / "registry" / "gpt_sovits" / role_id / "base-v1"
        weights.mkdir(parents=True)
        ckpt, pth = weights / "voice.ckpt", weights / "voice.pth"
        ckpt.write_bytes(b"x"); pth.write_bytes(b"x")
        manifest = weights.parent / "models.json"
        manifest.write_text(json.dumps({"models": [{"id": "base-v1", "required_files": [str(ckpt.relative_to(tmp_path)), str(pth.relative_to(tmp_path))]}]}), encoding="utf-8")

    models = manager.list_shared_models("gpt_sovits")

    assert len(models) == 2
    assert all(model.character_ids in {("iris",), ("violet",)} for model in models)
