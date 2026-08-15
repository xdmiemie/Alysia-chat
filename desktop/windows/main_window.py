from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Slot
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from desktop.controllers import AppController
from desktop.pages import ChatPage, TrainingPage
from desktop.panels import CharacterStatusPanel, LeftNavigation
from desktop.state import AppSnapshot, Workspace
from desktop.widgets import ResizeHandle, TitleBar
from desktop.windows.selection_dialogs import CharacterSelectionDialog, ModelSelectionDialog
from desktop.windows.settings_dialog import SettingsDialog


class MainWindow(QMainWindow):
    def __init__(self, controller: AppController, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setObjectName("mainWindow")
        self.setWindowTitle("Alysia Studio")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setMinimumSize(1280, 720)
        self.resize(1440, 900)

        frame = QWidget()
        frame.setObjectName("customWindowFrame")
        frame_layout = QGridLayout(frame)
        frame_layout.setContentsMargins(0, 0, 0, 0)
        frame_layout.setSpacing(0)
        frame_layout.setColumnStretch(1, 1)
        frame_layout.setRowStretch(1, 1)
        self._resize_handles: list[ResizeHandle] = []

        for row, column, edges in (
            (0, 0, Qt.Edge.TopEdge | Qt.Edge.LeftEdge),
            (0, 1, Qt.Edge.TopEdge),
            (0, 2, Qt.Edge.TopEdge | Qt.Edge.RightEdge),
            (1, 0, Qt.Edge.LeftEdge),
            (1, 2, Qt.Edge.RightEdge),
            (2, 0, Qt.Edge.BottomEdge | Qt.Edge.LeftEdge),
            (2, 1, Qt.Edge.BottomEdge),
            (2, 2, Qt.Edge.BottomEdge | Qt.Edge.RightEdge),
        ):
            handle = ResizeHandle(edges)
            if column != 1:
                handle.setFixedWidth(5)
            if row != 1:
                handle.setFixedHeight(5)
            self._resize_handles.append(handle)
            frame_layout.addWidget(handle, row, column)

        surface = QWidget()
        surface.setObjectName("windowSurface")
        surface_layout = QVBoxLayout(surface)
        surface_layout.setContentsMargins(0, 0, 0, 0)
        surface_layout.setSpacing(0)
        self.title_bar = TitleBar()
        surface_layout.addWidget(self.title_bar)

        body = QWidget()
        body.setObjectName("windowBody")
        layout = QHBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.navigation = LeftNavigation()
        self.pages = QStackedWidget()
        self.pages.setObjectName("workspaceStack")
        self.chat_page = ChatPage()
        self.training_page = TrainingPage()
        self.pages.addWidget(self.chat_page)
        self.pages.addWidget(self.training_page)
        self.character_panel = CharacterStatusPanel()
        layout.addWidget(self.navigation)
        layout.addWidget(self.pages, 1)
        layout.addWidget(self.character_panel)
        surface_layout.addWidget(body, 1)
        frame_layout.addWidget(surface, 1, 1)
        self.setCentralWidget(frame)

        self.navigation.workspace_requested.connect(self.controller.set_workspace)
        self.character_panel.settings_requested.connect(self.open_settings)
        self.character_panel.log_requested.connect(self.controller.request_log)
        self.character_panel.character_switch_requested.connect(self.open_character_selector)
        self.character_panel.model_switch_requested.connect(self.open_model_selector)
        self.controller.log_requested.connect(self.open_logs)
        self.controller.state_changed.connect(self.apply_state)

        QShortcut(QKeySequence("Ctrl+,"), self, activated=self.open_settings)
        QShortcut(QKeySequence("Ctrl+1"), self, activated=lambda: self.controller.set_workspace(Workspace.CHAT))
        QShortcut(
            QKeySequence("Ctrl+2"), self, activated=lambda: self.controller.set_workspace(Workspace.TRAINING)
        )
        self.apply_state(self.controller.state.snapshot)

    @Slot(object)
    def apply_state(self, snapshot: AppSnapshot) -> None:
        index = 0 if snapshot.workspace is Workspace.CHAT else 1
        self.pages.setCurrentIndex(index)
        self.navigation.select_workspace(snapshot.workspace)
        self.chat_page.apply_state(snapshot)
        self.training_page.apply_state(snapshot)
        self.character_panel.apply_state(snapshot)

    @Slot()
    def open_settings(self) -> None:
        SettingsDialog(self.controller.state.snapshot.log_path, self).exec()

    @Slot(str)
    def open_logs(self, log_path: str) -> None:
        SettingsDialog(log_path, self, show_logs=True).exec()

    @Slot()
    def open_character_selector(self) -> None:
        profiles = self.controller.available_characters()
        if not profiles:
            QMessageBox.warning(self, "切换角色", "没有找到可用角色。")
            return
        dialog = CharacterSelectionDialog(
            profiles,
            self.controller.state.snapshot.character_id,
            self,
        )
        if dialog.exec():
            self.controller.switch_character(dialog.selected_character_id)

    @Slot()
    def open_model_selector(self) -> None:
        models = self.controller.available_models()
        if not models:
            QMessageBox.warning(self, "切换模型", "当前角色没有已注册模型。")
            return
        snapshot = self.controller.state.snapshot
        dialog = ModelSelectionDialog(models, snapshot.qwen_model, snapshot.tts_model, self)
        if dialog.exec():
            self.controller.switch_models(
                qwen_model=dialog.selected_qwen_model,
                tts_model=dialog.selected_tts_model,
            )

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        self.controller.shutdown()
        event.accept()

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt API
        if event.type() is QEvent.Type.WindowStateChange:
            self.title_bar.sync_window_state()
            visible = not self.isMaximized()
            for handle in self._resize_handles:
                handle.setVisible(visible)
        super().changeEvent(event)
