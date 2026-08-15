from __future__ import annotations

from PySide6.QtCore import QElapsedTimer, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from desktop.widgets import TitleBar, hangup_icon, microphone_icon


class VoiceCallDialog(QDialog):
    mute_changed = Signal(bool)
    volume_changed = Signal(int)
    end_requested = Signal()

    def __init__(self, character_name: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("voiceCallDialog")
        self.setWindowTitle(f"与{character_name}语音通话")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setModal(False)
        self.setMinimumSize(520, 650)
        self.resize(560, 700)
        self._ending = False
        self._elapsed = QElapsedTimer()
        self._elapsed.start()
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._update_duration)
        self._timer.start()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.title_bar = TitleBar(title=f"与{character_name}语音通话")
        root.addWidget(self.title_bar)
        content = QWidget()
        content.setObjectName("voiceCallContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(38, 24, 38, 30)
        content_layout.setSpacing(16)
        self.avatar = QLabel((character_name or "A")[0])
        self.avatar.setObjectName("callAvatar")
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.avatar.setFixedSize(132, 132)
        content_layout.addWidget(self.avatar, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.name_label = QLabel(character_name)
        self.name_label.setObjectName("callCharacterName")
        self.name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content_layout.addWidget(self.name_label)
        self.duration_label = QLabel("00:00")
        self.duration_label.setObjectName("callDuration")
        self.duration_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content_layout.addWidget(self.duration_label)
        self.status_label = QLabel("正在连接")
        self.status_label.setObjectName("callStatus")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content_layout.addWidget(self.status_label)

        self.level = QProgressBar()
        self.level.setObjectName("callLevel")
        self.level.setRange(0, 100)
        self.level.setTextVisible(False)
        content_layout.addWidget(self.level)

        transcript_card = QFrame()
        transcript_card.setObjectName("callTextCard")
        transcript_layout = QVBoxLayout(transcript_card)
        transcript_layout.addWidget(QLabel("我说"))
        self.transcript_label = QLabel("等待你开口…")
        self.transcript_label.setObjectName("callTranscript")
        self.transcript_label.setWordWrap(True)
        transcript_layout.addWidget(self.transcript_label)
        content_layout.addWidget(transcript_card)

        self.reply_toggle = QPushButton("角色回复  ▾")
        self.reply_toggle.setObjectName("callReplyToggle")
        self.reply_toggle.setCheckable(True)
        self.reply_toggle.setChecked(True)
        content_layout.addWidget(self.reply_toggle)
        self.reply_label = QLabel("等待回复…")
        self.reply_label.setObjectName("callReply")
        self.reply_label.setWordWrap(True)
        content_layout.addWidget(self.reply_label)
        self.reply_toggle.toggled.connect(self.reply_label.setVisible)
        content_layout.addStretch(1)

        volume_row = QHBoxLayout()
        volume_row.addWidget(QLabel("扬声器音量"))
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(100)
        self.volume_slider.valueChanged.connect(self.volume_changed)
        volume_row.addWidget(self.volume_slider, 1)
        content_layout.addLayout(volume_row)

        controls = QHBoxLayout()
        controls.addStretch(1)
        self.mute_button = QPushButton()
        self.mute_button.setObjectName("callMuteButton")
        self.mute_button.setFixedSize(58, 58)
        self.mute_button.setIcon(microphone_icon())
        self.mute_button.setIconSize(QSize(29, 29))
        self.mute_button.setToolTip("麦克风静音")
        self.mute_button.setAccessibleName("麦克风静音")
        self.mute_button.setCheckable(True)
        self.mute_button.toggled.connect(self._toggle_mute)
        self.end_button = QPushButton()
        self.end_button.setObjectName("callEndButton")
        self.end_button.setFixedSize(62, 62)
        self.end_button.setIcon(hangup_icon())
        self.end_button.setIconSize(QSize(32, 32))
        self.end_button.setToolTip("结束通话")
        self.end_button.setAccessibleName("结束通话")
        self.end_button.clicked.connect(self._request_end)
        controls.addWidget(self.mute_button)
        controls.addSpacing(24)
        controls.addWidget(self.end_button)
        controls.addStretch(1)
        content_layout.addLayout(controls)
        root.addWidget(content, 1)

    @Slot(str, str)
    def set_call_state(self, _state: str, detail: str) -> None:
        self.status_label.setText(detail)

    @Slot(float)
    def set_level(self, value: float) -> None:
        self.level.setValue(max(0, min(100, int(value * 100))))

    @Slot(str)
    def set_transcript(self, text: str) -> None:
        self.transcript_label.setText(text or "（未识别到文字）")

    @Slot(str)
    def set_reply(self, text: str) -> None:
        self.reply_label.setText(text or "（空回复）")

    @Slot(str)
    def show_recoverable_error(self, message: str) -> None:
        self.status_label.setText(message)

    @Slot(object)
    def finish_call(self, summary) -> None:
        self._ending = True
        self._timer.stop()
        self.level.setValue(0)
        self.mute_button.setEnabled(False)
        self.volume_slider.setEnabled(False)
        self.end_button.setToolTip("关闭")
        self.end_button.setAccessibleName("关闭")
        self.end_button.clicked.disconnect()
        self.end_button.clicked.connect(self.accept)
        turns = int(getattr(summary, "turn_count", 0))
        error = str(getattr(summary, "error", ""))
        self.status_label.setText(error or f"通话已结束，共 {turns} 个回合")

    def _toggle_mute(self, muted: bool) -> None:
        label = "恢复麦克风" if muted else "麦克风静音"
        self.mute_button.setIcon(microphone_icon(muted=muted))
        self.mute_button.setToolTip(label)
        self.mute_button.setAccessibleName(label)
        self.mute_changed.emit(muted)

    def _request_end(self) -> None:
        if self._ending:
            return
        self._ending = True
        self.end_requested.emit()
        self.accept()

    def _update_duration(self) -> None:
        seconds = max(0, self._elapsed.elapsed() // 1000)
        self.duration_label.setText(f"{seconds // 60:02d}:{seconds % 60:02d}")

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if not self._ending:
            self._request_end()
        event.accept()
