"""Browser/WebSocket sharing for tmux sessions."""

from __future__ import annotations

import base64
import hashlib
import os
import select
import socket
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from PyQt6.QtWidgets import QMessageBox

from qterminator.config import Config
from qterminator.plugin import MenuProvider


_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>QTerminator tmux share</title>
<style>
body { margin: 0; background: #111; color: #ddd; font: 14px/1.25 monospace; }
#term { white-space: pre; padding: 12px; outline: none; min-height: 100vh; }
</style>
</head>
<body><pre id="term" tabindex="0"></pre>
<script>
const term = document.getElementById("term");
const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
ws.onmessage = (ev) => { term.textContent = ev.data; };
term.focus();
term.addEventListener("keydown", (ev) => {
  const special = {Enter:"\\r", Backspace:"\\x7f", Tab:"\\t",
    ArrowUp:"\\x1b[A", ArrowDown:"\\x1b[B", ArrowRight:"\\x1b[C", ArrowLeft:"\\x1b[D"};
  let text = special[ev.key] || (ev.key.length === 1 ? ev.key : "");
  if (ev.ctrlKey && ev.key.length === 1) text = String.fromCharCode(ev.key.toLowerCase().charCodeAt(0) - 96);
  if (text) { ws.send(text); ev.preventDefault(); }
});
</script></body></html>
"""


def _free_tcp_port(bind: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((bind, 0))
        return int(sock.getsockname()[1])


def _ws_accept(key: str) -> str:
    magic = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    raw = hashlib.sha1((key + magic).encode("ascii")).digest()
    return base64.b64encode(raw).decode("ascii")


def _ws_send_text(sock: socket.socket, text: str):
    data = text.encode("utf-8", errors="replace")
    header = bytearray([0x81])
    if len(data) < 126:
        header.append(len(data))
    elif len(data) < 65536:
        header.append(126)
        header.extend(struct.pack("!H", len(data)))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", len(data)))
    sock.sendall(bytes(header) + data)


_WS_MAX_PAYLOAD = 1_048_576  # 1 MiB — reject frames larger than this


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly *n* bytes, blocking until complete or EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("connection closed during recv_exact")
        buf.extend(chunk)
    return bytes(buf)


def _ws_recv_text(sock: socket.socket) -> Optional[str]:
    first = _recv_exact(sock, 2)
    opcode = first[0] & 0x0F
    if opcode == 0x8:  # close
        return None
    if opcode == 0x9:  # ping → send pong
        return ""
    if opcode not in (0x1, 0x0):  # only text and continuation
        return ""
    length = first[1] & 0x7F
    masked = bool(first[1] & 0x80)
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    if length > _WS_MAX_PAYLOAD:
        return None
    mask = _recv_exact(sock, 4) if masked else b""
    payload = _recv_exact(sock, length) if length else b""
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return payload.decode("utf-8", errors="replace")


def _capture(session: str) -> str:
    proc = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", session],
        capture_output=True,
        text=True,
        timeout=1,
    )
    if proc.returncode != 0:
        return proc.stderr.strip()
    return proc.stdout.rstrip("\n")


def _send_text(session: str, text: str):
    if text:
        subprocess.run(
            ["tmux", "send-keys", "-t", session, "-l", text],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1,
        )


class _WebHandler(BaseHTTPRequestHandler):
    server_version = "QTerminatorTmuxWeb/0.1"

    def log_message(self, _fmt, *_args):
        return

    def do_GET(self):
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self._websocket()
            return
        body = _HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _websocket(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self.send_error(400)
            return
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", _ws_accept(key))
        self.end_headers()
        sock = self.connection
        sock.setblocking(True)
        session = self.server.share.session
        read_only = self.server.share.read_only
        while not self.server.share.stopped:
            try:
                _ws_send_text(sock, _capture(session))
            except OSError:
                return
            ready, _, _ = select.select([sock], [], [], 0.25)
            if ready:
                try:
                    sock.settimeout(2.0)
                    text = _ws_recv_text(sock)
                    sock.settimeout(None)
                except OSError:
                    return
                if text is None:
                    return
                if text and not read_only:
                    _send_text(session, text)


@dataclass
class WebShare:
    session: str
    bind: str
    port: int
    read_only: bool
    server: ThreadingHTTPServer
    thread: threading.Thread
    stopped: bool = False

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.bind == "0.0.0.0" else self.bind
        return f"http://{host}:{self.port}/"

    def stop(self):
        self.stopped = True
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class _ShareHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler, share):
        super().__init__(addr, handler)
        self.share = share


_LOOPBACK_BINDS = frozenset({"127.0.0.1", "::1", "localhost"})


class TmuxWebShareService:
    def __init__(self, bind: str = "127.0.0.1", read_only: bool = True):
        self.bind = bind
        if bind not in _LOOPBACK_BINDS:
            read_only = True
        self.read_only = read_only
        self._shares: dict[str, list[WebShare]] = {}

    def start_share(self, session: str, port: int = 0) -> WebShare:
        port = port or _free_tcp_port(self.bind)
        placeholder = type("_PendingShare", (), {
            "session": session,
            "bind": self.bind,
            "port": port,
            "read_only": self.read_only,
            "stopped": False,
        })()
        server = _ShareHTTPServer((self.bind, port), _WebHandler, placeholder)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        share = WebShare(
            session=session,
            bind=self.bind,
            port=port,
            read_only=self.read_only,
            server=server,
            thread=thread,
        )
        server.share = share
        thread.start()
        self._shares.setdefault(session, []).append(share)
        return share

    def shares_for(self, session: str) -> list[WebShare]:
        live = [s for s in self._shares.get(session, []) if not s.stopped]
        if live:
            self._shares[session] = live
        else:
            self._shares.pop(session, None)
        return live

    def ports_for(self, session: str) -> list[int]:
        return [s.port for s in self.shares_for(session)]

    def stop_all_for(self, session: str):
        for share in self._shares.get(session, []):
            share.stop()
        self._shares.pop(session, None)

    def stop_all(self):
        for session in list(self._shares):
            self.stop_all_for(session)


class TmuxWebSharePlugin(MenuProvider):
    name = "tmux_web_share"
    description = "Share tmux sessions in a browser"
    version = "0.1"
    category = "Workspace"
    capabilities = ["menu_provider", "tmux_web_share"]

    def __init__(self):
        super().__init__()
        self._window = None
        self._service: Optional[TmuxWebShareService] = None

    def activate(self, app_controller):
        self._window = app_controller
        cfg = Config()
        bind = cfg.get("plugins", "tmux_web_share", "bind", default="127.0.0.1")
        read_only = bool(cfg.get(
            "plugins", "tmux_web_share", "read_only", default=True,
        ))
        self._service = TmuxWebShareService(bind=bind, read_only=read_only)
        if not hasattr(app_controller, "tmux_web_share"):
            app_controller.tmux_web_share = self._service

    def deactivate(self):
        if self._service is not None:
            self._service.stop_all()
        if (self._window is not None
                and getattr(self._window, "tmux_web_share", None) is self._service):
            del self._window.tmux_web_share
        self._service = None
        self._window = None

    def get_menu_items(self, terminal):
        tmux_mode = getattr(self._window, "tmux_mode", None)
        if self._service is None or tmux_mode is None:
            return []
        session = tmux_mode.get_session_for_terminal(terminal)
        if not session:
            return []
        items = [(f"Share in Browser... ({session})", lambda s=session: self._start(s))]
        ports = self._service.ports_for(session)
        if ports:
            items.append((
                f"  Stop browser sharing ({len(ports)} active)",
                lambda s=session: self._service.stop_all_for(s),
            ))
        return items

    def _start(self, session: str):
        try:
            share = self._service.start_share(session)
        except Exception as e:
            QMessageBox.critical(self._window, "Browser share failed", str(e))
            return
        QMessageBox.information(
            self._window,
            "Browser share started",
            share.url,
        )
