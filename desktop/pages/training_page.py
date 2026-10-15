from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QPointF, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QColor, QPaintEvent, QPainter, QPolygonF
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QProgressBar, QScrollArea, QSpinBox, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from desktop.characters import CharacterManager
from desktop.state import AppSnapshot
from desktop.widgets import LogConsole
from desktop.controllers.training_worker import Qwen3Job, Qwen3JobState, Qwen3TrainingWorker, load_incomplete_jobs
from desktop.controllers.teacher_service import FixedTeacherService, TEACHER_BASE_URL, TEACHER_MODEL_NAME
from desktop.pages.voice_training_page import VoiceTrainingPage
from training.qwen3.config import load_training_config
from training.qwen3.generate.pipeline import normalize_quotes


class TrainingPage(QWidget):
    characters_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("trainingPage")
        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        self.page_scroll = QScrollArea()
        self.page_scroll.setObjectName("trainingPageScroll")
        self.page_scroll.setWidgetResizable(True)
        self.page_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.page_content = QWidget()
        self.page_content.setObjectName("trainingPageContent")
        self.page_content.setMinimumHeight(1120)
        self.page_scroll.setWidget(self.page_content)
        shell.addWidget(self.page_scroll)
        root = QVBoxLayout(self.page_content)
        root.setContentsMargins(30, 26, 30, 24)
        root.setSpacing(16)

        title = QLabel("训练界面")
        title.setObjectName("pageTitle")
        subtitle = QLabel("训练任务的配置与日志工作区")
        subtitle.setObjectName("pageSubtitle")
        root.addWidget(title)
        root.addWidget(subtitle)

        manager_header = QHBoxLayout()
        heading = QLabel("角色管理中心")
        heading.setObjectName("sectionTitle")
        manager_header.addWidget(heading)
        manager_header.addStretch(1)
        root.addLayout(manager_header)
        manager_hint = QLabel("创建和管理角色配置、头像、参考语音与本地角色资源。")
        manager_hint.setObjectName("pageSubtitle")
        root.addWidget(manager_hint)
        self.character_manager = CharacterManager(_project_root())
        self.character_combo = CharacterManagerComboBox()
        self.character_combo.setObjectName("characterManagerCombo")
        self._reload_characters()
        manager_actions = QHBoxLayout()
        manager_actions.addWidget(self.character_combo, 1)
        self.create_character_button = QPushButton("创建角色")
        self.create_character_button.setObjectName("createCharacterButton")
        self.edit_character_button = QPushButton("编辑")
        self.copy_character_button = QPushButton("复制")
        self.import_character_button = QPushButton("导入")
        self.export_character_button = QPushButton("导出")
        self.delete_character_button = QPushButton("删除")
        for button in (self.create_character_button, self.edit_character_button, self.copy_character_button, self.import_character_button, self.export_character_button, self.delete_character_button):
            button.setObjectName("secondaryButton")
            manager_actions.addWidget(button)
        root.addLayout(manager_actions)
        self.create_character_button.clicked.connect(lambda: self._open_editor())
        self.edit_character_button.clicked.connect(lambda: self._open_editor(self._selected_id()))
        self.copy_character_button.clicked.connect(self._copy_selected)
        self.import_character_button.clicked.connect(self._import_bundle)
        self.export_character_button.clicked.connect(self._export_selected)
        self.delete_character_button.clicked.connect(self._delete_selected)

        self.training_worker = Qwen3TrainingWorker(self)
        self._clear_training_data_requested = False
        self._clear_dataset_requested = False
        self.teacher_service = FixedTeacherService(self)
        self._teacher_ready = False
        self._build_after_teacher_ready = False
        self.training_sections = QTabWidget()
        self.training_sections.setObjectName("trainingSections")
        self.training_sections.tabBar().setObjectName("trainingSectionBar")
        self.training_tabs = QTabWidget()
        self.training_tabs.setObjectName("qwen3TrainingTabs")
        self.training_tabs.addTab(self._build_source_tab(), "数据来源")
        self.training_tabs.addTab(self._build_settings_tab(), "Teacher 与 LoRA")
        self.training_tabs.addTab(self._build_preview_tab(), "数据预览")
        self.training_tabs.addTab(self._build_task_tab(), "任务控制")
        self.training_sections.addTab(self.training_tabs, "Qwen3 训练")
        self.voice_training_page = VoiceTrainingPage(self)
        self.voice_training_page.models_changed.connect(self._voice_models_changed)
        self.training_sections.addTab(self.voice_training_page, "GPT-SoVITS 训练")
        root.addWidget(self.training_sections)

        log_header = QLabel("任务日志")
        log_header.setObjectName("sectionTitle")
        root.addWidget(log_header)
        self.log_console = LogConsole()
        self.log_console.setPlainText(
            "[ready] 训练控制台已就绪。\n"
            "[info] 后续训练任务将在独立 worker 中运行，不会阻塞桌面主线程。"
        )
        root.addWidget(self.log_console, 1)
        self.training_worker.log.connect(self.log_console.append_log)
        self.training_worker.state_changed.connect(self._apply_job_state)
        self.training_worker.finished.connect(self._on_job_finished)
        self.teacher_service.status_changed.connect(self._on_teacher_status)
        self.teacher_service.ready.connect(self._on_teacher_ready)
        self.teacher_service.failed.connect(self._on_teacher_failed)
        self.character_combo.currentIndexChanged.connect(self._load_character_training_config)
        self._load_character_training_config()
        incomplete = load_incomplete_jobs()
        if incomplete:
            self._restored_job = incomplete[0]
            self._apply_job_state(self._restored_job)
            target = "数据来源页" if self._restored_job.kind == "dataset" else "任务控制页"
            self.log_console.append_log(f"[resume] 检测到暂停任务 {self._restored_job.job_id[:8]}；请在{target}继续或结束。")
        else:
            self._restored_job = None

    def _build_source_tab(self) -> QWidget:
        tab, layout = self._scrollable_tab("qwenSourceTab", 420)
        hint = QLabel("最低只需一份 UTF-8 角色语录：TXT 每行一条，或 JSONL 每行一个至少含 text 的对象。原始语录不会被修改。")
        hint.setWordWrap(True); hint.setObjectName("pageSubtitle"); layout.addWidget(hint)
        form = QFormLayout()
        self.data_mode_combo = QComboBox(); self.data_mode_combo.setObjectName("qwenDataMode")
        self.data_mode_combo.addItem("角色语录自动生成（推荐）", "quotes")
        self.data_mode_combo.addItem("已有 Chat JSONL（高级）", "chat_jsonl")
        self.data_mode_combo.currentIndexChanged.connect(self._sync_source_mode)
        form.addRow("Qwen3 数据来源", self.data_mode_combo)
        self.source_path_edit = QLineEdit(); self.source_path_edit.setObjectName("qwenSourcePath")
        source_row = QHBoxLayout(); source_row.addWidget(self.source_path_edit, 1)
        browse = QPushButton("选择文件"); browse.setObjectName("secondaryButton"); browse.clicked.connect(self._browse_source); source_row.addWidget(browse)
        form.addRow("quotes.txt / quotes.jsonl", source_row)
        self.profile_path_edit = QLineEdit(); profile_row = QHBoxLayout(); profile_row.addWidget(self.profile_path_edit, 1)
        profile_browse = QPushButton("选择 profile.md"); profile_browse.setObjectName("secondaryButton"); profile_browse.clicked.connect(self._browse_profile); profile_row.addWidget(profile_browse)
        form.addRow("角色资料（可选）", profile_row)
        layout.addLayout(form)
        actions = QHBoxLayout()
        self.validate_source_button = QPushButton("立即校验语录"); self.validate_source_button.setObjectName("secondaryButton"); self.validate_source_button.clicked.connect(self._validate_source)
        self.build_dataset_button = QPushButton("一键准备模型并构造训练集"); self.build_dataset_button.setObjectName("primaryButton"); self.build_dataset_button.clicked.connect(self._start_dataset)
        actions.addWidget(self.validate_source_button); actions.addWidget(self.build_dataset_button); actions.addStretch(1); layout.addLayout(actions)
        self.source_stats_label = QLabel("尚未校验素材。"); self.source_stats_label.setObjectName("pageSubtitle"); layout.addWidget(self.source_stats_label)
        layout.addWidget(QLabel("构造数据任务"))
        self.dataset_state_label = QLabel("任务状态：空闲"); self.dataset_state_label.setObjectName("sectionTitle"); layout.addWidget(self.dataset_state_label)
        self.dataset_progress = QProgressBar(); self.dataset_progress.setRange(0, 100); self.dataset_progress.setValue(0); layout.addWidget(self.dataset_progress)
        self.dataset_detail_label = QLabel("等待构造训练集"); self.dataset_detail_label.setObjectName("pageSubtitle"); self.dataset_detail_label.setWordWrap(True); layout.addWidget(self.dataset_detail_label)
        dataset_actions = QHBoxLayout()
        self.dataset_pause_button = QPushButton("暂停构造"); self.dataset_pause_button.setObjectName("secondaryButton"); self.dataset_pause_button.clicked.connect(self._toggle_dataset_pause)
        self.dataset_clear_button = QPushButton("结束并清除构造结果"); self.dataset_clear_button.setObjectName("secondaryButton"); self.dataset_clear_button.clicked.connect(self._end_and_clear_dataset)
        dataset_actions.addWidget(self.dataset_pause_button); dataset_actions.addWidget(self.dataset_clear_button); dataset_actions.addStretch(1)
        layout.addLayout(dataset_actions)
        return tab

    def _build_settings_tab(self) -> QWidget:
        tab, layout = self._scrollable_tab("qwenSettingsTab", 650); form = QFormLayout()
        self.generator_model_label = QLabel(TEACHER_MODEL_NAME); self.generator_model_label.setObjectName("fixedTeacherModel")
        self.judge_model_label = QLabel("跟随 Generator · " + TEACHER_MODEL_NAME); self.judge_model_label.setObjectName("fixedTeacherModel")
        self.teacher_status_label = QLabel("正在检查固定本地 Teacher…"); self.teacher_status_label.setObjectName("pageSubtitle")
        self.target_samples_spin = QSpinBox(); self.target_samples_spin.setRange(2, 100000); self.target_samples_spin.setValue(3000)
        self.candidates_spin = QSpinBox(); self.candidates_spin.setRange(1, 8); self.candidates_spin.setValue(3)
        self.regenerate_spin = QSpinBox(); self.regenerate_spin.setRange(0, 8); self.regenerate_spin.setValue(2)
        self.model_name_edit = QLineEdit(); self.model_name_edit.setPlaceholderText("例如：爱莉希雅 · 夏日语录 LoRA"); self.model_name_edit.setClearButtonEnabled(True)
        self.model_roles_checks: dict[str, QCheckBox] = {}
        self.model_roles_widget = QWidget()
        roles_layout = QHBoxLayout(self.model_roles_widget); roles_layout.setContentsMargins(0, 0, 0, 0); roles_layout.setSpacing(10)
        for role_id, role_name in self.character_manager.list_profiles():
            check = QCheckBox(f"{role_name} ({role_id})")
            self.model_roles_checks[role_id] = check
            roles_layout.addWidget(check)
        roles_layout.addStretch(1)
        self.method_combo = QComboBox(); self.method_combo.addItems(["qlora", "lora"])
        self.hf_path_edit = QLineEdit()
        self.epochs_spin = QDoubleSpinBox(); self.epochs_spin.setRange(0.1, 100); self.epochs_spin.setSingleStep(.5); self.epochs_spin.setValue(1.0)
        self.learning_rate_spin = QDoubleSpinBox(); self.learning_rate_spin.setDecimals(6); self.learning_rate_spin.setRange(.000001, .1); self.learning_rate_spin.setSingleStep(.00001); self.learning_rate_spin.setValue(.0002)
        self.rank_spin = QSpinBox(); self.rank_spin.setRange(1, 512); self.rank_spin.setValue(16)
        self.assistant_loss_check = QCheckBox("只计算 assistant 回复的 loss"); self.assistant_loss_check.setChecked(True)
        self.bf16_check = QCheckBox("bf16（否则使用 fp16）")
        for label, widget in (("Generator 模型", self.generator_model_label), ("Judge 模型", self.judge_model_label), ("本地服务", self.teacher_status_label), ("目标样本数", self.target_samples_spin), ("每场景候选数", self.candidates_spin), ("最大重生成", self.regenerate_spin), ("训练后模型名称（必填）", self.model_name_edit), ("所属角色（可多选）", self.model_roles_widget), ("训练方式", self.method_combo), ("HF 基础模型路径", self.hf_path_edit), ("Epochs", self.epochs_spin), ("Learning rate", self.learning_rate_spin), ("LoRA rank", self.rank_spin), ("SFT loss", self.assistant_loss_check), ("精度", self.bf16_check)):
            form.addRow(label, widget)
        layout.addLayout(form)
        save = QPushButton("保存本次任务 YAML"); save.setObjectName("secondaryButton"); save.clicked.connect(self._save_job_config); layout.addWidget(save); layout.addStretch(1)
        return tab

    def _build_preview_tab(self) -> QWidget:
        tab, layout = self._scrollable_tab("qwenPreviewTab", 760)
        refresh = QPushButton("刷新 Persona、覆盖率与样本预览"); refresh.setObjectName("secondaryButton"); refresh.clicked.connect(self._refresh_preview); layout.addWidget(refresh)
        self.persona_preview = QTextEdit(); self.persona_preview.setObjectName("personaPreview"); self.persona_preview.setPlaceholderText("构造完成后可在这里查看和编辑训练用 Persona JSON。")
        save_persona = QPushButton("保存训练用 Persona（不修改原始语录）"); save_persona.setObjectName("secondaryButton"); save_persona.clicked.connect(self._save_persona_override)
        self.coverage_preview = QTextEdit(); self.coverage_preview.setObjectName("coveragePreview"); self.coverage_preview.setReadOnly(True); self.coverage_preview.setMaximumHeight(120)
        self.dataset_preview = QTextEdit(); self.dataset_preview.setObjectName("datasetPreview"); self.dataset_preview.setReadOnly(True)
        layout.addWidget(QLabel("Persona（编辑不会修改原始语录）")); layout.addWidget(self.persona_preview, 1); layout.addWidget(save_persona)
        layout.addWidget(QLabel("场景覆盖与 Judge 统计")); layout.addWidget(self.coverage_preview)
        layout.addWidget(QLabel("最终 SFT 样本预览")); layout.addWidget(self.dataset_preview, 1)
        return tab

    def _build_task_tab(self) -> QWidget:
        tab, layout = self._scrollable_tab("qwenTaskTab", 360)
        self.job_state_label = QLabel("任务状态：空闲"); self.job_state_label.setObjectName("sectionTitle"); layout.addWidget(self.job_state_label)
        self.task_progress = QProgressBar(); self.task_progress.setRange(0, 100); self.task_progress.setValue(0); self.task_progress.setFormat("%p%"); layout.addWidget(self.task_progress)
        self.task_detail_label = QLabel("等待任务开始"); self.task_detail_label.setObjectName("pageSubtitle"); self.task_detail_label.setWordWrap(True); layout.addWidget(self.task_detail_label)
        self.task_metrics_label = QLabel(""); self.task_metrics_label.setObjectName("pageSubtitle"); self.task_metrics_label.setWordWrap(True); layout.addWidget(self.task_metrics_label)
        hint = QLabel("暂停会在安全 step 保存检查点；结束并导出会停止训练并导出模型；结束并清除会删除本次派生数据和训练输出，保留原始语录。")
        hint.setWordWrap(True); hint.setObjectName("pageSubtitle"); layout.addWidget(hint)
        actions = QHBoxLayout()
        self.start_training_button = QPushButton("开始训练"); self.start_training_button.setObjectName("primaryButton"); self.start_training_button.clicked.connect(lambda: self._start_training(False))
        self.pause_training_button = QPushButton("暂停训练"); self.pause_training_button.setObjectName("secondaryButton"); self.pause_training_button.clicked.connect(self._toggle_training_pause)
        self.clear_training_data_button = QPushButton("结束并清除训练数据"); self.clear_training_data_button.setObjectName("secondaryButton"); self.clear_training_data_button.clicked.connect(self._end_and_clear_training_data)
        self.finish_training_button = QPushButton("结束并导出"); self.finish_training_button.setObjectName("secondaryButton"); self.finish_training_button.clicked.connect(self._finish_training)
        for button in (self.start_training_button, self.pause_training_button, self.finish_training_button, self.clear_training_data_button): actions.addWidget(button)
        actions.addStretch(1); layout.addLayout(actions); self._set_task_buttons(Qwen3JobState.SOURCE_READY)
        layout.addWidget(QLabel("该角色的 Qwen3 模型版本"))
        models_row = QHBoxLayout()
        self.qwen_model_versions = QComboBox(); models_row.addWidget(self.qwen_model_versions, 1)
        refresh_models = QPushButton("刷新版本"); refresh_models.setObjectName("secondaryButton")
        refresh_models.clicked.connect(self._refresh_qwen_models); models_row.addWidget(refresh_models)
        layout.addLayout(models_row)
        return tab

    @staticmethod
    def _scrollable_tab(object_name: str, minimum_height: int) -> tuple[QScrollArea, QVBoxLayout]:
        """Keep controls at usable dimensions when the main window is short."""
        scroll = QScrollArea()
        scroll.setObjectName(f"{object_name}Scroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        content = QWidget()
        content.setObjectName(object_name)
        content.setMinimumHeight(minimum_height)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        scroll.setWidget(content)
        return scroll, layout

    def apply_state(self, _snapshot: AppSnapshot) -> None:
        self._active_character_id = _snapshot.character_id
        self.voice_training_page.set_runtime_busy(_snapshot.model_service_running)
        current = self.character_combo.findData(_snapshot.character_id)
        if current >= 0 and current != self.character_combo.currentIndex():
            self.character_combo.setCurrentIndex(current)

    def _sync_source_mode(self) -> None:
        quotes = self.data_mode_combo.currentData() == "quotes"
        self.validate_source_button.setText("立即校验语录" if quotes else "立即校验 Chat JSONL")
        self.build_dataset_button.setEnabled(quotes)
        self.source_path_edit.setPlaceholderText("选择 quotes.txt / quotes.jsonl" if quotes else "选择已有 Chat JSONL")

    def _browse_source(self) -> None:
        filters = "角色语录 (*.txt *.jsonl)" if self.data_mode_combo.currentData() == "quotes" else "Chat JSONL (*.jsonl)"
        path, _ = QFileDialog.getOpenFileName(self, "选择 Qwen3 数据来源", "", filters)
        if path:
            self.source_path_edit.setText(path); self._validate_source()

    def _browse_profile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择可选角色资料", "", "Markdown (*.md);;所有文件 (*)")
        if path:
            self.profile_path_edit.setText(path)

    def _validate_source(self) -> None:
        path = Path(self.source_path_edit.text().strip())
        if not path.exists():
            self.source_stats_label.setText("素材文件不存在。")
            return
        try:
            if self.data_mode_combo.currentData() == "quotes":
                quotes, rejected = normalize_quotes(path)
                source_count = len([line for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()])
                sources = len({str(item.source.get("source", "")) for item in quotes if item.source.get("source")})
                self.source_stats_label.setText(f"原始 {source_count} 条 · 有效 {len(quotes)} 条 · 重复/无效 {rejected} 条 · 来源 {sources} 个")
            else:
                from training.qwen3.preprocess.validate import validate_file
                count = validate_file(path)
                self.source_stats_label.setText(f"Chat JSONL 校验通过：{count} 条样本。")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.source_stats_label.setText(f"校验失败：{exc}")

    def _load_character_training_config(self, *_args) -> None:
        character_id = self._selected_id()
        if hasattr(self, "voice_training_page"):
            self.voice_training_page.set_character_id(character_id)
        self._sync_model_roles(character_id)
        self._refresh_qwen_models()
        if not character_id:
            return
        path = _project_root() / "training" / "qwen3" / "configs" / "characters" / f"{character_id}.yaml"
        if not path.exists():
            self.source_stats_label.setText(f"尚未找到 {character_id} 的 Qwen3 配置。")
            return
        try:
            config = load_training_config(path)
        except (OSError, ValueError) as exc:
            self.source_stats_label.setText(f"读取训练配置失败：{exc}"); return
        if not (config.dataset.quotes or config.dataset.raw).exists():
            completed_path = _latest_ready_dataset_config(character_id)
            if completed_path is not None:
                try:
                    config = load_training_config(completed_path)
                    path = completed_path
                except (OSError, ValueError):
                    pass
        self._training_config_path = path
        self.data_mode_combo.setCurrentIndex(0 if config.dataset.mode == "quotes" else 1)
        self.source_path_edit.setText(str(config.dataset.quotes or config.dataset.raw))
        self.profile_path_edit.setText(str(config.dataset.optional_profile or ""))
        self.target_samples_spin.setValue(config.generation.target_samples)
        self.candidates_spin.setValue(config.generation.candidates_per_scene)
        self.regenerate_spin.setValue(config.generation.max_regenerate)
        self.model_name_edit.setText(config.model_name)
        self.method_combo.setCurrentText(config.method)
        self.hf_path_edit.setText(str(config.base_model.hf_path or ""))
        self.epochs_spin.setValue(config.epochs)
        self.learning_rate_spin.setValue(config.learning_rate)
        self.rank_spin.setValue(config.lora_r)
        self.assistant_loss_check.setChecked(config.assistant_only_loss)
        self.bf16_check.setChecked(config.bf16)
        self._sync_source_mode(); self._refresh_preview()

    def _save_job_config(self, *, require_teacher: bool = True) -> Path | None:
        character_id = self._selected_id()
        source = Path(self.source_path_edit.text().strip())
        model_name = self.model_name_edit.text().strip()
        if not model_name:
            self.source_stats_label.setText("请填写训练后模型名称。")
            self.training_tabs.setCurrentIndex(1)
            self.model_name_edit.setFocus()
            return None
        if not character_id or not source.exists():
            self.source_stats_label.setText("请先选择角色和存在的数据来源文件。")
            return None
        base_path = getattr(self, "_training_config_path", None)
        if not base_path or not base_path.exists():
            self.source_stats_label.setText("找不到角色的基础训练配置。")
            return None
        base = load_training_config(base_path)
        config_dir = _project_root() / "storage" / "training_jobs" / "configs"
        config_dir.mkdir(parents=True, exist_ok=True)
        job_path = config_dir / f"{character_id}-{datetime_now_compact()}.yaml"
        # Checkpoints are discovered within run_dir when resuming.  A distinct
        # run directory per user task makes "continue" unambiguous even when
        # older runs used identical data and training settings.
        run_name = _new_training_run_name(base.run_name)
        mode = str(self.data_mode_combo.currentData())
        generated = _project_root() / "training" / "qwen3" / "datasets" / character_id / "generated"
        final = generated.parent / "final"
        profile = self.profile_path_edit.text().strip()
        chosen_roles = [role_id for role_id, check in self.model_roles_checks.items() if check.isChecked()]
        # The currently trained character owns the dataset and remains a model
        # association; extra checked roles share the same exported artifact.
        assigned_character_ids = list(dict.fromkeys([character_id, *chosen_roles]))
        if require_teacher and mode == "quotes" and not self._teacher_ready:
            self.source_stats_label.setText("固定本地 Teacher 尚未就绪；请等待模型检查、下载和服务启动完成。")
            return None
        content = f'''character_id: {character_id}
run_name: {run_name}
model_name: {_yaml(model_name)}
assigned_character_ids: {_yaml(','.join(assigned_character_ids))}
outputs_dir: {_yaml(str(base.outputs_dir))}

base_model:
  model_id: {_yaml(base.base_model.model_id)}
  gguf_path: {_yaml(str(base.base_model.gguf_path))}
  hf_path: {_yaml(self.hf_path_edit.text().strip())}

data_source:
  mode: {mode}
  quotes: {_yaml(str(source) if mode == "quotes" else "")}
  optional_profile: {_yaml(profile)}

dataset:
  raw: {_yaml(str(source))}
  normalized: {_yaml(str(generated / "normalized_quotes.jsonl") if mode == "quotes" else str(generated.parent / "normalized.jsonl"))}
  generated_dir: {_yaml(str(generated))}
  train: {_yaml(str(final / "train.jsonl") if mode == "quotes" else str(generated.parent / "train.jsonl"))}
  validation: {_yaml(str(final / "validation.jsonl") if mode == "quotes" else str(generated.parent / "validation.jsonl"))}
  validation_ratio: {base.dataset.validation_ratio}

teacher:
  generator:
    provider: openai_compatible
    model: {_yaml("Qwen3-8B-Instruct")}
    base_url: {_yaml(TEACHER_BASE_URL)}
  judge:
    provider: openai_compatible
    model: {_yaml("Qwen3-8B-Instruct")}
    base_url: {_yaml(TEACHER_BASE_URL)}

generation:
  target_samples: {self.target_samples_spin.value()}
  candidates_per_scene: {self.candidates_spin.value()}
  max_regenerate: {self.regenerate_spin.value()}
  seed: {base.generation.seed}
  keep_metadata: true
  auto_balance: true
  min_persona_score: {base.generation.min_persona_score}
  min_relevance_score: {base.generation.min_relevance_score}
  min_naturalness_score: {base.generation.min_naturalness_score}
  min_behavior_score: {base.generation.min_behavior_score}
  max_copy_score: {base.generation.max_copy_score}
  max_assistant_like_penalty: {base.generation.max_assistant_like_penalty}
  max_catchphrase_penalty: {base.generation.max_catchphrase_penalty}
  max_near_duplicate_ratio: {base.generation.max_near_duplicate_ratio}

training:
  method: {self.method_combo.currentText()}
  resume_from: auto
  save_steps: {base.save_steps}
  save_total_limit: {base.save_total_limit}
  best_checkpoint_name: {_yaml(base.best_checkpoint_name)}
  max_steps: {base.max_steps}
  learning_rate: {self.learning_rate_spin.value()}
  per_device_train_batch_size: {base.per_device_train_batch_size}
  gradient_accumulation_steps: {base.gradient_accumulation_steps}
  lora_r: {self.rank_spin.value()}
  lora_alpha: {base.lora_alpha}
  lora_dropout: {base.lora_dropout}
  epochs: {self.epochs_spin.value()}
  sequence_length: {base.sequence_length}
  assistant_only_loss: {str(self.assistant_loss_check.isChecked()).lower()}
  seed: {base.seed}
  gradient_checkpointing: {str(base.gradient_checkpointing).lower()}
  bf16: {str(self.bf16_check.isChecked()).lower()}
  fp16: {str(not self.bf16_check.isChecked()).lower()}
'''
        job_path.write_text(content, encoding="utf-8")
        self.log_console.append_log(f"[config] 已保存任务 YAML：{job_path}")
        self._job_config_path = job_path
        return job_path

    def _start_dataset(self) -> None:
        if self._restored_job is not None:
            target = 0 if self._restored_job.kind == "dataset" else 3
            self.source_stats_label.setText("已有暂停任务；请先继续或结束该任务。")
            self.training_tabs.setCurrentIndex(target); return
        if not self._teacher_ready:
            self._build_after_teacher_ready = True
            self.source_stats_label.setText("正在准备固定本地 Teacher；完成后将自动开始构造训练集。")
            self._ensure_teacher()
            return
        path = self._save_job_config()
        if path is None:
            return
        self.training_worker.start_dataset(self._selected_id(), path)
        self.training_tabs.setCurrentIndex(0)

    def _resume_dataset(self) -> None:
        if self._restored_job is None or self._restored_job.kind != "dataset":
            return
        if not self._teacher_ready:
            self._build_after_teacher_ready = True
            self._ensure_teacher()
            return
        self.training_worker.resume_dataset(self._restored_job)
        self._restored_job = None
        self.training_tabs.setCurrentIndex(0)

    def _start_training(self, resume: bool, *, finish: bool = False) -> None:
        if resume and self._restored_job is not None and self._restored_job.kind == "training":
            # The local Teacher is only needed while generating the dataset.
            # Release it before QLoRA resumes so the checkpoint has the full
            # 16 GB GPU budget available again.
            self._show_training_model_loading(resume=True)
            self.teacher_service.stop()
            self._teacher_ready = False
            self.training_worker.resume_training(self._restored_job, finish=finish)
            self._restored_job = None
            self.training_tabs.setCurrentIndex(3)
            return
        if self._restored_job is not None:
            self.training_tabs.setCurrentIndex(0 if self._restored_job.kind == "dataset" else 3); return
        path = getattr(self, "_job_config_path", None) or self._save_job_config(require_teacher=False)
        if path is None:
            return
        if not resume and self.data_mode_combo.currentData() == "quotes":
            final = _project_root() / "training" / "qwen3" / "datasets" / self._selected_id() / "final" / "train.jsonl"
            if not final.exists():
                self.source_stats_label.setText("请先完成 Quote-to-SFT 数据构造。")
                self.training_tabs.setCurrentIndex(0); return
        self._show_training_model_loading(resume=resume)
        # The Q4 Teacher occupies several GB of VRAM. Release it before
        # QLoRA begins so a 16 GB card is not asked to host both workloads.
        self.teacher_service.stop()
        self._teacher_ready = False
        self.training_worker.start_training(self._selected_id(), path, resume=resume)
        self.training_tabs.setCurrentIndex(3)

    def _show_training_model_loading(self, *, resume: bool) -> None:
        """Immediately replace dataset/paused progress while startup releases VRAM."""
        self.job_state_label.setText("任务状态：正在准备恢复训练" if resume else "任务状态：正在准备训练")
        self.task_progress.setValue(0)
        self.task_detail_label.setText("正在加载训练模型和检查点…" if resume else "正在加载训练模型…")
        self.task_metrics_label.clear()
        self._set_task_buttons(Qwen3JobState.RUNNING)

    def _ensure_teacher(self) -> None:
        self.teacher_service.ensure_started()

    @Slot(str)
    def _on_teacher_status(self, message: str) -> None:
        self.teacher_status_label.setText(message)
        self.log_console.append_log(f"[teacher] {message}")

    @Slot()
    def _on_teacher_ready(self) -> None:
        self._teacher_ready = True
        self._sync_source_mode()
        if self._build_after_teacher_ready:
            self._build_after_teacher_ready = False
            QTimer.singleShot(0, self._resume_dataset if self._restored_job is not None else self._start_dataset)

    @Slot(str)
    def _on_teacher_failed(self, message: str) -> None:
        self._teacher_ready = False
        self._build_after_teacher_ready = False
        self._sync_source_mode()
        self.source_stats_label.setText(message)

    def shutdown(self) -> None:
        self.teacher_service.stop()
        self.voice_training_page.shutdown()

    @Slot(object)
    def _apply_job_state(self, job: Qwen3Job) -> None:
        if job.kind == "dataset":
            self.dataset_state_label.setText(f"任务状态：{job.state.value} · {job.job_id[:8]}")
            self.dataset_progress.setValue(round(job.progress))
            self.dataset_detail_label.setText(job.detail or job.phase or "正在准备构造数据")
        else:
            self.job_state_label.setText(f"任务状态：{job.state.value} · {job.job_id[:8]}")
            self.task_progress.setValue(round(job.progress))
            self.task_detail_label.setText(job.detail or job.phase or "任务正在准备")
            self.task_metrics_label.setText(" · ".join(f"{key}={value}" for key, value in job.metrics.items() if key != "raw"))
        self._set_task_buttons(job.state)

    @Slot(object)
    def _on_job_finished(self, job: Qwen3Job) -> None:
        clearing = job.state is Qwen3JobState.PAUSED and (
            (job.kind == "dataset" and self._clear_dataset_requested)
            or (job.kind == "training" and self._clear_training_data_requested)
        )
        if job.state is Qwen3JobState.PAUSED:
            self._restored_job = job
            if job.kind == "dataset" and self._clear_dataset_requested:
                self._discard_dataset(job)
            elif job.kind == "training" and self._clear_training_data_requested:
                self._discard_training_data(job)
        elif job.kind == "dataset":
            self._clear_dataset_requested = False
        else:
            self._clear_training_data_requested = False
        if not clearing:
            self._refresh_preview()
        if job.state is Qwen3JobState.COMPLETED and job.kind == "training":
            self._offer_model_cleanup("qwen3", self._selected_id())
            self._refresh_qwen_models()
            self.characters_changed.emit()
            self.log_console.append_log("[done] Qwen3 模型已完成导出、注册和激活；模型列表可立即刷新。")

    def _voice_models_changed(self) -> None:
        self._offer_model_cleanup("gpt_sovits", self.voice_training_page.character_id)
        self.characters_changed.emit()

    def _refresh_qwen_models(self) -> None:
        if not hasattr(self, "qwen_model_versions"):
            return
        from runtime.qwen3 import model_registry
        character_id = self._selected_id()
        self.qwen_model_versions.clear()
        if not character_id:
            return
        manifest = model_registry.load_character_manifest(character_id)
        active = str(manifest.get("active_model_id") or "")
        for model in model_registry.list_models(character_id):
            label = "当前" if model.model_id == active else "候选 / 历史"
            self.qwen_model_versions.addItem(f"{label} · {model.display_name} · {model.status}", model.model_id)

    def _offer_model_cleanup(self, model_type: str, character_id: str) -> None:
        if not character_id:
            return
        if model_type == "qwen3":
            from runtime.qwen3 import model_registry
        else:
            from runtime.gpt_sovits import model_registry
        candidates = model_registry.plan_prune_trained_models(character_id)
        if not candidates:
            return
        files = model_registry.plan_prune_files(character_id)
        size = sum(path.stat().st_size for path in files)
        details = "版本：" + "、".join(model.model_id for model in candidates)
        details += "\n文件：\n" + ("\n".join(str(path) for path in files) if files else "无独占文件，原文件由其他角色保留")
        if QMessageBox.question(self, "清理旧模型",
                                f"已超过默认保留的 5 个训练版本。将移除以下版本及文件，预计释放 {size / (1024 ** 3):.2f} GiB：\n{details}") == QMessageBox.StandardButton.Yes:
            model_registry.prune_trained_models(character_id)

    def _set_task_buttons(self, state: Qwen3JobState) -> None:
        running = state in {Qwen3JobState.BUILDING_DATASET, Qwen3JobState.RUNNING, Qwen3JobState.PAUSING, Qwen3JobState.FINISHING, Qwen3JobState.EXPORTING}
        restored = getattr(self, "_restored_job", None)
        job = self.training_worker.job or restored
        job_kind = job.kind if job else ""
        self.start_training_button.setEnabled(state is Qwen3JobState.DATASET_READY)
        self.build_dataset_button.setEnabled(not running and state is not Qwen3JobState.PAUSED and self.data_mode_combo.currentData() == "quotes")
        dataset_job = job_kind == "dataset"
        training_job = job_kind == "training"
        self.dataset_pause_button.setText("继续构造" if dataset_job and state is Qwen3JobState.PAUSED else "暂停构造")
        self.dataset_pause_button.setEnabled(dataset_job and state in {Qwen3JobState.BUILDING_DATASET, Qwen3JobState.PAUSED})
        self.dataset_clear_button.setEnabled(dataset_job and state in {Qwen3JobState.BUILDING_DATASET, Qwen3JobState.PAUSING, Qwen3JobState.PAUSED})
        self.pause_training_button.setText("继续训练" if training_job and state is Qwen3JobState.PAUSED else "暂停训练")
        self.pause_training_button.setEnabled(training_job and state in {Qwen3JobState.RUNNING, Qwen3JobState.PAUSED})
        self.finish_training_button.setEnabled(training_job and state in {Qwen3JobState.RUNNING, Qwen3JobState.PAUSED})
        self.clear_training_data_button.setEnabled(training_job and state in {Qwen3JobState.RUNNING, Qwen3JobState.PAUSING, Qwen3JobState.PAUSED})

    def _toggle_dataset_pause(self) -> None:
        if self._restored_job is not None and self._restored_job.kind == "dataset":
            self._resume_dataset()
        elif self.training_worker.job and self.training_worker.job.kind == "dataset":
            self.training_worker.request_pause()

    def _toggle_training_pause(self) -> None:
        if self._restored_job is not None and self._restored_job.kind == "training":
            self._start_training(True)
        elif self.training_worker.job and self.training_worker.job.kind == "training":
            self.training_worker.request_pause()

    def _finish_training(self) -> None:
        if self._restored_job is not None and self._restored_job.kind == "training":
            self._start_training(True, finish=True)
        elif self.training_worker.job and self.training_worker.job.kind == "training":
            self.training_worker.request_finish()

    def _end_and_clear_dataset(self) -> None:
        job = self.training_worker.job or self._restored_job
        if job is None or job.kind != "dataset":
            return
        self._clear_dataset_requested = True
        if self.training_worker.is_running:
            self.training_worker.request_pause()
            self.dataset_detail_label.setText("正在安全结束构造；随后清除构造结果。")
        elif self._restored_job is not None:
            self._discard_dataset(self._restored_job)

    def _discard_dataset(self, job: Qwen3Job) -> None:
        try:
            cleared = self._clear_dataset_results(job)
        except (OSError, ValueError) as exc:
            self._clear_dataset_requested = False
            self.dataset_detail_label.setText(f"无法清除构造结果：{exc}")
            return
        self.training_worker.dismiss_paused_job(job, discarded=True)
        self._restored_job = None
        self._clear_dataset_requested = False
        self.persona_preview.clear(); self.coverage_preview.clear(); self.dataset_preview.clear()
        self.dataset_state_label.setText("任务状态：已结束并清除构造结果")
        self.dataset_progress.setValue(0)
        self.dataset_detail_label.setText(f"已清除 {cleared} 个构造结果目录；原始语录保留。")
        self._set_task_buttons(Qwen3JobState.SOURCE_READY)

    def _end_and_clear_training_data(self) -> None:
        job = self.training_worker.job or self._restored_job
        if job is None or job.kind != "training":
            return
        self._clear_training_data_requested = True
        if self.training_worker.is_running:
            self.training_worker.request_pause()
            self.task_detail_label.setText("正在于安全边界结束训练；暂停完成后将清除训练数据。")
            return
        if self._restored_job is not None:
            self._discard_training_data(self._restored_job)

    def _discard_training_data(self, job: Qwen3Job) -> None:
        try:
            cleared = self._clear_training_results(job)
        except (OSError, ValueError) as exc:
            self._clear_training_data_requested = False
            self.task_detail_label.setText(f"无法清除训练数据：{exc}")
            return
        self.training_worker.dismiss_paused_job(job, discarded=True)
        self._restored_job = None
        self._clear_training_data_requested = False
        self.persona_preview.clear(); self.coverage_preview.clear(); self.dataset_preview.clear()
        self.log_console.append_log(f"[training] 已结束训练并清除 {cleared} 个训练结果目录；原始语录保留。")
        self.job_state_label.setText("任务状态：已结束并清除训练数据")
        self.task_progress.setValue(0)
        self.task_detail_label.setText("训练数据和本次训练输出已清除，可重新构造训练集。")
        self._set_task_buttons(Qwen3JobState.SOURCE_READY)

    def _clear_training_results(self, job: Qwen3Job) -> int:
        config = load_training_config(Path(job.config_path))
        run_dir = config.run_dir.resolve()
        character_root = (config.outputs_dir / job.character_id).resolve()
        if config.character_id != job.character_id or run_dir.parent != character_root:
            raise ValueError(f"拒绝清理范围外训练输出：{run_dir}")
        removed = self._clear_dataset_results(job)
        if run_dir.exists():
            shutil.rmtree(run_dir)
            removed += 1
        return removed

    def _clear_dataset_results(self, job: Qwen3Job) -> int:
        """Remove only derived files for a paused dataset job, never source quotes."""
        config = load_training_config(Path(job.config_path))
        dataset_root = (_project_root() / "training" / "qwen3" / "datasets" / job.character_id).resolve()
        candidates = [config.dataset.generated_dir, config.dataset.train.parent, config.dataset.validation.parent]
        targets: list[Path] = []
        for candidate in candidates:
            if candidate is None:
                continue
            resolved = candidate.resolve()
            if resolved == dataset_root or dataset_root not in resolved.parents:
                raise ValueError(f"拒绝清理范围外路径：{resolved}")
            if resolved not in targets:
                targets.append(resolved)
        removed = 0
        for target in targets:
            if target.exists():
                shutil.rmtree(target)
                removed += 1
        return removed

    def _refresh_preview(self) -> None:
        character_id = self._selected_id()
        if not character_id:
            return
        generated = _project_root() / "training" / "qwen3" / "datasets" / character_id / "generated"
        persona = generated / "persona.json"; report = generated / "generation_report.json"; train = generated.parent / "final" / "train.jsonl"
        if persona.exists(): self.persona_preview.setPlainText(persona.read_text(encoding="utf-8"))
        if report.exists():
            try:
                data = json.loads(report.read_text(encoding="utf-8")); counts = data.get("category_counts", {})
                self.coverage_preview.setPlainText(json.dumps({"场景覆盖": counts, "候选": data.get("candidate_count"), "接受": data.get("accepted_count"), "拒绝": data.get("rejected_candidate_count")}, ensure_ascii=False, indent=2))
            except json.JSONDecodeError: self.coverage_preview.setPlainText("generation_report.json 无法解析")
        if train.exists(): self.dataset_preview.setPlainText("\n".join(train.read_text(encoding="utf-8").splitlines()[:5]))

    def _save_persona_override(self) -> None:
        character_id = self._selected_id()
        if not character_id:
            return
        try:
            value = json.loads(self.persona_preview.toPlainText())
            if not isinstance(value, dict):
                raise ValueError("Persona 必须是 JSON 对象")
        except (ValueError, json.JSONDecodeError) as exc:
            self.coverage_preview.setPlainText(f"Persona 未保存：{exc}")
            return
        path = _project_root() / "training" / "qwen3" / "datasets" / character_id / "generated" / "persona_override.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.log_console.append_log(f"[persona] 已保存训练用 Persona 覆盖：{path}")

    def _reload_characters(self, selected_id: str = "") -> None:
        self.character_combo.clear()
        for character_id, name in self.character_manager.list_profiles():
            self.character_combo.addItem(f"{name} ({character_id})", character_id)
        index = self.character_combo.findData(selected_id)
        if index >= 0:
            self.character_combo.setCurrentIndex(index)

    def _sync_model_roles(self, default_character_id: str) -> None:
        """Keep the multi-role selector current; one selected role is the default."""
        if not hasattr(self, "model_roles_widget"):
            return
        old_checks = getattr(self, "model_roles_checks", {})
        selected = {role_id for role_id, check in old_checks.items() if check.isChecked()}
        profiles = self.character_manager.list_profiles()
        known = {role_id for role_id, _ in profiles}
        selected &= known
        if not selected and default_character_id:
            selected = {default_character_id}
        layout = self.model_roles_widget.layout()
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.model_roles_checks = {}
        for role_id, role_name in profiles:
            check = QCheckBox(f"{role_name} ({role_id})")
            check.setChecked(role_id in selected)
            self.model_roles_checks[role_id] = check
            layout.addWidget(check)
        layout.addStretch(1)

    def _selected_id(self) -> str:
        return str(self.character_combo.currentData() or "")

    def _open_editor(self, character_id: str = "") -> None:
        # Imported lazily to avoid the MainWindow -> pages package import cycle.
        from desktop.windows.character_manager_dialog import CharacterManagerDialog
        dialog = CharacterManagerDialog(self.character_manager, character_id, self)
        dialog.saved.connect(self._on_saved)
        dialog.exec()

    def _on_saved(self, character_id: str) -> None:
        self._reload_characters(character_id)
        self.characters_changed.emit()

    def _copy_selected(self) -> None:
        from desktop.windows.character_manager_dialog import CharacterTextPromptDialog
        source = self._selected_id()
        if not source:
            return
        dialog = CharacterTextPromptDialog(self.tr("复制角色"), self.tr("新角色 ID："), f"{source}_copy", self)
        if not dialog.exec() or not dialog.value:
            return
        try:
            draft = self.character_manager.duplicate(source, dialog.value)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "复制角色", str(exc)); return
        self._on_saved(draft.character_id)

    def _import_bundle(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "导入角色配置包", "", "角色包 (*.zip)")
        if not path:
            return
        try:
            draft = self.character_manager.import_bundle(path)
        except FileExistsError as exc:
            QMessageBox.warning(self, "导入角色", f"{exc}\n请先删除同名角色后重试。")
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "导入角色", str(exc))
        else:
            self._on_saved(draft.character_id)
            missing = []
            restored = 0
            for model_type in ("qwen3", "gpt_sovits"):
                manifest = self.character_manager._load_model_manifest(model_type, draft.character_id)
                for model in manifest["models"]:
                    paths = [self.character_manager._project_model_path(value)
                             for value in model.get("required_files", [])]
                    if paths and all(path.is_file() for path in paths):
                        restored += 1
                    else:
                        missing.append(f"{model_type} · {model.get('display_name') or model.get('id')}")
            message = f"角色配置与参考音频已恢复；可用模型 {restored} 个。"
            if missing:
                message += "\n以下模型只有引用，需要重新定位文件：\n" + "\n".join(missing)
            QMessageBox.information(self, "导入角色", message)

    def _export_selected(self) -> None:
        character_id = self._selected_id()
        if not character_id:
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出角色配置包", f"{character_id}.alysia-character.zip", "角色包 (*.zip)")
        if not path:
            return
        choice = QMessageBox.question(self, "模型文件", "是否将大型模型文件也包含在角色包中？\n选择“否”时只保存模型引用。",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
                                      QMessageBox.StandardButton.No)
        if choice == QMessageBox.StandardButton.Cancel:
            return
        try:
            bundle = self.character_manager.export_bundle(character_id, path,
                                                          include_models=choice == QMessageBox.StandardButton.Yes)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "导出角色", str(exc)); return
        QMessageBox.information(self, "导出角色", f"已导出：{bundle}")

    def _delete_selected(self) -> None:
        from desktop.windows.character_manager_dialog import CharacterConfirmDialog, CharacterNoticeDialog
        character_id = self._selected_id()
        if not character_id:
            return
        if character_id == getattr(self, "_active_character_id", ""):
            CharacterNoticeDialog(
                self.tr("删除角色"),
                self.tr("请先在使用界面切换到其他角色，再删除当前角色。"),
                self,
            ).exec()
            return
        dialog = CharacterConfirmDialog(
            self.tr("删除角色"),
            self.tr(f"确认删除角色“{character_id}”及其本地资源？\n共享基础模型不会被删除。"),
            self,
        )
        if not dialog.exec():
            return
        try:
            self.character_manager.delete(character_id)
        except OSError as exc:
            QMessageBox.warning(self, "删除角色", str(exc)); return
        self._reload_characters(); self.characters_changed.emit()


