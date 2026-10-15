from __future__ import annotations

from pathlib import Path
import wave

from PySide6.QtCore import Qt, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton,
    QSpinBox, QDoubleSpinBox, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from desktop.characters import CharacterDraft, CharacterManager, ReferenceVoice
from desktop.widgets import TitleBar


class SharedModelDialog(QDialog):
    """Manage shared Qwen/TTS registry entries from the character editor."""

    models_changed = Signal()

    def __init__(self, manager: CharacterManager, model_type: str, character_id: str, parent=None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.model_type = model_type
        self.character_id = character_id
        label = "Qwen" if model_type == "qwen3" else "TTS"
        self.setObjectName("characterManagerDialog")
        self.setWindowTitle(f"管理 {label} 模型")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setModal(True)
        self.resize(660, 560)
        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)
        root.addWidget(TitleBar(title=self.windowTitle()))
        body = QWidget(); layout = QVBoxLayout(body); layout.setContentsMargins(24, 20, 24, 20); layout.setSpacing(12)
        hint = QLabel("模型清单与使用界面共用。当前版本和上一回滚版本受保护，不能从角色移除。")
        hint.setObjectName("dialogHint"); hint.setWordWrap(True); layout.addWidget(hint)
        self.model_list = QListWidget(); self.model_list.setObjectName("sharedModelList"); self.model_list.currentTextChanged.connect(self._load_selected)
        layout.addWidget(self.model_list, 1)
        link_row = QHBoxLayout()
        self.link_model_combo = QComboBox(); link_row.addWidget(self.link_model_combo, 1)
        link = QPushButton("关联已有模型"); link.setObjectName("secondaryButton"); link.clicked.connect(self._link_existing)
        link_row.addWidget(link); layout.addLayout(link_row)
        self.details = QLabel(); self.details.setObjectName("dialogHint"); self.details.setWordWrap(True)
        layout.addWidget(self.details)
        layout.addWidget(QLabel("所属角色（可多选）"))
        roles = QWidget(); role_layout = QHBoxLayout(roles); role_layout.setContentsMargins(0, 0, 0, 0)
        self.role_checks: dict[str, QCheckBox] = {}
        for role_id, role_name in manager.list_profiles():
            check = QCheckBox(f"{role_name} ({role_id})")
            self.role_checks[role_id] = check; role_layout.addWidget(check)
        role_layout.addStretch(1); layout.addWidget(roles)
        actions = QHBoxLayout()
        add = QPushButton("增加模型"); add.setObjectName("secondaryButton"); add.clicked.connect(self._add_model)
        save_roles = QPushButton("保存所属角色"); save_roles.setObjectName("secondaryButton"); save_roles.clicked.connect(self._save_roles)
        delete = QPushButton("移除当前角色关联"); delete.setObjectName("secondaryButton"); delete.clicked.connect(self._remove_current_association)
        self.delete_button = delete
        close = QPushButton("完成"); close.setObjectName("dialogConfirmButton"); close.clicked.connect(self.accept)
        for button in (add, save_roles, delete, close): actions.addWidget(button)
        layout.addLayout(actions); root.addWidget(body, 1)
        self._refresh()

    def _refresh(self, selected_id: str = "") -> None:
        selected_id = selected_id or self._selected_id()
        try:
            self._registry().list_models(self.character_id)
        except (OSError, ValueError):
            pass
        self.model_list.clear()
        manifest = self.manager._load_model_manifest(self.model_type, self.character_id)
        active = str(manifest.get("active_model_id") or "")
        previous = str(manifest.get("previous_model_id") or "")
        for model in self.manager.list_character_models(self.model_type, self.character_id):
            badges = [label for condition, label in ((model.model_id == active, "当前"),
                       (model.model_id == previous, "上一版"), (bool(model.raw.get("pinned")), "固定")) if condition]
            self.model_list.addItem(f"{model.display_name}  ·  {model.model_id}" + (f"  [{' / '.join(badges)}]" if badges else ""))
            self.model_list.item(self.model_list.count() - 1).setData(Qt.ItemDataRole.UserRole, model.registry_key)
        self.link_model_combo.clear()
        for model in self.manager.list_shared_models(self.model_type):
            if self.character_id not in model.character_ids:
                self.link_model_combo.addItem(f"{model.display_name} · {model.model_id}", model.registry_key)
        for index in range(self.model_list.count()):
            if (self.model_list.item(index).data(Qt.ItemDataRole.UserRole) == selected_id or
                    any(model.registry_key == self.model_list.item(index).data(Qt.ItemDataRole.UserRole)
                        and model.model_id == selected_id
                        for model in self.manager.list_character_models(self.model_type, self.character_id))):
                self.model_list.setCurrentRow(index); break
        if self.model_list.count() and self.model_list.currentRow() < 0:
            self.model_list.setCurrentRow(0)
        self._load_selected()

    def _selected_id(self) -> str:
        item = self.model_list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole)) if item else ""

    def _load_selected(self, *_args) -> None:
        model = next((item for item in self.manager.list_shared_models(self.model_type) if item.registry_key == self._selected_id()), None)
        for role_id, check in self.role_checks.items():
            check.setChecked(bool(model and role_id in model.character_ids))
        manifest = self.manager._load_model_manifest(self.model_type, self.character_id)
        self.delete_button.setEnabled(bool(model and self.character_id in model.character_ids and
            model.model_id not in {str(manifest.get("active_model_id") or ""),
                                   str(manifest.get("previous_model_id") or "")}))
        if not model:
            self.details.clear(); return
        raw = model.raw
        size = sum(path.stat().st_size for path in model.required_files if path.is_file())
        self.details.setText(
            f"创建：{raw.get('created_at') or '未知'}    大小：{size / (1024 ** 2):.1f} MiB    "
            f"状态：{raw.get('status') or '已注册'}\n"
            f"基础模型：{raw.get('base_model_id') or '未记录'}    "
            f"训练来源：{raw.get('source_run') or '手动导入 / 基础模型'}\n"
            f"数据哈希：{raw.get('data_hash') or '未记录'}"
        )

    def _selected_roles(self) -> list[str]:
        return [role_id for role_id, check in self.role_checks.items() if check.isChecked()]

    def _link_existing(self) -> None:
        model_key = str(self.link_model_combo.currentData() or "")
        if not model_key:
            return
        try:
            model = self.manager._shared_model(self.model_type, model_key)
            self.manager.associate_model(self.model_type, model_key,
                                         (*model.character_ids, self.character_id))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "关联模型", str(exc)); return
        self.models_changed.emit(); self._refresh(model_key)

    def _save_roles(self) -> None:
        model_key = self._selected_id()
        if not model_key:
            return
        current = next((item for item in self.manager.list_shared_models(self.model_type) if item.registry_key == model_key), None)
        selected = set(self._selected_roles())
        if not selected:
            QMessageBox.warning(self, "所属角色", "模型至少需要属于一个角色。")
            return
        try:
            if current:
                for role_id in set(current.character_ids) - selected:
                    manifest = self.manager._load_model_manifest(self.model_type, role_id)
                    if current.model_id in {str(manifest.get("active_model_id") or ""),
                                            str(manifest.get("previous_model_id") or "")}:
                        raise ValueError(f"角色“{role_id}”的当前或上一版模型不能移除。")
            self.manager.associate_model(self.model_type, model_key, selected)
            if current:
                for role_id in set(current.character_ids) - selected:
                    self.manager.remove_model_association(self.model_type, model_key, role_id)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "保存所属角色", str(exc)); return
        self.models_changed.emit(); self._refresh(model_key)

    def _add_model(self) -> None:
        dialog = SharedModelImportDialog(self.model_type, self._selected_roles() or [self.character_id], self)
        if not dialog.exec():
            return
        try:
            model = self.manager.add_shared_model(
                self.model_type, dialog.model_id, dialog.display_name, dialog.character_ids,
                dialog.required_files, model_format="gguf",
            )
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "增加模型", str(exc)); return
        self.models_changed.emit(); self._refresh(model.registry_key)

    def _registry(self):
        if self.model_type == "qwen3":
            from runtime.qwen3 import model_registry
        else:
            from runtime.gpt_sovits import model_registry
        return model_registry

    def _remove_current_association(self) -> None:
        model_key = self._selected_id()
        if not model_key or not self.character_id:
            return
        model = next((item for item in self.manager.list_shared_models(self.model_type) if item.registry_key == model_key), None)
        if not model or self.character_id not in model.character_ids:
            QMessageBox.information(self, "移除模型", "该模型未关联当前角色。")
            return
        if QMessageBox.question(self, "移除模型", f"确认从当前角色移除模型“{model.display_name}”吗？") != QMessageBox.StandardButton.Yes:
            return
        try:
            deleted_files = self.manager.remove_model_association(self.model_type, model_key, self.character_id)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "移除模型", str(exc)); return
        if deleted_files:
            CharacterNoticeDialog("模型已删除", f"模型“{model.display_name}”已不属于任何角色，模型本体文件已删除。", self).exec()
        self.models_changed.emit(); self._refresh()


