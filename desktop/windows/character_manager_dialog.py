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
        self.qwen_edit = QLineEdit(self.draft.qwen_model); self.qwen_edit.setPlaceholderText("训练完成后可填写该角色的模型 ID")
        self.prompt_edit = QTextEdit(self.draft.system_prompt); self.prompt_edit.setMinimumHeight(180)
        self.personality_edit = QTextEdit(self.draft.personality); self.personality_edit.setPlaceholderText("性格关键词或说明")
        self.style_edit = QTextEdit(self.draft.speaking_style); self.forbidden_edit = QTextEdit(self.draft.forbidden_behavior)
        self.temperature = QDoubleSpinBox(); self.temperature.setRange(0, 2); self.temperature.setSingleStep(.1); self.temperature.setValue(self.draft.temperature)
        self.top_p = QDoubleSpinBox(); self.top_p.setRange(.01, 1); self.top_p.setSingleStep(.05); self.top_p.setValue(self.draft.top_p)
        self.max_tokens = QSpinBox(); self.max_tokens.setRange(1, 8192); self.max_tokens.setValue(self.draft.max_tokens)
        form.addRow("默认运行模型", self.qwen_edit); form.addRow("System prompt", self.prompt_edit); form.addRow("Personality", self.personality_edit)
        form.addRow("Speaking style", self.style_edit); form.addRow("Forbidden behavior", self.forbidden_edit)
        form.addRow("Temperature", self.temperature); form.addRow("Top P", self.top_p); form.addRow("Max tokens", self.max_tokens)
        return tab

    def _voice_tab(self) -> QWidget:
        tab = QWidget(); form = QFormLayout(tab)
        self.tts_edit = QLineEdit(self.draft.tts_model); self.tts_edit.setPlaceholderText("训练完成后可填写该角色的模型 ID")
        self.emotion_edit = QLineEdit(self.draft.default_emotion)
        self.speed = QDoubleSpinBox(); self.speed.setRange(.25, 3); self.speed.setSingleStep(.05); self.speed.setValue(self.draft.speed)
        self.text_language = QComboBox(); self.text_language.addItems(["zh", "ja", "en"]); self._set_combo(self.text_language, self.draft.text_language)
        test = QPushButton("用中性参考语音测试 TTS"); test.clicked.connect(self._test_tts)
        form.addRow("默认 TTS 模型", self.tts_edit); form.addRow("默认情绪", self.emotion_edit); form.addRow("默认语速", self.speed); form.addRow("文本语言", self.text_language); form.addRow("", test)
        return tab

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
