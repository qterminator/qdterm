"""Agent control plugin for QTerminator.

Exposes a local Unix-domain JSON-RPC socket so an external agent process
(Claude Code, opencode, a plain shell client, ...) can introspect tabs,
inject input, read the raw PTY stream, and snapshot the rendered screen
state.

Two read surfaces:
  - ``tail_stream``: raw PTY bytes (base64) since a sequence number. Use
    when the agent needs byte-accurate output for log parsing.
  - ``get_screen``: rendered character grid + cursor position. Use when
    the agent is driving a TUI and needs to know what the user sees.

Both surfaces are backed by the shared ``ShadowScreenRegistry`` on
``MainWindow.shadow_screens`` (see ``qterminator/shadow_screen.py``).
Other plugins that need the same view of a terminal share the registry
so each terminal has at most one pyte instance regardless of how many
plugins are watching it.

Activation:
  - The plugin is discovered by ``PluginManager`` like any other.
  - It only opens the socket if either:
      ``$QTERMINATOR_AGENT_CONTROL=1`` is set, OR
      the config file has ``[plugins] agent_control = true``.
  - With no client attached, per-tab overhead is zero: no
    ``ShadowScreen`` is acquired until an agent calls ``attach``.

Security: connections are accepted only from the same UID via
``SO_PEERCRED``. Anything else is closed immediately.
"""

import base64
import json
import os
import socket
import struct
from typing import Any, Optional

from PyQt6.QtCore import QObject, QSocketNotifier, QBuffer, QByteArray, QIODevice

from qterminator.plugin import Plugin
from qterminator.config import Config


def _socket_path() -> str:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(runtime_dir, f"qterminator-agent-{os.getuid()}.sock")


def _key_to_bytes(name: str) -> bytes:
    """Translate a symbolic key name to the PTY bytes a real keypress would
    produce. Covers the keys TUIs actually care about."""
    name = name.lower()
    if len(name) == 1:
        return name.encode("utf-8")
    table = {
        "enter": b"\r",
        "return": b"\r",
        "tab": b"\t",
        "backspace": b"\x7f",
        "escape": b"\x1b",
        "esc": b"\x1b",
        "space": b" ",
        "up": b"\x1b[A",
        "down": b"\x1b[B",
        "right": b"\x1b[C",
        "left": b"\x1b[D",
        "home": b"\x1b[H",
        "end": b"\x1b[F",
        "pageup": b"\x1b[5~",
        "pagedown": b"\x1b[6~",
        "insert": b"\x1b[2~",
        "delete": b"\x1b[3~",
        "f1": b"\x1bOP", "f2": b"\x1bOQ", "f3": b"\x1bOR", "f4": b"\x1bOS",
        "f5": b"\x1b[15~", "f6": b"\x1b[17~", "f7": b"\x1b[18~",
        "f8": b"\x1b[19~", "f9": b"\x1b[20~", "f10": b"\x1b[21~",
        "f11": b"\x1b[23~", "f12": b"\x1b[24~",
    }
    if name in table:
        return table[name]
    for prefix in ("ctrl+", "c-", "ctrl-"):
        if name.startswith(prefix):
            ch = name[len(prefix):]
            if len(ch) == 1 and ("a" <= ch <= "z"):
                return bytes([ord(ch) - ord("a") + 1])
            break
    raise ValueError(f"unknown key name: {name!r}")


class _AttachState:
    """Plugin-side bookkeeping for one attached tab.

    Owns a ``ShadowScreenHandle`` (from the shared registry) and the
    listener callback registered with it. Holds the set of client fds
    that are subscribed to live stream events for this tab.
    """

    def __init__(self, handle, listener):
        self.handle = handle
        self.listener = listener
        self.subscribers: set[int] = set()


