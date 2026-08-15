from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal, Slot

from runtime.calls import CallSession


class VoiceCallWorker(QObject):
    """Qt adapter that keeps the complete call loop away from the UI thread."""

    state_changed = Signal(str, str)
    level_changed = Signal(float)
    transcript_changed = Signal(str)
    reply_changed = Signal(str)
    recoverable_error = Signal(str)
    completed = Signal(object)
    finished = Signal()

    def __init__(self, session: CallSession) -> None:
        super().__init__()
        self.session = session
        session.state_callback = self.state_changed.emit
        session.level_callback = self.level_changed.emit
        session.transcript_callback = self.transcript_changed.emit
        session.reply_callback = self.reply_changed.emit
        session.error_callback = self.recoverable_error.emit

    @Slot()
    def run(self) -> None:
        try:
            self.completed.emit(self.session.run())
        except Exception as exc:  # defensive UI boundary
            logging.getLogger(__name__).exception("Voice call failed")
            self.recoverable_error.emit(str(exc) or exc.__class__.__name__)
        finally:
            self.finished.emit()

    @Slot()
    def stop(self) -> None:
        self.session.stop()

    @Slot(bool)
    def set_muted(self, muted: bool) -> None:
        self.session.set_muted(muted)

    @Slot(float)
    def set_volume(self, volume: float) -> None:
        self.session.set_volume(volume)
