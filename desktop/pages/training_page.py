from __future__ import annotations

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QPaintEvent, QPainter, QPolygonF
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from desktop.characters import CharacterManager
from desktop.state import AppSnapshot
from desktop.widgets import LogConsole


class TrainingPage(QWidget):
    characters_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("trainingPage")
        root = QVBoxLayout(self)
        root.setContentsMargins(30, 26, 30, 24)
        root.setSpacing(16)

        title = QLabel("训练界面")
        title.setObjectName("pageTitle")
        subtitle = QLabel("训练任务的配置与日志工作区")
        subtitle.setObjectName("pageSubtitle")
        root.addWidget(title)
        root.addWidget(subtitle)

        manager_header = QHBoxLayout()
        heading = QLabel("角色管理中心")
        heading.setObjectName("sectionTitle")
        manager_header.addWidget(heading)
        manager_header.addStretch(1)
        root.addLayout(manager_header)
        manager_hint = QLabel("创建和管理角色配置、头像、参考语音与本地角色资源。")
        manager_hint.setObjectName("pageSubtitle")
        root.addWidget(manager_hint)
        self.character_manager = CharacterManager(_project_root())
        self.character_combo = CharacterManagerComboBox()
        self.character_combo.setObjectName("characterManagerCombo")
        self._reload_characters()
        manager_actions = QHBoxLayout()
        manager_actions.addWidget(self.character_combo, 1)
        self.create_character_button = QPushButton("创建角色")
        self.create_character_button.setObjectName("createCharacterButton")
        self.edit_character_button = QPushButton("编辑")
        self.copy_character_button = QPushButton("复制")
        self.import_character_button = QPushButton("导入")
        self.export_character_button = QPushButton("导出")
        self.delete_character_button = QPushButton("删除")
        for button in (self.create_character_button, self.edit_character_button, self.copy_character_button, self.import_character_button, self.export_character_button, self.delete_character_button):
            button.setObjectName("secondaryButton")
            manager_actions.addWidget(button)
        root.addLayout(manager_actions)
        self.create_character_button.clicked.connect(lambda: self._open_editor())
        self.edit_character_button.clicked.connect(lambda: self._open_editor(self._selected_id()))
        self.copy_character_button.clicked.connect(self._copy_selected)
        self.import_character_button.clicked.connect(self._import_bundle)
        self.export_character_button.clicked.connect(self._export_selected)
        self.delete_character_button.clicked.connect(self._delete_selected)

        cards = QGridLayout()
        cards.setSpacing(14)
        cards.addWidget(self._task_card("Qwen3 角色训练", "数据集、LoRA 参数与导出流程", "Stage 16 开放"), 0, 0)
        cards.addWidget(self._task_card("GPT-SoVITS 训练", "参考音频、切片与两阶段训练", "Stage 17 开放"), 0, 1)
        root.addLayout(cards)

        log_header = QLabel("任务日志")
        log_header.setObjectName("sectionTitle")
        root.addWidget(log_header)
        self.log_console = LogConsole()
        self.log_console.setPlainText(
            "[ready] 训练控制台已就绪。\n"
            "[info] 后续训练任务将在独立 worker 中运行，不会阻塞桌面主线程。"
        )
        root.addWidget(self.log_console, 1)

    @staticmethod
    def _task_card(title: str, description: str, action: str) -> QFrame:
        card = QFrame()
        card.setObjectName("trainingCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 18, 18, 18)
        title_label = QLabel(title)
        title_label.setObjectName("cardHeading")
        body = QLabel(description)
        body.setObjectName("cardBody")
        body.setWordWrap(True)
        button = QPushButton(action)
        button.setObjectName("secondaryButton")
        button.setEnabled(False)
        layout.addWidget(title_label)
        layout.addWidget(body)
        layout.addStretch(1)
        layout.addWidget(button)
        return card

    def apply_state(self, _snapshot: AppSnapshot) -> None:
        self._active_character_id = _snapshot.character_id

    def _reload_characters(self, selected_id: str = "") -> None:
        self.character_combo.clear()
        for character_id, name in self.character_manager.list_profiles():
            self.character_combo.addItem(f"{name} ({character_id})", character_id)
        index = self.character_combo.findData(selected_id)
        if index >= 0:
            self.character_combo.setCurrentIndex(index)

    def _selected_id(self) -> str:
        return str(self.character_combo.currentData() or "")

    def _open_editor(self, character_id: str = "") -> None:
        # Imported lazily to avoid the MainWindow -> pages package import cycle.
        from desktop.windows.character_manager_dialog import CharacterManagerDialog
        dialog = CharacterManagerDialog(self.character_manager, character_id, self)
        dialog.saved.connect(self._on_saved)
        dialog.exec()

    def _on_saved(self, character_id: str) -> None:
        self._reload_characters(character_id)
        self.characters_changed.emit()

    def _copy_selected(self) -> None:
        from desktop.windows.character_manager_dialog import CharacterTextPromptDialog
        source = self._selected_id()
        if not source:
            return
        dialog = CharacterTextPromptDialog(self.tr("复制角色"), self.tr("新角色 ID："), f"{source}_copy", self)
        if not dialog.exec() or not dialog.value:
            return
        try:
            draft = self.character_manager.duplicate(source, dialog.value)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "复制角色", str(exc)); return
        self._on_saved(draft.character_id)

    def _import_bundle(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "导入角色配置包", "", "角色包 (*.zip)")
        if not path:
            return
        try:
            draft = self.character_manager.import_bundle(path)
        except FileExistsError as exc:
            QMessageBox.warning(self, "导入角色", f"{exc}\n请先删除同名角色后重试。")
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "导入角色", str(exc))
        else:
            self._on_saved(draft.character_id)

    def _export_selected(self) -> None:
        character_id = self._selected_id()
        if not character_id:
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出角色配置包", f"{character_id}.zip", "角色包 (*.zip)")
        if not path:
            return
        try:
            bundle = self.character_manager.export_bundle(character_id, path)
        except OSError as exc:
            QMessageBox.warning(self, "导出角色", str(exc)); return
        QMessageBox.information(self, "导出角色", f"已导出：{bundle}")

    def _delete_selected(self) -> None:
        from desktop.windows.character_manager_dialog import CharacterConfirmDialog, CharacterNoticeDialog
        character_id = self._selected_id()
        if not character_id:
            return
        if character_id == getattr(self, "_active_character_id", ""):
            CharacterNoticeDialog(
                self.tr("删除角色"),
                self.tr("请先在使用界面切换到其他角色，再删除当前角色。"),
                self,
            ).exec()
            return
        dialog = CharacterConfirmDialog(
            self.tr("删除角色"),
            self.tr(f"确认删除角色“{character_id}”及其本地资源？\n共享基础模型不会被删除。"),
            self,
        )
        if not dialog.exec():
            return
        try:
            self.character_manager.delete(character_id)
        except OSError as exc:
            QMessageBox.warning(self, "删除角色", str(exc)); return
        self._reload_characters(); self.characters_changed.emit()


def _project_root():
    from pathlib import Path
    return Path(__file__).resolve().parents[2]


class CharacterManagerComboBox(QComboBox):
    """Keeps the dropdown affordance visible on the custom dark control."""

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt API
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#d9d4e6") if self.isEnabled() else QColor("#6f6c7e"))
        center_x = self.width() - 16
        center_y = self.height() / 2 + 1
        painter.drawPolygon(
            QPolygonF(
                [
                    QPointF(center_x - 4, center_y - 3),
                    QPointF(center_x + 4, center_y - 3),
                    QPointF(center_x, center_y + 2),
                ]
            )
        )
