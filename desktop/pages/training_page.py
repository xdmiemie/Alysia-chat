from __future__ import annotations

from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from desktop.state import AppSnapshot
from desktop.widgets import LogConsole


class TrainingPage(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("trainingPage")
        root = QVBoxLayout(self)
        root.setContentsMargins(30, 26, 30, 24)
        root.setSpacing(16)

        title = QLabel("训练界面")
        title.setObjectName("pageTitle")
        subtitle = QLabel("训练任务的配置与日志工作区")
        subtitle.setObjectName("pageSubtitle")
        root.addWidget(title)
        root.addWidget(subtitle)

        cards = QGridLayout()
        cards.setSpacing(14)
        cards.addWidget(self._task_card("Qwen3 角色训练", "数据集、LoRA 参数与导出流程", "Stage 16 开放"), 0, 0)
        cards.addWidget(self._task_card("GPT-SoVITS 训练", "参考音频、切片与两阶段训练", "Stage 17 开放"), 0, 1)
        root.addLayout(cards)

        log_header = QLabel("任务日志")
        log_header.setObjectName("sectionTitle")
        root.addWidget(log_header)
        self.log_console = LogConsole()
        self.log_console.setPlainText(
            "[ready] 训练控制台已就绪。\n"
            "[info] 后续训练任务将在独立 worker 中运行，不会阻塞桌面主线程。"
        )
        root.addWidget(self.log_console, 1)

    @staticmethod
    def _task_card(title: str, description: str, action: str) -> QFrame:
        card = QFrame()
        card.setObjectName("trainingCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 18, 18, 18)
        title_label = QLabel(title)
        title_label.setObjectName("cardHeading")
        body = QLabel(description)
        body.setObjectName("cardBody")
        body.setWordWrap(True)
        button = QPushButton(action)
        button.setObjectName("secondaryButton")
        button.setEnabled(False)
        layout.addWidget(title_label)
        layout.addWidget(body)
        layout.addStretch(1)
        layout.addWidget(button)
        return card

    def apply_state(self, _snapshot: AppSnapshot) -> None:
        pass