class SharedModelImportDialog(QDialog):
    """Small themed import form; TTS requires both of its runtime weights."""

    def __init__(self, model_type: str, selected_roles: list[str], parent=None) -> None:
        super().__init__(parent)
        self.model_type = model_type
        self.setObjectName("characterManagerDialog"); self.setWindowTitle("增加模型")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint); self.setModal(True); self.setMinimumWidth(570)
        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0); root.addWidget(TitleBar(title=self.windowTitle()))
        body = QWidget(); layout = QVBoxLayout(body); layout.setContentsMargins(24, 20, 24, 20)
        form = QFormLayout(); self.id_edit = QLineEdit(); self.id_edit.setPlaceholderText("例如：summer-voice-v1")
        self.name_edit = QLineEdit(); self.name_edit.setPlaceholderText("显示名称")
        self.roles_edit = QLineEdit("，".join(selected_roles)); self.roles_edit.setReadOnly(True)
        self.first_file = QLineEdit(); first_button = QPushButton("选择文件"); first_button.clicked.connect(lambda: self._browse(self.first_file, "模型文件"))
        first_row = QWidget(); first_layout = QHBoxLayout(first_row); first_layout.setContentsMargins(0, 0, 0, 0); first_layout.addWidget(self.first_file, 1); first_layout.addWidget(first_button)
        form.addRow("模型 ID", self.id_edit); form.addRow("模型名称", self.name_edit); form.addRow("所属角色", self.roles_edit)
        form.addRow("Qwen 文件" if model_type == "qwen3" else "GPT 权重 (.ckpt)", first_row)
        self.second_file = QLineEdit()
        if model_type == "gpt_sovits":
            second_button = QPushButton("选择文件"); second_button.clicked.connect(lambda: self._browse(self.second_file, "SoVITS 权重"))
            second_row = QWidget(); second_layout = QHBoxLayout(second_row); second_layout.setContentsMargins(0, 0, 0, 0); second_layout.addWidget(self.second_file, 1); second_layout.addWidget(second_button)
            form.addRow("SoVITS 权重 (.pth)", second_row)
        layout.addLayout(form); layout.addWidget(_character_dialog_buttons(self, ok_text="导入模型")); root.addWidget(body)

    @property
    def model_id(self) -> str: return self.id_edit.text().strip()
    @property
    def display_name(self) -> str: return self.name_edit.text().strip()
    @property
    def character_ids(self) -> list[str]: return [item.strip() for item in self.roles_edit.text().split("，") if item.strip()]
    @property
    def required_files(self) -> list[str]: return [item.text().strip() for item in (self.first_file, self.second_file) if item.text().strip()]

    def _browse(self, target: QLineEdit, title: str) -> None:
        filter_text = "Qwen (*.gguf *.bin *.safetensors);;所有文件 (*)" if self.model_type == "qwen3" else "模型权重 (*.ckpt *.pth);;所有文件 (*)"
        path, _ = QFileDialog.getOpenFileName(self, title, "", filter_text)
        if path: target.setText(path)


