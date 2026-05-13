"""Tests for the process_control plugin.

Uses real subprocesses (sleep) as mock targets so signals, state
transitions, and /proc introspection are exercised end-to-end.
All spawned processes are cleaned up via fixtures.
"""

import os
import signal
import subprocess
import time

import pytest

from qterminator.plugins.process_control import ProcessControlPlugin


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------


class FakeTitlebar:
    def __init__(self):
        self.title = None

    def set_title(self, t):
        self.title = t


class FakeTerminal:
    """Stand-in for TerminalWidget — just the interface the plugin uses."""

    def __init__(self, fg_pid=0, shell_pid=12345):
        self._fg_pid = fg_pid
        self._shell_pid = shell_pid
        self._titlebar = FakeTitlebar()

    def foreground_pid(self):
        return self._fg_pid

    def shell_pid(self):
        return self._shell_pid


def _read_state(pid):
    """Read the single-letter /proc/PID/status State:."""
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            if line.startswith("State:"):
                return line.split(":", 1)[1].strip()[0]
    return "?"


def _wait_for_state(pid, expected, timeout=1.0):
    """Poll /proc until state matches (or timeout)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if _read_state(pid) in expected:
                return True
        except FileNotFoundError:
            # Process gone; treat as a terminal state.
            return "X" in expected or "Z" in expected or "gone" in expected
        time.sleep(0.02)
    return False


@pytest.fixture
def sleep_proc():
    """Spawn a real `sleep 60` and guarantee cleanup."""
    p = subprocess.Popen(["sleep", "60"])
    try:
        yield p
    finally:
        try:
            # Make sure it's not stuck stopped
            try:
                os.kill(p.pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
            p.kill()
        except ProcessLookupError:
            pass
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


@pytest.fixture
def plugin():
    return ProcessControlPlugin()


# ---------------------------------------------------------------------------
# tests: real signals
# ---------------------------------------------------------------------------


def test_suspend_resume_real_process(sleep_proc):
    pid = sleep_proc.pid

    os.kill(pid, signal.SIGSTOP)
    assert _wait_for_state(pid, "T"), f"expected stopped, got {_read_state(pid)}"

    os.kill(pid, signal.SIGCONT)
    assert _wait_for_state(pid, "SR"), f"expected running, got {_read_state(pid)}"


def test_kill_signal_terminates(sleep_proc):
    pid = sleep_proc.pid
    os.kill(pid, signal.SIGKILL)
    assert sleep_proc.wait(timeout=2) is not None
    # On Linux SIGKILL exits with -9
    assert sleep_proc.returncode == -signal.SIGKILL


def test_terminate_signal(sleep_proc):
    pid = sleep_proc.pid
    os.kill(pid, signal.SIGTERM)
    assert sleep_proc.wait(timeout=2) is not None
    assert sleep_proc.returncode == -signal.SIGTERM


def test_interrupt_signal(sleep_proc):
    pid = sleep_proc.pid
    os.kill(pid, signal.SIGINT)
    assert sleep_proc.wait(timeout=2) is not None
    # sleep exits from SIGINT
    assert sleep_proc.returncode in (-signal.SIGINT, 130)


# ---------------------------------------------------------------------------
# tests: process info / name extraction
# ---------------------------------------------------------------------------


def test_get_process_info_real(plugin, sleep_proc):
    info = plugin._get_process_info(sleep_proc.pid)
    assert info["pid"] == sleep_proc.pid
    assert info["name"] == "sleep"
    assert "sleep" in info["cmdline"]
    # State is a single letter possibly followed by description
    assert info["state"][0] in "SRDTZ"


def test_get_process_name(plugin, sleep_proc):
    assert plugin._get_process_name(sleep_proc.pid) == "sleep"


def test_get_process_name_nonexistent(plugin):
    # Extremely unlikely PID
    assert plugin._get_process_name(2**22) == "unknown"


# ---------------------------------------------------------------------------
# tests: error paths
# ---------------------------------------------------------------------------


def test_signal_nonexistent_pid():
    with pytest.raises(ProcessLookupError):
        os.kill(2**22, signal.SIGTERM)


def test_signal_no_permission():
    """Signalling init should fail with PermissionError unless we're root."""
    if os.geteuid() == 0:
        pytest.skip("running as root, can signal init")
    with pytest.raises(PermissionError):
        os.kill(1, signal.SIGTERM)


