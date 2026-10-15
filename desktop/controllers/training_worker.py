"""Non-blocking Qwen3 training task runner used by the Stage 16 page."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

from runtime.common.python_executable import resolve_qwen_python


PROJECT_ROOT = Path(__file__).resolve().parents[2]
JOB_ROOT = PROJECT_ROOT / "storage" / "training_jobs"


class Qwen3JobState(str, Enum):
    SOURCE_READY = "SOURCE_READY"
    BUILDING_DATASET = "BUILDING_DATASET"
    DATASET_READY = "DATASET_READY"
    RUNNING = "RUNNING"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    FINISHING = "FINISHING"
    EXPORTING = "EXPORTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class Qwen3Job:
    job_id: str
    kind: str
    character_id: str
    config_path: str
    state: Qwen3JobState
    created_at: str
    updated_at: str
    error: str = ""
    progress: float = 0.0
    phase: str = ""
    detail: str = ""
    metrics: dict[str, object] = field(default_factory=dict)


class Qwen3TrainingWorker(QObject):
    """Runs one task at a time; pause/finish are cooperative file requests.

    `train_lora.py` polls these files at step boundaries, asks Trainer to save
    a normal checkpoint, and only then exits.  This intentionally avoids a
    destructive QProcess.kill for a user-facing pause action.
    """

    log = Signal(str)
    state_changed = Signal(object)
    finished = Signal(object)

    def __init__(self, parent=None, *, project_root: Path = PROJECT_ROOT) -> None:
        super().__init__(parent)
        self.project_root = project_root
        self.job_root = project_root / "storage" / "training_jobs"
        self.process: QProcess | None = None
        self.job: Qwen3Job | None = None
        self._pause_file: Path | None = None
        self._finish_file: Path | None = None
        self._output_buffer = ""
        self.log.connect(self._persist_log)

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.state() != QProcess.ProcessState.NotRunning

    def start_dataset(self, character_id: str, config_path: Path, *, resume: bool = False) -> Qwen3Job:
        return self._start("dataset", character_id, config_path, resume=resume)

    def start_training(self, character_id: str, config_path: Path, *, resume: bool = False) -> Qwen3Job:
        return self._start("training", character_id, config_path, resume=resume)

    def request_pause(self) -> None:
        if not self.job or not self.is_running or self._pause_file is None:
            return
        self._pause_file.write_text("pause requested\n", encoding="utf-8")
        self._set_state(Qwen3JobState.PAUSING)
        self.log.emit("[pause] 已请求在当前安全边界暂停；已完成的数据会保留。")

    def resume_dataset(self, job: Qwen3Job) -> Qwen3Job:
        if job.kind != "dataset":
            raise ValueError("resume_dataset requires a dataset job")
        return self._start("dataset", job.character_id, Path(job.config_path), resume=True, existing=job)

    def resume_training(self, job: Qwen3Job, *, finish: bool = False) -> Qwen3Job:
        """Continue a paused LoRA job from its newest normal checkpoint."""
        if job.kind != "training":
            raise ValueError("resume_training requires a training job")
        return self._start("training", job.character_id, Path(job.config_path), resume=True, existing=job, finish=finish)

    def dismiss_paused_job(self, job: Qwen3Job, *, discarded: bool = False) -> None:
        """Remove a paused task from the task panel after its artifacts are handled."""
        detail = "training results discarded" if discarded else "stopped by user; generated artifacts retained"
        self.job = Qwen3Job(**{**asdict(job), "state": Qwen3JobState.CANCELLED, "updated_at": _now(), "error": detail})
        self._persist()
        self.state_changed.emit(self.job)

    def request_finish(self) -> None:
        if not self.job or self.job.kind != "training" or not self.is_running or self._finish_file is None:
            return
        self._finish_file.write_text("finish requested\n", encoding="utf-8")
        self._set_state(Qwen3JobState.FINISHING)
        self.log.emit("[finish] 将在当前安全 step 保存，随后导出、注册并激活候选模型。")

    def _start(self, kind: str, character_id: str, config_path: Path, *, resume: bool = False, existing: Qwen3Job | None = None, finish: bool = False) -> Qwen3Job:
        if self.is_running:
            raise RuntimeError("已有 Qwen3 任务正在执行")
        qwen_python = resolve_qwen_python(self.project_root)
        job_id = existing.job_id if existing else uuid4().hex
        now = _now()
        initial = Qwen3JobState.BUILDING_DATASET if kind == "dataset" else Qwen3JobState.RUNNING
        phase = "preparing" if kind == "dataset" else "loading_model"
        detail = "正在准备训练集" if kind == "dataset" else "正在加载训练模型…"
        # A dataset job may have completed at 100%, and a resumed training job
        # retains its previous durable progress.  Neither value describes the
        # new process while its model/checkpoint is being loaded.
        self.job = Qwen3Job(
            job_id, kind, character_id, str(config_path), initial,
            existing.created_at if existing else now, now,
            progress=0.0, phase=phase, detail=detail,
        )
        self._pause_file = self.job_root / f"{job_id}.pause"
        self._finish_file = self.job_root / f"{job_id}.finish"
        self._pause_file.parent.mkdir(parents=True, exist_ok=True)
        self._pause_file.unlink(missing_ok=True)
        self._finish_file.unlink(missing_ok=True)
        if finish and kind == "training":
            self._finish_file.write_text("finish requested\n", encoding="utf-8")
        self._output_buffer = ""
        self._persist()
        self.state_changed.emit(self.job)

        process = QProcess(self)
        process.setWorkingDirectory(str(self.project_root))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        # QProcess pipes do not inherit a real Windows console.  Without an
        # explicit encoding Python may emit the system ANSI code page while
        # the UI correctly decodes as UTF-8, producing mojibake in task logs.
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONIOENCODING", "utf-8")
        environment.insert("PYTHONUTF8", "1")
        process.setProcessEnvironment(environment)
        process.readyReadStandardOutput.connect(self._read_output)
        process.errorOccurred.connect(lambda _error: self._fail(process.errorString()))
        process.finished.connect(self._process_finished)
        self.process = process
        relative_config = _relative_to_root(config_path, self.project_root)
        if kind == "dataset":
            process.start(str(qwen_python), ["training/qwen3/generate/build_dataset.py", "--config", relative_config, "--pause-file", str(self._pause_file)])
        else:
            resume_checkpoint = ""
            if resume and existing is not None:
                resume_checkpoint = self._resume_checkpoint_for(existing)
            args = _training_powershell_args(
                relative_config,
                pause_file=self._pause_file,
                finish_file=self._finish_file,
                resume=resume,
                resume_from=resume_checkpoint or "auto",
                python=str(qwen_python),
            )
            process.start("powershell.exe", args)
        self.log.emit(f"[job:{job_id[:8]}] 已启动 {kind} 任务。")
        return self.job

    def _read_output(self) -> None:
        if self.process is None:
            return
        text = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace")
        if text:
            self.log.emit(text.rstrip())
            # QProcess may split one JSON progress line across arbitrary read
            # notifications.  Preserve its incomplete tail until a line break
            # arrives; otherwise a failed JSON parse is silently lost and the
            # task panel incorrectly keeps the previous 100% dataset value.
            self._output_buffer += text
            lines = self._output_buffer.splitlines(keepends=True)
            self._output_buffer = ""
            if lines and not lines[-1].endswith(("\n", "\r")):
                self._output_buffer = lines.pop()
            for line in lines:
                self._handle_output_line(line.rstrip("\r\n"))

    def _handle_output_line(self, line: str) -> None:
        line = line.lstrip("\r")
        # quickstart/train_qwen3.ps1 rebuilds/validates the data split before
        # invoking Trainer and emits dataset-style [progress] messages.  They
        # must never overwrite the training bar with the dataset's final 100%.
        if line.startswith("[progress] ") and self.job and self.job.kind == "dataset":
            try:
                value = json.loads(line.removeprefix("[progress] "))
                self.job = Qwen3Job(**{**asdict(self.job), "updated_at": _now(), "progress": float(value.get("percent", 0)), "phase": str(value.get("phase", "")), "detail": str(value.get("message", "")), "metrics": value})
                self._persist(); self.state_changed.emit(self.job)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        elif line.startswith("[training_progress] ") and self.job:
            try:
                value = json.loads(line.removeprefix("[training_progress] "))
                metrics = {**dict(value.get("metrics", {})), "steps_per_second": value.get("steps_per_second")}
                self.job = Qwen3Job(**{**asdict(self.job), "updated_at": _now(), "progress": float(value.get("percent", 0)), "phase": "training", "detail": f"第 {value.get('completed', 0)}/{value.get('total', 0)} step", "metrics": metrics})
                self._persist(); self.state_changed.emit(self.job)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        elif line.startswith("generated ") and line.endswith(" SFT examples") and self.job and self.job.kind == "dataset":
            match = re.match(r"generated\s+(\d+)\s+SFT examples", line)
            if match:
                count = int(match.group(1))
                self.job = Qwen3Job(**{
                    **asdict(self.job), "updated_at": _now(), "progress": 100.0,
                    "phase": "complete", "detail": "训练集已生成，正在完成校验",
                    "metrics": {**self.job.metrics, "accepted_examples": count},
                })
                self._persist(); self.state_changed.emit(self.job)
        elif self.job and self.job.kind == "training":
            self._capture_training_metrics(line)
            if self.job and self.job.state is Qwen3JobState.FINISHING and "convert" in text.lower():
                self._set_state(Qwen3JobState.EXPORTING)

    def _process_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        if self._output_buffer:
            self._handle_output_line(self._output_buffer)
            self._output_buffer = ""
        job = self.job
        if job is None:
            return
        if self._pause_file is not None and self._pause_file.exists():
            self._remember_pause_checkpoint()
            self._set_state(Qwen3JobState.PAUSED)
        elif exit_code == 0:
            if job.kind == "dataset":
                self._set_state(
                    Qwen3JobState.DATASET_READY,
                    progress=100.0,
                    phase="complete",
                    detail="训练集已就绪，可开始训练。",
                )
            else:
                self._set_state(Qwen3JobState.COMPLETED)
        else:
            self._fail(f"任务进程退出，退出码 {exit_code}")
        self.finished.emit(self.job)
        if self.process is not None:
            self.process.deleteLater()
        self.process = None

    def _fail(self, error: str) -> None:
        if self.job is None or self.job.state is Qwen3JobState.FAILED:
            return
        self.log.emit(f"[error] {error}")
        self.job = Qwen3Job(**{**asdict(self.job), "state": Qwen3JobState.FAILED, "updated_at": _now(), "error": error})
        self._persist()
        self.state_changed.emit(self.job)

    def _set_state(self, state: Qwen3JobState, *, progress: float | None = None, phase: str | None = None, detail: str | None = None) -> None:
        if self.job is None:
            return
        values = {**asdict(self.job), "state": state, "updated_at": _now()}
        if progress is not None:
            values["progress"] = progress
        if phase is not None:
            values["phase"] = phase
        if detail is not None:
            values["detail"] = detail
        self.job = Qwen3Job(**values)
        self._persist()
        self.state_changed.emit(self.job)

    def _capture_training_metrics(self, line: str) -> None:
        values = {key: float(value) for key, value in re.findall(r"['\"]?(loss|learning_rate|epoch|grad_norm)['\"]?\s*[=:]\s*([0-9.eE+-]+)", line)}
        if not values or self.job is None:
            return
        metrics = {**self.job.metrics, **values, "raw": line}
        self.job = Qwen3Job(**{**asdict(self.job), "updated_at": _now(), "metrics": metrics, "detail": line})
        self._persist(); self.state_changed.emit(self.job)

    def _persist(self) -> None:
        if self.job is None:
            return
        self.job_root.mkdir(parents=True, exist_ok=True)
        path = self.job_root / f"{self.job.job_id}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({**asdict(self.job), "state": self.job.state.value}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    def _persist_log(self, text: str) -> None:
        """Keep the exact child-process error after the UI has been closed."""
        if self.job is None or not text.strip():
            return
        self.job_root.mkdir(parents=True, exist_ok=True)
        path = self.job_root / f"{self.job.job_id}.log"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text.rstrip() + "\n")

    def _remember_pause_checkpoint(self) -> None:
        """Persist the exact checkpoint created for this pause request.

        This also protects pre-existing shared output directories: selecting
        ``auto`` there could otherwise prefer a larger checkpoint belonging to
        an older task with matching data/config hashes.
        """
        if self.job is None or self.job.kind != "training":
            return
        try:
            from training.qwen3.config import load_training_config

            run_dir = load_training_config(self.job.config_path).run_dir
            checkpoints = [
                path for path in run_dir.glob("checkpoint-*")
                if path.is_dir() and (path / "alysia_checkpoint.json").exists()
            ]
            checkpoint = max(checkpoints, key=lambda path: path.stat().st_mtime_ns)
        except (OSError, ValueError):
            return
        metrics = {**self.job.metrics, "resume_checkpoint": str(checkpoint.resolve())}
        self.job = Qwen3Job(**{**asdict(self.job), "updated_at": _now(), "metrics": metrics})
        self._persist()

    def _resume_checkpoint_for(self, job: Qwen3Job) -> str:
        """Find this job's checkpoint, including jobs paused by an older UI."""
        exact = str(job.metrics.get("resume_checkpoint", "")).strip()
        if exact and Path(exact).is_dir():
            return exact

        # Jobs created before exact checkpoint persistence can still be
        # recovered: their durable log records the final completed step before
        # TRAINING_PAUSED, and a cooperative pause always saves that step.
        paused_step: int | None = None
        last_step: int | None = None
        log_path = self.job_root / f"{job.job_id}.log"
        try:
            for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("[training_progress] "):
                    value = json.loads(line.removeprefix("[training_progress] "))
                    last_step = int(value.get("completed", 0))
                elif "TRAINING_PAUSED:" in line and last_step is not None:
                    paused_step = last_step
        except (OSError, ValueError, json.JSONDecodeError):
            return ""
        if paused_step is None:
            return ""
        try:
            from training.qwen3.config import load_training_config

            checkpoint = load_training_config(job.config_path).run_dir / f"checkpoint-{paused_step}"
            return str(checkpoint.resolve()) if (checkpoint / "alysia_checkpoint.json").exists() else ""
        except (OSError, ValueError):
            return ""


