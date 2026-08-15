from __future__ import annotations

from dataclasses import replace
import logging
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Signal, Slot

from runtime.conversation import Message
from runtime.core import AlysiaCore, UnifiedMessage
from runtime.qwen3.config import load_qwen3_config
from runtime.qwen3.service import Qwen3ChatSession
from runtime.qwen3.types import CharacterReply, ChatTurn


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ReplyFactory = Callable[[str, str, str, list[Message]], CharacterReply]


def generate_reply(
    character_id: str,
    model_id: str,
    user_text: str,
    history: list[Message],
) -> CharacterReply:
    """Build a character-scoped Qwen session and restore its recent turns."""
    character_root = PROJECT_ROOT / "configs" / "characters" / character_id
    config = replace(
        load_qwen3_config(PROJECT_ROOT / "configs" / "runtime" / "qwen3.yaml"),
        model_manifest=PROJECT_ROOT / "model_store" / "active" / "qwen3.json",
        character_config=character_root / "character.yaml",
        system_prompt=character_root / "system_prompt.txt",
        examples_path=character_root / "examples.jsonl",
    )
    # Model activation is handled before the worker is started. Checking the
    # manifest here catches an external model change instead of silently using
    # the wrong version for a traceable reply.
    import json

    manifest = json.loads(config.model_manifest.read_text(encoding="utf-8-sig"))
    if str(manifest.get("character_id") or "") != character_id:
        raise RuntimeError("当前 Qwen3 服务角色与会话角色不一致，请重新切换模型")
    if str(manifest.get("model_id") or manifest.get("version") or "") != model_id:
        raise RuntimeError("当前 Qwen3 服务模型与所选模型不一致，请重新切换模型")

    session = Qwen3ChatSession.from_config(config)
    session.turns = _restore_turns(history)[-config.max_history_turns :]
    # Text, voice-bubble, and call modes all enter through AlysiaCore.  Text
    # mode explicitly skips synthesis; Stage 13 can enable it for the same
    # message without replacing this orchestration boundary.
    core = AlysiaCore(
        transcriber=_UnusedTranscriber(),
        chat_session_factory=lambda _session_id: session,
        synthesizer=_UnusedSynthesizer(),
        output_dir=PROJECT_ROOT / "storage" / "generated_audio" / "desktop",
    )
    result = core.process(
        UnifiedMessage.local_text(
            user_text,
            session_id=f"desktop:{character_id}",
            character_id=character_id,
            metadata={"synthesize_reply": False, "qwen_model": model_id},
        )
    )
    if not result.ok or not result.reply:
        raise RuntimeError(result.error or "Qwen3 未返回有效回复")
    return CharacterReply(
        reply=str(result.reply["reply"]),
        emotion=str(result.reply.get("emotion") or "neutral"),
        intensity=float(result.reply.get("intensity") or 0.5),
        speed=float(result.reply.get("speed") or 1.0),
    )


class _UnusedTranscriber:
    def transcribe(self, _audio_path):
        raise RuntimeError("text chat must not invoke ASR")


class _UnusedSynthesizer:
    def synthesize(self, _request):
        raise RuntimeError("text chat must not invoke TTS")


def _restore_turns(history: list[Message]) -> list[ChatTurn]:
    turns: list[ChatTurn] = []
    pending_user = ""
    for message in history:
        if message.role == "user":
            pending_user = message.text
        elif message.role == "assistant" and pending_user and message.status == "completed":
            turns.append(
                ChatTurn(
                    user=pending_user,
                    assistant=CharacterReply(
                        reply=message.text,
                        emotion="neutral",
                        intensity=0.5,
                        speed=1.0,
                    ),
                )
            )
            pending_user = ""
    return turns


class ChatWorker(QObject):
    progress = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        *,
        character_id: str,
        model_id: str,
        user_text: str,
        history: list[Message],
        reply_factory: ReplyFactory = generate_reply,
    ) -> None:
        super().__init__()
        self.character_id = character_id
        self.model_id = model_id
        self.user_text = user_text
        self.history = list(history)
        self.reply_factory = reply_factory

    @Slot()
    def run(self) -> None:
        try:
            self.progress.emit("generating")
            reply = self.reply_factory(
                self.character_id,
                self.model_id,
                self.user_text,
                self.history,
            )
            self.succeeded.emit(reply)
        except Exception as exc:
            logging.getLogger(__name__).exception("Desktop chat request failed")
            self.failed.emit(str(exc) or exc.__class__.__name__)
        finally:
            self.finished.emit()
