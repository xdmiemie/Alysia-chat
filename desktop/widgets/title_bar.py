from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton


class TitleBar(QFrame):
    """Themeable replacement for the native Windows title bar."""

    def __init__(self, parent=None, *, title: str = "Alysia Studio") -> None:
        super().__init__(parent)
        self.setObjectName("titleBar")
        self.setFixedHeight(44)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 6, 0)
        layout.setSpacing(8)
        mark = QLabel("✦")
        mark.setObjectName("titleBarMark")
        self.title_label = QLabel(title)
        self.title_label.setObjectName("titleBarTitle")
        layout.addWidget(mark)
        layout.addWidget(self.title_label)
        layout.addStretch(1)

        self.minimize_button = self._window_button("—", "最小化", "minimizeButton")
        self.maximize_button = self._window_button("□", "最大化", "maximizeButton")
        self.close_button = self._window_button("×", "关闭", "closeButton")
        self.minimize_button.clicked.connect(lambda: self.window().showMinimized())
        self.maximize_button.clicked.connect(self.toggle_maximized)
        self.close_button.clicked.connect(lambda: self.window().close())
        layout.addWidget(self.minimize_button)
        layout.addWidget(self.maximize_button)
        layout.addWidget(self.close_button)

    @staticmethod
    def _window_button(text: str, tooltip: str, object_name: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName(object_name)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.setFixedSize(46, 34)
        return button

    def toggle_maximized(self) -> None:
        window = self.window()
        if window.isMaximized():
            window.showNormal()
        else:
            window.showMaximized()
        self.sync_window_state()

    def sync_window_state(self) -> None:
        maximized = self.window().isMaximized()
        self.maximize_button.setText("❐" if maximized else "□")
        self.maximize_button.setToolTip("还原" if maximized else "最大化")
        self.maximize_button.setAccessibleName(self.maximize_button.toolTip())

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if event.button() is Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None and handle.startSystemMove():
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if event.button() is Qt.MouseButton.LeftButton:
            self.toggle_maximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class ResizeHandle(QFrame):
    """Thin native system-resize target used around a frameless window."""

    def __init__(self, edges: Qt.Edge, parent=None) -> None:
        super().__init__(parent)
        self.edges = edges
        self.setObjectName("resizeHandle")
        horizontal = bool(edges & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge))
        vertical = bool(edges & (Qt.Edge.TopEdge | Qt.Edge.BottomEdge))
        if horizontal and vertical:
            rising = edges in (
                Qt.Edge.LeftEdge | Qt.Edge.BottomEdge,
                Qt.Edge.RightEdge | Qt.Edge.TopEdge,
            )
            self.setCursor(
                Qt.CursorShape.SizeBDiagCursor if rising else Qt.CursorShape.SizeFDiagCursor
            )
        elif horizontal:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        else:
            self.setCursor(Qt.CursorShape.SizeVerCursor)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if event.button() is Qt.MouseButton.LeftButton and not self.window().isMaximized():
            handle = self.window().windowHandle()
            if handle is not None and handle.startSystemResize(self.edges):
                event.accept()
                return
        super().mousePressEvent(event)
