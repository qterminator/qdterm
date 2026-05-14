"""Tests for the agent_control plugin.

Exercises the JSON-RPC protocol end-to-end through the real Unix socket,
not by calling RPC methods directly. The plugin's notifier-driven server
runs on the Qt main thread; client I/O runs in a worker thread while the
main thread pumps the event loop via ``qtbot.wait``.
"""

import base64
import json
import os
import socket
import threading
import time

import pytest

import qterminator.config as config_mod
from qterminator.config import Config


pytest.importorskip("pyte")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _agent_env(tmp_path, monkeypatch):
    """Enable the plugin and isolate the socket path / config dir."""
    monkeypatch.setenv("QTERMINATOR_AGENT_CONTROL", "1")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(
        config_mod, "CONFIG_FILE", str(tmp_path / "config" / "config.toml"),
    )
    Config._instance = None
    yield
    Config._instance = None


@pytest.fixture
def window(qtbot):
    from qterminator.window import MainWindow
    win = MainWindow()
    win.resize(900, 600)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(150)  # let the shell start and produce its first prompt
    yield win


@pytest.fixture
def plugin(window):
    pm = window._plugin_manager
    p = pm._instances.get("agent_control")
    assert p is not None, "agent_control plugin not loaded"
    assert p.socket_path is not None, "socket not opened (env gate failed?)"
    return p


