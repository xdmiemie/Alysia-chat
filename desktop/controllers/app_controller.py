from __future__ import annotations

import json
import logging
from pathlib import Path
from threading import Event
from typing import Callable, Iterable
from uuid import uuid4

from PySide6.QtCore import QObject, QProcess, QThread, QTimer, Signal, Slot

from desktop.controllers.chat_worker import ChatWorker, ReplyFactory, generate_reply
from desktop.controllers.call_worker import VoiceCallWorker
from desktop.controllers.model_service_worker import ModelServiceWorker
from desktop.controllers.voice_worker import (
    SynthesisPipeline,
    VoiceChatWorker,
    VoicePipeline,
    VoicePipelineResult,
    VoicePipelineFailure,
    VoiceSynthesisWorker,
    generate_voice_reply,
    synthesize_voice_reply,
)
from desktop.state import AppSnapshot, AppState, AppStatus, Workspace
from desktop.state.events import StartupResult
from runtime.common.config import load_flat_yaml
from runtime.conversation import Conversation, ConversationStore, Message
from runtime.core.models import new_request_id
from runtime.audio.audio_format import persist_chat_wav
from runtime.audio.player import play_wav
from runtime.audio.recorder import ButtonRecorder, RecordingResult
from runtime.audio.vad_recorder import RealtimeVadRecorder, VadConfig
from runtime.calls import CallSession, CallStore
from runtime.calls.preferences import load_call_preferences
from runtime.gpt_sovits import model_registry as tts_registry
from runtime.gpt_sovits.server_manager import (
    gpt_sovits_server_matches,
    stop_gpt_sovits_server,
)
from runtime.qwen3 import model_registry as qwen_registry
from runtime.qwen3.server_manager import qwen_server_matches, stop_qwen_server
from runtime.qwen3.types import CharacterReply

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE_PATH = PROJECT_ROOT / "storage" / "desktop" / "state.json"
DEFAULT_LOG_PATH = PROJECT_ROOT / "storage" / "logs" / "desktop.log"
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "storage" / "database" / "alysia.db"
DEFAULT_CHAT_RECORDINGS_PATH = PROJECT_ROOT / "storage" / "recordings" / "chat"
DEFAULT_CALLS_PATH = PROJECT_ROOT / "storage" / "calls"


def discover_character(character_id: str) -> tuple[str, str]:
    characters_dir = PROJECT_ROOT / "configs" / "characters"
    requested = characters_dir / character_id / "character.yaml"
    candidates = [requested, *sorted(characters_dir.glob("*/character.yaml"))]
    seen: set[Path] = set()
    for path in candidates:
        if path in seen or not path.exists():
            continue
        seen.add(path)
        raw = load_flat_yaml(path)
        # The directory name is the runtime/registry key. Some legacy character
        # files use a different persona id internally (for example fish/qiyu).
        resolved_id = path.parent.name
        resolved_name = str(raw.get("name") or resolved_id)
        return resolved_id, resolved_name
    raise FileNotFoundError("未找到 configs/characters/*/character.yaml")


def list_character_profiles() -> list[tuple[str, str]]:
    profiles: list[tuple[str, str]] = []
    characters_dir = PROJECT_ROOT / "configs" / "characters"
    for path in sorted(characters_dir.glob("*/character.yaml")):
        raw = load_flat_yaml(path)
        character_id = path.parent.name
        name = str(raw.get("name") or character_id)
        profiles.append((character_id, name))
    return profiles


def _character_qwen_model(character_id: str) -> str:
    registry = PROJECT_ROOT / "model_store" / "registry" / "qwen3" / character_id / "models.json"
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
        return str(data.get("active_model_id") or "按需加载")
    except (OSError, json.JSONDecodeError):
        return "按需加载"


def _character_tts_model(character_id: str) -> str:
    registry = PROJECT_ROOT / "model_store" / "registry" / "gpt_sovits" / character_id / "models.json"
    try:
        data = json.loads(registry.read_text(encoding="utf-8-sig"))
        return str(data.get("active_model_id") or "按需加载")
    except (OSError, json.JSONDecodeError):
        return "按需加载"


