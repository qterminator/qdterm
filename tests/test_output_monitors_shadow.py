"""Regression tests for output_monitors using ShadowScreenRegistry."""

import pytest

pytest.importorskip("pyte")

import qterminator.config as config_mod
from qterminator.config import Config
from qterminator.plugins.output_monitors import (
    BuildProgressMonitor,
    ErrorDetector,
    LogLevelColorizer,
    SensitiveDataWarner,
)
from qterminator.shadow_screen import ShadowScreenRegistry


@pytest.fixture(autouse=True)
def fresh_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(
        config_mod, "CONFIG_FILE", str(tmp_path / "config.toml"),
    )
    Config._instance = None
    yield
    Config._instance = None


@pytest.fixture
def terminal(qtbot):
    from qterminator.terminal import TerminalWidget
    t = TerminalWidget()
    qtbot.addWidget(t)
    t.resize(800, 400)
    t.show()
    qtbot.waitExposed(t)
    yield t


class _Split:
    def __init__(self, terminal):
        self._terminal = terminal

    def find_terminals(self):
        return [self._terminal]


class _Tabs:
    def __init__(self, terminal):
        self._split = _Split(terminal)

    def count(self):
        return 1

    def widget(self, _index):
        return self._split


class _Window:
    def __init__(self, terminal):
        self.shadow_screens = ShadowScreenRegistry()
        self._tabs = _Tabs(terminal)

    def _connect_terminal(self, _terminal):
        pass


def test_build_progress_uses_final_rendered_carriage_return(qtbot, terminal):
    win = _Window(terminal)
    plugin = BuildProgressMonitor()
    plugin.activate(win)
    try:
        handle, _listener = plugin._handles[id(terminal)]
        handle.shadow.feed("\r[1/100]\r[2/100]\r[3/100]\n")
        qtbot.wait(plugin.DEBOUNCE_MS + 40)
        assert terminal._titlebar._activity_label.toolTip() == "Build progress: 3%"
    finally:
        plugin.deactivate()


def test_error_detector_snapshot_flags_rendered_screen(terminal):
    plugin = ErrorDetector()
    plugin.on_snapshot(terminal, {"lines": ["", "BUILD ERROR", ""]})
    assert terminal._titlebar._activity_label.toolTip() == "Error detected in output"
    assert terminal._titlebar._title_label.text().startswith("\u26a0 ")


def test_log_level_snapshot_replaces_raw_history(terminal):
    plugin = LogLevelColorizer()
    plugin.on_output(terminal, "ERROR one\nERROR two\n")
    plugin.on_snapshot(terminal, {"lines": ["INFO ok", "DEBUG detail"]})
    tooltip = terminal._titlebar._activity_label.toolTip()
    assert "ERROR=0" in tooltip
    assert "INFO=1" in tooltip
    assert "DEBUG=1" in tooltip


def test_sensitive_data_snapshot_detects_overwritten_secret(terminal):
    plugin = SensitiveDataWarner()
    plugin.on_snapshot(
        terminal,
        {"lines": ["token = abcdefghijklmnopqrstuvwxyz123456"]},
    )
    assert "Possible secret" in terminal._titlebar._activity_label.toolTip()
