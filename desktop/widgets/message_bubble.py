from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout


class MessageBubble(QFrame):
    retry_requested = Signal(str)

    def __init__(
        self,
        sender: str,
        text: str,
        *,
        outgoing: bool = False,
        created_at: int | None = None,
        status: str = "completed",
        model_version: str = "",
        request_id: str = "",
        message_id: str = "",
        avatar: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("messageBubbleOutgoing" if outgoing else "messageBubble")
        self.setMaximumWidth(720)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 13, 18, 13)
        layout.setSpacing(5)
        header = QHBoxLayout()
        sender_label = QLabel(f"{avatar[:1]}  {sender}" if avatar and not outgoing else sender)
        sender_label.setObjectName("bubbleSender")
        header.addWidget(sender_label)
        header.addStretch(1)
        if created_at:
            timestamp = datetime.fromtimestamp(created_at / 1000).strftime("%H:%M")
            time_label = QLabel(timestamp)
            time_label.setObjectName("bubbleMeta")
            header.addWidget(time_label)
        body = QLabel(text)
        body.setObjectName("bubbleText")
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addLayout(header)
        layout.addWidget(body)

        footer = QHBoxLayout()
        details = []
        if status == "failed":
            details.append("发送失败")
        if model_version:
            details.append(f"模型 {model_version}")
        if request_id:
            details.append(f"请求 {request_id}")
        if details:
            detail_label = QLabel("  ·  ".join(details))
            detail_label.setObjectName("bubbleMeta")
            detail_label.setToolTip("\n".join(details))
            footer.addWidget(detail_label)
        footer.addStretch(1)
        copy_button = QPushButton("复制")
        copy_button.setObjectName("bubbleAction")
        copy_button.clicked.connect(lambda: QApplication.clipboard().setText(text))
        footer.addWidget(copy_button)
        if status == "failed" and message_id:
            retry_button = QPushButton("重试")
            retry_button.setObjectName("bubbleAction")
            retry_button.clicked.connect(lambda: self.retry_requested.emit(message_id))
            footer.addWidget(retry_button)
        layout.addLayout(footer)