class CharacterTextPromptDialog(QDialog):
    """Frameless replacement for QInputDialog used by character actions."""

    def __init__(self, title: str, label: str, value: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("characterManagerDialog")
        self.setWindowTitle(title)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setModal(True)
        self.setMinimumWidth(430)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(TitleBar(title=title))
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        hint = QLabel(label)
        hint.setObjectName("dialogHint")
        self.value_edit = QLineEdit(value)
        self.value_edit.selectAll()
        layout.addWidget(hint)
        layout.addWidget(self.value_edit)
        layout.addWidget(_character_dialog_buttons(self, ok_text="确认"))
        root.addWidget(content)

    @property
    def value(self) -> str:
        return self.value_edit.text().strip()


class CharacterConfirmDialog(QDialog):
    """Frameless confirmation dialog for non-recoverable character actions."""

    def __init__(self, title: str, message: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("characterManagerDialog")
        self.setWindowTitle(title)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setModal(True)
        self.setMinimumWidth(460)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(TitleBar(title=title))
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 22, 24, 20)
        message_label = QLabel(message)
        message_label.setObjectName("dialogHint")
        message_label.setWordWrap(True)
        layout.addWidget(message_label)
        layout.addSpacing(8)
        layout.addWidget(_character_dialog_buttons(self, ok_text="确认删除"))
        root.addWidget(content)


class CharacterNoticeDialog(QDialog):
    """Frameless informational dialog for protected character operations."""

    def __init__(self, title: str, message: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("characterManagerDialog")
        self.setWindowTitle(title)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setModal(True)
        self.setMinimumWidth(440)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(TitleBar(title=title))
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 22, 24, 20)
        message_label = QLabel(message)
        message_label.setObjectName("dialogHint")
        message_label.setWordWrap(True)
        layout.addWidget(message_label)
        layout.addSpacing(8)
        close_button = QPushButton("知道了")
        close_button.setObjectName("dialogConfirmButton")
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button)
        root.addWidget(content)


