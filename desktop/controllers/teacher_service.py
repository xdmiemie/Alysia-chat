"""Fixed local Qwen3 Teacher bootstrap for the Stage 16 training page."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QTimer, Signal


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEACHER_MODEL_NAME = "Qwen3-8B-Instruct · Q4_K_M（本地固定）"
TEACHER_MODEL_ID = "Qwen3-8B-Instruct"
TEACHER_HOST = "127.0.0.1"
TEACHER_PORT = 8081
TEACHER_BASE_URL = f"http://{TEACHER_HOST}:{TEACHER_PORT}/v1"
TEACHER_MODEL_PATH = PROJECT_ROOT / "model_store" / "teacher" / "qwen3-8b-instruct" / "Qwen3-8B-Q4_K_M.gguf"
_SHARED_BASE_PATH = PROJECT_ROOT / "model_store" / "registry" / "qwen3" / "elysia" / "qwen3-8b-q4-k-m-base-v1" / "Qwen3-8B-Q4_K_M.gguf"
TEACHER_STDOUT_LOG = PROJECT_ROOT / "storage" / "logs" / "qwen3-teacher-server.out.log"
TEACHER_STDERR_LOG = PROJECT_ROOT / "storage" / "logs" / "qwen3-teacher-server.err.log"


class FixedTeacherService(QObject):
    status_changed = Signal(str)
    ready = Signal()
    failed = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.download_process: QProcess | None = None
        self.server_process: QProcess | None = None
        self._starting = False
        self._last_preflight_error = ""

    @property
    def base_url(self) -> str:
        return TEACHER_BASE_URL

    @property
    def model_name(self) -> str:
        return TEACHER_MODEL_NAME

    def ensure_started(self) -> None:
        if self._starting:
            return
        if self._preflight():
            self.status_changed.emit("本地 Qwen3-8B Teacher 已就绪。")
            self.ready.emit()
            return
        if self._server_responding():
            self._fail(
                "8081 端口已有服务，但 Teacher 预检失败："
                + (self._last_preflight_error or "不是兼容的 Qwen3 Teacher 服务")
            )
            return
        self._starting = True
        # A second Qwen3 instance alongside the chat server can exceed a
        # 16 GB card.  The training page owns the GPU while building data.
        try:
            from runtime.qwen3.server_manager import qwen_server_running, stop_qwen_server
            if qwen_server_running():
                self.status_changed.emit("正在释放聊天 Qwen3 服务的 GPU 显存，准备启动 Teacher…")
                stop_qwen_server()
        except OSError:
            # A failed best-effort shutdown will be surfaced by llama.cpp if
            # the GPU is still unavailable; do not hide the bootstrap status.
            pass
        model_path = self._model_path()
        if model_path.exists():
            self._start_server(model_path)
        else:
            self._download_model()

    def stop(self) -> None:
        for process in (self.download_process, self.server_process):
            if process is not None and process.state() != QProcess.ProcessState.NotRunning:
                process.terminate()
                if not process.waitForFinished(2000):
                    process.kill()
        self.download_process = None
        self.server_process = None
        self._starting = False

    def _model_path(self) -> Path:
        # Existing Q4 base weights are identical fixed Teacher weights for this
        # purpose and avoid a second 5–6 GB download on established installs.
        return TEACHER_MODEL_PATH if TEACHER_MODEL_PATH.exists() else _SHARED_BASE_PATH

    def _download_model(self) -> None:
        self.status_changed.emit("正在下载本地 Qwen3-8B-Instruct Q4_K_M Teacher（首次仅一次）…")
        process = QProcess(self)
        process.setWorkingDirectory(str(PROJECT_ROOT))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process.finished.connect(self._download_finished)
        process.start(sys.executable, ["scripts/download_qwen3_teacher_model.py"])
        self.download_process = process

    def _download_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        process, self.download_process = self.download_process, None
        if process is not None:
            process.deleteLater()
        if exit_code != 0 or not TEACHER_MODEL_PATH.exists():
            self._fail("本地 Teacher 下载失败；请检查网络、磁盘空间和 huggingface_hub。")
            return
        self._start_server(TEACHER_MODEL_PATH)

    def _start_server(self, model_path: Path) -> None:
        executable = _find_llama_server()
        if executable is None:
            self._fail("未找到 tools/llama.cpp/llama-server.exe，请先运行 quickstart/bootstrap.ps1。")
            return
        self.status_changed.emit("正在启动本地 Qwen3-8B Teacher 服务…")
        process = QProcess(self)
        process.setWorkingDirectory(str(PROJECT_ROOT))
        TEACHER_STDOUT_LOG.parent.mkdir(parents=True, exist_ok=True)
        process.setStandardOutputFile(str(TEACHER_STDOUT_LOG), QProcess.OpenModeFlag.Append)
        process.setStandardErrorFile(str(TEACHER_STDERR_LOG), QProcess.OpenModeFlag.Append)
        process.errorOccurred.connect(lambda _error: self._fail(process.errorString()))
        process.started.connect(self._server_started)
        process.finished.connect(self._server_finished)
        process.start(str(executable), ["--model", str(model_path), "--host", TEACHER_HOST, "--port", str(TEACHER_PORT), "--ctx-size", "8192", "--n-gpu-layers", "99"])
        self.server_process = process

    def _server_started(self) -> None:
        self.status_changed.emit("本地 Qwen3-8B Teacher 正在加载到 GPU…")
        self._wait_until_ready(0)

    def _wait_until_ready(self, attempt: int) -> None:
        if self._preflight():
            self._starting = False
            self.status_changed.emit("本地 Qwen3-8B Teacher 已启动（GPU 推理，端口 8081）。")
            self.ready.emit()
            return
        if attempt >= 120:
            self.stop()
            self._fail(
                "本地 Teacher 服务预检超时："
                + (self._last_preflight_error or "请查看 llama.cpp 日志")
            )
            return
        QTimer.singleShot(500, lambda: self._wait_until_ready(attempt + 1))

    def _server_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        process, self.server_process = self.server_process, None
        if process is not None:
            process.deleteLater()
        if exit_code != 0:
            self._fail(
                f"本地 Teacher 服务意外退出（退出码 {exit_code}）：{_log_tail(TEACHER_STDERR_LOG)}"
            )

    def _fail(self, message: str) -> None:
        self._starting = False
        self.status_changed.emit(message)
        self.failed.emit(message)

    @staticmethod
    def _server_responding() -> bool:
        try:
            with urllib.request.urlopen(f"http://{TEACHER_HOST}:{TEACHER_PORT}/health", timeout=0.5) as response:
                return response.status == 200
        except (OSError, urllib.error.URLError):
            return False

    def _preflight(self) -> bool:
        """Require an actual OpenAI-compatible JSON response, not just a port."""
        if not self._server_responding():
            self._last_preflight_error = "服务端口尚未就绪"
            return False
        payload = {
            "model": TEACHER_MODEL_ID,
            "messages": [{"role": "user", "content": "Return only this JSON object: {\"ok\":true}"}],
            "temperature": 0.0,
            "max_tokens": 32,
            "response_format": {"type": "json_object"},
            # Qwen3 otherwise spends the short probe budget in its reasoning
            # channel and leaves message.content empty.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        request = urllib.request.Request(
            TEACHER_BASE_URL + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
            content = str(data["choices"][0]["message"]["content"])
            json.loads(content)
            self._last_preflight_error = ""
            return True
        except urllib.error.HTTPError as exc:
            self._last_preflight_error = f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace').strip() or exc.reason}"
        except (OSError, urllib.error.URLError, KeyError, IndexError, TypeError, ValueError) as exc:
            self._last_preflight_error = str(exc)
        return False


def _find_llama_server() -> Path | None:
    candidates = list((PROJECT_ROOT / "tools" / "llama.cpp").rglob("llama-server.exe"))
    return candidates[0] if candidates else None


def _log_tail(path: Path, limit: int = 1200) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        return text[-limit:] if text else "未写入错误日志"
    except OSError:
        return "无法读取 Teacher 错误日志"
