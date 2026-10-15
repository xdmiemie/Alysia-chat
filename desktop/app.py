from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

PROJECT_ROOT = Path(__file__).resolve().parents[1]
THEME_PATH = Path(__file__).resolve().parent / "resources" / "qss" / "alysia.qss"
CONTROL_ICON_ROOT = THEME_PATH.parent.parent / "icons"
LOG_PATH = PROJECT_ROOT / "storage" / "logs" / "desktop.log"


def load_theme_stylesheet() -> str:
    return THEME_PATH.read_text(encoding="utf-8").replace(
        "__CONTROL_ICON_ROOT__", CONTROL_ICON_ROOT.resolve().as_posix()
    )


def configure_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8")],
    )


def create_application(argv: list[str] | None = None) -> QApplication:
    QCoreApplication.setOrganizationName("Alysia Studio")
    QCoreApplication.setApplicationName("Alysia Studio")
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(argv if argv is not None else sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(load_theme_stylesheet())
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#111225"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#f6f3ff"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#17182d"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#f6f3ff"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#ee6fad"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(palette)
    return app