class _AgentServer(QObject):
    """Owns the listening socket and routes JSON-RPC requests on the Qt
    main thread via QSocketNotifier — no extra threads, no locking."""

    def __init__(self, plugin: "AgentControlPlugin", window):
        super().__init__()
        self._plugin = plugin
        self._window = window
        self._path = _socket_path()
        self._sock: Optional[socket.socket] = None
        self._notifier: Optional[QSocketNotifier] = None
        self._clients: dict[int, "_Client"] = {}

    def start(self):
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(self._path)
        os.chmod(self._path, 0o600)
        s.listen(8)
        s.setblocking(False)
        self._sock = s
        self._notifier = QSocketNotifier(s.fileno(), QSocketNotifier.Type.Read, self)
        self._notifier.activated.connect(self._accept)

    def stop(self):
        if self._notifier:
            self._notifier.setEnabled(False)
            self._notifier.deleteLater()
            self._notifier = None
        for client in list(self._clients.values()):
            client.close()
        if self._sock:
            try:
                self._sock.close()
            finally:
                self._sock = None
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass

    @property
    def socket_path(self) -> str:
        return self._path

    def _accept(self, *_):
        try:
            conn, _addr = self._sock.accept()
        except BlockingIOError:
            return
        if not _peer_uid_matches(conn):
            conn.close()
            return
        conn.setblocking(False)
        client = _Client(conn, self)
        self._clients[conn.fileno()] = client

    def remove_client(self, fd: int):
        self._clients.pop(fd, None)
        # Drop any subscriptions this client held.
        for state in self._plugin.tab_states.values():
            state.subscribers.discard(fd)

    def broadcast_stream(self, tab_id: int, seq: int, raw: bytes):
        state = self._plugin.tab_states.get(tab_id)
        if not state or not state.subscribers:
            return
        msg = {
            "event": "data",
            "tab_id": tab_id,
            "seq": seq,
            "bytes_b64": base64.b64encode(raw).decode("ascii"),
        }
        line = (json.dumps(msg) + "\n").encode("utf-8")
        for fd in list(state.subscribers):
            client = self._clients.get(fd)
            if client:
                client.send_raw(line)

    def handle(self, client: "_Client", req: dict) -> dict:
        method = req.get("method")
        params = req.get("params") or {}
        rid = req.get("id")
        try:
            fn = getattr(self._plugin, f"rpc_{method}", None)
            if fn is None:
                return _err(rid, -32601, f"unknown method: {method}")
            result = fn(client, **params)
            return {"jsonrpc": "2.0", "id": rid, "result": result}
        except _RpcError as e:
            return _err(rid, e.code, e.message)
        except TypeError as e:
            return _err(rid, -32602, f"invalid params: {e}")
        except Exception as e:  # noqa: BLE001
            return _err(rid, -32000, f"{type(e).__name__}: {e}")


