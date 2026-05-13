"""Per-terminal titlebar widget showing title, group, and status indicators."""

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPalette, QColor
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton


ACTIVE_BG = "#2a6ea8"
INACTIVE_BG = "#3c3c3c"
TITLE_HEIGHT = 20

# Group colors for visual distinction
GROUP_COLORS = [
    "#c0392b", "#27ae60", "#2980b9", "#8e44ad",
    "#d35400", "#16a085", "#2c3e50", "#f39c12",
]


class TerminalTitlebar(QFrame):
    """Small titlebar shown above each terminal in split views."""

    close_clicked = pyqtSignal()
    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(TITLE_HEIGHT)
        self.setAutoFillBackground(True)
        self._active = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 2, 0)
        layout.setSpacing(4)

        # Group indicator (colored dot)
        self._group_label = QLabel()
        self._group_label.setFixedSize(12, 12)
        self._group_label.hide()
        layout.addWidget(self._group_label)

        # Read-only indicator
        self._readonly_label = QLabel("[RO]")
        self._readonly_label.setStyleSheet("color: #e74c3c; font-size: 10px; font-weight: bold;")
        self._readonly_label.hide()
        layout.addWidget(self._readonly_label)

        # Activity indicator
        self._activity_label = QLabel("\u25cf")  # ● dot
        self._activity_label.setStyleSheet("color: #f1c40f; font-size: 10px;")
        self._activity_label.setToolTip("Activity detected")
        self._activity_label.hide()
        layout.addWidget(self._activity_label)

        # Title
        self._title_label = QLabel("Terminal")
        self._title_label.setStyleSheet("color: #ddd; font-size: 11px;")
        layout.addWidget(self._title_label, 1)

        # Close button
        self._close_btn = QPushButton("\u00d7")  # × symbol
        self._close_btn.setFixedSize(16, 16)
        self._close_btn.setFlat(True)
        self._close_btn.setStyleSheet(
            "QPushButton { color: #aaa; font-size: 12px; border: none; }"
            "QPushButton:hover { color: #fff; background: #555; border-radius: 3px; }"
        )
        self._close_btn.clicked.connect(self.close_clicked.emit)
        layout.addWidget(self._close_btn)

        self.set_active(False)

    def set_title(self, title):
        if len(title) > 60:
            title = title[:57] + "..."
        self._title_label.setText(title)

    def set_active(self, active):
        self._active = active
        bg = ACTIVE_BG if active else INACTIVE_BG
        self.setStyleSheet(f"TerminalTitlebar {{ background-color: {bg}; }}")

    def set_group(self, group_name):
        """Show group indicator with a color based on group name."""
        if group_name:
            color_idx = hash(group_name) % len(GROUP_COLORS)
            color = GROUP_COLORS[color_idx]
            self._group_label.setStyleSheet(
                f"background-color: {color}; border-radius: 6px;"
            )
            self._group_label.setToolTip(f"Group: {group_name}")
            self._group_label.show()
        else:
            self._group_label.hide()

    def set_read_only(self, read_only):
        self._readonly_label.setVisible(read_only)

    def set_activity(self, has_activity):
        self._activity_label.setVisible(has_activity)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)
