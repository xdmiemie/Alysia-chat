from __future__ import annotations

import json
import os
import wave
from pathlib import Path

import pytest

from desktop.characters import CharacterDraft, CharacterManager
from runtime.core import model_versions
from runtime.qwen3 import model_registry


def _registry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(model_registry, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(model_registry, "REGISTRY_ROOT", tmp_path / "model_store" / "registry" / "qwen3")
    monkeypatch.setattr(model_registry, "ACTIVE_MANIFEST", tmp_path / "model_store" / "active" / "qwen3.json")


def _model(tmp_path: Path, model_id: str, *, activate: bool = False) -> None:
    artifact = tmp_path / "model_store" / "registry" / "qwen3" / "violet" / model_id / "model.gguf"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"GGUF")
    model_registry.register_model(character_id="violet", model_id=model_id, display_name=model_id,
                                  kind="trained", model_format="gguf", required_files=[artifact], activate=activate)


def test_failed_activation_keeps_both_manifests(tmp_path: Path, monkeypatch) -> None:
    _registry(tmp_path, monkeypatch)
    _model(tmp_path, "first", activate=True)
    _model(tmp_path, "second")
    registry_path = model_registry.character_manifest_path("violet")
    active_path = model_registry.ACTIVE_MANIFEST
    before = (registry_path.read_bytes(), active_path.read_bytes())

    with pytest.raises(RuntimeError, match="load failed"):
        model_registry.activate_model("violet", "second", smoke_test=lambda _model: (_ for _ in ()).throw(RuntimeError("load failed")))
    assert (registry_path.read_bytes(), active_path.read_bytes()) == before

    original = model_versions.atomic_json
    def fail_registry(path, data):
        if path == registry_path:
            raise OSError("write failed")
        original(path, data)
    monkeypatch.setattr(model_versions, "atomic_json", fail_registry)
    with pytest.raises(OSError, match="write failed"):
        model_registry.activate_model("violet", "second")
    assert (registry_path.read_bytes(), active_path.read_bytes()) == before


def test_rollback_and_pinned_version_are_protected(tmp_path: Path, monkeypatch) -> None:
    _registry(tmp_path, monkeypatch)
    for index in range(8):
        _model(tmp_path, f"trained-{index}", activate=index == 0)
    manifest = model_registry.load_character_manifest("violet")
    next(item for item in manifest["models"] if item["id"] == "trained-1")["pinned"] = True
    model_registry.save_character_manifest("violet", manifest)
    assert {item.model_id for item in model_registry.plan_prune_trained_models("violet")} == {"trained-2"}
    model_registry.activate_model("violet", "trained-7")
    assert model_registry.load_character_manifest("violet")["previous_model_id"] == "trained-0"
    assert model_registry.rollback_model("violet").model_id == "trained-0"


def test_character_bundle_restores_references_and_model_files(tmp_path: Path) -> None:
    manager = CharacterManager(tmp_path / "source")
    draft = CharacterDraft.template("violet", "Violet")
    wav = tmp_path / "voice.wav"
    with wave.open(str(wav), "wb") as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(16_000)
        output.writeframes(b"\0\0" * 16_000 * 3)
    manager.add_reference(draft, "neutral", wav, "你好")
    manager.save(draft)
    artifact = tmp_path / "model.gguf"
    artifact.write_bytes(b"GGUF")
    manager.add_shared_model("qwen3", "voice-chat-v1", "Voice chat", ["violet"], [artifact])
    active = manager.list_character_models("qwen3", "violet")[0]
    with pytest.raises(ValueError, match="不能删除"):
        manager.remove_model_association("qwen3", active.registry_key, "violet")

    bundle = manager.export_bundle("violet", tmp_path / "violet.zip", include_models=True)
    imported_manager = CharacterManager(tmp_path / "destination")
    imported = imported_manager.import_bundle(bundle)
    assert imported.qwen_model == "voice-chat-v1"
    assert (tmp_path / "destination" / "configs" / "characters" / "violet" / "references" / "neutral.wav").is_file()
    manifest = imported_manager._load_model_manifest("qwen3", "violet")
    assert len(manifest["models"]) == 1
    restored = imported_manager._project_model_path(manifest["models"][0]["required_files"][0])
    assert restored.read_bytes() == b"GGUF"
    assert json.loads((tmp_path / "destination" / "model_store" / "registry" / "qwen3" / "violet" / "models.json").read_text(encoding="utf-8"))["active_model_id"] == "voice-chat-v1"


def test_model_dialog_has_basic_actions_and_usage_selector_offers_rollback(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QPushButton
    from desktop.windows.character_manager_dialog import SharedModelDialog
    from desktop.windows.selection_dialogs import ModelSelectionDialog

    app = QApplication.instance() or QApplication([])
    manager = CharacterManager(tmp_path)
    manager.save(CharacterDraft.template("violet", "Violet"))
    artifact = tmp_path / "model.gguf"
    artifact.write_bytes(b"GGUF")
    manager.add_shared_model("qwen3", "chat-v1", "Chat v1", ["violet"], [artifact])
    version_dialog = SharedModelDialog(manager, "qwen3", "violet")
    assert "当前" in version_dialog.model_list.item(0).text()
    assert not version_dialog.delete_button.isEnabled()
    assert "大小" in version_dialog.details.text()
    buttons = {button.text() for button in version_dialog.findChildren(QPushButton)}
    assert {"关联已有模型", "增加模型", "保存所属角色", "移除当前角色关联", "完成"} <= buttons
    assert not buttons & {"设为当前", "回滚上一版", "重命名", "固定 / 取消固定", "启动测试", "清理旧版本"}

    selector = ModelSelectionDialog([("qwen", "chat-v1", "Qwen3 · Chat v1")], "chat-v1", "",
                                    previous_qwen="chat-v0")
    rollback_button = next(button for button in selector.findChildren(QPushButton)
                           if button.text() == "回滚 Qwen3 上一版")
    assert rollback_button.isEnabled()
    rollback_button.click()
    assert selector.rollback_kind == "qwen"
    version_dialog.close(); selector.close(); app.processEvents()


def test_failed_model_start_restores_previous_selection(monkeypatch) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import desktop.controllers.app_controller as controller_module
    from desktop.controllers import AppController
    from desktop.state import AppSnapshot, AppState

    app = QApplication.instance() or QApplication([])
    controller = AppController(state=AppState(AppSnapshot(character_id="violet", qwen_model="new", tts_model="voice")))
    controller._service_action = "start"
    controller._pending_activation = {"qwen": "old"}
    restored = []
    monkeypatch.setattr(controller_module.qwen_registry, "activate_model", lambda character, model: restored.append((character, model)))
    monkeypatch.setattr(controller_module.tts_registry, "load_character_manifest", lambda _character: {})
    controller._on_service_failed("cannot load")

    assert restored == [("violet", "old")]
    assert controller.state.snapshot.qwen_model == "old"
    assert "已恢复上一版本" in controller.state.snapshot.error_summary
    controller.shutdown(); app.processEvents()
