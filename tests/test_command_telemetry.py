"""Tests for the command_telemetry plugin.

Layers:
  - ProcTreeSampler: mock /proc entries, verify CPU/RSS computation.
  - Pure helpers (parse / sample / tab-title suffix regex).
  - Service-level: drive ;C / ;D through shell_integration's real
    OSC parser; assert telemetry annotates the CommandRecord, JSONL
    log writes, tab title gets the [Xs] suffix without compounding.
  - Inline display mode.
  - Config validation.
  - Plugin lifecycle on a real MainWindow.
  - agent_control rpc_command_telemetry.
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
# ProcTreeSampler tests with mock /proc
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_proc(tmp_path):
    """Build a minimal /proc-like tree for ProcTreeSampler tests.

    Layout:
      /proc/100/stat        — root process
      /proc/100/status      — VmRSS 10240 kB
      /proc/100/task/100/children — "101 102"
      /proc/101/stat        — child 1
      /proc/101/status      — VmRSS 5120 kB
      /proc/101/task/101/children — "" (empty)
      /proc/102/stat        — child 2
      /proc/102/status      — VmRSS 2048 kB
      /proc/102/task/102/children — "" (empty)
    """
    proc = tmp_path / "proc"

    # Helper: create a /proc/<pid>/stat with given utime/stime.
    # Format: pid (comm) S ppid pgid session tty_nr tpgid flags
    #         minflt cminflt majflt cmajflt utime stime cutime cstime ...
    # Fields before utime/stime are indices 0..12 (13 fields before
    # the close-paren suffix). After ") S" we need 11 fields before
    # utime (index 11) and stime (index 12) in the post-paren split.
    def make_proc(pid, ppid, utime, stime, vmrss_kb, children_str):
        d = proc / str(pid)
        d.mkdir(parents=True)
        # Build stat: "pid (comm) S ppid ... utime stime ..."
        # After ") " we need: state ppid pgid sid tty tpgid flags
        # minflt cminflt majflt cmajflt utime stime ...
        # That's index 0=state 1=ppid 2=pgid ... 11=utime 12=stime
        post_paren = f"S {ppid} 0 0 0 0 0 0 0 0 0 {utime} {stime} 0 0 20 0 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
        stat_content = f"{pid} (test) {post_paren}\n"
        (d / "stat").write_text(stat_content)

        status_content = (
            f"Name:\ttest\n"
            f"Pid:\t{pid}\n"
            f"PPid:\t{ppid}\n"
            f"VmRSS:\t{vmrss_kb} kB\n"
        )
        (d / "status").write_text(status_content)

        task = d / "task" / str(pid)
        task.mkdir(parents=True)
        (task / "children").write_text(children_str + "\n")

    # Root (pid 100): 500 utime + 200 stime ticks, 10240 kB RSS
    make_proc(100, 1, 500, 200, 10240, "101 102")
    # Child 1 (pid 101): 100 utime + 50 stime ticks, 5120 kB RSS
    make_proc(101, 100, 100, 50, 5120, "")
    # Child 2 (pid 102): 30 utime + 10 stime ticks, 2048 kB RSS
    make_proc(102, 100, 30, 10, 2048, "")

    return str(proc)


def test_proc_tree_sampler_read_children(fake_proc):
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root=fake_proc)
    children = sampler.read_children(100)
    assert sorted(children) == [101, 102]
    # Leaf processes have no children.
    assert sampler.read_children(101) == []
    assert sampler.read_children(102) == []


def test_proc_tree_sampler_walk_tree(fake_proc):
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root=fake_proc)
    pids = sampler.walk_tree(100)
    assert sorted(pids) == [100, 101, 102]


def test_proc_tree_sampler_read_cpu_seconds(fake_proc):
    from qterminator.plugins.command_telemetry import ProcTreeSampler, _CLK_TCK
    sampler = ProcTreeSampler(proc_root=fake_proc)
    cpu = sampler.read_cpu_seconds(100)
    expected = (500 + 200) / _CLK_TCK
    assert abs(cpu - expected) < 0.001


def test_proc_tree_sampler_read_rss_bytes(fake_proc):
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root=fake_proc)
    # 10240 kB * 1024 = 10485760 bytes
    assert sampler.read_rss_bytes(100) == 10240 * 1024
    assert sampler.read_rss_bytes(101) == 5120 * 1024
    assert sampler.read_rss_bytes(102) == 2048 * 1024


def test_proc_tree_sampler_sample_aggregates_tree(fake_proc):
    from qterminator.plugins.command_telemetry import ProcTreeSampler, _CLK_TCK
    sampler = ProcTreeSampler(proc_root=fake_proc)
    snap = sampler.sample(100)
    # CPU: sum of all processes
    expected_cpu = (500 + 200 + 100 + 50 + 30 + 10) / _CLK_TCK
    assert abs(snap["cpu_seconds"] - expected_cpu) < 0.001
    # RSS: sum across tree at this instant
    expected_rss = (10240 + 5120 + 2048) * 1024
    assert snap["peak_rss_bytes"] == expected_rss
    # Process count: 3 (root + 2 children)
    assert snap["process_count"] == 3


def test_proc_tree_sampler_zero_pid():
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler()
    snap = sampler.sample(0)
    assert snap == {"cpu_seconds": 0.0, "peak_rss_bytes": 0, "process_count": 0}


def test_proc_tree_sampler_missing_pid():
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler()
    assert sampler.read_children(2_147_000_000) == []
    assert sampler.read_cpu_seconds(2_147_000_000) == 0.0
    assert sampler.read_rss_bytes(2_147_000_000) == 0


def test_proc_tree_sampler_fallback_ppid_scan(tmp_path):
    """When /proc/<pid>/task/<pid>/children is missing, the sampler
    should fall back to scanning all /proc/*/stat for matching ppid."""
    from qterminator.plugins.command_telemetry import ProcTreeSampler, _CLK_TCK

    proc = tmp_path / "proc_no_children"

    def make_proc_no_children(pid, ppid, utime, stime, vmrss_kb):
        d = proc / str(pid)
        d.mkdir(parents=True)
        post_paren = f"S {ppid} 0 0 0 0 0 0 0 0 0 {utime} {stime} 0 0 20 0 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
        (d / "stat").write_text(f"{pid} (test) {post_paren}\n")
        (d / "status").write_text(
            f"Name:\ttest\nPid:\t{pid}\nPPid:\t{ppid}\n"
            f"VmRSS:\t{vmrss_kb} kB\n"
        )
        # NO task/<pid>/children file — force fallback

    make_proc_no_children(200, 1, 100, 50, 4096)
    make_proc_no_children(201, 200, 20, 10, 2048)

    sampler = ProcTreeSampler(proc_root=str(proc))
    # read_children should find 201 as child of 200 via ppid scan
    children = sampler.read_children(200)
    assert 201 in children
    # walk_tree should include both
    pids = sampler.walk_tree(200)
    assert sorted(pids) == [200, 201]


# ---------------------------------------------------------------------------
# Pure helper tests (backward-compat module-level functions)
# ---------------------------------------------------------------------------

def test_tab_suffix_regex_strips_prior_entry():
    """Repeated commands must not compound suffixes: ``[1s] [2s] [3s]``."""
    from qterminator.plugins.command_telemetry import _TAB_SUFFIX_RE

    assert _TAB_SUFFIX_RE.sub("", "tab1  [12.4s]") == "tab1"
    assert _TAB_SUFFIX_RE.sub("", "tab1  [12.4s · 412MB]") == "tab1"
    assert _TAB_SUFFIX_RE.sub("", "tab1  [3.2s · 12MB · 4.0s CPU]") == "tab1"
    # No suffix -> no change.
    assert _TAB_SUFFIX_RE.sub("", "plain-title") == "plain-title"
    # Brackets without a duration -> not our suffix; leave alone.
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
# Service tests -- drive real OSC parser via ShadowScreen
# ---------------------------------------------------------------------------

@pytest.fixture
def terminal(qtbot):
    """A real TerminalWidget -- needed because ShadowScreenRegistry
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
    # Permissions are tightened on creation -- the log will contain
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
    # notifications, etc.) stop before pytest tears qtbot down --
    # otherwise their _check_all slots fire on a freed QTabWidget
    # in the next event loop iteration and crash subsequent tests.
    win.close()
    qtbot.wait(60)


def test_service_emits_broadcast_event_when_agent_control_present(terminal):
    """When ``agent_control`` is on the window the service must push a
    ``command_finished`` event with telemetry -- no polling required."""
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
    """``;D`` without a prior ``;C`` produces no telemetry -- without
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
# Inline display mode
# ---------------------------------------------------------------------------

def test_service_inline_display_sends_text_to_terminal(terminal):
    """When display='inline', a dim grey telemetry line should be
    injected into the terminal via send_text."""
    reg, shell_int = _build_shell_int()

    sent_texts = []
    original_send = terminal.send_text

    def mock_send_text(text):
        sent_texts.append(text)

    terminal.send_text = mock_send_text
    try:
        svc = _build_service(display="inline")
        svc.attach(shell_int)
        shell_int.ensure_attached(terminal)
        shadow = reg._shadows[id(terminal)][0]

        shadow.feed("\x1b]133;A\x1b\\\x1b]133;B\x1b\\\x1b]133;C\x1b\\")
        time.sleep(0.005)
        shadow.feed("\x1b]133;D;0\x1b\\")

        assert sent_texts, "send_text was not called for inline display"
        # The injected text should contain the telemetry summary.
        injected = sent_texts[-1]
        assert "\x1b[2;37m" in injected, "expected dim grey SGR"
        assert "\x1b[0m" in injected, "expected SGR reset"
        # Should contain a duration like "0.0s"
        assert "s" in injected

        svc.detach(shell_int)
    finally:
        terminal.send_text = original_send


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def test_service_rejects_invalid_display_mode():
    """An unrecognized display mode should fall back to 'tab_status'."""
    svc = _build_service(display="banana")
    assert svc.display == "tab_status"


def test_valid_display_modes_constant():
    from qterminator.plugins.command_telemetry import VALID_DISPLAY_MODES
    assert "tab_status" in VALID_DISPLAY_MODES
    assert "inline" in VALID_DISPLAY_MODES
    assert "off" in VALID_DISPLAY_MODES


# ---------------------------------------------------------------------------
# get_telemetry_history
# ---------------------------------------------------------------------------

def test_service_get_telemetry_history(terminal):
    """get_telemetry_history returns annotated records in order."""
    reg, shell_int = _build_shell_int()

    class _W:
        _tabs = None
        shell_integration = shell_int

    svc = _build_service(window=_W(), display="off")
    svc.attach(shell_int)
    history = shell_int.ensure_attached(terminal)
    shadow = reg._shadows[id(terminal)][0]

    # Run 3 commands.
    for code in (0, 1, 0):
        shadow.feed("\x1b]133;A\x1b\\\x1b]133;B\x1b\\\x1b]133;C\x1b\\")
        time.sleep(0.005)
        shadow.feed(f"\x1b]133;D;{code}\x1b\\")

    records = svc.get_telemetry_history(terminal, limit=10)
    assert len(records) == 3
    assert [r["exit_status"] for r in records] == [0, 1, 0]
    for r in records:
        assert "telemetry" in r
        assert "duration" in r["telemetry"]

    # Limit works.
    records2 = svc.get_telemetry_history(terminal, limit=2)
    assert len(records2) == 2
    # Should return the last 2 in order.
    assert [r["exit_status"] for r in records2] == [1, 0]

    svc.detach(shell_int)


def test_service_get_telemetry_history_returns_empty_without_shell_int(terminal):
    """When shell_integration is not on the window, returns empty list."""
    svc = _build_service(display="off")
    assert svc.get_telemetry_history(terminal) == []


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


# ---------------------------------------------------------------------------
# agent_control rpc_command_telemetry
# ---------------------------------------------------------------------------

def test_agent_control_rpc_command_telemetry(terminal):
    """The rpc_command_telemetry RPC method returns annotated records."""
    from qterminator.plugins.agent_control import AgentControlPlugin

    reg, shell_int = _build_shell_int()

    class _FakeTabs:
        def count(self):
            return 1

        def widget(self, _i):
            return self

        def find_terminals(self):
            return [terminal]

    class _W:
        _tabs = _FakeTabs()
        shell_integration = shell_int

    svc = _build_service(window=_W(), display="off")
    svc.attach(shell_int)
    shell_int.ensure_attached(terminal)
    shadow = reg._shadows[id(terminal)][0]

    # Attach the telemetry service to the window.
    _W.command_telemetry = svc

    # Run 2 commands.
    for code in (0, 42):
        shadow.feed("\x1b]133;A\x1b\\\x1b]133;B\x1b\\\x1b]133;C\x1b\\")
        time.sleep(0.005)
        shadow.feed(f"\x1b]133;D;{code}\x1b\\")

    # Build a minimal AgentControlPlugin for the RPC test.
    plugin = AgentControlPlugin()
    plugin._window = _W()

    result = plugin.rpc_command_telemetry(None, tab_id=id(terminal), limit=10)
    assert "records" in result
    assert len(result["records"]) == 2
    assert result["records"][0]["exit_status"] == 0
    assert result["records"][1]["exit_status"] == 42
    for rec in result["records"]:
        assert "telemetry" in rec
        assert "duration" in rec["telemetry"]

    svc.detach(shell_int)


def test_agent_control_rpc_command_telemetry_raises_without_plugin():
    """rpc_command_telemetry must raise when command_telemetry is not loaded."""
    from qterminator.plugins.agent_control import AgentControlPlugin

    class _FakeTerminal:
        pass

    t = _FakeTerminal()

    class _FakeTabs:
        def count(self):
            return 1

        def widget(self, _i):
            return self

        def find_terminals(self):
            return [t]

    class _W:
        _tabs = _FakeTabs()

    plugin = AgentControlPlugin()
    plugin._window = _W()

    # _RpcError is raised when the plugin is not loaded.
    with pytest.raises(Exception, match="command_telemetry plugin not loaded"):
        plugin.rpc_command_telemetry(None, tab_id=id(t))
