from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from desktop.state import AppSnapshot
from desktop.widgets import MessageBubble
from runtime.conversation import Conversation, Message


class ChatInput(QTextEdit):
    submit_requested = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt API
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter} and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.submit_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class ChatPage(QWidget):
    send_requested = Signal(str)
    retry_requested = Signal(str)
    conversation_requested = Signal(str)
    new_conversation_requested = Signal()
    rename_requested = Signal(str)
    delete_requested = Signal()
    clear_context_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("chatPage")
        self._snapshot = AppSnapshot()
        self._conversations: list[Conversation] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(30, 26, 30, 24)
        root.setSpacing(14)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        title = QLabel("使用界面")
        title.setObjectName("pageTitle")
        self.subtitle = QLabel("本地文字聊天")
        self.subtitle.setObjectName("pageSubtitle")
        titles.addWidget(title)
        titles.addWidget(self.subtitle)
        header.addLayout(titles)
        header.addStretch(1)
        privacy = QLabel("●  本地运行")
        privacy.setObjectName("localBadge")
        header.addWidget(privacy)
        root.addLayout(header)

        history_bar = QFrame()
        history_bar.setObjectName("conversationBar")
        history_layout = QHBoxLayout(history_bar)
        history_layout.setContentsMargins(12, 8, 12, 8)
        history_layout.setSpacing(8)
        history_label = QLabel("会话")
        history_label.setObjectName("fieldLabel")
        self.conversation_combo = QComboBox()
        self.conversation_combo.setObjectName("conversationCombo")
        self.conversation_combo.setMinimumWidth(240)
        self.conversation_combo.currentIndexChanged.connect(self._conversation_changed)
        self.new_button = QPushButton("＋ 新建")
        self.new_button.setObjectName("compactButton")
        self.rename_button = QPushButton("重命名")
        self.rename_button.setObjectName("compactButton")
        self.delete_button = QPushButton("删除")
        self.delete_button.setObjectName("compactButton")
        self.clear_button = QPushButton("清空上下文")
        self.clear_button.setObjectName("compactButton")
        self.new_button.clicked.connect(self.new_conversation_requested)
        self.rename_button.clicked.connect(self._request_rename)
        self.delete_button.clicked.connect(self._request_delete)
        self.clear_button.clicked.connect(self.clear_context_requested)
        history_layout.addWidget(history_label)
        history_layout.addWidget(self.conversation_combo, 1)
        history_layout.addWidget(self.new_button)
        history_layout.addWidget(self.rename_button)
        history_layout.addWidget(self.delete_button)
        history_layout.addWidget(self.clear_button)
        root.addWidget(history_bar)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("messageScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content = QWidget()
        self.content.setObjectName("messageCanvas")
        self.message_layout = QVBoxLayout(self.content)
        self.message_layout.setContentsMargins(12, 18, 12, 18)
        self.message_layout.setSpacing(14)
        self.message_layout.addStretch(1)
        self.scroll.setWidget(self.content)
        root.addWidget(self.scroll, 1)

        composer = QFrame()
        composer.setObjectName("composer")
        composer_layout = QHBoxLayout(composer)
        composer_layout.setContentsMargins(10, 8, 10, 8)
        self.input = ChatInput()
        self.input.setObjectName("chatInput")
        self.input.setPlaceholderText("输入消息；Enter 发送，Shift+Enter 换行")
        self.input.setAcceptRichText(False)
        self.input.setMinimumHeight(46)
        self.input.setMaximumHeight(110)
        self.input.textChanged.connect(self._sync_send_button)
        self.input.submit_requested.connect(self._submit)
        self.voice_button = QPushButton("◉")
        self.voice_button.setObjectName("iconButton")
        self.voice_button.setToolTip("语音气泡将在 Stage 13 开放")
        self.voice_button.setEnabled(False)
        self.send_button = QPushButton("发送  ↑")
        self.send_button.setObjectName("sendButton")
        self.send_button.clicked.connect(self._submit)
        composer_layout.addWidget(self.voice_button)
        composer_layout.addWidget(self.input, 1)
        composer_layout.addWidget(self.send_button)
        root.addWidget(composer)
        self.apply_state(self._snapshot)

    def apply_state(self, snapshot: AppSnapshot) -> None:
        self._snapshot = snapshot
        self.subtitle.setText(f"与{snapshot.character_name}的本地会话")
        self.input.setEnabled(snapshot.chat_enabled)
        self.input.setPlaceholderText(
            "输入消息；Enter 发送，Shift+Enter 换行"
            if snapshot.model_service_running
            else "请先在右侧点击“启动模型”"
        )
        for button in (
            self.new_button,
            self.rename_button,
            self.delete_button,
            self.clear_button,
            self.conversation_combo,
        ):
            button.setEnabled(snapshot.controls_enabled)
        self._sync_send_button()

    def set_conversations(self, conversations: list[Conversation], current_id: str) -> None:
        self._conversations = list(conversations)
        blocker = QSignalBlocker(self.conversation_combo)
        self.conversation_combo.clear()
        for conversation in conversations:
            self.conversation_combo.addItem(conversation.title, conversation.id)
        index = self.conversation_combo.findData(current_id)
        if index >= 0:
            self.conversation_combo.setCurrentIndex(index)
        del blocker

    def set_messages(self, messages: list[Message]) -> None:
        while self.message_layout.count():
            item = self.message_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            child_layout = item.layout()
            if child_layout is not None:
                while child_layout.count():
                    child = child_layout.takeAt(0)
                    if child.widget() is not None:
                        child.widget().deleteLater()

        if not messages:
            welcome_row = QHBoxLayout()
            welcome = MessageBubble(
                self._snapshot.character_name,
                "新的会话已经准备好。想聊点什么？",
                avatar=self._snapshot.character_name,
            )
            welcome_row.addWidget(welcome)
            welcome_row.addStretch(1)
            self.message_layout.addLayout(welcome_row)
        for message in messages:
            outgoing = message.role == "user"
            sender = "你" if outgoing else self._snapshot.character_name
            row = QHBoxLayout()
            bubble = MessageBubble(
                sender,
                message.text,
                outgoing=outgoing,
                created_at=message.created_at,
                status=message.status,
                model_version=message.model_version,
                request_id=message.request_id if not outgoing else "",
                message_id=message.id,
                avatar=self._snapshot.character_name,
            )
            bubble.retry_requested.connect(self.retry_requested)
            if outgoing:
                row.addStretch(1)
                row.addWidget(bubble)
            else:
                row.addWidget(bubble)
                row.addStretch(1)
            self.message_layout.addLayout(row)
        self.message_layout.addStretch(1)
        self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum())

    def _submit(self) -> None:
        text = self.input.toPlainText().strip()
        if text and self._snapshot.chat_enabled:
            self.input.clear()
            self.send_requested.emit(text)

    def _sync_send_button(self) -> None:
        self.send_button.setEnabled(
            self._snapshot.chat_enabled and bool(self.input.toPlainText().strip())
        )

    def _conversation_changed(self, index: int) -> None:
        conversation_id = self.conversation_combo.itemData(index) if index >= 0 else ""
        if conversation_id:
            self.conversation_requested.emit(str(conversation_id))

    def _request_rename(self) -> None:
        current = self.conversation_combo.currentText()
        title, accepted = QInputDialog.getText(self, "重命名对话", "对话名称", text=current)
        if accepted and title.strip():
            self.rename_requested.emit(title.strip())

    def _request_delete(self) -> None:
        if QMessageBox.question(
            self,
            "删除对话",
            "确定删除当前对话及其全部消息吗？此操作不可撤销。",
        ) == QMessageBox.StandardButton.Yes:
            self.delete_requested.emit()
