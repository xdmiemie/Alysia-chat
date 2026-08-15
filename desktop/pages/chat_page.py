from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from desktop.state import AppSnapshot
from desktop.widgets import MessageBubble


class ChatPage(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("chatPage")
        root = QVBoxLayout(self)
        root.setContentsMargins(30, 26, 30, 24)
        root.setSpacing(16)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        title = QLabel("使用界面")
        title.setObjectName("pageTitle")
        self.subtitle = QLabel("与爱莉希雅的本地会话")
        self.subtitle.setObjectName("pageSubtitle")
        titles.addWidget(title)
        titles.addWidget(self.subtitle)
        header.addLayout(titles)
        header.addStretch(1)
        privacy = QLabel("●  本地运行")
        privacy.setObjectName("localBadge")
        header.addWidget(privacy)
        root.addLayout(header)

        scroll = QScrollArea()
        scroll.setObjectName("messageScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("messageCanvas")
        self.message_layout = QVBoxLayout(content)
        self.message_layout.setContentsMargins(12, 18, 12, 18)
        self.message_layout.setSpacing(14)
        welcome_row = QHBoxLayout()
        welcome = MessageBubble(
            "Alysia Studio",
            "欢迎回来。桌面应用骨架已就绪，角色模型会在真正使用时按需启动。",
        )
        welcome_row.addWidget(welcome)
        welcome_row.addStretch(1)
        self.message_layout.addLayout(welcome_row)
        self.message_layout.addStretch(1)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        composer = QFrame()
        composer.setObjectName("composer")
        composer_layout = QHBoxLayout(composer)
        composer_layout.setContentsMargins(10, 8, 10, 8)
        self.input = QLineEdit()
        self.input.setObjectName("chatInput")
        self.input.setPlaceholderText("文字聊天将在 Stage 12 开放")
        self.input.setEnabled(False)
        voice = QPushButton("◉")
        voice.setObjectName("iconButton")
        voice.setToolTip("语音气泡将在后续阶段开放")
        voice.setEnabled(False)
        send = QPushButton("发送  ↑")
        send.setObjectName("sendButton")
        send.setEnabled(False)
        composer_layout.addWidget(voice)
        composer_layout.addWidget(self.input, 1)
        composer_layout.addWidget(send)
        root.addWidget(composer)

    def apply_state(self, snapshot: AppSnapshot) -> None:
        self.subtitle.setText(f"与{snapshot.character_name}的本地会话")
