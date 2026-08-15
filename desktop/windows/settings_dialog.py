from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from desktop.widgets import LogConsole, TitleBar
from runtime.calls.preferences import load_call_preferences, save_call_preferences


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SETTINGS_PATH = PROJECT_ROOT / "storage" / "desktop" / "settings.json"


class SettingsDialog(QDialog):
    def __init__(self, log_path: str, parent=None, *, show_logs: bool = False) -> None:
        super().__init__(parent)
        self.setObjectName("settingsDialog")
        self.setWindowTitle("Alysia Studio 设置")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.resize(720, 520)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.title_bar = TitleBar(title="设置与日志")
        root.addWidget(self.title_bar)
        content = QWidget()
        content.setObjectName("settingsContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(16, 14, 16, 16)
        content_layout.setSpacing(12)
        tabs = QTabWidget()
        tabs.setObjectName("settingsTabs")

        general = QWidget()
        general.setObjectName("settingsPage")
        general_layout = QVBoxLayout(general)
        general_layout.setContentsMargins(20, 20, 20, 20)
        general_layout.addWidget(QLabel("模型服务采用按需启动。"))
        preferences = load_call_preferences(SETTINGS_PATH)
        self.save_call_audio = QCheckBox("保存语音通话中的用户与角色音频")
        self.save_call_audio.setChecked(bool(preferences.get("save_call_audio", True)))
        self.save_call_audio.setToolTip("关闭后仍会保留通话文本、耗时、角色和模型版本记录。")
        general_layout.addWidget(self.save_call_audio)
        general_layout.addStretch(1)
        tabs.addTab(general, "常规")

        diagnostics = QWidget()
        diagnostics.setObjectName("settingsPage")
        diagnostics_layout = QVBoxLayout(diagnostics)
        diagnostics_layout.setContentsMargins(12, 12, 12, 12)
        path_label = QLabel(f"日志文件：{log_path}")
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        diagnostics_layout.addWidget(path_label)
        console = LogConsole()
        try:
            console.setPlainText(Path(log_path).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            console.setPlainText("日志文件尚未生成。")
        diagnostics_layout.addWidget(console, 1)
        tabs.addTab(diagnostics, "诊断与日志")
        if show_logs:
            tabs.setCurrentWidget(diagnostics)

        content_layout.addWidget(tabs)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        close_button.setText("关闭")
        close_button.setObjectName("dialogConfirmButton")
        buttons.rejected.connect(self.reject)
        self.finished.connect(lambda _result: self._save_preferences())
        content_layout.addWidget(buttons)
        root.addWidget(content, 1)

    def _save_preferences(self) -> None:
        save_call_preferences(
            save_call_audio=self.save_call_audio.isChecked(),
            path=SETTINGS_PATH,
        )
