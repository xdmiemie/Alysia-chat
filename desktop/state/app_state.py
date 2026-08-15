from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from threading import RLock
from typing import Callable


class AppStatus(str, Enum):
    IDLE = "idle"
    SWITCHING_CHARACTER = "switching_character"
    SWITCHING_MODEL = "switching_model"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    GENERATING_REPLY = "generating_reply"
    SYNTHESIZING = "synthesizing"
    PLAYING = "playing"
    CALLING = "calling"
    TRAINING = "training"
    TRAINING_PAUSED = "training_paused"
    STARTING_SERVICE = "starting_service"
    ERROR = "error"

    @property
    def display_name(self) -> str:
        return {
            self.IDLE: "空闲",
            self.SWITCHING_CHARACTER: "切换角色中",
            self.SWITCHING_MODEL: "切换模型中",
            self.RECORDING: "录音中",
            self.TRANSCRIBING: "识别中",
            self.THINKING: "思考中",
            self.GENERATING_REPLY: "回复生成中",
            self.SYNTHESIZING: "语音合成中",
            self.PLAYING: "播放中",
            self.CALLING: "通话中",
            self.TRAINING: "训练中",
            self.TRAINING_PAUSED: "训练暂停",
            self.STARTING_SERVICE: "服务启动中",
            self.ERROR: "错误",
        }[self]

    @property
    def busy(self) -> bool:
        return self not in {self.IDLE, self.TRAINING_PAUSED, self.ERROR}


class Workspace(str, Enum):
    CHAT = "chat"
    TRAINING = "training"


@dataclass(frozen=True)
class AppSnapshot:
    status: AppStatus = AppStatus.IDLE
    workspace: Workspace = Workspace.CHAT
    character_id: str = "elysia"
    character_name: str = "爱莉希雅"
    session_id: str = "desktop-default"
    qwen_model: str = "按需加载"
    tts_model: str = "按需加载"
    status_detail: str = ""
    error_summary: str = ""
    log_path: str = "storage/logs/desktop.log"

    @property
    def controls_enabled(self) -> bool:
        return not self.status.busy


StateListener = Callable[[AppSnapshot], None]


class AppState:
    """Single thread-safe source of truth for desktop control availability."""

    def __init__(self, initial: AppSnapshot | None = None) -> None:
        self._snapshot = initial or AppSnapshot()
        self._listeners: list[StateListener] = []
        self._lock = RLock()

    @property
    def snapshot(self) -> AppSnapshot:
        with self._lock:
            return self._snapshot

    def subscribe(self, listener: StateListener) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)
        listener(self.snapshot)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def update(self, **changes: object) -> AppSnapshot:
        with self._lock:
            next_snapshot = replace(self._snapshot, **changes)
            if next_snapshot == self._snapshot:
                return self._snapshot
            self._snapshot = next_snapshot
            listeners = tuple(self._listeners)
        for listener in listeners:
            listener(next_snapshot)
        return next_snapshot

    def set_status(self, status: AppStatus, detail: str = "") -> AppSnapshot:
        return self.update(status=status, status_detail=detail, error_summary="")

    def set_error(self, summary: str, *, log_path: str | None = None) -> AppSnapshot:
        changes: dict[str, object] = {
            "status": AppStatus.ERROR,
            "status_detail": summary,
            "error_summary": summary,
        }
        if log_path is not None:
            changes["log_path"] = log_path
        return self.update(**changes)
