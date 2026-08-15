from __future__ import annotations

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QPlainTextEdit


class LogConsole(QPlainTextEdit):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("logConsole")
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        fixed.setPointSize(10)
        self.setFont(fixed)
        self.setPlaceholderText("任务日志会显示在这里，可选择并复制。")

    def append_log(self, text: str) -> None:
        self.appendPlainText(text.rstrip())
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())