def list_character_models(character_id: str) -> list[tuple[str, str, str]]:
    choices: list[tuple[str, str, str]] = []
    qwen_registry = PROJECT_ROOT / "model_store" / "registry" / "qwen3" / character_id / "models.json"
    try:
        qwen_data = json.loads(qwen_registry.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        qwen_data = {}
    for item in qwen_data.get("models", []):
        model_id = str(item.get("id") or "")
        if model_id:
            display_name = str(item.get("display_name") or model_id)
            choices.append(("qwen", model_id, f"Qwen3 · {display_name}"))

    tts_manifest = PROJECT_ROOT / "model_store" / "registry" / "gpt_sovits" / character_id / "models.json"
    try:
        tts_data = json.loads(tts_manifest.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        tts_data = {}
    for item in tts_data.get("models", []):
        model_id = str(item.get("id") or "")
        if model_id:
            display_name = str(item.get("display_name") or model_id)
            choices.append(("tts", model_id, f"GPT-SoVITS · {display_name}"))
    return choices


class StartupWorker(QObject):
    progress = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, character_id: str, cancelled: Event) -> None:
        super().__init__()
        self.character_id = character_id
        self.cancelled = cancelled

    @Slot()
    def run(self) -> None:
        try:
            self.progress.emit("正在加载本地配置…")
            if self.cancelled.is_set():
                return
            app_config = load_flat_yaml(PROJECT_ROOT / "configs" / "runtime" / "app.yaml")
            restored = self._load_restored_state()
            character_id = self.character_id or str(
                restored.get("character_id") or app_config.get("character_id") or "elysia"
            )

            self.progress.emit("正在执行轻量环境检查…")
            required = (PROJECT_ROOT / "configs", PROJECT_ROOT / "runtime", PROJECT_ROOT / "model_store")
            missing = [path.name for path in required if not path.exists()]
            if missing:
                raise FileNotFoundError(f"项目目录缺失：{', '.join(missing)}")
            if self.cancelled.is_set():
                return

            self.progress.emit("正在加载角色与模型信息…")
            character_id, character_name = discover_character(character_id)
            result = StartupResult(
                character_id=character_id,
                character_name=character_name,
                session_id=str(restored.get("session_id") or f"desktop-{character_id}"),
                # Always enter the primary usage/chat workspace.  Training is
                # an explicit destination and should never become a surprising
                # startup screen because it was open when the app last closed.
                workspace=Workspace.CHAT.value,
                qwen_model=_character_qwen_model(character_id),
                tts_model=_character_tts_model(character_id),
                log_path=DEFAULT_LOG_PATH,
            )
            if not self.cancelled.is_set():
                self.succeeded.emit(result)
        except Exception as exc:  # UI boundary: convert failures into visible state.
            logging.getLogger(__name__).exception("Desktop startup failed")
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()

    @staticmethod
    def _load_restored_state() -> dict[str, object]:
        try:
            return json.loads(DEFAULT_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}


class AppController(QObject):
    state_changed = Signal(object)
    startup_finished = Signal()
    log_requested = Signal(str)
    conversations_changed = Signal(object)
    messages_changed = Signal(object)
    recording_changed = Signal(bool, int, float)
    call_started = Signal(object)
    call_state_changed = Signal(str, str)
    call_level_changed = Signal(float)
    call_transcript_changed = Signal(str)
    call_reply_changed = Signal(str)
    call_error = Signal(str)
    call_finished = Signal(object)

    def __init__(
        self,
        state: AppState | None = None,
        parent: QObject | None = None,
        *,
        conversation_store: ConversationStore | None = None,
        reply_factory: ReplyFactory = generate_reply,
        recorder_factory: Callable[..., ButtonRecorder] = ButtonRecorder,
        voice_pipeline: VoicePipeline = generate_voice_reply,
        synthesis_pipeline: SynthesisPipeline = synthesize_voice_reply,
        chat_recordings_path: str | Path = DEFAULT_CHAT_RECORDINGS_PATH,
        call_recorder_factory: Callable[..., object] = RealtimeVadRecorder,
        call_player: Callable[..., None] = play_wav,
        calls_path: str | Path = DEFAULT_CALLS_PATH,
    ) -> None:
        super().__init__(parent)
        self.state = state or AppState()
        self.conversations = conversation_store or ConversationStore(DEFAULT_DATABASE_PATH)
        self.reply_factory = reply_factory
        self.recorder_factory = recorder_factory
        self.voice_pipeline = voice_pipeline
        self.synthesis_pipeline = synthesis_pipeline
        self.chat_recordings_path = Path(chat_recordings_path)
        self.call_recorder_factory = call_recorder_factory
        self.call_player = call_player
        self.calls_path = Path(calls_path)
        self._unsubscribe = self.state.subscribe(self.state_changed.emit)
        self._startup_thread: QThread | None = None
        self._startup_worker: StartupWorker | None = None
        self._startup_cancelled = Event()
        self._processes: set[QProcess] = set()
        self._shutting_down = False
        self._chat_thread: QThread | None = None
        self._chat_worker: ChatWorker | None = None
        self._pending_request: dict[str, str] = {}
        self._chat_result_error = ""
        self._chat_result_succeeded = False
        self._voice_thread: QThread | None = None
        self._voice_worker: VoiceChatWorker | VoiceSynthesisWorker | None = None
        self._voice_pending: dict[str, str] = {}
        self._voice_result_error = ""
        self._voice_result_succeeded = False
        self._regenerate_message_id = ""
        self._recorder: ButtonRecorder | None = None
        self._recording_message_id = ""
        self._recording_conversation_id = ""
        self._recording_timer = QTimer(self)
        self._recording_timer.setInterval(50)
        self._recording_timer.timeout.connect(self._emit_recording_progress)
        self._service_thread: QThread | None = None
        self._service_worker: ModelServiceWorker | None = None
        self._service_action = ""
        self._call_thread: QThread | None = None
        self._call_worker: VoiceCallWorker | None = None
        self._call_summary = None
        self._select_initial_conversation()

    def start(self, character_id: str = "") -> None:
        if self._startup_thread is not None:
            return
        self._startup_cancelled.clear()
        self.state.set_status(AppStatus.STARTING_SERVICE, "准备 Alysia Studio…")
        thread = QThread(self)
        worker = StartupWorker(character_id, self._startup_cancelled)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_progress)
        worker.succeeded.connect(self._on_startup_succeeded)
        worker.failed.connect(self._on_startup_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._startup_thread_finished)
        self._startup_thread = thread
        self._startup_worker = worker
        thread.start()

    @Slot(str)
    def _on_progress(self, message: str) -> None:
        if not self._shutting_down:
            self.state.set_status(AppStatus.STARTING_SERVICE, message)

    @Slot(object)
    def _on_startup_succeeded(self, result: StartupResult) -> None:
        if self._shutting_down:
            return
        try:
            workspace = Workspace(result.workspace)
        except ValueError:
            workspace = Workspace.CHAT
        self.state.update(
            character_id=result.character_id,
            character_name=result.character_name,
            session_id=result.session_id,
            workspace=workspace,
            qwen_model=result.qwen_model,
            tts_model=result.tts_model,
            log_path=str(result.log_path),
        )
        service_running = False
        try:
            service_running = qwen_server_matches(
                qwen_registry.find_model(result.character_id, result.qwen_model)
            ) and gpt_sovits_server_matches(
                tts_registry.find_model(result.character_id, result.tts_model)
            )
        except Exception:
            service_running = False
        self.state.update(model_service_running=service_running)
        self._select_character_conversation(result.character_id, preferred_id=result.session_id)
        self.state.set_status(
            AppStatus.IDLE,
            "Qwen3 模型正在运行，可以开始聊天"
            if service_running
            else "请点击右侧“启动模型”后开始聊天",
        )
        self.refresh_chat()
        self.startup_finished.emit()

    @Slot(str)
    def _on_startup_failed(self, summary: str) -> None:
        if not self._shutting_down:
            self.state.set_error(summary or "启动失败", log_path=str(DEFAULT_LOG_PATH))
            self.startup_finished.emit()

    @Slot()
    def _startup_thread_finished(self) -> None:
        thread = self._startup_thread
        self._startup_worker = None
        self._startup_thread = None
        if thread is not None:
            thread.deleteLater()

    def set_workspace(self, workspace: Workspace) -> None:
        self.state.update(workspace=workspace)

    def available_characters(self) -> list[tuple[str, str]]:
        return list_character_profiles()

    def available_models(self) -> list[tuple[str, str, str]]:
        return list_character_models(self.state.snapshot.character_id)

    def list_conversations(self) -> list[Conversation]:
        return self.conversations.list_conversations(self.state.snapshot.character_id)

    def current_messages(self) -> list[Message]:
        return self.conversations.list_messages(self.state.snapshot.session_id)

    def refresh_chat(self) -> None:
        self.conversations_changed.emit(self.list_conversations())
        self.messages_changed.emit(self.current_messages())

    def new_conversation(self, title: str = "新对话") -> Conversation:
        if not self.state.snapshot.controls_enabled:
            raise RuntimeError("当前任务尚未结束")
        record = self.conversations.create_conversation(self.state.snapshot.character_id, title)
        self.state.update(session_id=record.id)
        self.refresh_chat()
        return record

    def open_conversation(self, conversation_id: str) -> None:
        if not self.state.snapshot.controls_enabled:
            return
        record = self.conversations.get_conversation(conversation_id)
        if record is None or record.character_id != self.state.snapshot.character_id:
            self.state.set_error("该会话不存在或不属于当前角色")
            return
        self.state.update(session_id=record.id)
        self.messages_changed.emit(self.conversations.list_messages(record.id))

    def rename_conversation(self, title: str) -> None:
        self.conversations.rename_conversation(self.state.snapshot.session_id, title)
        self.refresh_chat()

    def delete_current_conversation(self) -> None:
        if not self.state.snapshot.controls_enabled:
            return
        current = self.state.snapshot.session_id
        self.conversations.delete_conversation(current)
        record = self.conversations.latest_or_create(self.state.snapshot.character_id)
        self.state.update(session_id=record.id)
        self.refresh_chat()

    def clear_context(self) -> Conversation:
        """Start a fresh conversation while retaining the old one in history."""
        return self.new_conversation()

    def switch_character(self, character_id: str) -> None:
        if not self.state.snapshot.controls_enabled:
            return
        if self.state.snapshot.model_service_running:
            self.state.set_error("请先关闭当前模型，再切换角色")
            return
        profiles = dict(self.available_characters())
        if character_id not in profiles:
            self.state.set_error(f"角色不存在：{character_id}")
            return
        self.state.set_status(AppStatus.SWITCHING_CHARACTER, f"正在切换到{profiles[character_id]}…")
        conversation = self.conversations.latest_or_create(character_id)
        self.state.update(
            character_id=character_id,
            character_name=profiles[character_id],
            session_id=conversation.id,
            qwen_model=_character_qwen_model(character_id),
            tts_model=_character_tts_model(character_id),
        )
        self.state.set_status(AppStatus.IDLE, f"已切换到{profiles[character_id]}，模型将在使用时加载")
        self.refresh_chat()

    def switch_model(self, kind: str, model_id: str) -> None:
        if kind == "qwen":
            self.switch_models(qwen_model=model_id)
        elif kind == "tts":
            self.switch_models(tts_model=model_id)
        else:
            self.state.set_error(f"未知模型类型：{kind}")

    def switch_models(
        self,
        *,
        qwen_model: str | None = None,
        tts_model: str | None = None,
    ) -> None:
        if not self.state.snapshot.controls_enabled:
            return
        if (
            self.state.snapshot.model_service_running
            and qwen_model
            and qwen_model != self.state.snapshot.qwen_model
        ):
            self.state.set_error("请先关闭当前模型，再切换 Qwen3 版本")
            return
        available = self.available_models()
        valid_qwen = {model_id for kind, model_id, _label in available if kind == "qwen"}
        valid_tts = {model_id for kind, model_id, _label in available if kind == "tts"}
        if qwen_model and qwen_model not in valid_qwen:
            self.state.set_error(f"Qwen3 模型不属于当前角色：{qwen_model}")
            return
        if tts_model and tts_model not in valid_tts:
            self.state.set_error(f"TTS 模型不属于当前角色：{tts_model}")
            return

        changes: dict[str, str] = {}
        if qwen_model:
            changes["qwen_model"] = qwen_model
        if tts_model:
            changes["tts_model"] = tts_model
        if not changes:
            return
        self.state.set_status(AppStatus.SWITCHING_MODEL, "正在更新运行模型…")
        character_id = self.state.snapshot.character_id
        qwen_needs_activation = bool(
            qwen_model
            and not _manifest_selects(qwen_registry.ACTIVE_MANIFEST, character_id, qwen_model)
        )
        tts_needs_activation = bool(
            tts_model
            and not _manifest_selects(
                tts_registry.active_manifest_path(character_id), character_id, tts_model
            )
        )
        if not qwen_needs_activation and not tts_needs_activation:
            self.state.update(**changes)
            self.state.set_status(AppStatus.IDLE, "模型选择已就绪")
            return
        paths = [
            qwen_registry.character_manifest_path(character_id),
            qwen_registry.ACTIVE_MANIFEST,
            tts_registry.character_manifest_path(character_id),
            tts_registry.active_manifest_path(character_id),
        ]
        backups = _read_files(paths)
        try:
            # Resolve every target first so a missing second model never leaves
            # the first one partially activated.
            if qwen_model:
                qwen_registry.find_model(character_id, qwen_model)
            if tts_model:
                tts_registry.find_model(character_id, tts_model)
            if qwen_needs_activation:
                qwen_registry.activate_model(character_id, qwen_model)
            if tts_needs_activation:
                tts_registry.activate_model(character_id, tts_model)
            _verify_active_models(character_id, qwen_model, tts_model)
        except Exception as exc:
            _restore_files(paths, backups)
            self.state.set_error(f"模型切换失败，已保留原模型：{exc}")
            return
        self.state.update(**changes)
        self.state.set_status(AppStatus.IDLE, "模型已验证并切换，将在服务启动时加载")

    def send_chat(self, text: str) -> None:
        cleaned = text.strip()
        if not cleaned or not self.state.snapshot.chat_enabled or self._chat_thread is not None:
            return
        snapshot = self.state.snapshot
        conversation = self.conversations.get_conversation(snapshot.session_id)
        if conversation is None or conversation.character_id != snapshot.character_id:
            conversation = self.conversations.latest_or_create(snapshot.character_id)
            self.state.update(session_id=conversation.id)
        history = self.conversations.context_messages(conversation.id, max_turns=8)
        request_id = new_request_id()
        user_message = self.conversations.add_message(
            conversation.id,
            "user",
            cleaned,
            character_id=snapshot.character_id,
            request_id=request_id,
        )
        self._pending_request = {
            "request_id": request_id,
            "conversation_id": conversation.id,
            "character_id": snapshot.character_id,
            "character_name": snapshot.character_name,
            "model_id": snapshot.qwen_model,
            "user_message_id": user_message.id,
            "user_text": cleaned,
        }
        self._chat_result_error = ""
        self._chat_result_succeeded = False
        self.refresh_chat()
        self.state.set_status(AppStatus.THINKING, "正在整理会话上下文…")

        thread = QThread(self)
        worker = ChatWorker(
            character_id=snapshot.character_id,
            model_id=snapshot.qwen_model,
            user_text=cleaned,
            history=history,
            reply_factory=self.reply_factory,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_chat_progress)
        worker.succeeded.connect(self._on_chat_succeeded)
        worker.failed.connect(self._on_chat_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._chat_thread_finished)
        self._chat_thread = thread
        self._chat_worker = worker
        thread.start()

    def start_voice_recording(self) -> None:
        """Begin click-to-record capture; failures stay visible in app state."""
        if (
            not self.state.snapshot.chat_enabled
            or self._recorder is not None
            or self._chat_thread is not None
            or self._voice_thread is not None
        ):
            return
        snapshot = self.state.snapshot
        conversation = self.conversations.get_conversation(snapshot.session_id)
        if conversation is None or conversation.character_id != snapshot.character_id:
            conversation = self.conversations.latest_or_create(snapshot.character_id)
            self.state.update(session_id=conversation.id)
        audio_config = load_flat_yaml(PROJECT_ROOT / "configs" / "runtime" / "audio.yaml")
        try:
            recorder = self.recorder_factory(
                sample_rate=16000,
                channels=1,
                input_device=audio_config.get("input_device"),
                max_duration_seconds=float(audio_config.get("chat_max_recording_seconds") or 60),
            )
            recorder.start()
        except Exception as exc:
            self.state.set_error(f"录音设备不可用：{exc}")
            return
        self._recorder = recorder
        self._recording_message_id = uuid4().hex
        self._recording_conversation_id = conversation.id
        self.state.set_status(AppStatus.RECORDING, "正在录音；再次点击结束")
        self._recording_timer.start()
        self.recording_changed.emit(True, 0, 0.0)

    def stop_voice_recording(self) -> None:
        recorder = self._recorder
        if recorder is None:
            return
        self._recording_timer.stop()
        try:
            result = recorder.stop_chat_message(
                self.chat_recordings_path,
                self._recording_conversation_id,
                self._recording_message_id,
            )
        except Exception as exc:
            self._clear_recording()
            self.state.set_error(f"保存录音失败：{exc}")
            return
        conversation_id = self._recording_conversation_id
        message_id = self._recording_message_id
        self._clear_recording()
        self._submit_voice_recording(conversation_id, message_id, result)

    def cancel_voice_recording(self) -> None:
        if self._recorder is None:
            return
        self._recording_timer.stop()
        try:
            self._recorder.cancel()
        finally:
            self._clear_recording()
        self.state.set_status(AppStatus.IDLE, "录音已取消")

    def send_voice_file(self, audio_path: str | Path) -> None:
        """Normalize and submit an existing WAV (also useful for headless QA)."""
        if not self.state.snapshot.chat_enabled or self._voice_thread is not None:
            return
        snapshot = self.state.snapshot
        conversation = self.conversations.get_conversation(snapshot.session_id)
        if conversation is None or conversation.character_id != snapshot.character_id:
            conversation = self.conversations.latest_or_create(snapshot.character_id)
            self.state.update(session_id=conversation.id)
        message_id = uuid4().hex
        try:
            raw_path, normalized_path, metrics = persist_chat_wav(
                audio_path,
                self.chat_recordings_path,
                conversation.id,
                message_id,
            )
            result = RecordingResult(
                normalized_path,
                normalized_path.with_name("metrics.json"),
                metrics,
            )
            self._submit_voice_recording(conversation.id, message_id, result, raw_path=raw_path)
        except Exception as exc:
            self.state.set_error(f"语音文件不可用：{exc}")

    def _submit_voice_recording(
        self,
        conversation_id: str,
        message_id: str,
        recording: RecordingResult,
        *,
        raw_path: Path | None = None,
    ) -> None:
        sense_config = load_flat_yaml(PROJECT_ROOT / "configs" / "runtime" / "sensevoice.yaml")
        min_duration = int(sense_config.get("min_duration_ms") or 300)
        silence_threshold = float(sense_config.get("silence_peak_threshold") or 0.002)
        duration_ms = int(recording.metrics.duration_seconds * 1000)
        if duration_ms < min_duration:
            self.state.set_error(f"录音太短，请至少录制 {min_duration / 1000:.1f} 秒")
            return
        if recording.metrics.peak_normalized <= silence_threshold:
            self.state.set_error("没有检测到声音，录音未发送")
            return
        snapshot = self.state.snapshot
        history = self.conversations.context_messages(conversation_id, max_turns=8)
        request_id = new_request_id()
        user_message = self.conversations.add_voice_message(
            conversation_id,
            "user",
            recording.wav_path,
            "",
            message_id=message_id,
            character_id=snapshot.character_id,
            request_id=request_id,
            status="pending",
            duration_ms=duration_ms,
            sample_rate=recording.metrics.sample_rate,
            metadata={"raw_audio_path": str(raw_path or recording.wav_path.with_name("raw.wav"))},
        )
        assistant_message_id = uuid4().hex
        output_path = (
            self.chat_recordings_path
            / conversation_id
            / assistant_message_id
            / "reply.wav"
        )
        self._voice_pending = {
            "request_id": request_id,
            "conversation_id": conversation_id,
            "character_id": snapshot.character_id,
            "qwen_model_id": snapshot.qwen_model,
            "tts_model_id": snapshot.tts_model,
            "user_message_id": user_message.id,
            "assistant_message_id": assistant_message_id,
        }
        self._voice_result_error = ""
        self._voice_result_succeeded = False
        self.refresh_chat()
        self.state.set_status(AppStatus.TRANSCRIBING, "正在识别语音…")
        thread = QThread(self)
        worker = VoiceChatWorker(
            character_id=snapshot.character_id,
            qwen_model_id=snapshot.qwen_model,
            tts_model_id=snapshot.tts_model,
            audio_path=str(recording.wav_path),
            output_path=str(output_path),
            history=history,
            pipeline=self.voice_pipeline,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_voice_progress)
        worker.succeeded.connect(self._on_voice_succeeded)
        worker.failed.connect(self._on_voice_failed)
        worker.failed_with_context.connect(self._on_voice_failed_with_context)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._voice_thread_finished)
        self._voice_thread = thread
        self._voice_worker = worker
        thread.start()

    @Slot()
    def _emit_recording_progress(self) -> None:
        recorder = self._recorder
        if recorder is None:
            return
        elapsed_ms = int(recorder.elapsed_seconds * 1000)
        self.recording_changed.emit(True, elapsed_ms, recorder.level)
        if recorder.reached_max_duration:
            self.stop_voice_recording()

    def _clear_recording(self) -> None:
        self._recorder = None
        self._recording_message_id = ""
        self._recording_conversation_id = ""
        self.recording_changed.emit(False, 0, 0.0)

    @Slot(str)
    def _on_voice_progress(self, stage: str) -> None:
        if self._shutting_down:
            return
        if stage == "transcribing":
            self.state.set_status(AppStatus.TRANSCRIBING, "正在识别语音…")
        elif stage == "generating":
            self.state.set_status(AppStatus.GENERATING_REPLY, "正在生成语音回复…")
        elif stage == "synthesizing":
            self.state.set_status(AppStatus.SYNTHESIZING, "正在合成语音…")

    @Slot(object)
    def _on_voice_succeeded(self, result: VoicePipelineResult) -> None:
        if self._shutting_down or not self._voice_pending:
            return
        pending = dict(self._voice_pending)
        self.conversations.update_message(
            pending["user_message_id"],
            text=result.transcript,
            status="completed",
        )
        self.conversations.add_voice_message(
            pending["conversation_id"],
            "assistant",
            result.audio_path,
            result.reply_text,
            message_id=pending["assistant_message_id"],
            character_id=pending["character_id"],
            model_version=pending["qwen_model_id"],
            tts_model_version=result.tts_model_version,
            request_id=pending["request_id"],
            parent_message_id=pending["user_message_id"],
            duration_ms=result.duration_ms,
            sample_rate=result.sample_rate,
            emotion=result.emotion,
            reference_audio=result.reference_audio,
            synthesis_ms=result.synthesis_ms,
            metadata=result.metadata,
        )
        self._voice_result_succeeded = True
        self.refresh_chat()

    @Slot(str)
    def _on_voice_failed(self, error: str) -> None:
        if self._shutting_down or not self._voice_pending:
            return
        self.conversations.update_message(
            self._voice_pending["user_message_id"],
            status="failed",
            metadata={"error": error},
        )
        self._voice_result_error = error
        self.refresh_chat()

    @Slot(object)
    def _on_voice_failed_with_context(self, failure: VoicePipelineFailure) -> None:
        if self._shutting_down or not self._voice_pending:
            return
        pending = dict(self._voice_pending)
        if failure.transcript:
            self.conversations.update_message(
                pending["user_message_id"],
                text=failure.transcript,
                status="completed",
            )
        else:
            self.conversations.update_message(
                pending["user_message_id"],
                status="failed",
                metadata={"error": str(failure)},
            )
        if failure.reply_text:
            output_path = (
                self.chat_recordings_path
                / pending["conversation_id"]
                / pending["assistant_message_id"]
                / "reply.wav"
            )
            self.conversations.add_voice_message(
                pending["conversation_id"],
                "assistant",
                output_path,
                failure.reply_text,
                message_id=pending["assistant_message_id"],
                character_id=pending["character_id"],
                model_version=pending["qwen_model_id"],
                tts_model_version=pending["tts_model_id"],
                request_id=pending["request_id"],
                parent_message_id=pending["user_message_id"],
                emotion=failure.emotion,
                status="failed",
                metadata={"error": str(failure), "failed_stage": failure.stage},
            )
        self._voice_result_error = str(failure)
        self.refresh_chat()

    @Slot()
    def _voice_thread_finished(self) -> None:
        thread = self._voice_thread
        self._voice_worker = None
        self._voice_thread = None
        self._voice_pending = {}
        self._regenerate_message_id = ""
        if thread is not None:
            thread.deleteLater()
        if not self._shutting_down:
            if self._voice_result_error:
                self.state.set_error(f"语音消息处理失败：{self._voice_result_error}")
            elif self._voice_result_succeeded:
                self.state.set_status(AppStatus.IDLE, "语音回复已生成")
        self._voice_result_error = ""
        self._voice_result_succeeded = False

    def regenerate_voice(self, message_id: str) -> None:
        message = self.conversations.get_message(message_id)
        if (
            message is None
            or message.role != "assistant"
            or message.message_type != "voice"
            or not message.text.strip()
            or self._voice_thread is not None
            or not self.state.snapshot.controls_enabled
        ):
            return
        snapshot = self.state.snapshot
        self._regenerate_message_id = message.id
        self._voice_result_error = ""
        self._voice_result_succeeded = False
        self.state.set_status(AppStatus.SYNTHESIZING, "正在重新生成语音…")
        thread = QThread(self)
        worker = VoiceSynthesisWorker(
            pipeline=self.synthesis_pipeline,
            character_id=message.character_id or snapshot.character_id,
            tts_model_id=snapshot.tts_model,
            text=message.text,
            emotion=message.emotion or "neutral",
            output_path=message.audio_path,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_regeneration_succeeded)
        worker.failed.connect(self._on_voice_failed_regeneration)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._voice_thread_finished)
        self._voice_thread = thread
        self._voice_worker = worker
        thread.start()

    @Slot(object)
    def _on_regeneration_succeeded(self, result: VoicePipelineResult) -> None:
        if not self._regenerate_message_id:
            return
        self.conversations.update_message(
            self._regenerate_message_id,
            audio_path=result.audio_path,
            duration_ms=result.duration_ms,
            sample_rate=result.sample_rate,
            emotion=result.emotion,
            reference_audio=result.reference_audio,
            tts_model_version=result.tts_model_version,
            synthesis_ms=result.synthesis_ms,
            metadata=result.metadata,
            status="completed",
        )
        self._voice_result_succeeded = True
        self.refresh_chat()

    @Slot(str)
    def _on_voice_failed_regeneration(self, error: str) -> None:
        self._voice_result_error = error

    def set_voice_playing(self, playing: bool) -> None:
        if self._shutting_down or self._recorder is not None:
            return
        if playing:
            self.state.set_status(AppStatus.PLAYING, "正在播放语音消息…")
        elif self.state.snapshot.status is AppStatus.PLAYING:
            self.state.set_status(AppStatus.IDLE, "语音播放已停止")

    def start_voice_call(self) -> None:
        """Start one cancellable half-duplex call on a dedicated worker thread."""
        snapshot = self.state.snapshot
        if (
            self._call_thread is not None
            or not snapshot.chat_enabled
            or self._recorder is not None
            or self._chat_thread is not None
            or self._voice_thread is not None
        ):
            return
        conversation = self.conversations.get_conversation(snapshot.session_id)
        if conversation is None or conversation.character_id != snapshot.character_id:
            conversation = self.conversations.latest_or_create(snapshot.character_id)
            self.state.update(session_id=conversation.id)
        audio_config = load_flat_yaml(PROJECT_ROOT / "configs" / "runtime" / "audio.yaml")
        try:
            recorder = self.call_recorder_factory(
                config=VadConfig(
                    sample_rate=16000,
                    channels=1,
                    frame_ms=30,
                    pre_roll_ms=450,
                ),
                input_device=audio_config.get("input_device"),
            )
        except TypeError:
            # Small injected test doubles may not accept runtime configuration.
            recorder = self.call_recorder_factory()
        preferences = load_call_preferences(PROJECT_ROOT / "storage" / "desktop" / "settings.json")
        session = CallSession(
            character_id=snapshot.character_id,
            conversation_id=conversation.id,
            qwen_model_id=snapshot.qwen_model,
            tts_model_id=snapshot.tts_model,
            history=self.conversations.context_messages(conversation.id, max_turns=8),
            recorder=recorder,
            pipeline=self.voice_pipeline,
            player=self.call_player,
            store=CallStore(self.calls_path),
            save_audio=bool(preferences.get("save_call_audio", True)),
            echo_guard_seconds=float(audio_config.get("call_echo_guard_ms") or 180) / 1000,
            output_device=audio_config.get("output_device"),
        )
        thread = QThread(self)
        worker = VoiceCallWorker(session)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.state_changed.connect(self._on_call_state)
        worker.level_changed.connect(self.call_level_changed)
        worker.transcript_changed.connect(self.call_transcript_changed)
        worker.reply_changed.connect(self.call_reply_changed)
        worker.recoverable_error.connect(self.call_error)
        worker.completed.connect(self._on_call_completed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._call_thread_finished)
        self._call_thread = thread
        self._call_worker = worker
        self._call_summary = None
        self.state.set_status(AppStatus.CALLING, "正在连接语音通话…")
        self.call_started.emit(
            {
                "character_id": snapshot.character_id,
                "character_name": snapshot.character_name,
                "qwen_model": snapshot.qwen_model,
                "tts_model": snapshot.tts_model,
            }
        )
        thread.start()

    def end_voice_call(self) -> None:
        worker = self._call_worker
        if worker is not None:
            worker.stop()
            self.call_state_changed.emit("ending", "正在结束通话…")

    def set_call_muted(self, muted: bool) -> None:
        if self._call_worker is not None:
            self._call_worker.set_muted(muted)

    def set_call_volume(self, value: int | float) -> None:
        if self._call_worker is not None:
            numeric = float(value)
            self._call_worker.set_volume(numeric / 100.0 if numeric > 1.0 else numeric)

    @Slot(str, str)
    def _on_call_state(self, state: str, detail: str) -> None:
        if self._shutting_down:
            return
        self.state.set_status(AppStatus.CALLING, detail)
        self.call_state_changed.emit(state, detail)

    @Slot(object)
    def _on_call_completed(self, summary) -> None:
        self._call_summary = summary
        self.call_finished.emit(summary)

    @Slot()
    def _call_thread_finished(self) -> None:
        thread = self._call_thread
        self._call_thread = None
        self._call_worker = None
        if thread is not None:
            thread.deleteLater()
        if self._shutting_down:
            return
        summary = self._call_summary
        if summary is not None and summary.status == "failed":
            self.state.set_error(f"语音通话已结束：{summary.error or '音频设备异常'}")
        else:
            turns = int(getattr(summary, "turn_count", 0)) if summary is not None else 0
            self.state.set_status(AppStatus.IDLE, f"语音通话已结束，共 {turns} 个回合")
        self._call_summary = None

    def toggle_model_service(self) -> None:
        if self.state.snapshot.model_service_running:
            self.stop_model_service()
        else:
            self.start_model_service()

    def start_model_service(self) -> None:
        if self._service_thread is not None or not self.state.snapshot.controls_enabled:
            return
        snapshot = self.state.snapshot
        if not _manifest_selects(qwen_registry.ACTIVE_MANIFEST, snapshot.character_id, snapshot.qwen_model):
            self.switch_models(qwen_model=snapshot.qwen_model)
            if self.state.snapshot.status is AppStatus.ERROR:
                return
            snapshot = self.state.snapshot
        self.state.set_status(AppStatus.STARTING_SERVICE, "正在启动并检查 Qwen3 模型…")
        self._start_service_worker(
            "start",
            snapshot.character_id,
            snapshot.qwen_model,
            snapshot.tts_model,
        )

    def stop_model_service(self) -> None:
        if self._service_thread is not None or not self.state.snapshot.controls_enabled:
            return
        if self._chat_thread is not None:
            self.state.set_error("当前回复尚未结束，暂时不能关闭模型")
            return
        self.state.set_status(AppStatus.STARTING_SERVICE, "正在关闭 Qwen3 模型…")
        self._start_service_worker("stop")

    def _start_service_worker(
        self,
        action: str,
        character_id: str = "",
        model_id: str = "",
        tts_model_id: str = "",
    ) -> None:
        thread = QThread(self)
        worker = ModelServiceWorker(action, character_id, model_id, tts_model_id)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_service_succeeded)
        worker.failed.connect(self._on_service_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._service_thread_finished)
        self._service_action = action
        self._service_thread = thread
        self._service_worker = worker
        thread.start()

    @Slot(bool)
    def _on_service_succeeded(self, running: bool) -> None:
        if self._shutting_down:
            return
        self.state.update(model_service_running=running)
        detail = "Qwen3 与 GPT-SoVITS 已启动，可以开始聊天" if running else "模型服务已关闭"
        self.state.set_status(AppStatus.IDLE, detail)

    @Slot(str)
    def _on_service_failed(self, error: str) -> None:
        if not self._shutting_down:
            if self._service_action == "start":
                self.state.update(model_service_running=False)
            self.state.set_error(f"模型服务操作失败：{error}")

    @Slot()
    def _service_thread_finished(self) -> None:
        thread = self._service_thread
        self._service_thread = None
        self._service_worker = None
        self._service_action = ""
        if thread is not None:
            thread.deleteLater()

    def retry_message(self, message_id: str) -> None:
        failed = self.conversations.get_message(message_id)
        if failed is None or failed.status != "failed" or not failed.parent_message_id:
            return
        original = self.conversations.get_message(failed.parent_message_id)
        if original is not None and original.role == "user":
            self.send_chat(original.text)

    @Slot(str)
    def _on_chat_progress(self, stage: str) -> None:
        if stage == "generating" and not self._shutting_down:
            self.state.set_status(AppStatus.GENERATING_REPLY, "正在生成回复…")

    @Slot(object)
    def _on_chat_succeeded(self, reply: CharacterReply) -> None:
        if self._shutting_down or not self._pending_request:
            return
        pending = dict(self._pending_request)
        self.conversations.add_message(
            pending["conversation_id"],
            "assistant",
            reply.reply,
            character_id=pending["character_id"],
            model_version=pending["model_id"],
            request_id=pending["request_id"],
            parent_message_id=pending["user_message_id"],
        )
        self._chat_result_succeeded = True
        self.refresh_chat()

    @Slot(str)
    def _on_chat_failed(self, error: str) -> None:
        if self._shutting_down or not self._pending_request:
            return
        pending = dict(self._pending_request)
        self.conversations.add_message(
            pending["conversation_id"],
            "assistant",
            f"回复生成失败：{error}",
            character_id=pending["character_id"],
            model_version=pending["model_id"],
            request_id=pending["request_id"],
            parent_message_id=pending["user_message_id"],
            status="failed",
        )
        self._chat_result_error = error
        self.refresh_chat()

    @Slot()
    def _chat_thread_finished(self) -> None:
        thread = self._chat_thread
        self._chat_worker = None
        self._chat_thread = None
        self._pending_request = {}
        if thread is not None:
            thread.deleteLater()
        if not self._shutting_down:
            if self._chat_result_error:
                self.state.set_error(f"回复生成失败：{self._chat_result_error}")
            elif self._chat_result_succeeded:
                self.state.set_status(AppStatus.IDLE, "回复已生成")
        self._chat_result_error = ""
        self._chat_result_succeeded = False

    def request_log(self) -> None:
        self.log_requested.emit(self.state.snapshot.log_path)

    def register_process(self, process: QProcess) -> None:
        """Register an app-owned service so window shutdown can stop it safely."""
        self._processes.add(process)
        process.finished.connect(lambda *_: self._processes.discard(process))

    def start_managed_process(
        self,
        program: str,
        arguments: list[str] | None = None,
        *,
        label: str = "模型服务",
    ) -> QProcess:
        """Start an owned service asynchronously without blocking Qt's event loop."""
        process = QProcess(self)
        self.register_process(process)
        self.state.set_status(AppStatus.STARTING_SERVICE, f"正在启动{label}…")
        process.started.connect(
            lambda: self.state.set_status(AppStatus.IDLE, f"{label}已启动")
            if not self._shutting_down
            else None
        )
        process.errorOccurred.connect(
            lambda _error: self.state.set_error(
                f"{label}启动失败：{process.errorString()}", log_path=str(DEFAULT_LOG_PATH)
            )
            if not self._shutting_down
            else None
        )
        process.start(program, arguments or [])
        return process

    def save_state(self) -> None:
        snapshot = self.state.snapshot
        data = {
            "character_id": snapshot.character_id,
            "session_id": snapshot.session_id,
            "workspace": snapshot.workspace.value,
        }
        DEFAULT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = DEFAULT_STATE_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(DEFAULT_STATE_PATH)

    def shutdown(self, timeout_ms: int = 2500) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self._startup_cancelled.set()
        self._recording_timer.stop()
        if self._recorder is not None:
            try:
                self._recorder.cancel()
            except Exception:
                logging.getLogger(__name__).exception("Failed to cancel recording during shutdown")
            self._clear_recording()
        if self._call_worker is not None:
            self._call_worker.stop()
        for process in tuple(self._processes):
            if process.state() != QProcess.ProcessState.NotRunning:
                process.terminate()
                if not process.waitForFinished(timeout_ms):
                    process.kill()
                    process.waitForFinished(1000)
        self._processes.clear()
        thread = self._startup_thread
        if thread is not None and thread.isRunning():
            thread.quit()
            thread.wait(timeout_ms)
        chat_thread = self._chat_thread
        if chat_thread is not None and chat_thread.isRunning():
            # Network generation has a bounded timeout. Give it a chance to
            # finish; Qt threads cannot be killed safely.
            chat_thread.quit()
            chat_thread.wait(timeout_ms)
        voice_thread = self._voice_thread
        if voice_thread is not None and voice_thread.isRunning():
            voice_thread.quit()
            voice_thread.wait(timeout_ms)
        call_thread = self._call_thread
        if call_thread is not None and call_thread.isRunning():
            call_thread.quit()
            call_thread.wait(timeout_ms)
        service_thread = self._service_thread
        if service_thread is not None and service_thread.isRunning():
            if self._service_worker is not None:
                self._service_worker.cancel()
            service_thread.quit()
            service_thread.wait(max(timeout_ms, 5000))
        try:
            stop_qwen_server()
        except Exception:
            logging.getLogger(__name__).exception("Failed to stop Qwen3 service during shutdown")
        try:
            stop_gpt_sovits_server()
        except Exception:
            logging.getLogger(__name__).exception(
                "Failed to stop GPT-SoVITS service during shutdown"
            )
        if chat_thread is not None and chat_thread.isRunning():
            chat_thread.wait(max(timeout_ms, 5000))
        if voice_thread is not None and voice_thread.isRunning():
            voice_thread.wait(max(timeout_ms, 5000))
        if call_thread is not None and call_thread.isRunning():
            call_thread.wait(max(timeout_ms, 5000))
        if service_thread is not None and service_thread.isRunning():
            service_thread.wait(max(timeout_ms, 5000))
        self.state.update(model_service_running=False)
        self.save_state()
        self._unsubscribe()

    def _select_initial_conversation(self) -> None:
        snapshot = self.state.snapshot
        self._select_character_conversation(snapshot.character_id, preferred_id=snapshot.session_id)

    def _select_character_conversation(self, character_id: str, preferred_id: str = "") -> Conversation:
        preferred = self.conversations.get_conversation(preferred_id) if preferred_id else None
        record = (
            preferred
            if preferred is not None and preferred.character_id == character_id
            else self.conversations.latest_or_create(character_id)
        )
        self.state.update(session_id=record.id)
        return record