def _wait_for_socket(path, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if os.path.exists(path):
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# RPC client used by tests
# ---------------------------------------------------------------------------

class RpcClient:
    """Blocking JSON-RPC client. All calls are executed in a worker thread
    while the caller pumps the Qt event loop via ``qtbot.wait``."""

    def __init__(self, path: str):
        self.path = path
        self._s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._s.connect(path)
        self._next = 1
        self._buf = b""

    def close(self):
        try:
            self._s.close()
        except OSError:
            pass

    def _send_recv(self, method, **params):
        rid = self._next
        self._next += 1
        req = json.dumps({"jsonrpc": "2.0", "id": rid,
                          "method": method, "params": params})
        self._s.sendall((req + "\n").encode("utf-8"))
        while True:
            while b"\n" not in self._buf:
                chunk = self._s.recv(65536)
                if not chunk:
                    raise RuntimeError("server closed")
                self._buf += chunk
            line, _, rest = self._buf.partition(b"\n")
            self._buf = rest
            msg = json.loads(line)
            if msg.get("id") == rid:
                return msg

    def call(self, qtbot, method, **params):
        result = [None]
        err = [None]
        done = threading.Event()

        def target():
            try:
                result[0] = self._send_recv(method, **params)
            except Exception as e:  # noqa: BLE001
                err[0] = e
            finally:
                done.set()

        threading.Thread(target=target, daemon=True).start()
        end = time.monotonic() + 5.0
        while not done.is_set() and time.monotonic() < end:
            qtbot.wait(15)
        if err[0]:
            raise err[0]
        if not done.is_set():
            raise TimeoutError(f"{method} did not return in 5s")
        return result[0]


@pytest.fixture
def rpc(plugin):
    c = RpcClient(plugin.socket_path)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_socket_appears_when_enabled(plugin):
    assert _wait_for_socket(plugin.socket_path), "socket file never appeared"


def test_list_tabs_returns_initial_tab(qtbot, rpc):
    resp = rpc.call(qtbot, "list_tabs")
    assert "result" in resp, resp
    tabs = resp["result"]
    assert isinstance(tabs, list) and len(tabs) == 1
    t = tabs[0]
    for key in ("id", "title", "shell_pid", "cols", "rows", "attached",
                "tmux_session"):
        assert key in t
    assert t["attached"] is False
    # No tmux_mode plugin enabled here, but the key is still present.
    # (Value is None whether or not the tmux_mode plugin happens to be
    # loaded — the initial tab spawns the user's $SHELL.)
    assert t["tmux_session"] is None


def test_list_tabs_reports_tmux_session_when_service_present(qtbot, rpc, window):
    """If MainWindow exposes a tmux_mode service, agent_control surfaces
    the detection result through list_tabs.tmux_session."""
    class _FakeService:
        def get_session_for_terminal(self, _t):
            return "qterm-fake"
    window.tmux_mode = _FakeService()
    try:
        resp = rpc.call(qtbot, "list_tabs")
        assert resp["result"][0]["tmux_session"] == "qterm-fake"
    finally:
        del window.tmux_mode


def test_list_tabs_reports_shared_via_mosh(qtbot, rpc, window):
    """When tmux_share exposes active shares, list_tabs surfaces the
    list of UDP ports under shared_via_mosh."""
    class _FakeTmuxMode:
        def get_session_for_terminal(self, _t):
            return "qterm-fake"
    class _FakeShare:
        def ports_for(self, sess):
            return [60042] if sess == "qterm-fake" else []
    window.tmux_mode = _FakeTmuxMode()
    window.tmux_share = _FakeShare()
    try:
        resp = rpc.call(qtbot, "list_tabs")
        assert resp["result"][0]["shared_via_mosh"] == [60042]
    finally:
        del window.tmux_mode
        del window.tmux_share


def test_send_requires_attach(qtbot, rpc):
    tabs = rpc.call(qtbot, "list_tabs")["result"]
    tid = tabs[0]["id"]
    resp = rpc.call(qtbot, "send_text", tab_id=tid, text="echo nope\n")
    assert "error" in resp
    assert resp["error"]["code"] == -32001


def test_attach_send_and_tail_stream(qtbot, rpc):
    tabs = rpc.call(qtbot, "list_tabs")["result"]
    tid = tabs[0]["id"]

    a = rpc.call(qtbot, "attach", tab_id=tid)
    assert a["result"]["ok"] is True

    rpc.call(qtbot, "send_text", tab_id=tid, text="echo HELLO_AGENT\n")
    # Let the PTY echo and the shell run the command.
    qtbot.wait(400)

    tail = rpc.call(qtbot, "tail_stream", tab_id=tid, since=0)["result"]
    raw = base64.b64decode(tail["bytes_b64"]).decode("utf-8", errors="replace")
    assert "HELLO_AGENT" in raw, f"stream had: {raw!r}"
    assert tail["latest_seq"] >= 1


def test_get_screen_reports_grid_and_cursor(qtbot, rpc):
    tabs = rpc.call(qtbot, "list_tabs")["result"]
    tid = tabs[0]["id"]
    rpc.call(qtbot, "attach", tab_id=tid)

    rpc.call(qtbot, "send_text", tab_id=tid, text="echo SCREEN_MARK\n")
    qtbot.wait(400)

    snap = rpc.call(qtbot, "get_screen", tab_id=tid)["result"]
    assert isinstance(snap["lines"], list) and snap["lines"], snap
    joined = "\n".join(snap["lines"])
    assert "SCREEN_MARK" in joined, f"screen had: {joined!r}"
    assert 0 <= snap["cursor"]["x"] < snap["cols"]
    assert 0 <= snap["cursor"]["y"] < snap["rows"]


def test_screenshot_returns_png(qtbot, rpc):
    tabs = rpc.call(qtbot, "list_tabs")["result"]
    tid = tabs[0]["id"]
    shot = rpc.call(qtbot, "screenshot", tab_id=tid)["result"]
    png = base64.b64decode(shot["png_b64"])
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    assert shot["width"] > 0 and shot["height"] > 0


def test_open_tab_grows_list(qtbot, rpc):
    before = rpc.call(qtbot, "list_tabs")["result"]
    rpc.call(qtbot, "open_tab")
    qtbot.wait(150)
    after = rpc.call(qtbot, "list_tabs")["result"]
    assert len(after) == len(before) + 1


def test_detach_clears_pyte_and_rejects_further_input(qtbot, rpc, plugin):
    tabs = rpc.call(qtbot, "list_tabs")["result"]
    tid = tabs[0]["id"]
    rpc.call(qtbot, "attach", tab_id=tid)
    assert tid in plugin.tab_states

    rpc.call(qtbot, "detach", tab_id=tid)
    assert tid not in plugin.tab_states  # last subscriber gone → torn down

    resp = rpc.call(qtbot, "send_text", tab_id=tid, text="echo x\n")
    assert "error" in resp and resp["error"]["code"] == -32001


def test_send_keys_translates_ansi(qtbot, rpc):
    tabs = rpc.call(qtbot, "list_tabs")["result"]
    tid = tabs[0]["id"]
    rpc.call(qtbot, "attach", tab_id=tid)
    # Type "abc", then send Ctrl-U (kill line) and Enter — the visible
    # screen should not end up with "abc" remaining on the prompt line.
    rpc.call(qtbot, "send_text", tab_id=tid, text="abc")
    rpc.call(qtbot, "send_keys", tab_id=tid, keys=["ctrl+u", "enter"])
    qtbot.wait(300)
    snap = rpc.call(qtbot, "get_screen", tab_id=tid)["result"]
    # On the prompt line containing the cursor, "abc" should not be
    # the trailing visible characters (ctrl+u killed it).
    cursor_line = snap["lines"][snap["cursor"]["y"]].rstrip()
    assert not cursor_line.endswith("abc"), f"line: {cursor_line!r}"


def test_uid_mismatch_rejected(qtbot, plugin):
    # The plugin is loaded via importlib.util.spec_from_file_location, so
    # its module globals are a *separate* dict from the import-system's
    # qterminator.plugins.agent_control module. Patch via the live
    # globals dict on one of the plugin's own callables so the stub is
    # seen by the running _accept method.
    g = plugin.__class__.__init__.__globals__
    real = g["_peer_uid_matches"]
    try:
        g["_peer_uid_matches"] = lambda conn: False
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(plugin.socket_path)
        qtbot.wait(250)  # let the listener notifier fire and reject
        s.settimeout(1.0)
        closed = False
        try:
            s.sendall(b'{"jsonrpc":"2.0","id":1,"method":"list_tabs"}\n')
            qtbot.wait(150)
            data = s.recv(64)
            closed = (data == b"")
        except (BrokenPipeError, ConnectionResetError, OSError):
            closed = True
        assert closed, "rejected connection should be closed by the server"
        s.close()
    finally:
        g["_peer_uid_matches"] = real
