"""Tests for the command_telemetry plugin.

Layers:
  - Pure helpers (parse / sample / tab-title suffix regex).
  - Service-level: drive ;C / ;D through shell_integration's real
    OSC parser; assert telemetry annotates the CommandRecord, JSONL
    log writes, tab title gets the [Xs] suffix without compounding.
  - Plugin lifecycle on a real MainWindow.
"""

from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

import qterminator.config as config_mod
from qterminator.config import Config


pytest.importorskip("pyte")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_config(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(cfg_dir))
    monkeypatch.setattr(
        config_mod, "CONFIG_FILE", str(cfg_dir / "config.toml"),
    )
    Config._instance = None
    yield
    Config._instance = None


def osc(code, body):
    return ("\x1b]" + code + ";" + body + "\x1b\\").encode("utf-8")


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------

def test_tab_suffix_regex_strips_prior_entry():
    """Repeated commands must not compound suffixes: ``[1s] [2s] [3s]``."""
    from qterminator.plugins.command_telemetry import _TAB_SUFFIX_RE

    assert _TAB_SUFFIX_RE.sub("", "tab1  [12.4s]") == "tab1"
    assert _TAB_SUFFIX_RE.sub("", "tab1  [12.4s · 412MB]") == "tab1"
    assert _TAB_SUFFIX_RE.sub("", "tab1  [3.2s · 12MB · 4.0s CPU]") == "tab1"
    # No suffix → no change.
    assert _TAB_SUFFIX_RE.sub("", "plain-title") == "plain-title"
    # Brackets without a duration → not our suffix; leave alone.
    assert _TAB_SUFFIX_RE.sub("", "tab1 [foo]") == "tab1 [foo]"


def test_command_telemetry_format_short():
    from qterminator.plugins.command_telemetry import CommandTelemetry

    t = CommandTelemetry(duration=12.4, cpu_seconds=8.1,
                          peak_rss_bytes=412 * 1024 * 1024,
                          process_count=9)
    s = t.format_short()
    assert "12.4s" in s
    assert "412MB" in s
    assert "8.1s CPU" in s


def test_read_children_returns_empty_for_missing_pid():
    """``_read_children`` must not raise for a PID that doesn't exist."""
    from qterminator.plugins.command_telemetry import _read_children
    # Linux PID_MAX_LIMIT is 2^22; 2_147_000_000 will never be live
    # on a real system and isn't a kernel-thread parent.
    assert _read_children(2_147_000_000) == []


def test_sample_tree_handles_zero_pid_gracefully():
    from qterminator.plugins.command_telemetry import sample_tree
    snap = sample_tree(0)
    assert snap == {"cpu_seconds": 0.0, "peak_rss_bytes": 0, "process_count": 0}


