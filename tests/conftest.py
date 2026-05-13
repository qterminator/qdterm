"""Shared fixtures and cleanup for QTerminator tests.

QTermWidget opens PTY file descriptors (~5 fds) per terminal instance.
Without explicit cleanup, tests exhaust the system fd limit.
"""

import gc

import pytest
from PyQt6.QtWidgets import QApplication


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
