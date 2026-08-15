from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPaintEvent, QPainter, QPolygonF
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QLabel,
    QVBoxLayout,
)


class SelectionComboBox(QComboBox):
    """Dark combo box with a consistently visible dropdown indicator."""

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt API
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#d9d4e6") if self.isEnabled() else QColor("#6f6c7e"))
        center_y = self.height() / 2 + 1
        painter.drawPolygon(
            QPolygonF(
                [
                    QPointF(self.width() - 21, center_y - 3),
                    QPointF(self.width() - 13, center_y - 3),
                    QPointF(self.width() - 17, center_y + 2),
                ]
            )
        )


class CharacterSelectionDialog(QDialog):
    def __init__(
        self,
        profiles: list[tuple[str, str]],
        current_id: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("selectionDialog")
        self.setWindowTitle("切换角色")
        self.setModal(True)
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        title = QLabel("选择角色")
        title.setObjectName("dialogTitle")
        hint = QLabel("每个角色拥有独立的会话和模型配置。")
        hint.setObjectName("dialogHint")
        layout.addWidget(title)
        layout.addWidget(hint)

        self.character_combo = SelectionComboBox()
        self.character_combo.setObjectName("selectionCombo")
        self.character_combo.setMinimumHeight(44)
        for character_id, name in profiles:
            self.character_combo.addItem(f"{name}  ({character_id})", character_id)
        current_index = self.character_combo.findData(current_id)
        if current_index >= 0:
            self.character_combo.setCurrentIndex(current_index)
        layout.addWidget(self.character_combo)
        layout.addSpacing(8)
        layout.addWidget(_dialog_buttons(self))

    @property
    def selected_character_id(self) -> str:
        return str(self.character_combo.currentData())


class ModelSelectionDialog(QDialog):
    def __init__(
        self,
        models: list[tuple[str, str, str]],
        current_qwen: str,
        current_tts: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("selectionDialog")
        self.setWindowTitle("切换模型")
        self.setModal(True)
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        title = QLabel("选择运行模型")
        title.setObjectName("dialogTitle")
        hint = QLabel("Qwen3 与 TTS 模型可分别选择，确认后将在使用时按需加载。")
        hint.setObjectName("dialogHint")
        hint.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addSpacing(4)

        self.qwen_combo = self._model_combo(
            layout,
            "Qwen3 对话模型",
            [(model_id, label.removeprefix("Qwen3 · ")) for kind, model_id, label in models if kind == "qwen"],
            current_qwen,
        )
        self.tts_combo = self._model_combo(
            layout,
            "GPT-SoVITS / TTS 模型",
            [(model_id, label.removeprefix("GPT-SoVITS · ")) for kind, model_id, label in models if kind == "tts"],
            current_tts,
        )
        layout.addSpacing(8)
        layout.addWidget(_dialog_buttons(self))

    @staticmethod
    def _model_combo(
        layout: QVBoxLayout,
        label: str,
        choices: list[tuple[str, str]],
        current_id: str,
    ) -> QComboBox:
        field = QFrame()
        field.setObjectName("selectionField")
        field_layout = QVBoxLayout(field)
        field_layout.setContentsMargins(14, 12, 14, 14)
        field_layout.setSpacing(8)
        field_label = QLabel(label)
        field_label.setObjectName("fieldLabel")
        combo = SelectionComboBox()
        combo.setObjectName("selectionCombo")
        combo.setMinimumHeight(44)
        if choices:
            for model_id, display_name in choices:
                combo.addItem(display_name, model_id)
            current_index = combo.findData(current_id)
            if current_index >= 0:
                combo.setCurrentIndex(current_index)
        else:
            combo.addItem("没有已注册模型", "")
            combo.setEnabled(False)
        field_layout.addWidget(field_label)
        field_layout.addWidget(combo)
        layout.addWidget(field)
        return combo

    @property
    def selected_qwen_model(self) -> str:
        return str(self.qwen_combo.currentData() or "")

    @property
    def selected_tts_model(self) -> str:
        return str(self.tts_combo.currentData() or "")


def _dialog_buttons(dialog: QDialog) -> QDialogButtonBox:
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
    )
    buttons.setObjectName("selectionButtons")
    cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
    confirm = buttons.button(QDialogButtonBox.StandardButton.Ok)
    cancel.setText("取消")
    cancel.setObjectName("dialogCancelButton")
    confirm.setText("确认")
    confirm.setObjectName("dialogConfirmButton")
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    return buttons
