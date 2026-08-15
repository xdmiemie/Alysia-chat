from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout


class MessageBubble(QFrame):
    def __init__(self, sender: str, text: str, *, outgoing: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("messageBubbleOutgoing" if outgoing else "messageBubble")
        self.setMaximumWidth(720)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 13, 18, 13)
        layout.setSpacing(5)
        sender_label = QLabel(sender)
        sender_label.setObjectName("bubbleSender")
        body = QLabel(text)
        body.setObjectName("bubbleText")
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(sender_label)
        layout.addWidget(body)