class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _err(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _peer_uid_matches(conn: socket.socket) -> bool:
    try:
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
        return uid == os.getuid()
    except OSError:
        return False


class _Client(QObject):
    def __init__(self, conn: socket.socket, server: _AgentServer):
        super().__init__()
        self._conn = conn
        self._fd = conn.fileno()
        self._server = server
        self._buf = bytearray()
        self._notifier = QSocketNotifier(self._fd, QSocketNotifier.Type.Read, self)
        self._notifier.activated.connect(self._on_readable)
        # Tabs this client has called attach() on; cleaned up on disconnect.
        self.attached_tabs: set[int] = set()

    @property
    def fd(self) -> int:
        return self._fd

    def _on_readable(self, *_):
        try:
            chunk = self._conn.recv(8192)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self.close()
            return
        if not chunk:
            self.close()
            return
        self._buf.extend(chunk)
        while b"\n" in self._buf:
            line, _, rest = self._buf.partition(b"\n")
            self._buf = bytearray(rest)
            if not line.strip():
                continue
            try:
                req = json.loads(line.decode("utf-8"))
            except Exception:
                self.send_obj(_err(None, -32700, "parse error"))
                continue
            resp = self._server.handle(self, req)
            self.send_obj(resp)

    def send_obj(self, obj: dict):
        self.send_raw((json.dumps(obj) + "\n").encode("utf-8"))

    def send_raw(self, data: bytes):
        try:
            self._conn.sendall(data)
        except OSError:
            self.close()

    def close(self):
        # Auto-detach every tab this client had attached so the registry
        # refcount drops correctly even on abrupt disconnects.
        for tab_id in list(self.attached_tabs):
            try:
                self._server._plugin._detach_client_from_tab(self._fd, tab_id)
            except Exception:
                pass
        self.attached_tabs.clear()
        self._server.remove_client(self._fd)
        if self._notifier:
            self._notifier.setEnabled(False)
            self._notifier.deleteLater()
            self._notifier = None
        try:
            self._conn.close()
        except OSError:
            pass


class AgentControlPlugin(Plugin):
    name = "agent_control"
    description = "Expose tabs to external agents via a local Unix socket."
    version = "0.1"
    capabilities = ["agent_control"]

    def __init__(self):
        super().__init__()
        self._window = None
        self._server: Optional[_AgentServer] = None
        # tab_id (id(terminal)) -> _AttachState. Present only for tabs that
        # at least one client has called attach() on.
        self.tab_states: dict[int, _AttachState] = {}

    @staticmethod
    def _is_enabled() -> bool:
        if os.environ.get("QTERMINATOR_AGENT_CONTROL") == "1":
            return True
        try:
            return bool(Config().get("plugins", "agent_control", default=False))
        except Exception:
            return False

    def activate(self, app_controller):
        if not self._is_enabled():
            return
        if not hasattr(app_controller, "shadow_screens"):
            # Window predates the registry — refuse to load rather than
            # silently fall back to per-plugin pyte.
            raise RuntimeError(
                "agent_control requires MainWindow.shadow_screens registry"
            )
        self._window = app_controller
        self._server = _AgentServer(self, app_controller)
        self._server.start()

    def deactivate(self):
        # Release all shadow handles. The registry drops the underlying
        # signal connection when the last consumer releases.
        for tab_id, state in list(self.tab_states.items()):
            state.handle.remove_listener(state.listener)
            state.handle.release()
        self.tab_states.clear()
        if self._server:
            self._server.stop()
            self._server = None

    @property
    def socket_path(self) -> Optional[str]:
        return self._server.socket_path if self._server else None

    # -- tab discovery --

    def _enumerate_terminals(self) -> list:
        out = []
        if not self._window:
            return out
        tabs = getattr(self._window, "_tabs", None)
        if tabs is None:
            return out
        for i in range(tabs.count()):
            split = tabs.widget(i)
            for term_widget in split.find_terminals():
                out.append(term_widget)
        return out

    def _get_terminal(self, tab_id: int):
        for term_widget in self._enumerate_terminals():
            if id(term_widget) == tab_id:
                return term_widget
        raise _RpcError(-32004, f"no such tab: {tab_id}")

    # -- internal: subscribe / unsubscribe a single client to a tab --

    def _attach_client_to_tab(self, client_fd: int, tab_id: int):
        state = self.tab_states.get(tab_id)
        if state is None:
            term_widget = self._get_terminal(tab_id)
            handle = self._window.shadow_screens.acquire(term_widget)

            def listener(seq: int, raw: bytes, tid=tab_id):
                if self._server:
                    self._server.broadcast_stream(tid, seq, raw)

            handle.add_listener(listener)
            state = _AttachState(handle, listener)
            self.tab_states[tab_id] = state
        state.subscribers.add(client_fd)

    def _detach_client_from_tab(self, client_fd: int, tab_id: int):
        state = self.tab_states.get(tab_id)
        if state is None:
            return
        state.subscribers.discard(client_fd)
        if not state.subscribers:
            state.handle.remove_listener(state.listener)
            state.handle.release()
            self.tab_states.pop(tab_id, None)

    # -- RPC methods --

    def rpc_list_tabs(self, _client):
        out = []
        for term_widget in self._enumerate_terminals():
            qtw = term_widget.term
            tid = id(term_widget)
            try:
                cols = int(qtw.screenColumnsCount() or 0)
            except Exception:
                cols = 0
            try:
                metrics = qtw.fontMetrics()
                line_h = max(1, metrics.height())
                rows = max(1, qtw.height() // line_h)
            except Exception:
                rows = 0
            tmux_session = None
            tmux_mode = getattr(self._window, "tmux_mode", None)
            if tmux_mode is not None:
                try:
                    tmux_session = tmux_mode.get_session_for_terminal(term_widget)
                except Exception:
                    tmux_session = None
            out.append({
                "id": tid,
                "title": term_widget.title(),
                "shell_pid": int(qtw.getShellPID() or 0),
                "cols": cols,
                "rows": rows,
                "attached": tid in self.tab_states,
                "working_directory": term_widget.working_directory(),
                "tmux_session": tmux_session,
            })
        return out

    def rpc_attach(self, client, tab_id: int):
        # Validate the tab exists before doing any registry work.
        self._get_terminal(tab_id)
        self._attach_client_to_tab(client.fd, tab_id)
        client.attached_tabs.add(tab_id)
        state = self.tab_states[tab_id]
        return {"ok": True, "latest_seq": state.handle.latest_seq}

    def rpc_detach(self, client, tab_id: int):
        if tab_id not in client.attached_tabs:
            raise _RpcError(-32001, "not attached")
        self._detach_client_from_tab(client.fd, tab_id)
        client.attached_tabs.discard(tab_id)
        return {"ok": True}

    def rpc_send_text(self, client, tab_id: int, text: str):
        if tab_id not in client.attached_tabs:
            raise _RpcError(-32001, "not attached")
        term_widget = self._get_terminal(tab_id)
        term_widget.send_text(text)
        return {"ok": True}

    def rpc_send_keys(self, client, tab_id: int, keys: list):
        if tab_id not in client.attached_tabs:
            raise _RpcError(-32001, "not attached")
        term_widget = self._get_terminal(tab_id)
        out = bytearray()
        for k in keys:
            out.extend(_key_to_bytes(k))
        term_widget.send_text(out.decode("utf-8", errors="replace"))
        return {"ok": True}

    def rpc_tail_stream(self, client, tab_id: int, since: int = 0):
        if tab_id not in client.attached_tabs:
            raise _RpcError(-32001, "not attached")
        state = self.tab_states[tab_id]
        latest, raw = state.handle.tail(since)
        return {
            "latest_seq": latest,
            "bytes_b64": base64.b64encode(raw).decode("ascii"),
        }

    def rpc_get_screen(self, client, tab_id: int):
        if tab_id not in client.attached_tabs:
            raise _RpcError(-32001, "not attached")
        state = self.tab_states[tab_id]
        return state.handle.snapshot()

    def rpc_screenshot(self, _client, tab_id: int):
        term_widget = self._get_terminal(tab_id)
        pixmap = term_widget.term.grab()
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        pixmap.save(buf, "PNG")
        buf.close()
        return {
            "width": pixmap.width(),
            "height": pixmap.height(),
            "png_b64": bytes(ba.toBase64()).decode("ascii"),
        }

    def rpc_open_tab(self, _client, working_directory: Optional[str] = None):
        if not self._window:
            raise _RpcError(-32003, "no window")
        self._window.new_tab(working_directory=working_directory)
        terms = self._enumerate_terminals()
        if not terms:
            raise _RpcError(-32003, "open_tab succeeded but no terminals?")
        return {"id": id(terms[-1])}

    def rpc_close_tab(self, _client, tab_id: int):
        term_widget = self._get_terminal(tab_id)
        tabs = self._window._tabs
        for i in range(tabs.count()):
            split = tabs.widget(i)
            if term_widget in split.find_terminals():
                self._window._on_tab_close_requested(i)
                # Drop any state we held on this tab.
                state = self.tab_states.pop(tab_id, None)
                if state:
                    try:
                        state.handle.remove_listener(state.listener)
                    finally:
                        state.handle.release()
                return {"ok": True}
        raise _RpcError(-32004, "tab not found in any tab index")
