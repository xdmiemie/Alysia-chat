from __future__ import annotations

import logging
from threading import Event

from PySide6.QtCore import QObject, Signal, Slot

from runtime.qwen3.model_registry import find_model
from runtime.qwen3.server_manager import qwen_server_running, start_qwen_server, stop_qwen_server
from runtime.gpt_sovits.model_registry import find_model as find_tts_model
from runtime.gpt_sovits.server_manager import (
    gpt_sovits_server_running,
    start_gpt_sovits_server,
    stop_gpt_sovits_server,
)


class ModelServiceWorker(QObject):
    succeeded = Signal(bool)
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        action: str,
        character_id: str = "",
        model_id: str = "",
        tts_model_id: str = "",
    ) -> None:
        super().__init__()
        self.action = action
        self.character_id = character_id
        self.model_id = model_id
        self.tts_model_id = tts_model_id
        self.cancelled = Event()

    def cancel(self) -> None:
        self.cancelled.set()

    @Slot()
    def run(self) -> None:
        try:
            if self.action == "start":
                qwen_was_running = qwen_server_running()
                try:
                    start_qwen_server(
                        find_model(self.character_id, self.model_id),
                        cancelled=self.cancelled,
                    )
                    start_gpt_sovits_server(
                        find_tts_model(self.character_id, self.tts_model_id),
                        cancelled=self.cancelled,
                    )
                except Exception:
                    if not qwen_was_running:
                        stop_qwen_server()
                    stop_gpt_sovits_server()
                    raise
                self.succeeded.emit(True)
            elif self.action == "stop":
                # A missing process already represents the desired stopped state.
                qwen_stopped = stop_qwen_server()
                tts_stopped = stop_gpt_sovits_server()
                failures = []
                if not qwen_stopped and qwen_server_running():
                    failures.append("当前 8080 Qwen3 服务不是由 Alysia Studio 启动的")
                if not tts_stopped and gpt_sovits_server_running():
                    failures.append("当前 9880 GPT-SoVITS 服务不是由 Alysia Studio 启动的")
                if failures:
                    raise RuntimeError("无法关闭模型：" + "；".join(failures))
                self.succeeded.emit(False)
            else:
                raise ValueError(f"unknown model service action: {self.action}")
        except Exception as exc:
            logging.getLogger(__name__).exception("Model service %s failed", self.action)
            self.failed.emit(str(exc) or exc.__class__.__name__)
        finally:
            self.finished.emit()
