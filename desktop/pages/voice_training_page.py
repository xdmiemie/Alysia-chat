from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Qt, QProcess, QProcessEnvironment, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QPushButton, QProgressBar,
                               QScrollArea, QSpinBox, QTabWidget, QTextEdit, QVBoxLayout, QWidget)

from desktop.controllers.audio_playback import VoicePlaybackController
from desktop.controllers.voice_training_worker import VoiceTrainingWorker, load_paused_voice_jobs
from desktop.widgets import LogConsole
from runtime.gpt_sovits import model_registry
from runtime.common.python_executable import resolve_runtime_python
from training.gpt_sovits.config import load_training_config

ROOT = Path(__file__).resolve().parents[2]


class VoiceTrainingPage(QScrollArea):
    models_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("voiceTrainingPage")
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.character_id = ""
        self._config_path: Path | None = None
        self._paused_job: dict[str, object] | None = None
        self._clear_training_requested = False
        self._runtime_busy = False
        self.worker = VoiceTrainingWorker(self)
        self.playback = VoicePlaybackController(self)
        self.preview_process: QProcess | None = None
        self._preview_buffer = ""
        self._preview_files: dict[str, str] = {}
        content = QWidget()
        content.setMinimumHeight(420)
        self.setWidget(content)
        root = QVBoxLayout(content)
        root.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("voiceTrainingTabs")
        self.tabs.addTab(self._dataset_tab(), "语音数据")
        self.tabs.addTab(self._parameters_tab(), "S1 / S2 参数")
        self.tabs.addTab(self._task_tab(), "任务控制")
        self.tabs.addTab(self._preview_tab(), "A/B 试听")
        root.addWidget(self.tabs)
        self.logs = LogConsole()
        self.logs.setObjectName("voiceTrainingLog")
        self.logs.setMaximumHeight(180)
        root.addWidget(self.logs)
        self.worker.log.connect(self.logs.append_log)
        self.worker.changed.connect(self._on_changed)
        self.worker.finished.connect(self._on_finished)
        self.playback.failed.connect(lambda _id, error: self.preview_status.setText(error))

    @staticmethod
    def _tab(name: str) -> tuple[QScrollArea, QVBoxLayout]:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setObjectName(name + "Scroll")
        widget = QWidget()
        widget.setObjectName(name)
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        scroll.setWidget(widget)
        return scroll, layout

    def _path_row(self, edit: QLineEdit, directory: bool = True) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(edit, 1)
        button = QPushButton("浏览")
        button.setObjectName("secondaryButton")
        def browse() -> None:
            path = (QFileDialog.getExistingDirectory(self, "选择目录", edit.text()) if directory else
                    QFileDialog.getOpenFileName(self, "选择文件", edit.text(), "所有文件 (*)")[0])
            if path:
                edit.setText(path)
        button.clicked.connect(browse)
        row.addWidget(button)
        return row

    def _dataset_tab(self) -> QWidget:
        tab, layout = self._tab("voiceDatasetTab")
        form = QFormLayout()
        self.dataset_root = QLineEdit(); self.dataset_root.setObjectName("voiceDatasetRoot")
        self.raw_audio = QLineEdit(); self.raw_audio.setObjectName("voiceRawAudio")
        self.manifest = QLineEdit(); self.manifest.setObjectName("voiceManifest")
        self.preprocessing_output = QLineEdit(); self.preprocessing_output.setObjectName("voicePreprocessOutput")
        self.validation_ratio = QDoubleSpinBox(); self.validation_ratio.setDecimals(2); self.validation_ratio.setRange(.01, .5); self.validation_ratio.setSingleStep(.05)
        for label, edit, directory in (("数据集根目录", self.dataset_root, True), ("原始音频目录", self.raw_audio, True),
                                       ("Manifest JSONL", self.manifest, False), ("预处理输出目录", self.preprocessing_output, True)):
            form.addRow(label, self._path_row(edit, directory))
        form.addRow("验证集比例", self.validation_ratio)
        layout.addLayout(form)
        actions = QHBoxLayout()
        for label, action in (("扫描数据集", "scan"), ("校验音频", "validate"), ("预处理 / 转写", "prepare")):
            button = QPushButton(label)
            button.setObjectName("secondaryButton" if action != "prepare" else "primaryButton")
            button.clicked.connect(lambda _checked=False, value=action: self._start(value))
            actions.addWidget(button)
        layout.addLayout(actions)
        self.dataset_summary = QLabel("尚未扫描")
        self.dataset_summary.setWordWrap(True)
        self.dataset_summary.setObjectName("pageSubtitle")
        layout.addWidget(self.dataset_summary)
        layout.addWidget(QLabel("问题样本（含 manifest 行号和音频路径）"))
        self.dataset_issues = QTextEdit()
        self.dataset_issues.setObjectName("voiceDatasetIssues")
        self.dataset_issues.setReadOnly(True)
        layout.addWidget(self.dataset_issues, 1)
        return tab

    def _parameters_tab(self) -> QWidget:
        tab, layout = self._tab("voiceParametersTab")
        form = QFormLayout()
        self.s1_pretrained = QLineEdit(); self.s2_pretrained = QLineEdit()
        form.addRow("预训练 S1 权重", self._path_row(self.s1_pretrained, False))
        form.addRow("预训练 S2 权重", self._path_row(self.s2_pretrained, False))
        self.s1_epochs = QSpinBox(); self.s1_epochs.setRange(1, 10000)
        self.s2_epochs = QSpinBox(); self.s2_epochs.setRange(1, 10000)
        self.s1_batch = QSpinBox(); self.s1_batch.setRange(1, 512)
        self.s2_batch = QSpinBox(); self.s2_batch.setRange(1, 512)
        self.learning_rate = QDoubleSpinBox(); self.learning_rate.setDecimals(7); self.learning_rate.setRange(.0000001, 1); self.learning_rate.setSingleStep(.00001)
        self.save_interval = QSpinBox(); self.save_interval.setRange(1, 100000)
        self.save_limit = QSpinBox(); self.save_limit.setRange(1, 100)
        self.num_workers = QSpinBox(); self.num_workers.setRange(0, 64)
        self.precision = QComboBox(); self.precision.addItems(["fp16", "bf16", "fp32"])
        self.seed = QSpinBox(); self.seed.setRange(0, 2**31 - 1)
        self.resume_from = QLineEdit("auto")
        self.force_scratch = QCheckBox("新实验从头训练")
        self.force_scratch.setChecked(True)
        self.outputs_dir = QLineEdit(); self.experiment_name = QLineEdit()
        for label, widget in (("S1 epochs", self.s1_epochs), ("S2 epochs", self.s2_epochs),
                              ("S1 batch size", self.s1_batch), ("S2 batch size", self.s2_batch),
                              ("Learning rate", self.learning_rate), ("保存间隔（step）", self.save_interval),
                              ("保留检查点数", self.save_limit), ("Num workers", self.num_workers),
                              ("Precision", self.precision), ("Seed", self.seed),
                              ("Resume from", self.resume_from), ("Force from scratch", self.force_scratch)):
            form.addRow(label, widget)
        form.addRow("输出目录", self._path_row(self.outputs_dir))
        form.addRow("实验名称", self.experiment_name)
        layout.addLayout(form)
        button = QPushButton("保存本次训练配置")
        button.setObjectName("secondaryButton")
        button.clicked.connect(self._save_config_clicked)
        layout.addWidget(button)
        layout.addStretch(1)
        return tab

    def _task_tab(self) -> QWidget:
        tab, layout = self._tab("voiceTaskTab")
        self.task_state = QLabel("任务状态：空闲")
        self.task_state.setObjectName("sectionTitle")
        layout.addWidget(self.task_state)
        self.progress = QProgressBar(); self.progress.setRange(0, 100)
        layout.addWidget(self.progress)
        self.phase = QLabel("等待任务开始")
        self.phase.setObjectName("pageSubtitle")
        self.phase.setWordWrap(True)
        layout.addWidget(self.phase)
        hint = QLabel("暂停会在保存间隔结束后保留检查点；结束并导出会生成 S1/S2 权重并验证应用；结束并清除会删除本次实验输出，保留原始音频。")
        hint.setWordWrap(True); hint.setObjectName("pageSubtitle")
        layout.addWidget(hint)
        actions = QHBoxLayout()
        self.start_button = QPushButton("开始训练"); self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(lambda: self._start("train"))
        self.pause_button = QPushButton("暂停训练"); self.pause_button.clicked.connect(self._toggle_pause)
        self.finish_button = QPushButton("结束并导出"); self.finish_button.clicked.connect(self._finish)
        self.clear_button = QPushButton("结束并清除训练数据"); self.clear_button.clicked.connect(self._end_and_clear_training_data)
        for button in (self.start_button, self.pause_button, self.finish_button, self.clear_button):
            if button is not self.start_button:
                button.setObjectName("secondaryButton")
            actions.addWidget(button)
        layout.addLayout(actions)
        layout.addStretch(1)
        self._buttons("IDLE")
        return tab

    def _preview_tab(self) -> QWidget:
        tab, layout = self._tab("voicePreviewTab")
        form = QFormLayout()
        self.preview_model = QComboBox(); self.preview_model.setObjectName("voicePreviewModel")
        self.preview_emotion = QComboBox(); self.preview_emotion.addItems(["neutral", "happy", "gentle", "sad", "surprised", "serious"])
        self.preview_text = QComboBox(); self.preview_text.setObjectName("voicePreviewText")
        self.preview_text.addItems(["你好呀，今天过得怎么样？", "辛苦啦，先休息一下吧。",
                                    "真的？那也太让人开心了吧！", "别担心，我会陪着你的。"])
        form.addRow("旧模型 / 候选模型", self.preview_model)
        form.addRow("情绪", self.preview_emotion)
        form.addRow("固定测试句", self.preview_text)
        layout.addLayout(form)
        actions = QHBoxLayout()
        generate = QPushButton("生成试听"); generate.setObjectName("primaryButton"); generate.clicked.connect(self._generate_preview)
        play = QPushButton("播放当前模型"); play.clicked.connect(self._play_preview)
        actions.addWidget(generate); actions.addWidget(play)
        layout.addLayout(actions)
        self.preview_status = QLabel("选择模型与测试句后生成试听；切换模型即可 A/B 对比。")
        self.preview_status.setWordWrap(True)
        layout.addWidget(self.preview_status)
        layout.addStretch(1)
        return tab

    def set_character_id(self, character_id: str) -> None:
        if character_id == self.character_id:
            return
        self.character_id = character_id
        self._config_path = None
        path = ROOT / "training" / "gpt_sovits" / "configs" / "characters" / f"{character_id}.yaml"
        if not path.exists():
            self.dataset_summary.setText(f"{character_id} 尚无 GPT-SoVITS 训练配置。")
            return
        config = load_training_config(path)
        self.dataset_root.setText(str(config.dataset.root))
        self.raw_audio.setText(str(config.dataset.raw_audio_dir))
        self.manifest.setText(str(config.dataset.raw_manifest))
        self.preprocessing_output.setText(str(config.dataset.normalized_manifest.parent))
        self.validation_ratio.setValue(config.dataset.validation_ratio)
        self.s1_pretrained.setText(str(config.base_model.s1_checkpoint))
        self.s2_pretrained.setText(str(config.base_model.s2_checkpoint))
        count = self._sample_count(config.dataset.raw_manifest) or 100
        self.s1_epochs.setValue(config.s1.epochs or max(1, ceil_div(config.s1.max_steps, ceil_div(count, config.s1.batch_size))))
        self.s2_epochs.setValue(config.s2.epochs or max(1, ceil_div(config.s2.max_steps, ceil_div(count, config.s2.batch_size))))
        self.s1_batch.setValue(config.s1.batch_size); self.s2_batch.setValue(config.s2.batch_size)
        self.learning_rate.setValue(config.s1.learning_rate)
        self.save_interval.setValue(config.s1.save_steps); self.save_limit.setValue(config.s1.save_total_limit)
        self.num_workers.setValue(config.s1.num_workers); self.precision.setCurrentText(config.s1.precision)
        self.seed.setValue(config.seed); self.resume_from.setText(config.resume_from)
        self.outputs_dir.setText(str(config.outputs_dir))
        self.experiment_name.setText(config.run_name)
        paused = [job for job in load_paused_voice_jobs() if job.get("character_id") == character_id]
        self._paused_job = paused[0] if paused else None
        if self._paused_job:
            self._on_changed(self._paused_job)
        else:
            self._buttons("IDLE")
        self.refresh_models()

    def set_runtime_busy(self, busy: bool) -> None:
        self._runtime_busy = busy

    @staticmethod
    def _sample_count(path: Path) -> int:
        try:
            return sum(bool(line.strip()) for line in path.read_text(encoding="utf-8-sig").splitlines())
        except OSError:
            return 0

    def _save_config_clicked(self) -> None:
        try:
            self._config_path = self._save_config()
            self.logs.append_log(f"[config] 已保存 {self._config_path}")
        except (OSError, ValueError) as exc:
            self.phase.setText(str(exc))

    def _save_config(self) -> Path:
        try:
            import yaml
        except ModuleNotFoundError as exc:
            raise RuntimeError("缺少 PyYAML；请运行 python -m pip install -r desktop/requirements.txt") from exc
        base_path = ROOT / "training" / "gpt_sovits" / "configs" / "characters" / f"{self.character_id}.yaml"
        if not base_path.exists():
            raise ValueError("当前角色缺少 GPT-SoVITS 训练模板")
        raw = yaml.safe_load(base_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("训练模板内容无效")
        name = self.experiment_name.text().strip()
        if not name or any(char in name for char in '\\/:*?"<>|'):
            raise ValueError("实验名称不能为空，且不能包含路径字符")
        # A new run directory also keeps unrelated historical checkpoints out
        # of the native upstream auto-resume search.
        raw["run_name"] = f"{name}-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"
        raw["outputs_dir"] = self.outputs_dir.text().strip()
        # New experiments always use a distinct run directory.  The Resume
        # from control selects a previous desktop job; the recipe itself uses
        # its own native checkpoint search within that job's directory.
        raw["resume_from"] = "auto"
        raw["seed"] = self.seed.value()
        dataset = raw.setdefault("dataset", {})
        dataset["root"] = self.dataset_root.text().strip()
        dataset["raw_audio_dir"] = self.raw_audio.text().strip()
        dataset["raw_manifest"] = self.manifest.text().strip()
        work = Path(self.preprocessing_output.text().strip())
        dataset["normalized_manifest"] = str(work / "manifest.normalized.jsonl")
        dataset["validation_report"] = str(work / "validation_report.json")
        dataset["validation_ratio"] = self.validation_ratio.value()
        base = raw.setdefault("base_model", {})
        base["s1_checkpoint"] = self.s1_pretrained.text().strip()
        base["s2_checkpoint"] = self.s2_pretrained.text().strip()
        count = self._sample_count(Path(dataset["raw_manifest"])) or len(list(Path(dataset["raw_audio_dir"]).rglob("*.wav"))) or 1
        for stage_name, epochs, batch in (("s1", self.s1_epochs.value(), self.s1_batch.value()),
                                          ("s2", self.s2_epochs.value(), self.s2_batch.value())):
            stage = raw.setdefault(stage_name, {})
            stage["batch_size"] = batch
            stage["epochs"] = epochs
            stage["max_steps"] = epochs * ceil_div(count, batch)
            stage["save_steps"] = self.save_interval.value()
            stage["save_total_limit"] = self.save_limit.value()
            stage["learning_rate"] = self.learning_rate.value()
            stage["num_workers"] = self.num_workers.value()
            stage["precision"] = self.precision.currentText()
        target = ROOT / "storage" / "training_jobs" / "configs" / f"{self.character_id}-voice-{uuid4().hex}.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return target

    def _start(self, action: str) -> None:
        if self.worker.is_running:
            self.phase.setText("已有训练任务正在运行")
            return
        try:
            if action == "train" and self._runtime_busy:
                raise RuntimeError("请先在使用界面关闭运行模型，释放 GPU 后再训练")
            if action == "train" and not self.force_scratch.isChecked():
                if self._paused_job:
                    self._resume()
                    return
                resume_path = Path(self.resume_from.text().strip())
                if not resume_path.name.endswith(".voice.json") or not resume_path.is_file():
                    raise ValueError("Resume from 请填写暂停任务的 .voice.json 路径，或勾选从头训练")
                job = json.loads(resume_path.read_text(encoding="utf-8"))
                if job.get("character_id") != self.character_id or job.get("state") != "PAUSED":
                    raise ValueError("所选任务不是当前角色的暂停任务")
                self._paused_job = job
                self._resume()
                return
            if action == "train" and self._paused_job:
                raise ValueError("已有暂停任务，请先从检查点继续")
            path = self._save_config()
            self._config_path = path
            if action == "train":
                self.refresh_models()
            self.worker.start(action, path)
            self.tabs.setCurrentIndex(2 if action == "train" else 0)
        except (OSError, ValueError, RuntimeError) as exc:
            self.phase.setText(str(exc))
            self.logs.append_log(f"[voice:error] {exc}")

    def _resume(self, *, finish: bool = False) -> None:
        if self.worker.is_running:
            self.phase.setText("当前任务尚未退出，请等待检查点保存完成")
            return
        if self._runtime_busy:
            self.phase.setText("请先关闭运行模型，释放 GPU 后再继续训练")
            return
        if not self._paused_job:
            self.phase.setText("没有可恢复的训练任务")
            return
        try:
            self.worker.start("train", Path(str(self._paused_job["config_path"])),
                              resume=self._paused_job, finish=finish)
        except (OSError, ValueError, RuntimeError) as exc:
            self.phase.setText(str(exc))

    def _finish(self) -> None:
        if self.worker.is_running:
            self.worker.request_finish()
        else:
            self._resume(finish=True)

    def _toggle_pause(self) -> None:
        if self.worker.is_running:
            self.worker.request_pause()
        else:
            self._resume()

    def _end_and_clear_training_data(self) -> None:
        job = self.worker.job or self._paused_job
        if not job or job.get("action") != "train":
            return
        self._clear_training_requested = True
        if self.worker.is_running:
            self.worker.request_pause()
            self.phase.setText("正在安全结束训练；随后清除本次实验结果。")
        elif self._paused_job:
            self._discard_training_results(self._paused_job)

    def _discard_training_results(self, job: dict[str, object]) -> None:
        try:
            config = load_training_config(Path(str(job["config_path"])))
            run_dir = config.run_dir.resolve()
            character_root = (config.outputs_dir / str(job["character_id"])).resolve()
            if config.character_id != job["character_id"] or run_dir.parent != character_root:
                raise ValueError(f"拒绝清理范围外训练输出：{run_dir}")
            if run_dir.exists():
                shutil.rmtree(run_dir)
            self.worker.discard_paused_job(job)
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            self._clear_training_requested = False
            self.phase.setText(f"无法清除训练结果：{exc}")
            return
        self._paused_job = None
        self._clear_training_requested = False
        self.task_state.setText("任务状态：已结束并清除训练结果")
        self.progress.setValue(0)
        self.phase.setText("本次实验输出和检查点已清除；原始音频保留。")
        self.logs.append_log("[voice] 已结束训练并清除本次实验结果")
        self._buttons("IDLE")

    def _on_changed(self, job: dict[str, object]) -> None:
        self.task_state.setText(f"任务状态：{job.get('state')} · {job.get('phase')}")
        detail = str(job.get("detail") or "")
        if job.get("state") == "PAUSED" and job.get("resume_checkpoint"):
            detail += f"\n检查点：{job['resume_checkpoint']}"
        self.phase.setText(detail)
        self.progress.setValue(round(float(job.get("progress") or 0)))
        self._buttons(str(job.get("state")))
        report = job.get("report")
        if isinstance(report, dict):
            stats = report.get("stats", {})
            if isinstance(stats, dict):
                self.dataset_summary.setText(
                    f"总样本 {stats.get('total_samples', 0)} · 有效 {report.get('record_count', 0)} · "
                    f"总时长 {report.get('total_duration_seconds', 0)} 秒 · 平均 {stats.get('average_duration_seconds', 0)} 秒\n"
                    f"情绪 {stats.get('emotion_counts', {})} · 过短 {stats.get('too_short', 0)} · "
                    f"过长 {stats.get('too_long', 0)} · 空文本 {stats.get('empty_text', 0)} · "
                    f"损坏 {stats.get('damaged', 0)} · 重复 {stats.get('duplicates', 0)}")
            issues = [*report.get("errors", []), *report.get("warnings", [])]
            self.dataset_issues.setPlainText("\n".join(str(issue) for issue in issues) or "未发现问题")

    def _on_finished(self, job: dict[str, object]) -> None:
        state = str(job.get("state"))
        self._config_path = None
        if state == "PAUSED":
            self._paused_job = job
            if self._clear_training_requested:
                self._discard_training_results(job)
                return
        else:
            self._clear_training_requested = False
        if state == "COMPLETED" and job.get("action") == "train":
            self._paused_job = None
            self.models_changed.emit()
            self.refresh_models()
            self.logs.append_log("[voice:done] 候选已完成固定句验证并应用到角色")
        elif state == "FAILED" and job.get("action") == "train":
            self.refresh_models()
        self._buttons(state)

    def _buttons(self, state: str) -> None:
        running = state in {"RUNNING", "PAUSING", "FINISHING"}
        training = self.worker.job is not None and self.worker.job.get("action") == "train"
        resumable = state in {"PAUSED", "FAILED"} and self._paused_job is not None and not self.worker.is_running
        self.start_button.setEnabled(not running and self._paused_job is None)
        self.pause_button.setEnabled((state == "RUNNING" and training and self.worker.is_running) or resumable)
        self.pause_button.setText("正在保存检查点…" if state == "PAUSING" else "继续训练" if resumable else "暂停训练")
        self.finish_button.setEnabled((state == "RUNNING" and training and self.worker.is_running) or resumable)
        self.clear_button.setEnabled((state in {"RUNNING", "PAUSING"} and training and self.worker.is_running) or resumable)
        for button in (self.start_button, self.pause_button, self.finish_button, self.clear_button):
            button.setCursor(Qt.CursorShape.PointingHandCursor if button.isEnabled() else Qt.CursorShape.ArrowCursor)

    def refresh_models(self) -> None:
        previous = self.preview_model.currentData()
        self.preview_model.clear()
        if not self.character_id:
            return
        try:
            manifest = model_registry.load_character_manifest(self.character_id)
            active = str(manifest.get("active_model_id") or "")
            for model in model_registry.list_models(self.character_id):
                label = "当前" if model.model_id == active else "候选 / 历史"
                self.preview_model.addItem(f"{label} · {model.display_name}", model.model_id)
            index = self.preview_model.findData(previous)
            if index >= 0:
                self.preview_model.setCurrentIndex(index)
        except (OSError, ValueError) as exc:
            self.preview_status.setText(str(exc))

    def _preview_key(self) -> str:
        return "|".join((str(self.preview_model.currentData()), self.preview_emotion.currentText(),
                         self.preview_text.currentText()))

    def _generate_preview(self) -> None:
        if self._runtime_busy:
            self.preview_status.setText("请先关闭运行模型，以便独立加载试听模型")
            return
        if self.preview_process is not None and self.preview_process.state() != QProcess.ProcessState.NotRunning:
            return
        model_id = str(self.preview_model.currentData() or "")
        if not model_id:
            self.preview_status.setText("请先选择旧模型或候选模型")
            return
        try:
            runtime_python = resolve_runtime_python(ROOT)
        except FileNotFoundError as exc:
            self.preview_status.setText(str(exc))
            return
        process = QProcess(self)
        process.setWorkingDirectory(str(ROOT))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        process.setProcessEnvironment(env)
        output = ROOT / "storage" / "generated_audio" / "voice_training_preview" / uuid4().hex
        process.readyReadStandardOutput.connect(self._read_preview)
        process.finished.connect(self._preview_finished)
        self.preview_process = process
        self._preview_buffer = ""
        self.preview_status.setText("正在生成试听…")
        process.start(str(runtime_python), [str(ROOT / "training" / "gpt_sovits" / "preview.py"),
                                      "--character", self.character_id, "--model", model_id,
                                      "--emotion", self.preview_emotion.currentText(),
                                      "--text", self.preview_text.currentText(), "--output-dir", str(output)])

    def _read_preview(self) -> None:
        if self.preview_process is None:
            return
        chunk = bytes(self.preview_process.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._preview_buffer += chunk
        lines = self._preview_buffer.splitlines(keepends=True)
        self._preview_buffer = ""
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._preview_buffer = lines.pop()
        for line in lines:
            if line.startswith("[voice_preview] "):
                try:
                    result = json.loads(line.removeprefix("[voice_preview] "))
                    key = "|".join((result["model_id"], result["emotion"], result["text"]))
                    self._preview_files[key] = result["audio_path"]
                    self.preview_status.setText(f"生成耗时 {result['elapsed_ms']} ms · {result['audio_path']}")
                except (ValueError, KeyError):
                    pass
            else:
                self.logs.append_log(line.rstrip())

    def _preview_finished(self, code: int, _status: QProcess.ExitStatus) -> None:
        if code:
            self.preview_status.setText(f"试听生成失败（退出码 {code}）；详见任务日志")
        if self.preview_process is not None:
            self.preview_process.deleteLater()
        self.preview_process = None

    def _play_preview(self) -> None:
        path = self._preview_files.get(self._preview_key())
        if not path:
            self.preview_status.setText("请先生成此模型、情绪和测试句的试听")
            return
        self.playback.toggle(self._preview_key(), path)

    def shutdown(self) -> None:
        self.playback.stop()
        if self.worker.is_running and self.worker.job and self.worker.job.get("action") == "train":
            self.worker.request_pause()
        if self.preview_process is not None and self.preview_process.state() != QProcess.ProcessState.NotRunning:
            self.preview_process.kill()


def ceil_div(value: int, divisor: int) -> int:
    return max(1, (value + max(1, divisor) - 1) // max(1, divisor))