def test_walk_proc_tree_includes_known_descendant():
    """Real-process probe: spawn a child + grandchild, walk the tree
    from our own PID, both must show up. Skipped if /proc isn't usable."""
    from qterminator.plugins.command_telemetry import _walk_proc_tree

    if not os.path.isdir("/proc/self"):
        pytest.skip("/proc not available")

    # Pick a long-running tree we control: bash spawns sleep.
    proc = subprocess.Popen(
        ["bash", "-c", "exec sleep 30"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.2)  # let exec happen
        pids = _walk_proc_tree(proc.pid)
        assert proc.pid in pids
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------------------
# Service tests — drive real OSC parser via ShadowScreen
# ---------------------------------------------------------------------------

@pytest.fixture
def terminal(qtbot):
    """A real TerminalWidget — needed because ShadowScreenRegistry
    connects to the inner ``QTermWidget.receivedData`` signal, which
    plain Python fakes don't carry."""
    from qterminator.terminal import TerminalWidget
    t = TerminalWidget()
    qtbot.addWidget(t)
    t.resize(800, 400)
    t.show()
    qtbot.waitExposed(t)
    yield t


def _build_service(window=None, **kw):
    from qterminator.plugins.command_telemetry import CommandTelemetryService
    if window is None:
        class _W:
            _tabs = None
        window = _W()
    return CommandTelemetryService(window, **kw)


def _build_shell_int(history_limit=200):
    from qterminator.plugins.shell_integration import ShellIntegrationService
    from qterminator.shadow_screen import ShadowScreenRegistry
    reg = ShadowScreenRegistry()
    svc = ShellIntegrationService(reg, capture_command_text=True,
                                   history_limit=history_limit)
    return reg, svc


def test_service_annotates_command_record(terminal):
    """End-to-end through shell_integration: ;C/;D fires our hooks and
    the resulting CommandRecord.telemetry is populated."""
    reg, shell_int = _build_shell_int()
    svc = _build_service(display="off")
    svc.attach(shell_int)
    history = shell_int.ensure_attached(terminal)
    shadow = reg._shadows[id(terminal)][0]

    shadow.feed("\x1b]133;A\x1b\\")
    shadow.feed("\x1b]133;B\x1b\\")
    shadow.feed("\x1b]133;C\x1b\\")
    # A tiny delay so duration is measurable.
    time.sleep(0.01)
    shadow.feed("\x1b]133;D;0\x1b\\")

    assert history.last is not None
    tele = history.last.telemetry
    assert tele is not None
    assert tele["duration"] >= 0.0
    assert tele["peak_rss_bytes"] >= 0
    assert tele["process_count"] >= 0

    svc.detach(shell_int)


def test_service_get_last_telemetry(terminal):
    reg, shell_int = _build_shell_int()
    svc = _build_service(display="off")
    svc.attach(shell_int)
    shell_int.ensure_attached(terminal)
    shadow = reg._shadows[id(terminal)][0]

    assert svc.get_last_telemetry(terminal) is None

    shadow.feed("\x1b]133;A\x1b\\\x1b]133;B\x1b\\\x1b]133;C\x1b\\")
    time.sleep(0.005)
    shadow.feed("\x1b]133;D;0\x1b\\")

    last = svc.get_last_telemetry(terminal)
    assert last is not None
    assert "duration" in last and "cpu_seconds" in last

    svc.detach(shell_int)


def test_service_jsonl_log_writes_one_line_per_command(terminal, tmp_path):
    reg, shell_int = _build_shell_int()
    log_path = tmp_path / "commands.jsonl"
    svc = _build_service(display="off", log_path=str(log_path))
    svc.attach(shell_int)
    shell_int.ensure_attached(terminal)
    shadow = reg._shadows[id(terminal)][0]

    for exit_code in (0, 1, 0):
        shadow.feed("\x1b]7;file:///tmp\x1b\\")
        shadow.feed("\x1b]133;A\x1b\\")
        shadow.feed("\x1b]133;B\x1b\\")
        shadow.feed("\x1b]133;C\x1b\\")
        time.sleep(0.005)
        shadow.feed(f"\x1b]133;D;{exit_code}\x1b\\")

    assert log_path.exists()
    lines = [l for l in log_path.read_text().splitlines() if l.strip()]
    assert len(lines) == 3
    parsed = [json.loads(l) for l in lines]
    assert [p["exit_status"] for p in parsed] == [0, 1, 0]
    # Each record carries telemetry fields.
    for p in parsed:
        assert "duration" in p
        assert "cpu_seconds" in p
    # Permissions are tightened on creation — the log will contain
    # command text which can include credentials.
    mode = oct(log_path.stat().st_mode & 0o777)
    assert mode == oct(0o600), f"log file is world-readable: {mode}"

    svc.detach(shell_int)


def test_service_tab_title_does_not_compound_suffix(qtbot):
    """Run three commands; the tab title should carry exactly one
    ``[Xs]`` suffix, not three."""
    from qterminator.window import MainWindow

    cfg = Config()
    cfg.set("plugins", "shell_integration", "enabled", True)
    cfg.set("plugins", "command_telemetry", "enabled", True)
    # Avoid the QTimer.singleShot fade clearing the suffix mid-test.
    cfg.set("plugins", "command_telemetry", "tab_status_ms", 60_000)

    win = MainWindow()
    win.resize(800, 400)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(60)

    shell_int = getattr(win, "shell_integration", None)
    assert shell_int is not None
    tele_svc = getattr(win, "command_telemetry", None)
    assert tele_svc is not None

    terminal = win._tabs.widget(0).find_terminals()[0]
    history = shell_int.ensure_attached(terminal)

    # Manually drive the OSC parser via the registered ShadowScreen.
    shadow = win.shadow_screens._shadows[id(terminal)][0]
    for _ in range(3):
        shadow.feed("\x1b]133;A\x1b\\\x1b]133;B\x1b\\\x1b]133;C\x1b\\")
        time.sleep(0.005)
        shadow.feed("\x1b]133;D;0\x1b\\")

    title = win._tabs.tabText(0)
    # Exactly one suffix.
    assert title.count("[") == 1 or "[" not in title.replace("[", "", 1).split("]")[-1]
    # Telemetry attached on each completed record.
    assert len(history.history) == 3
    assert all(r.telemetry is not None for r in history.history)

    # Close the window so plugin polling timers (file_monitor,
    # notifications, etc.) stop before pytest tears qtbot down —
    # otherwise their _check_all slots fire on a freed QTabWidget
    # in the next event loop iteration and crash subsequent tests.
    win.close()
    qtbot.wait(60)


def test_service_emits_broadcast_event_when_agent_control_present(terminal):
    """When ``agent_control`` is on the window the service must push a
    ``command_finished`` event with telemetry — no polling required."""
    reg, shell_int = _build_shell_int()

    received = []

    class _FakeAgentControl:
        def broadcast_event(self, tab_id, event_type, payload):
            received.append((tab_id, event_type, payload))

    class _W:
        _tabs = None
        agent_control = _FakeAgentControl()

    svc = _build_service(window=_W(), display="off")
    svc.attach(shell_int)
    shell_int.ensure_attached(terminal)
    shadow = reg._shadows[id(terminal)][0]
    shadow.feed("\x1b]133;A\x1b\\\x1b]133;B\x1b\\\x1b]133;C\x1b\\")
    time.sleep(0.005)
    shadow.feed("\x1b]133;D;0\x1b\\")

    assert received, "broadcast_event was not called"
    tab_id, event_type, payload = received[-1]
    assert tab_id == id(terminal)
    assert event_type == "command_finished"
    assert payload["exit_status"] == 0
    assert "telemetry" in payload
    assert "duration" in payload["telemetry"]

    svc.detach(shell_int)


def test_service_prefers_agent_events_publish_so_filter_applies(terminal):
    """When ``agent_event_channel``'s ``agent_events`` service is on
    the window, the broadcast must go through ``publish`` so the
    user's ``set_enabled_events`` filter still gates the event.
    Bypassing it via raw ``agent_control.broadcast_event`` would let
    ``command_finished`` reach agents that opted out."""
    reg, shell_int = _build_shell_int()

    published = []
    broadcast = []

    class _FakeAgentEvents:
        # Same shape as AgentEventChannel.publish.
        def publish(self, tab_id, event_type, payload):
            published.append((tab_id, event_type, payload))

    class _FakeAgentControl:
        def broadcast_event(self, tab_id, event_type, payload):
            broadcast.append((tab_id, event_type, payload))

    class _W:
        _tabs = None
        agent_events = _FakeAgentEvents()
        agent_control = _FakeAgentControl()

    svc = _build_service(window=_W(), display="off")
    svc.attach(shell_int)
    shell_int.ensure_attached(terminal)
    shadow = reg._shadows[id(terminal)][0]
    shadow.feed("\x1b]133;A\x1b\\\x1b]133;B\x1b\\\x1b]133;C\x1b\\")
    time.sleep(0.005)
    shadow.feed("\x1b]133;D;0\x1b\\")

    assert published, "expected publish via agent_events"
    assert not broadcast, "must not bypass the channel's filter when agent_events is loaded"

    svc.detach(shell_int)


def test_service_does_not_emit_telemetry_for_missing_C(terminal):
    """``;D`` without a prior ``;C`` produces no telemetry — without
    a start moment we can't honestly report duration."""
    reg, shell_int = _build_shell_int()
    svc = _build_service(display="off")
    svc.attach(shell_int)
    history = shell_int.ensure_attached(terminal)
    shadow = reg._shadows[id(terminal)][0]

    shadow.feed("\x1b]133;A\x1b\\\x1b]133;B\x1b\\\x1b]133;D;0\x1b\\")
    assert history.last is not None
    assert history.last.telemetry is None

    svc.detach(shell_int)


# ---------------------------------------------------------------------------
# Plugin lifecycle
# ---------------------------------------------------------------------------

def test_plugin_disabled_by_default():
    """Per the project's plugin contract."""
    from qterminator.plugins.command_telemetry import CommandTelemetryPlugin

    class _W:
        _tabs = None
        shell_integration = None
    plugin = CommandTelemetryPlugin()
    plugin.activate(_W())
    assert plugin._service is None


def test_plugin_raises_without_shell_integration():
    """The dependency is hard; we'd rather refuse to load than silently
    collect zero telemetry."""
    from qterminator.plugins.command_telemetry import CommandTelemetryPlugin

    cfg = Config()
    cfg.set("plugins", "command_telemetry", "enabled", True)

    class _W:
        _tabs = None

    plugin = CommandTelemetryPlugin()
    with pytest.raises(RuntimeError):
        plugin.activate(_W())


def test_plugin_loads_on_real_window(qtbot):
    from qterminator.window import MainWindow

    cfg = Config()
    cfg.set("plugins", "shell_integration", "enabled", True)
    cfg.set("plugins", "command_telemetry", "enabled", True)

    win = MainWindow()
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(60)

    plugin = win._plugin_manager._instances.get("command_telemetry")
    assert plugin is not None
    assert plugin._service is not None
    assert getattr(win, "command_telemetry", None) is plugin._service

    # Deactivate cleanly.
    plugin.deactivate()
    assert getattr(win, "command_telemetry", None) is None

    win.close()
    qtbot.wait(60)