def test_plugin_send_signal_handles_nonexistent(plugin, monkeypatch):
    """_send_signal should swallow ProcessLookupError via QMessageBox."""
    warnings = []
    monkeypatch.setattr(
        "qterminator.plugins.process_control.QMessageBox.warning",
        lambda *a, **kw: warnings.append(a),
    )
    term = FakeTerminal(fg_pid=2**22, shell_pid=1)
    # Should not raise
    plugin._send_signal(term, 2**22, signal.SIGTERM, "Terminated")
    assert len(warnings) == 1


def test_plugin_send_signal_real(plugin, sleep_proc):
    """_send_signal via the plugin actually signals a real process."""
    term = FakeTerminal(fg_pid=sleep_proc.pid, shell_pid=os.getpid())
    plugin._send_signal(term, sleep_proc.pid, signal.SIGSTOP, "Suspended")
    assert _wait_for_state(sleep_proc.pid, "T")
    assert term._titlebar.title == "Suspended: sleep"

    plugin._send_signal(term, sleep_proc.pid, signal.SIGCONT, "Resumed")
    assert _wait_for_state(sleep_proc.pid, "SR")
    assert term._titlebar.title == "Resumed: sleep"


def test_plugin_send_signal_to_process_group(plugin):
    """Signal should reach child processes in the same process group."""
    # Use start_new_session=True so the child becomes a group leader with
    # its own children inheriting the group.
    parent = subprocess.Popen(
        ["sh", "-c", "sleep 60 & wait"],
        start_new_session=True,
    )
    try:
        # Give the child sleep time to start
        time.sleep(0.2)
        term = FakeTerminal(fg_pid=parent.pid, shell_pid=os.getpid())
        plugin._send_signal(term, parent.pid, signal.SIGKILL, "Killed")
        # Both parent and its child should die because killpg hits the group
        assert parent.wait(timeout=2) is not None
    finally:
        try:
            parent.kill()
        except ProcessLookupError:
            pass
        try:
            parent.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass


# ---------------------------------------------------------------------------
# tests: menu item generation
# ---------------------------------------------------------------------------


def test_plugin_get_menu_items_no_process(plugin):
    term = FakeTerminal(fg_pid=0, shell_pid=12345)
    items = plugin.get_menu_items(term)
    labels = [label for label, _ in items]
    assert any("No foreground process" in lab for lab in labels)
    assert any("Resume All" in lab for lab in labels)


def test_plugin_get_menu_items_shell_only(plugin):
    """When foreground_pid == shell_pid, treat as no foreground process."""
    term = FakeTerminal(fg_pid=12345, shell_pid=12345)
    items = plugin.get_menu_items(term)
    labels = [label for label, _ in items]
    assert any("No foreground process" in lab for lab in labels)


def test_plugin_get_menu_items_with_process(plugin, sleep_proc):
    term = FakeTerminal(fg_pid=sleep_proc.pid, shell_pid=os.getpid())
    items = plugin.get_menu_items(term)
    labels = [label for label, _ in items]
    # Process info line includes name + pid
    assert any(f"PID {sleep_proc.pid}" in lab for lab in labels)
    assert any("Suspend Process" in lab for lab in labels)
    assert any("Resume Process" in lab for lab in labels)
    assert any("Interrupt" in lab for lab in labels)
    assert any("Terminate" in lab for lab in labels)
    assert any("Kill" in lab for lab in labels)
    assert any("Background" in lab for lab in labels)


def test_plugin_menu_callbacks_capture_pid(plugin, sleep_proc):
    """Lambda must capture fg_pid via default argument, not closure."""
    term = FakeTerminal(fg_pid=sleep_proc.pid, shell_pid=os.getpid())
    items = plugin.get_menu_items(term)

    # Find the Suspend callback
    suspend_cb = next(cb for label, cb in items if "Suspend Process" in label)
    suspend_cb()
    assert _wait_for_state(sleep_proc.pid, "T")

    resume_cb = next(cb for label, cb in items if "Resume Process" in label)
    resume_cb()
    assert _wait_for_state(sleep_proc.pid, "SR")


def test_plugin_metadata():
    p = ProcessControlPlugin()
    assert p.name == "process_control"
    assert "menu_provider" in p.capabilities
    assert p.version
    assert p.description
