from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

from desktop.state import Workspace


class LeftNavigation(QFrame):
    workspace_requested = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("leftNavigation")
        self.setMinimumWidth(176)
        self.setMaximumWidth(220)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 24, 18, 18)
        layout.setSpacing(10)

        mark = QLabel("✦")
        mark.setObjectName("brandMark")
        name = QLabel("Alysia Studio")
        name.setObjectName("brandName")
        caption = QLabel("LOCAL COMPANION")
        caption.setObjectName("brandCaption")
        layout.addWidget(mark)
        layout.addWidget(name)
        layout.addWidget(caption)
        layout.addSpacing(24)

        self._buttons: dict[Workspace, QPushButton] = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for workspace, icon, label in (
            (Workspace.CHAT, "✧", "使用界面"),
            (Workspace.TRAINING, "◇", "训练界面"),
        ):
            button = QPushButton(f"{icon}   {label}")
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.clicked.connect(lambda _checked=False, value=workspace: self.workspace_requested.emit(value))
            group.addButton(button)
            self._buttons[workspace] = button
            layout.addWidget(button)

        self._buttons[Workspace.CHAT].setChecked(True)
        layout.addStretch(1)

        version = QLabel("Preview · Stage 11")
        version.setObjectName("versionLabel")
        layout.addWidget(version)

    def select_workspace(self, workspace: Workspace) -> None:
        self._buttons[workspace].setChecked(True)
