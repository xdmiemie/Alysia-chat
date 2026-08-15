from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
)

from runtime.conversation import Message


class VoiceMessageBubble(QFrame):
    play_requested = Signal(str, str)
    seek_requested = Signal(str, int)
    regenerate_requested = Signal(str)

    def __init__(
        self,
        sender: str,
        message: Message,
        *,
        outgoing: bool = False,
        avatar: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.message = message
        self.setObjectName("messageBubbleOutgoing" if outgoing else "messageBubble")
        self.setMaximumWidth(620)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 13, 18, 13)
        layout.setSpacing(7)

        header = QHBoxLayout()
        sender_label = QLabel(f"{avatar[:1]}  {sender}" if avatar and not outgoing else sender)
        sender_label.setObjectName("bubbleSender")
        header.addWidget(sender_label)
        if message.emotion and not outgoing:
            emotion = QLabel(f"{message.emotion}")
            emotion.setObjectName("voiceEmotion")
            header.addWidget(emotion)
        header.addStretch(1)
        timestamp = datetime.fromtimestamp(message.created_at / 1000).strftime("%H:%M")
        time_label = QLabel(timestamp)
        time_label.setObjectName("bubbleMeta")
        header.addWidget(time_label)
        layout.addLayout(header)

        audio_row = QHBoxLayout()
        self.play_button = QPushButton("▶")
        self.play_button.setObjectName("voicePlayButton")
        self.play_button.setFixedSize(36, 36)
        self.play_button.clicked.connect(
            lambda: self.play_requested.emit(message.id, message.audio_path)
        )
        audio_row.addWidget(self.play_button)
        self.waveform = QLabel("▂▄▆▃▇▅▂▄▆▃")
        self.waveform.setObjectName("voiceWaveform")
        audio_row.addWidget(self.waveform)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setObjectName("voiceProgress")
        self.slider.setRange(0, max(1, message.duration_ms))
        self.slider.sliderMoved.connect(lambda value: self.seek_requested.emit(message.id, value))
        audio_row.addWidget(self.slider, 1)
        self.duration_label = QLabel(_duration(message.duration_ms))
        self.duration_label.setObjectName("bubbleMeta")
        audio_row.addWidget(self.duration_label)
        layout.addLayout(audio_row)

        self.error_label = QLabel("")
        self.error_label.setObjectName("voiceError")
        missing = bool(message.audio_path) and not Path(message.audio_path).is_file()
        if message.status == "pending":
            self.error_label.setText("处理中…")
        elif message.status == "failed":
            error = (message.metadata or {}).get("error")
            legacy_error = message.text if message.text.startswith("语音处理失败：") else ""
            self.error_label.setText(str(error or legacy_error or "语音处理失败"))
        elif not message.audio_path or missing:
            self.error_label.setText("音频文件不存在")
        self.error_label.setVisible(bool(self.error_label.text()))
        self.play_button.setEnabled(message.status == "completed" and not missing and bool(message.audio_path))
        layout.addWidget(self.error_label)

        self.transcript_label = QLabel(message.text)
        self.transcript_label.setObjectName("bubbleText")
        self.transcript_label.setWordWrap(True)
        self.transcript_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.transcript_label.setVisible(False)
        layout.addWidget(self.transcript_label)

        footer = QHBoxLayout()
        transcript_button = QPushButton("显示文字")
        transcript_button.setObjectName("bubbleAction")

        def toggle_transcript() -> None:
            visible = not self.transcript_label.isVisible()
            self.transcript_label.setVisible(visible)
            transcript_button.setText("隐藏文字" if visible else "显示文字")

        transcript_button.clicked.connect(toggle_transcript)
        footer.addWidget(transcript_button)
        if not outgoing:
            regenerate = QPushButton("重新生成语音")
            regenerate.setObjectName("bubbleAction")
            regenerate.setEnabled(bool(message.text.strip()))
            regenerate.clicked.connect(lambda: self.regenerate_requested.emit(message.id))
            footer.addWidget(regenerate)
        if message.tts_model_version:
            model = QLabel(f"TTS {message.tts_model_version} · {message.synthesis_ms} ms")
            model.setObjectName("bubbleMeta")
            footer.addWidget(model)
        footer.addStretch(1)
        layout.addLayout(footer)

    def set_playing(self, playing: bool) -> None:
        self.play_button.setText("Ⅱ" if playing else "▶")
        self.waveform.setProperty("playing", playing)
        self.waveform.style().unpolish(self.waveform)
        self.waveform.style().polish(self.waveform)

    def set_progress(self, position_ms: int, duration_ms: int) -> None:
        duration = max(1, duration_ms or self.message.duration_ms)
        if not self.slider.isSliderDown():
            self.slider.setRange(0, duration)
            self.slider.setValue(max(0, min(position_ms, duration)))
        self.duration_label.setText(f"{_duration(position_ms)} / {_duration(duration)}")

    def set_playback_error(self, error: str) -> None:
        self.set_playing(False)
        self.error_label.setText(error or "音频播放失败")
        self.error_label.setVisible(True)


def _duration(milliseconds: int) -> str:
    seconds = max(0, int(milliseconds)) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"
