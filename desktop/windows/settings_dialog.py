from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QTabWidget, QVBoxLayout, QWidget

from desktop.widgets import LogConsole


class SettingsDialog(QDialog):
    def __init__(self, log_path: str, parent=None, *, show_logs: bool = False) -> None:
        super().__init__(parent)
        self.setObjectName("settingsDialog")
        self.setWindowTitle("Alysia Studio 设置")
        self.resize(720, 520)
        root = QVBoxLayout(self)
        tabs = QTabWidget()

        general = QWidget()
        general_layout = QVBoxLayout(general)
        general_layout.setContentsMargins(20, 20, 20, 20)
        general_layout.addWidget(QLabel("模型服务采用按需启动；角色与模型管理将在后续阶段加入。"))
        general_layout.addStretch(1)
        tabs.addTab(general, "常规")

        diagnostics = QWidget()
        diagnostics_layout = QVBoxLayout(diagnostics)
        diagnostics_layout.setContentsMargins(12, 12, 12, 12)
        path_label = QLabel(f"日志文件：{log_path}")
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        diagnostics_layout.addWidget(path_label)
        console = LogConsole()
        try:
            console.setPlainText(Path(log_path).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            console.setPlainText("日志文件尚未生成。")
        diagnostics_layout.addWidget(console, 1)
        tabs.addTab(diagnostics, "诊断与日志")
        if show_logs:
            tabs.setCurrentWidget(diagnostics)

        root.addWidget(tabs)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