def load_incomplete_jobs(root: Path = JOB_ROOT) -> list[Qwen3Job]:
    jobs: list[Qwen3Job] = []
    for path in root.glob("*.json") if root.exists() else ():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            state = Qwen3JobState(str(value["state"]))
            if state in {Qwen3JobState.BUILDING_DATASET, Qwen3JobState.RUNNING, Qwen3JobState.PAUSING, Qwen3JobState.PAUSED, Qwen3JobState.FINISHING, Qwen3JobState.EXPORTING}:
                # A freshly launched desktop process owns no QProcess from a
                # previous run. Convert any in-flight persisted state into a
                # resumable pause so its controls never get stuck disabled.
                if state is not Qwen3JobState.PAUSED:
                    state = Qwen3JobState.PAUSED
                    value["state"] = state.value
                    value["updated_at"] = _now()
                    value["detail"] = "上次进程已中断；可从已保存内容继续。"
                    temporary = path.with_suffix(".tmp")
                    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    temporary.replace(path)
                jobs.append(Qwen3Job(**{**value, "state": state}))
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return sorted(jobs, key=lambda item: item.updated_at, reverse=True)


def _relative_to_root(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _training_powershell_args(
    config: str,
    *,
    pause_file: Path | None,
    finish_file: Path | None,
    resume: bool,
    resume_from: str = "auto",
    python: str | None = None,
) -> list[str]:
    """Build mutually exclusive start/resume command-line flags.

    A config normally says ``resume_from: auto``.  Passing ``-FromScratch``
    for a fresh UI task is therefore essential: it overrides that default
    and prevents unrelated historical checkpoints from being loaded.
    """
    args = [
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "quickstart/train_qwen3.ps1",
        "-Config", config, "-PauseFile", str(pause_file), "-FinishFile", str(finish_file), "-Activate",
    ]
    if python:
        args.extend(["-Python", python])
    if resume:
        args.extend(["-ResumeFrom", resume_from])
    else:
        args.append("-FromScratch")
    return args


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