def _read_files(paths: Iterable[Path]) -> dict[Path, bytes | None]:
    return {path: path.read_bytes() if path.exists() else None for path in paths}


def _restore_files(paths: Iterable[Path], backups: dict[Path, bytes | None]) -> None:
    for path in paths:
        original = backups[path]
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".rollback")
            temporary.write_bytes(original)
            temporary.replace(path)


def _verify_active_models(character_id: str, qwen_model: str | None, tts_model: str | None) -> None:
    entries = []
    if qwen_model:
        active = json.loads(qwen_registry.ACTIVE_MANIFEST.read_text(encoding="utf-8-sig"))
        if active.get("character_id") != character_id or active.get("model_id") != qwen_model:
            raise RuntimeError("Qwen3 active manifest 验证失败")
        entries.append(qwen_registry.find_model(character_id, qwen_model))
    if tts_model:
        path = tts_registry.active_manifest_path(character_id)
        active = json.loads(path.read_text(encoding="utf-8-sig"))
        if active.get("character_id") != character_id or active.get("model_id") != tts_model:
            raise RuntimeError("GPT-SoVITS active manifest 验证失败")
        entries.append(tts_registry.find_model(character_id, tts_model))
    missing = [path for entry in entries for path in entry.required_files if not path.exists()]
    if missing:
        raise FileNotFoundError("模型文件缺失：" + ", ".join(str(path) for path in missing))


def _manifest_selects(path: Path, character_id: str, model_id: str) -> bool:
    try:
        active = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    active_id = str(active.get("model_id") or active.get("version") or "")
    return str(active.get("character_id") or "") == character_id and active_id == model_id
