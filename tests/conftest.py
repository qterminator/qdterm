"""Shared fixtures and cleanup for QTerminator tests.

QTermWidget opens PTY file descriptors (~5 fds) per terminal instance.
Without explicit cleanup, tests exhaust the system fd limit.

Runs under Qt's offscreen platform plugin by default so tests don't pop
real windows on the active desktop. Override with
``QT_QPA_PLATFORM=xcb pytest`` if you need an on-screen run.
"""

import gc
import os
import sys
import types

# Default to offscreen rendering. Must be set before QApplication is
# constructed, which happens lazily on first qtbot use.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtGui import QFont
from PyQt6.QtGui import QPainter
from PyQt6.QtGui import QPen
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QApplication
from PyQt6.QtWidgets import QWidget


try:
    import QTermWidget  # noqa: F401
except ModuleNotFoundError:
    qtermwidget = types.ModuleType("QTermWidget")

    class _ScrollBarPosition:
        NoScrollBar = 0
        ScrollBarLeft = 1
        ScrollBarRight = 2

    class _FakeQTermWidget(QWidget):
        finished = pyqtSignal()
        titleChanged = pyqtSignal()
        termGetFocus = pyqtSignal()
        activity = pyqtSignal()
        silence = pyqtSignal()
        bell = pyqtSignal(str)
        urlActivated = pyqtSignal(object, bool)
        copyAvailable = pyqtSignal(bool)
        termKeyPressed = pyqtSignal(object)
        receivedData = pyqtSignal(str)
        sendData = pyqtSignal(bytes, int)

        ScrollBarPosition = _ScrollBarPosition

        def __init__(self, *_args, **_kwargs):
            super().__init__()
            self._font = QFont("Monospace", 11)
            self._working_directory = os.getcwd()
            self._shell_program = os.environ.get("SHELL", "/bin/bash")
            self._args = []
            self._key_bindings = "linux"
            self._history_size = 10000
            self._color_scheme = "Linux"
            self._shell_pid = os.getpid()
            self._foreground_pid = self._shell_pid
            self._selected_text = ""
            self._sent_text = []
            self.setMinimumSize(640, 360)

        @staticmethod
        def availableColorSchemes():
            return ["BlackOnWhite", "Linux", "WhiteOnBlack"]

        def setTerminalFont(self, font):
            self._font = QFont(font)

        def getTerminalFont(self):
            return QFont(self._font)

        def setColorScheme(self, name):
            self._color_scheme = name

        def setHistorySize(self, lines):
            self._history_size = lines

        def setScrollBarPosition(self, _position):
            pass

        def setKeyBindings(self, name):
            self._key_bindings = name

        def keyBindings(self):
            return self._key_bindings

        def setBlinkingCursor(self, _enabled):
            pass

        def setTerminalOpacity(self, _opacity):
            pass

        def setAutoClose(self, _enabled):
            pass

        def setWorkingDirectory(self, path):
            self._working_directory = path

        def workingDirectory(self):
            return self._working_directory

        def setShellProgram(self, program):
            self._shell_program = program

        def setArgs(self, args):
            self._args = list(args)

        def startShellProgram(self):
            self._shell_pid = os.getpid()
            self._foreground_pid = self._shell_pid

        def title(self):
            return "QTermWidget"

        def copyClipboard(self):
            pass

        def pasteClipboard(self):
            pass

        def pasteSelection(self):
            pass

        def clear(self):
            self._selected_text = ""

        def zoomIn(self):
            self._font.setPointSize(self._font.pointSize() + 1)

        def zoomOut(self):
            self._font.setPointSize(max(1, self._font.pointSize() - 1))

        def sendText(self, text):
            self._sent_text.append(text)
            data = text.encode()
            self.sendData.emit(data, len(data))
            if text == "\a":
                return
            output = text.replace("\x15", "\r\x1b[K")
            self.receivedData.emit(output)

        def sendKeyEvent(self, event):
            self.termKeyPressed.emit(event)

        def selectedText(self):
            return self._selected_text

        def getShellPID(self):
            return self._shell_pid

        def getForegroundProcessId(self):
            return self._foreground_pid

        def screenColumnsCount(self):
            return 80

        def grab(self, _rectangle=None):
            pixmap = QPixmap(320, 180)
            pixmap.fill(QColor("#101418"))
            painter = QPainter(pixmap)
            painter.setFont(self._font)
            painter.setPen(QPen(QColor("#9cdcfe")))
            painter.drawText(8, 24, "admin@qterminator$ screenshot")
            painter.end()
            return pixmap

        def toggleShowSearchBar(self):
            pass

        def setMonitorActivity(self, _enabled):
            pass

        def setMonitorSilence(self, _enabled):
            pass

        def setSilenceTimeout(self, _timeout):
            pass

        def scrollToEnd(self):
            pass

        def paintEvent(self, event):
            super().paintEvent(event)
            painter = QPainter(self)
            painter.fillRect(self.rect(), QColor("#101418"))
            painter.setFont(self._font)
            painter.setPen(QPen(QColor("#9cdcfe")))
            line_h = max(14, painter.fontMetrics().height())
            rows = max(1, self.height() // line_h)
            for row in range(rows):
                y = (row + 1) * line_h - 3
                if row % 4 == 0:
                    painter.setPen(QPen(QColor("#c586c0")))
                elif row % 4 == 1:
                    painter.setPen(QPen(QColor("#dcdcaa")))
                elif row % 4 == 2:
                    painter.setPen(QPen(QColor("#ce9178")))
                else:
                    painter.setPen(QPen(QColor("#9cdcfe")))
                painter.drawText(8, y, f"admin@qterminator:{row:02d}$ echo terminal smoke output {row:02d}")

    qtermwidget.QTermWidget = _FakeQTermWidget
    sys.modules["QTermWidget"] = qtermwidget


@pytest.fixture(autouse=True)
def _cleanup_after_test():
    """Free PTY fds after every test to prevent fd exhaustion.

    Runs multiple processEvents+gc rounds to ensure deleteLater()
    calls from qtbot are processed and C++ objects are freed.
    """
    yield
    app = QApplication.instance()
    if app:
        for _ in range(3):
            app.processEvents()
            gc.collect()
            app.processEvents()
