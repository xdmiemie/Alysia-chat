from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

from training.gpt_sovits.desktop_job import PAUSED_EXIT
from runtime.common.python_executable import resolve_runtime_python

PROJECT_ROOT = Path(__file__).resolve().parents[2]
JOB_ROOT = PROJECT_ROOT / "storage" / "training_jobs"


class VoiceTrainingWorker(QObject):
    log = Signal(str)
    changed = Signal(object)
    finished = Signal(object)
    preview_ready = Signal(object)

    def __init__(self, parent=None, *, project_root: Path = PROJECT_ROOT) -> None:
        super().__init__(parent)
        self.root = project_root
        self.process: QProcess | None = None
        self.job: dict[str, object] | None = None
        self._buffer = ""
        self._preview: list[dict[str, object]] = []
        self.log.connect(self._persist_log)

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.state() != QProcess.ProcessState.NotRunning

    def start(self, action: str, config_path: Path, *, resume: dict[str, object] | None = None,
              finish: bool = False) -> None:
        if self.is_running:
            raise RuntimeError("已有 GPT-SoVITS 任务正在运行")
        runtime_python = resolve_runtime_python(self.root)
        job_id = str(resume["job_id"]) if resume else uuid4().hex
        character_id = str(resume["character_id"]) if resume else ""
        if not character_id:
            from training.gpt_sovits.config import load_training_config
            character_id = load_training_config(config_path).character_id
        self.job = {"job_id": job_id, "action": action, "character_id": character_id,
                    "config_path": str(config_path), "state": "RUNNING",
                    "phase": str(resume.get("phase", action)) if resume else action,
                    "progress": float(resume.get("progress", 0)) if resume else 0.0,
                    "detail": (f"正在从检查点继续：{resume.get('resume_checkpoint', '自动定位')}"
                               if resume else "正在启动任务"),
                    "report": resume.get("report", {}) if resume else {},
                    "resume_checkpoint": resume.get("resume_checkpoint", "") if resume else "",
                    "created_at": resume.get("created_at", _now()) if resume else _now(),
                    "updated_at": _now()}
        pause_file = self.root / "storage" / "training_jobs" / f"{job_id}.voice.pause"
        finish_file = self.root / "storage" / "training_jobs" / f"{job_id}.voice.finish"
        pause_file.parent.mkdir(parents=True, exist_ok=True)
        pause_file.unlink(missing_ok=True)
        finish_file.unlink(missing_ok=True)
        if finish:
            finish_file.write_text("finish\n", encoding="utf-8")
        self._buffer = ""
        self._preview = []
        self._persist()
        self.changed.emit(self.job.copy())
        process = QProcess(self)
        process.setWorkingDirectory(str(self.root))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONIOENCODING", "utf-8")
        environment.insert("PYTHONUTF8", "1")
        process.setProcessEnvironment(environment)
        process.readyReadStandardOutput.connect(self._read)
        process.errorOccurred.connect(lambda _error: self._error(process.errorString()))
        process.finished.connect(self._done)
        self.process = process
        args = [str(self.root / "training" / "gpt_sovits" / "desktop_job.py"), action,
                "--config", str(config_path), "--pause-file", str(pause_file),
                "--finish-file", str(finish_file)]
        if resume:
            args.append("--resume")
        process.start(str(runtime_python), args)
        self.log.emit(f"[voice:{job_id[:8]}] {action} 已启动")

    def request_pause(self) -> None:
        if not self.is_running or not self.job or self.job["action"] != "train":
            return
        if self.job.get("state") == "PAUSING":
            return
        path = self.root / "storage" / "training_jobs" / f"{self.job['job_id']}.voice.pause"
        path.write_text("pause\n", encoding="utf-8")
        self._update(state="PAUSING", detail="等待当前保存间隔完成，然后安全暂停")
        self.log.emit("[voice] 已请求暂停；当前保存间隔结束并写入完整检查点后停止")

    def request_finish(self) -> None:
        if not self.is_running or not self.job or self.job["action"] != "train":
            return
        path = self.root / "storage" / "training_jobs" / f"{self.job['job_id']}.voice.finish"
        path.write_text("finish\n", encoding="utf-8")
        self._update(state="FINISHING", detail="正在完成完整 S1/S2 权重后验证并应用")

    def discard_paused_job(self, job: dict[str, object]) -> None:
        if self.is_running:
            raise RuntimeError("训练任务尚未退出")
        job_id = str(job["job_id"])
        if not job_id or any(not (char.isalnum() or char in "_-") for char in job_id):
            raise ValueError("训练任务 ID 无效")
        for suffix in (".voice.json", ".voice.log", ".voice.pause", ".voice.finish"):
            (self.root / "storage" / "training_jobs" / f"{job_id}{suffix}").unlink(missing_ok=True)
        self.job = None

    def _read(self) -> None:
        if self.process is None:
            return
        chunk = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace")
        if not chunk:
            return
        self._buffer += chunk
        lines = self._buffer.splitlines(keepends=True)
        self._buffer = ""
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._buffer = lines.pop()
        for line in lines:
            self._line(line.strip())

    def _line(self, line: str) -> None:
        if line.startswith("[voice_progress] "):
            try:
                value = json.loads(line.removeprefix("[voice_progress] "))
            except json.JSONDecodeError:
                return
            updates = {"phase": value.get("phase", ""), "progress": value.get("percent", 0),
                       "detail": value.get("message", "")}
            report_path = value.get("report_path")
            if report_path:
                try:
                    updates["report"] = json.loads(Path(str(report_path)).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pass
            if value.get("model_id"):
                updates["model_id"] = value["model_id"]
            if value.get("resume_checkpoint"):
                updates["resume_checkpoint"] = value["resume_checkpoint"]
            self._update(**updates)
            self.log.emit(f"[{updates['phase']}] {updates['detail']}")
        elif line.startswith("[voice_preview] "):
            try:
                self._preview.append(json.loads(line.removeprefix("[voice_preview] ")))
            except json.JSONDecodeError:
                pass
        elif line:
            self.log.emit(line)

    def _done(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        if self._buffer:
            self._line(self._buffer)
        if self.job is None:
            return
        process = self.process
        self.process = None
        if exit_code == PAUSED_EXIT and self.job["action"] == "train":
            self._update(state="PAUSED")
        elif exit_code == 0:
            self._update(state="COMPLETED", progress=100.0)
        else:
            self._update(state="FAILED", detail=f"进程退出码 {exit_code}；请查看上方日志")
        self.finished.emit(self.job.copy())
        if self._preview:
            self.preview_ready.emit(self._preview.copy())
        if process is not None:
            process.deleteLater()

    def _error(self, message: str) -> None:
        self.log.emit(f"[voice:error] {message}")
        self._update(state="FAILED", detail=message)

    def _update(self, **updates: object) -> None:
        if self.job is None:
            return
        self.job.update(updates)
        self.job["updated_at"] = _now()
        self._persist()
        self.changed.emit(self.job.copy())

    def _persist(self) -> None:
        if self.job is None or self.job["action"] != "train":
            return
        path = self.root / "storage" / "training_jobs" / f"{self.job['job_id']}.voice.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    def _persist_log(self, value: str) -> None:
        if self.job is None or not value.strip():
            return
        path = self.root / "storage" / "training_jobs" / f"{self.job['job_id']}.voice.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(value.rstrip() + "\n")


def load_paused_voice_jobs(root: Path = JOB_ROOT) -> list[dict[str, object]]:
    jobs = []
    for path in root.glob("*.voice.json") if root.exists() else ():
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            if job["state"] in {"RUNNING", "PAUSING", "FINISHING"}:
                job["state"] = "PAUSED"
                job["detail"] = "上次桌面进程已中断；将从已保存检查点继续"
                path.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if job["state"] == "PAUSED":
                jobs.append(job)
        except (OSError, ValueError, KeyError):
            continue
    return sorted(jobs, key=lambda item: str(item.get("updated_at", "")), reverse=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
