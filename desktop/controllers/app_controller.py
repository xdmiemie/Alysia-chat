from __future__ import annotations

import json
import logging
from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, QProcess, QThread, Signal, Slot

from desktop.state import AppSnapshot, AppState, AppStatus, Workspace
from desktop.state.events import StartupResult
from runtime.common.config import load_flat_yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE_PATH = PROJECT_ROOT / "storage" / "desktop" / "state.json"
DEFAULT_LOG_PATH = PROJECT_ROOT / "storage" / "logs" / "desktop.log"


def _active_model_name(path: Path, fallback: str = "按需加载") -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    for key in ("model_id", "version", "name"):
        if data.get(key):
            return str(data[key])
    manifest = data.get("manifest")
    return Path(str(manifest)).stem if manifest else fallback


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
    registry = PROJECT_ROOT / "model_store" / "registry" / "gpt_sovits" / character_id
    versions = sorted(path.name for path in registry.iterdir() if path.is_dir()) if registry.exists() else []
    return versions[0] if versions else "按需加载"


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

    tts_registry = PROJECT_ROOT / "model_store" / "registry" / "gpt_sovits" / character_id
    if tts_registry.exists():
        for path in sorted(tts_registry.iterdir()):
            if path.is_dir():
                choices.append(("tts", path.name, f"GPT-SoVITS · {path.name}"))
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
                workspace=str(restored.get("workspace") or Workspace.CHAT.value),
                qwen_model=_active_model_name(PROJECT_ROOT / "model_store" / "active" / "qwen3.json"),
                tts_model=_active_model_name(
                    PROJECT_ROOT / "model_store" / "active" / f"gpt_sovits_{character_id}.json"
                ),
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

    def __init__(self, state: AppState | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.state = state or AppState()
        self._unsubscribe = self.state.subscribe(self.state_changed.emit)
        self._startup_thread: QThread | None = None
        self._startup_worker: StartupWorker | None = None
        self._startup_cancelled = Event()
        self._processes: set[QProcess] = set()
        self._shutting_down = False

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
        self.state.set_status(AppStatus.IDLE, "模型服务将在使用时按需启动")
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

    def switch_character(self, character_id: str) -> None:
        if not self.state.snapshot.controls_enabled:
            return
        profiles = dict(self.available_characters())
        if character_id not in profiles:
            self.state.set_error(f"角色不存在：{character_id}")
            return
        self.state.set_status(AppStatus.SWITCHING_CHARACTER, f"正在切换到{profiles[character_id]}…")
        self.state.update(
            character_id=character_id,
            character_name=profiles[character_id],
            session_id=f"desktop-{character_id}",
            qwen_model=_character_qwen_model(character_id),
            tts_model=_character_tts_model(character_id),
        )
        self.state.set_status(AppStatus.IDLE, f"已切换到{profiles[character_id]}，模型将在使用时加载")

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
        self.state.update(**changes)
        self.state.set_status(AppStatus.IDLE, "模型选择已更新，将在使用时按需加载")

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
        self.save_state()
        self._unsubscribe()
