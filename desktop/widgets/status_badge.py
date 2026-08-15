from __future__ import annotations

from PySide6.QtWidgets import QLabel

from desktop.state import AppStatus


class StatusBadge(QLabel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("statusBadge")
        self.set_status(AppStatus.IDLE)

    def set_status(self, status: AppStatus) -> None:
        tone = "error" if status is AppStatus.ERROR else "busy" if status.busy else "idle"
        self.setProperty("tone", tone)
        self.setText(f"●  {status.display_name}")
        self.style().unpolish(self)
        self.style().polish(self)
