from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout

from desktop.state import AppSnapshot, AppStatus
from desktop.widgets import StatusBadge


class CharacterStatusPanel(QFrame):
    settings_requested = Signal()
    log_requested = Signal()
    character_switch_requested = Signal()
    model_switch_requested = Signal()
    model_service_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("characterPanel")
        self.setMinimumWidth(258)
        self.setMaximumWidth(330)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 28, 24, 22)
        layout.setSpacing(12)

        eyebrow = QLabel("CURRENT CHARACTER")
        eyebrow.setObjectName("eyebrow")
        layout.addWidget(eyebrow)

        self.avatar = QLabel("爱")
        self.avatar.setObjectName("characterAvatar")
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.avatar.setFixedSize(88, 88)
        layout.addWidget(self.avatar, alignment=Qt.AlignmentFlag.AlignHCenter)

        self.character_name = QLabel("爱莉希雅")
        self.character_name.setObjectName("characterName")
        self.character_name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.character_name)

        self.status_badge = StatusBadge()
        self.status_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.status_badge)

        self.detail_label = QLabel("模型服务将在使用时按需启动")
        self.detail_label.setObjectName("statusDetail")
        self.detail_label.setWordWrap(True)
        self.detail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.detail_label)
        layout.addSpacing(12)

        model_card = QFrame()
        model_card.setObjectName("modelCard")
        model_layout = QVBoxLayout(model_card)
        model_layout.setContentsMargins(16, 14, 16, 14)
        model_layout.setSpacing(7)
        model_title = QLabel("当前模型")
        model_title.setObjectName("cardTitle")
        self.qwen_label = QLabel("Qwen3  ·  按需加载")
        self.qwen_label.setObjectName("modelValue")
        self.qwen_label.setWordWrap(True)
        self.tts_label = QLabel("语音  ·  按需加载")
        self.tts_label.setObjectName("modelValue")
        self.tts_label.setWordWrap(True)
        model_layout.addWidget(model_title)
        model_layout.addWidget(self.qwen_label)
        model_layout.addWidget(self.tts_label)
        layout.addWidget(model_card)

        self.error_card = QFrame()
        self.error_card.setObjectName("errorCard")
        error_layout = QVBoxLayout(self.error_card)
        error_layout.setContentsMargins(14, 12, 14, 12)
        self.error_label = QLabel()
        self.error_label.setObjectName("errorText")
        self.error_label.setWordWrap(True)
        self.view_log_button = QPushButton("查看日志")
        self.view_log_button.setObjectName("secondaryButton")
        self.view_log_button.clicked.connect(self.log_requested)
        error_layout.addWidget(self.error_label)
        error_layout.addWidget(self.view_log_button)
        self.error_card.hide()
        layout.addWidget(self.error_card)
        layout.addStretch(1)

        self.switch_character_button = QPushButton("♙  切换角色")
        self.switch_character_button.setObjectName("secondaryButton")
        self.switch_character_button.clicked.connect(self.character_switch_requested)
        self.switch_model_button = QPushButton("⇄  切换模型")
        self.switch_model_button.setObjectName("secondaryButton")
        self.switch_model_button.clicked.connect(self.model_switch_requested)
        self.model_service_button = QPushButton("▶  启动模型")
        self.model_service_button.setObjectName("primaryButton")
        self.model_service_button.clicked.connect(self.model_service_requested)
        self.call_button = QPushButton("♬  发起语音通话")
        self.call_button.setObjectName("primaryButton")
        self.call_button.setToolTip("语音通话将在 Stage 14 开放")
        self.call_button.setEnabled(False)
        self.settings_button = QPushButton("⚙  设置")
        self.settings_button.setObjectName("secondaryButton")
        self.settings_button.clicked.connect(self.settings_requested)
        layout.addWidget(self.switch_character_button)
        layout.addWidget(self.switch_model_button)
        layout.addWidget(self.model_service_button)
        layout.addWidget(self.call_button)
        layout.addWidget(self.settings_button)

    def apply_state(self, snapshot: AppSnapshot) -> None:
        self.character_name.setText(snapshot.character_name)
        self.avatar.setText((snapshot.character_name or "A")[0])
        self.status_badge.set_status(snapshot.status)
        self.detail_label.setText(snapshot.status_detail or snapshot.status.display_name)
        self.qwen_label.setText(f"Qwen3  ·  {snapshot.qwen_model}")
        self.tts_label.setText(f"语音  ·  {snapshot.tts_model}")
        self.error_label.setText(snapshot.error_summary)
        self.error_card.setVisible(snapshot.status is AppStatus.ERROR)
        can_change_runtime = snapshot.controls_enabled and not snapshot.model_service_running
        self.switch_character_button.setEnabled(can_change_runtime)
        self.switch_model_button.setEnabled(can_change_runtime)
        self.model_service_button.setText(
            "■  关闭模型" if snapshot.model_service_running else "▶  启动模型"
        )
        self.model_service_button.setEnabled(snapshot.controls_enabled)