def _character_dialog_buttons(dialog: QDialog, *, ok_text: str) -> QDialogButtonBox:
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
    )
    cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
    confirm = buttons.button(QDialogButtonBox.StandardButton.Ok)
    cancel.setText("取消")
    cancel.setObjectName("dialogCancelButton")
    confirm.setText(ok_text)
    confirm.setObjectName("dialogConfirmButton")
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    return buttons


class CharacterManagerDialog(QDialog):
    """Four-step editor which keeps all resource writes inside CharacterManager."""

    saved = Signal(str)

    def __init__(self, manager: CharacterManager, character_id: str = "", parent=None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.editing_id = character_id
        self.draft = manager.load(character_id) if character_id else CharacterDraft.template()
        self.setObjectName("characterManagerDialog")
        self.setWindowTitle("编辑角色" if character_id else "创建角色")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setModal(True)
        self.resize(720, 650)
        self._player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_output)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(TitleBar(title=self.windowTitle()))
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(24, 20, 24, 20)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("characterEditorTabs")
        self.tabs.addTab(self._basic_tab(), "1. 基本信息")
        self.tabs.addTab(self._persona_tab(), "2. Qwen3 人设")
        self.tabs.addTab(self._voice_tab(), "3. GPT-SoVITS")
        self.tabs.addTab(self._references_tab(), "4. 参考语音")
        layout.addWidget(self.tabs, 1)
        self.status = QLabel("完整角色需要模型和中性参考音频；也可以保存为未完成角色。")
        self.status.setObjectName("dialogHint")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Save)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存角色")
        buttons.accepted.connect(self._save)
        layout.addWidget(buttons)
        root.addWidget(body, 1)

    def _basic_tab(self) -> QWidget:
        tab = QWidget(); form = QFormLayout(tab)
        self.id_edit = QLineEdit(self.draft.character_id); self.id_edit.setObjectName("characterIdEdit")
        self.id_edit.setEnabled(not self.editing_id)
        self.name_edit = QLineEdit(self.draft.display_name); self.name_edit.setObjectName("characterNameEdit")
        self.description_edit = QTextEdit(self.draft.description); self.description_edit.setMaximumHeight(80)
        self.language_combo = QComboBox(); self.language_combo.addItems(["zh", "ja", "en"]); self._set_combo(self.language_combo, self.draft.language)
        self.reply_combo = QComboBox(); self.reply_combo.addItems(["auto", "text", "voice"]); self._set_combo(self.reply_combo, self.draft.reply_mode)
        self.history_spin = QSpinBox(); self.history_spin.setRange(1, 64); self.history_spin.setValue(self.draft.history_turns)
        self.memory_check = QCheckBox("启用长期记忆"); self.memory_check.setChecked(self.draft.memory_enabled)
        self.avatar_edit = QLineEdit(self.draft.avatar_source); self.avatar_edit.setReadOnly(True)
        avatar_button = QPushButton("选择头像"); avatar_button.clicked.connect(self._choose_avatar)
        avatar_row = QWidget(); row = QHBoxLayout(avatar_row); row.setContentsMargins(0, 0, 0, 0); row.addWidget(self.avatar_edit, 1); row.addWidget(avatar_button)
        form.addRow("角色 ID", self.id_edit); form.addRow("显示名称", self.name_edit); form.addRow("简介", self.description_edit)
        form.addRow("默认语言", self.language_combo); form.addRow("默认回复模式", self.reply_combo); form.addRow("上下文轮数", self.history_spin)
        form.addRow("", self.memory_check); form.addRow("头像", avatar_row)
        return tab

    def _persona_tab(self) -> QWidget:
        tab = QWidget(); form = QFormLayout(tab)
        self.qwen_edit = QLineEdit(self.draft.qwen_model); self.qwen_edit.setReadOnly(True); self.qwen_edit.setPlaceholderText("在使用界面切换当前版本")
        qwen_manage = QPushButton("管理 Qwen 模型")
        qwen_manage.clicked.connect(lambda: self._manage_models("qwen3"))
        qwen_row = QWidget(); qwen_layout = QHBoxLayout(qwen_row); qwen_layout.setContentsMargins(0, 0, 0, 0); qwen_layout.addWidget(self.qwen_edit, 1); qwen_layout.addWidget(qwen_manage)
        self.prompt_edit = QTextEdit(self.draft.system_prompt); self.prompt_edit.setMinimumHeight(180)
        self.personality_edit = QTextEdit(self.draft.personality); self.personality_edit.setPlaceholderText("性格关键词或说明")
        self.style_edit = QTextEdit(self.draft.speaking_style); self.forbidden_edit = QTextEdit(self.draft.forbidden_behavior)
        self.temperature = QDoubleSpinBox(); self.temperature.setRange(0, 2); self.temperature.setSingleStep(.1); self.temperature.setValue(self.draft.temperature)
        self.top_p = QDoubleSpinBox(); self.top_p.setRange(.01, 1); self.top_p.setSingleStep(.05); self.top_p.setValue(self.draft.top_p)
        self.max_tokens = QSpinBox(); self.max_tokens.setRange(1, 8192); self.max_tokens.setValue(self.draft.max_tokens)
        form.addRow("默认运行模型", qwen_row); form.addRow("System prompt", self.prompt_edit); form.addRow("Personality", self.personality_edit)
        form.addRow("Speaking style", self.style_edit); form.addRow("Forbidden behavior", self.forbidden_edit)
        form.addRow("Temperature", self.temperature); form.addRow("Top P", self.top_p); form.addRow("Max tokens", self.max_tokens)
        return tab

    def _voice_tab(self) -> QWidget:
        tab = QWidget(); form = QFormLayout(tab)
        self.tts_edit = QLineEdit(self.draft.tts_model); self.tts_edit.setReadOnly(True); self.tts_edit.setPlaceholderText("在使用界面切换当前版本")
        tts_manage = QPushButton("管理 TTS 模型")
        tts_manage.clicked.connect(lambda: self._manage_models("gpt_sovits"))
        tts_row = QWidget(); tts_layout = QHBoxLayout(tts_row); tts_layout.setContentsMargins(0, 0, 0, 0); tts_layout.addWidget(self.tts_edit, 1); tts_layout.addWidget(tts_manage)
        self.emotion_edit = QLineEdit(self.draft.default_emotion)
        self.speed = QDoubleSpinBox(); self.speed.setRange(.25, 3); self.speed.setSingleStep(.05); self.speed.setValue(self.draft.speed)
        self.text_language = QComboBox(); self.text_language.addItems(["zh", "ja", "en"]); self._set_combo(self.text_language, self.draft.text_language)
        test = QPushButton("用中性参考语音测试 TTS"); test.clicked.connect(self._test_tts)
        form.addRow("默认 TTS 模型", tts_row); form.addRow("默认情绪", self.emotion_edit); form.addRow("默认语速", self.speed); form.addRow("文本语言", self.text_language); form.addRow("", test)
        return tab

    def _manage_models(self, model_type: str) -> None:
        if not self.editing_id:
            CharacterNoticeDialog("请先保存角色", "新建角色保存后，才能将共享模型关联到该角色。", self).exec()
            return
        dialog = SharedModelDialog(self.manager, model_type, self.editing_id, self)
        dialog.models_changed.connect(lambda: self._refresh_default_model(model_type))
        dialog.models_changed.connect(lambda: self.saved.emit(self.editing_id))
        dialog.exec()

    def _refresh_default_model(self, model_type: str) -> None:
        current = self.qwen_edit if model_type == "qwen3" else self.tts_edit
        available = {model.model_id for model in self.manager.list_shared_models(model_type) if self.editing_id in model.character_ids}
        manifest = self.manager._load_model_manifest(model_type, self.editing_id)
        active = str(manifest.get("active_model_id") or "")
        current.setText(active if active in available else "")

    def _references_tab(self) -> QWidget:
        tab = QWidget(); layout = QVBoxLayout(tab)
        hint = QLabel("每个参考音频将标准化为 16 kHz、16-bit、单声道 WAV。建议时长 3–10 秒，并填写准确文本。")
        hint.setWordWrap(True); hint.setObjectName("dialogHint"); layout.addWidget(hint)
        self.reference_list = QListWidget(); self.reference_list.setObjectName("referenceVoiceList"); self.reference_list.currentTextChanged.connect(self._show_reference)
        layout.addWidget(self.reference_list, 1)
        self.reference_text = QTextEdit(); self.reference_text.setPlaceholderText("此参考音频对应的准确文本")
        layout.addWidget(self.reference_text)
        actions = QHBoxLayout(); add = QPushButton("添加 / 替换"); add.clicked.connect(self._choose_reference)
        remove = QPushButton("移除"); remove.clicked.connect(self._remove_reference)
        preview = QPushButton("试听"); preview.clicked.connect(self._preview_reference)
        apply_text = QPushButton("更新文本"); apply_text.clicked.connect(self._apply_reference_text)
        for button in (add, remove, preview, apply_text): actions.addWidget(button)
        layout.addLayout(actions); self._refresh_references()
        return tab

    def _set_combo(self, combo: QComboBox, value: str) -> None:
        index = combo.findText(value)
        if index >= 0: combo.setCurrentIndex(index)

    def _choose_avatar(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择头像", "", "Images (*.png *.jpg *.jpeg *.webp)")
        if path: self.avatar_edit.setText(path)

    def _choose_reference(self) -> None:
        emotion, ok = QFileDialog.getOpenFileName(self, "选择参考音频", "", "WAV audio (*.wav)")
        if not ok or not emotion: return
        current = self.reference_list.currentItem().text() if self.reference_list.currentItem() else "neutral"
        # A compact naming prompt avoids hard-coded emotion choices.
        from PySide6.QtWidgets import QInputDialog
        name, accepted = QInputDialog.getText(self, "情绪名称", "情绪：", text=current)
        if not accepted or not name.strip(): return
        try:
            self.manager.add_reference(self.draft, name, emotion, self.reference_text.toPlainText())
            self._refresh_references(name.strip().lower())
        except (OSError, ValueError, wave.Error) as exc:
            QMessageBox.warning(self, "参考音频", str(exc))

    def _show_reference(self, emotion: str) -> None:
        ref = self.draft.references.get(emotion)
        self.reference_text.setPlainText(ref.text if ref else "")

    def _apply_reference_text(self) -> None:
        item = self.reference_list.currentItem()
        if item and item.text() in self.draft.references:
            self.draft.references[item.text()].text = self.reference_text.toPlainText().strip()

    def _remove_reference(self) -> None:
        item = self.reference_list.currentItem()
        if item: self.draft.references.pop(item.text(), None); self._refresh_references()

    def _preview_reference(self) -> None:
        item = self.reference_list.currentItem()
        ref = self.draft.references.get(item.text()) if item else None
        if not ref or not ref.filename or not Path(ref.filename).is_file():
            QMessageBox.information(self, "试听", "请先选择有效的参考音频。")
            return
        self._player.setSource(QUrl.fromLocalFile(str(Path(ref.filename).resolve()))); self._player.play()

    def _test_tts(self) -> None:
        QMessageBox.information(self, "TTS 测试", "保存后请在使用界面启动模型，再用中性参考音频发起语音回复测试。")

    def _refresh_references(self, selected: str = "") -> None:
        self.reference_list.clear()
        for emotion in sorted(self.draft.references):
            ref = self.draft.references[emotion]
            suffix = " · 已配置" if ref.filename else " · 未配置"
            self.reference_list.addItem(emotion + suffix)
        # Preserve the plain emotion as item data-free label for simple editing.
        for index in range(self.reference_list.count()):
            label = self.reference_list.item(index).text(); emotion = label.split(" · ", 1)[0]
            self.reference_list.item(index).setText(emotion)
            if emotion == selected: self.reference_list.setCurrentRow(index)
        if self.reference_list.count() and self.reference_list.currentRow() < 0: self.reference_list.setCurrentRow(0)

    def _collect(self) -> None:
        self._apply_reference_text()
        d = self.draft; d.character_id = self.id_edit.text().strip(); d.display_name = self.name_edit.text().strip(); d.description = self.description_edit.toPlainText().strip()
        d.language = self.language_combo.currentText(); d.reply_mode = self.reply_combo.currentText(); d.history_turns = self.history_spin.value(); d.memory_enabled = self.memory_check.isChecked(); d.avatar_source = self.avatar_edit.text().strip()
        d.qwen_model = self.qwen_edit.text().strip(); d.system_prompt = self.prompt_edit.toPlainText().strip(); d.personality = self.personality_edit.toPlainText().strip(); d.speaking_style = self.style_edit.toPlainText().strip(); d.forbidden_behavior = self.forbidden_edit.toPlainText().strip()
        d.temperature = self.temperature.value(); d.top_p = self.top_p.value(); d.max_tokens = self.max_tokens.value(); d.tts_model = self.tts_edit.text().strip(); d.default_emotion = self.emotion_edit.text().strip() or "neutral"; d.speed = self.speed.value(); d.text_language = self.text_language.currentText()

    def _save(self) -> None:
        self._collect(); validation = self.manager.validate(self.draft, editing_id=self.editing_id)
        if validation.errors:
            QMessageBox.warning(self, "无法保存", "\n".join(validation.errors)); return
        if validation.warnings and QMessageBox.question(self, "保存未完成角色", "\n".join(validation.warnings) + "\n\n仍要保存吗？") != QMessageBox.StandardButton.Yes: return
        try:
            self.manager.save(self.draft, editing_id=self.editing_id, allow_incomplete=True)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "保存失败", str(exc)); return
        self.saved.emit(self.draft.character_id); self.accept()