def _project_root():
    return Path(__file__).resolve().parents[2]


def _latest_ready_dataset_config(character_id: str) -> Path | None:
    root = _project_root() / "storage" / "training_jobs"
    newest: tuple[str, Path] | None = None
    for job_path in root.glob("*.json") if root.exists() else ():
        try:
            job = json.loads(job_path.read_text(encoding="utf-8"))
            config_path = Path(str(job["config_path"]))
            if job.get("character_id") == character_id and job.get("kind") == "dataset" and job.get("state") == "DATASET_READY" and config_path.exists():
                candidate = (str(job.get("updated_at", "")), config_path)
                if newest is None or candidate[0] > newest[0]:
                    newest = candidate
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return newest[1] if newest else None


def _yaml(value: str) -> str:
    """The project YAML reader accepts JSON-style quoted scalar values."""
    return json.dumps(value, ensure_ascii=False)


def datetime_now_compact() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _new_training_run_name(base_run_name: str) -> str:
    """Return an output-directory-safe name unique to one UI training task."""
    return f"{base_run_name}-{datetime_now_compact()}-{uuid4().hex[:8]}"


class CharacterManagerComboBox(QComboBox):
    """Keeps the dropdown affordance visible on the custom dark control."""

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt API
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#d9d4e6") if self.isEnabled() else QColor("#6f6c7e"))
        center_x = self.width() - 16
        center_y = self.height() / 2 + 1
        painter.drawPolygon(
            QPolygonF(
                [
                    QPointF(center_x - 4, center_y - 3),
                    QPointF(center_x + 4, center_y - 3),
                    QPointF(center_x, center_y + 2),
                ]
            )
        )
