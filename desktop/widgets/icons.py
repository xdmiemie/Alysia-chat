from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap


IconPainter = Callable[[QPainter, QColor], None]


def microphone_icon(*, muted: bool = False) -> QIcon:
    """Return a font-independent microphone icon, optionally crossed out."""

    def draw(painter: QPainter, color: QColor) -> None:
        pen = QPen(color, 4.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(24, 9, 16, 29), 8, 8)
        arch = QPainterPath(QPointF(18, 27))
        arch.cubicTo(18, 47, 46, 47, 46, 27)
        painter.drawPath(arch)
        painter.drawLine(QPointF(32, 46), QPointF(32, 54))
        painter.drawLine(QPointF(23, 54), QPointF(41, 54))
        if muted:
            painter.setPen(
                QPen(
                    QColor("#ff7eaa") if color.lightness() > 100 else color,
                    5.5,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                )
            )
            painter.drawLine(QPointF(13, 12), QPointF(51, 53))

    return _painted_icon(draw)


def hangup_icon() -> QIcon:
    """Return the horizontal receiver used by traditional hang-up buttons."""

    def draw(painter: QPainter, color: QColor) -> None:
        painter.setPen(QPen(color, 7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        receiver = QPainterPath(QPointF(16, 39))
        receiver.cubicTo(25, 29, 39, 29, 48, 39)
        painter.drawPath(receiver)
        painter.setPen(QPen(color, 9, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(QPointF(15, 37), QPointF(20, 42))
        painter.drawLine(QPointF(49, 37), QPointF(44, 42))

    return _painted_icon(draw)


def _painted_icon(draw: IconPainter) -> QIcon:
    icon = QIcon(_pixmap(draw, QColor("#f8f5ff")))
    icon.addPixmap(_pixmap(draw, QColor("#777489")), QIcon.Mode.Disabled, QIcon.State.Off)
    return icon


def _pixmap(draw: IconPainter, color: QColor) -> QPixmap:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    draw(painter, color)
    painter.end()
    return pixmap
