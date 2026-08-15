from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal, Slot
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer


class VoicePlaybackController(QObject):
    """One QMediaPlayer shared by every bubble, enforcing single playback."""

    state_changed = Signal(str, bool)
    position_changed = Signal(str, int, int)
    failed = Signal(str, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.audio_output = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.message_id = ""
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.player.errorOccurred.connect(self._on_error)

    def toggle(self, message_id: str, audio_path: str) -> None:
        path = Path(audio_path)
        if not path.is_file():
            self.failed.emit(message_id, "音频文件不存在")
            return
        if message_id == self.message_id:
            if self.player.playbackState() is QMediaPlayer.PlaybackState.PlayingState:
                self.player.pause()
            else:
                self.player.play()
            return
        previous = self.message_id
        if previous:
            self.state_changed.emit(previous, False)
        self.player.stop()
        self.message_id = message_id
        self.player.setSource(QUrl.fromLocalFile(str(path.resolve())))
        self.player.play()

    def seek(self, message_id: str, position_ms: int) -> None:
        if message_id == self.message_id:
            self.player.setPosition(max(0, int(position_ms)))

    def stop(self) -> None:
        current = self.message_id
        self.player.stop()
        if current:
            self.state_changed.emit(current, False)
        self.message_id = ""

    @Slot(int)
    def _on_position(self, position: int) -> None:
        if self.message_id:
            self.position_changed.emit(self.message_id, position, self.player.duration())

    @Slot(int)
    def _on_duration(self, duration: int) -> None:
        if self.message_id:
            self.position_changed.emit(self.message_id, self.player.position(), duration)

    @Slot(object)
    def _on_state(self, state) -> None:
        if self.message_id:
            self.state_changed.emit(
                self.message_id,
                state is QMediaPlayer.PlaybackState.PlayingState,
            )

    @Slot(object)
    def _on_media_status(self, status) -> None:
        if status is QMediaPlayer.MediaStatus.EndOfMedia and self.message_id:
            completed = self.message_id
            self.position_changed.emit(completed, self.player.duration(), self.player.duration())
            self.state_changed.emit(completed, False)
            self.message_id = ""

    @Slot(object, str)
    def _on_error(self, _error, error_string: str) -> None:
        if self.message_id:
            failed_id = self.message_id
            self.failed.emit(failed_id, error_string or "音频播放失败")
            self.state_changed.emit(failed_id, False)
            self.message_id = ""
