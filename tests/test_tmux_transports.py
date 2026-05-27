"""Tests for tmux SSH/web sharing transports."""

import base64
import os
import socket
import struct
import urllib.request

from qterminator.plugins.tmux_ssh_share import TmuxSSHShareService, SSHShare
from qterminator.plugins.tmux_web_share import (
    TmuxWebShareService,
    _ws_accept,
)


def test_tmux_ssh_share_ports_prune_dead_entries(tmp_path):
    class _Proc:
        def poll(self):
            return 1

    svc = TmuxSSHShareService(
        bind="127.0.0.1",
        authorized_keys=str(tmp_path / "authorized_keys"),
    )
    share = SSHShare(
        session="qterm-1",
        bind="127.0.0.1",
        port=22022,
        proc=_Proc(),
        temp_dir=str(tmp_path),
        authorized_keys=str(tmp_path / "authorized_keys"),
    )
    svc._shares["qterm-1"] = [share]
    assert svc.ports_for("qterm-1") == []


def test_tmux_ssh_start_share_writes_forced_tmux_config(monkeypatch, tmp_path):
    captured = {}

    class _Proc:
        stderr = None

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr("qterminator.plugins.tmux_ssh_share.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("qterminator.plugins.tmux_ssh_share._free_tcp_port", lambda _bind: 22222)

    def fake_run(argv, **_kwargs):
        captured["keygen"] = argv

    def fake_popen(argv, **_kwargs):
        captured["sshd"] = argv
        return _Proc()

    monkeypatch.setattr("qterminator.plugins.tmux_ssh_share.subprocess.run", fake_run)
    monkeypatch.setattr("qterminator.plugins.tmux_ssh_share.subprocess.Popen", fake_popen)
    svc = TmuxSSHShareService(
        bind="127.0.0.1",
        authorized_keys=str(tmp_path / "authorized_keys"),
    )
    share = svc.start_share("qterm-1")
    try:
        assert share.port == 22222
        config_path = captured["sshd"][captured["sshd"].index("-f") + 1]
        text = open(config_path, encoding="utf-8").read()
        assert "ForceCommand tmux attach -t qterm-1" in text
        assert f"AuthorizedKeysFile {tmp_path / 'authorized_keys'}" in text
        assert captured["keygen"][0] == "ssh-keygen"
    finally:
        share.stop()


def test_tmux_web_share_serves_browser_page():
    svc = TmuxWebShareService(bind="127.0.0.1", read_only=True)
    share = svc.start_share("qterm-1")
    try:
        with urllib.request.urlopen(share.url, timeout=2) as resp:
            body = resp.read().decode("utf-8")
        assert "QTerminator tmux share" in body
        assert "WebSocket" in body
    finally:
        svc.stop_all()


def _masked_frame(text: str) -> bytes:
    payload = text.encode("utf-8")
    mask = b"abcd"
    header = bytearray([0x81, 0x80 | len(payload)])
    return bytes(header) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def _read_ws_headers(sock: socket.socket) -> tuple[str, bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        data += sock.recv(1024)
    head, rest = data.split(b"\r\n\r\n", 1)
    return head.decode("ascii"), rest


def _recv_ws_text(sock: socket.socket, initial: bytes = b"") -> str:
    buf = bytearray(initial)

    def take(n: int) -> bytes:
        while len(buf) < n:
            buf.extend(sock.recv(n - len(buf)))
        out = bytes(buf[:n])
        del buf[:n]
        return out

    first = take(2)
    length = first[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", take(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", take(8))[0]
    return take(length).decode("utf-8")



def test_websocket_roundtrip_sends_input_to_tmux(monkeypatch):
    sent = []
    monkeypatch.setattr("qterminator.plugins.tmux_web_share._capture", lambda session: f"{session}:SCREEN")
    monkeypatch.setattr("qterminator.plugins.tmux_web_share._send_text", lambda session, text: sent.append((session, text)))

    svc = TmuxWebShareService(bind="127.0.0.1", read_only=False)
    share = svc.start_share("qterm-1")
    try:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        sock = socket.create_connection(("127.0.0.1", share.port), timeout=2)
        sock.settimeout(2)
        try:
            req = (
                "GET /ws HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{share.port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            )
            sock.sendall(req.encode("ascii"))
            headers, rest = _read_ws_headers(sock)
            assert "101 Switching Protocols" in headers
            assert _ws_accept(key) in headers
            assert _recv_ws_text(sock, rest) == "qterm-1:SCREEN"
            sock.sendall(_masked_frame("x"))
            for _ in range(20):
                if sent:
                    break
                sock.settimeout(0.1)
                try:
                    _recv_ws_text(sock)
                except TimeoutError:
                    pass
            assert sent == [("qterm-1", "x")]
        finally:
            sock.close()
    finally:
        svc.stop_all()


def test_websocket_read_only_drops_input(monkeypatch):
    sent = []
    monkeypatch.setattr("qterminator.plugins.tmux_web_share._capture", lambda _session: "SCREEN")
    monkeypatch.setattr("qterminator.plugins.tmux_web_share._send_text", lambda session, text: sent.append((session, text)))
    svc = TmuxWebShareService(bind="127.0.0.1", read_only=True)
    share = svc.start_share("qterm-1")
    try:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        sock = socket.create_connection(("127.0.0.1", share.port), timeout=2)
        sock.settimeout(2)
        try:
            sock.sendall((
                "GET /ws HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{share.port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode("ascii"))
            headers, rest = _read_ws_headers(sock)
            assert "101 Switching Protocols" in headers
            _recv_ws_text(sock, rest)
            sock.sendall(_masked_frame("x"))
            sock.settimeout(0.2)
            try:
                _recv_ws_text(sock)
            except TimeoutError:
                pass
            assert sent == []
        finally:
            sock.close()
    finally:
        svc.stop_all()


# ---------------------------------------------------------------------------
# SSHShare.connect_string
# ---------------------------------------------------------------------------

def test_ssh_share_connect_string_format(tmp_path):
    import getpass
    from qterminator.plugins.tmux_ssh_share import SSHShare

    class _Proc:
        def poll(self):
            return None

    share = SSHShare(
        session="qterm-1", bind="10.0.0.5", port=22022,
        proc=_Proc(), temp_dir=str(tmp_path),
        authorized_keys=str(tmp_path / "ak"),
    )
    s = share.connect_string
    assert f"ssh -p 22022 {getpass.getuser()}@10.0.0.5" == s


def test_ssh_share_connect_string_with_0000_bind(tmp_path):
    """0.0.0.0 bind should map to 127.0.0.1 in connect string."""
    import getpass

    class _Proc:
        def poll(self):
            return None

    share = SSHShare(
        session="qterm-1", bind="0.0.0.0", port=22022,
        proc=_Proc(), temp_dir=str(tmp_path),
        authorized_keys=str(tmp_path / "ak"),
    )
    s = share.connect_string
    assert "127.0.0.1" in s
    assert "0.0.0.0" not in s
    assert f"{getpass.getuser()}@127.0.0.1" in s


# ---------------------------------------------------------------------------
# SSHShare.is_alive with dead process
# ---------------------------------------------------------------------------

def test_ssh_share_is_alive_with_dead_process(tmp_path):
    class _DeadProc:
        def poll(self):
            return 1

    share = SSHShare(
        session="qterm-1", bind="127.0.0.1", port=22022,
        proc=_DeadProc(), temp_dir=str(tmp_path),
        authorized_keys=str(tmp_path / "ak"),
    )
    assert share.is_alive() is False


def test_ssh_share_is_alive_with_running_process(tmp_path):
    class _LiveProc:
        def poll(self):
            return None

    share = SSHShare(
        session="qterm-1", bind="127.0.0.1", port=22022,
        proc=_LiveProc(), temp_dir=str(tmp_path),
        authorized_keys=str(tmp_path / "ak"),
    )
    assert share.is_alive() is True


# ---------------------------------------------------------------------------
# SSHShare.stop cleans up temp_dir
# ---------------------------------------------------------------------------

def test_ssh_share_stop_cleans_up_temp_dir(tmp_path):
    td = tmp_path / "sshd-temp"
    td.mkdir()
    (td / "host_key").write_text("fake")

    class _Proc:
        def poll(self):
            return None

        def terminate(self):
            self._terminated = True

        def wait(self, timeout=None):
            return 0

    share = SSHShare(
        session="qterm-1", bind="127.0.0.1", port=22022,
        proc=_Proc(), temp_dir=str(td),
        authorized_keys=str(tmp_path / "ak"),
    )
    share.stop()
    assert not td.exists()


# ---------------------------------------------------------------------------
# TmuxSSHShareService.available when tools missing
# ---------------------------------------------------------------------------

def test_ssh_service_available_when_tools_missing(monkeypatch):
    monkeypatch.setattr("qterminator.plugins.tmux_ssh_share.shutil.which", lambda name: None)
    svc = TmuxSSHShareService(bind="127.0.0.1")
    assert svc.available() is False


def test_ssh_service_available_when_sshd_only(monkeypatch):
    monkeypatch.setattr(
        "qterminator.plugins.tmux_ssh_share.shutil.which",
        lambda name: "/usr/sbin/sshd" if name == "sshd" else None,
    )
    svc = TmuxSSHShareService(bind="127.0.0.1")
    assert svc.available() is False


# ---------------------------------------------------------------------------
# TmuxSSHShareService.stop_all stops all sessions
# ---------------------------------------------------------------------------

def test_ssh_service_stop_all(tmp_path):
    stopped = []

    class _Proc:
        def poll(self):
            return None

        def terminate(self):
            stopped.append(True)

        def wait(self, timeout=None):
            return 0

    svc = TmuxSSHShareService(bind="127.0.0.1")
    td1 = tmp_path / "td1"
    td1.mkdir()
    td2 = tmp_path / "td2"
    td2.mkdir()
    s1 = SSHShare(
        session="qterm-1", bind="127.0.0.1", port=22022,
        proc=_Proc(), temp_dir=str(td1),
        authorized_keys=str(tmp_path / "ak"),
    )
    s2 = SSHShare(
        session="qterm-2", bind="127.0.0.1", port=22023,
        proc=_Proc(), temp_dir=str(td2),
        authorized_keys=str(tmp_path / "ak"),
    )
    svc._shares["qterm-1"] = [s1]
    svc._shares["qterm-2"] = [s2]
    svc.stop_all()
    assert len(stopped) == 2
    assert svc._shares == {}


# ---------------------------------------------------------------------------
# TmuxSSHShareService.shares_for prunes multiple dead entries
# ---------------------------------------------------------------------------

def test_ssh_shares_for_prunes_multiple_dead(tmp_path):
    class _DeadProc:
        def poll(self):
            return 1

    svc = TmuxSSHShareService(bind="127.0.0.1")
    shares = []
    for i in range(3):
        s = SSHShare(
            session="qterm-1", bind="127.0.0.1", port=22022 + i,
            proc=_DeadProc(), temp_dir=str(tmp_path / f"td{i}"),
            authorized_keys=str(tmp_path / "ak"),
        )
        shares.append(s)
    svc._shares["qterm-1"] = shares
    live = svc.shares_for("qterm-1")
    assert live == []
    assert "qterm-1" not in svc._shares


# ---------------------------------------------------------------------------
# WebShare.url format
# ---------------------------------------------------------------------------

def test_web_share_url_format():
    from qterminator.plugins.tmux_web_share import WebShare
    share = WebShare(
        session="qterm-1", bind="10.0.0.5", port=8080,
        read_only=False, server=None, thread=None,
    )
    assert share.url == "http://10.0.0.5:8080/"


def test_web_share_url_with_0000_bind():
    from qterminator.plugins.tmux_web_share import WebShare
    share = WebShare(
        session="qterm-1", bind="0.0.0.0", port=9090,
        read_only=True, server=None, thread=None,
    )
    assert share.url == "http://127.0.0.1:9090/"
    assert "0.0.0.0" not in share.url


# ---------------------------------------------------------------------------
# TmuxWebShareService.shares_for prunes stopped entries
# ---------------------------------------------------------------------------

def test_web_shares_for_prunes_stopped():
    from qterminator.plugins.tmux_web_share import TmuxWebShareService, WebShare
    svc = TmuxWebShareService(bind="127.0.0.1")
    alive = WebShare(
        session="qterm-1", bind="127.0.0.1", port=8080,
        read_only=False, server=None, thread=None, stopped=False,
    )
    dead = WebShare(
        session="qterm-1", bind="127.0.0.1", port=8081,
        read_only=False, server=None, thread=None, stopped=True,
    )
    svc._shares["qterm-1"] = [alive, dead]
    live = svc.shares_for("qterm-1")
    assert len(live) == 1
    assert live[0].port == 8080


# ---------------------------------------------------------------------------
# TmuxWebShareService.stop_all stops all sessions
# ---------------------------------------------------------------------------

def test_web_service_stop_all():
    svc = TmuxWebShareService(bind="127.0.0.1")
    s1 = svc.start_share("qterm-1")
    s2 = svc.start_share("qterm-2")
    assert not s1.stopped
    assert not s2.stopped
    svc.stop_all()
    assert s1.stopped
    assert s2.stopped
    assert svc._shares == {}


# ---------------------------------------------------------------------------
# _ws_accept produces correct hash
# ---------------------------------------------------------------------------

def test_ws_accept_known_value():
    """RFC 6455 example: known key produces known accept value."""
    import hashlib
    key = "dGhlIHNhbXBsZSBub25jZQ=="
    magic = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    expected = base64.b64encode(
        hashlib.sha1((key + magic).encode("ascii")).digest()
    ).decode("ascii")
    assert _ws_accept(key) == expected
    assert _ws_accept(key) == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


# ---------------------------------------------------------------------------
# _free_tcp_port returns a usable port
# ---------------------------------------------------------------------------

def test_free_tcp_port_returns_usable_port():
    from qterminator.plugins.tmux_web_share import _free_tcp_port
    port = _free_tcp_port("127.0.0.1")
    assert isinstance(port, int)
    assert 1024 <= port <= 65535
    # Verify we can actually bind to it (ephemeral port race is possible
    # but very unlikely in a test).
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))


# ---------------------------------------------------------------------------
# Multiple web shares for same session
# ---------------------------------------------------------------------------

def test_multiple_web_shares_for_same_session():
    svc = TmuxWebShareService(bind="127.0.0.1")
    s1 = svc.start_share("qterm-1")
    s2 = svc.start_share("qterm-1")
    try:
        assert s1.port != s2.port
        live = svc.shares_for("qterm-1")
        assert len(live) == 2
        ports = svc.ports_for("qterm-1")
        assert s1.port in ports
        assert s2.port in ports
    finally:
        svc.stop_all()


# ---------------------------------------------------------------------------
# Web share HTML page contains expected elements
# ---------------------------------------------------------------------------

def test_web_share_html_page_content():
    svc = TmuxWebShareService(bind="127.0.0.1", read_only=True)
    share = svc.start_share("qterm-1")
    try:
        with urllib.request.urlopen(share.url, timeout=2) as resp:
            body = resp.read().decode("utf-8")
        assert "<!doctype html>" in body.lower() or "<!DOCTYPE html>" in body
        assert "<pre id=\"term\"" in body
        assert "WebSocket" in body
        assert "keydown" in body
        assert "QTerminator tmux share" in body
    finally:
        svc.stop_all()


# ---------------------------------------------------------------------------
# _WebHandler returns 200 for GET /
# ---------------------------------------------------------------------------

def test_web_handler_returns_200_for_get():
    svc = TmuxWebShareService(bind="127.0.0.1", read_only=True)
    share = svc.start_share("qterm-1")
    try:
        with urllib.request.urlopen(share.url, timeout=2) as resp:
            assert resp.status == 200
            content_type = resp.headers.get("Content-Type", "")
            assert "text/html" in content_type
    finally:
        svc.stop_all()


# ---------------------------------------------------------------------------
# Security: non-loopback bind forces read_only
# ---------------------------------------------------------------------------

def test_web_share_public_bind_forces_read_only():
    """Non-loopback bind forces read_only=True regardless of constructor arg."""
    svc = TmuxWebShareService(bind="0.0.0.0", read_only=False)
    assert svc.read_only is True


def test_web_share_loopback_allows_read_write():
    """Loopback bind permits read_only=False."""
    svc = TmuxWebShareService(bind="127.0.0.1", read_only=False)
    assert svc.read_only is False


def test_web_share_default_is_read_only():
    """Default read_only is True."""
    svc = TmuxWebShareService(bind="127.0.0.1")
    assert svc.read_only is True


# ---------------------------------------------------------------------------
# Security: SSH session name validation
# ---------------------------------------------------------------------------

def test_ssh_start_share_rejects_unsafe_session_name(monkeypatch, tmp_path):
    """Session names with shell metacharacters are rejected."""
    monkeypatch.setattr(
        "qterminator.plugins.tmux_ssh_share.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    svc = TmuxSSHShareService(
        bind="127.0.0.1",
        authorized_keys=str(tmp_path / "authorized_keys"),
    )
    import pytest as _pytest
    with _pytest.raises(ValueError, match="unsafe characters"):
        svc.start_share("qterm-1; rm -rf /")
    with _pytest.raises(ValueError, match="unsafe characters"):
        svc.start_share("session\nnewline")
    with _pytest.raises(ValueError, match="unsafe characters"):
        svc.start_share("session with spaces")


def test_ssh_start_share_accepts_safe_session_names(tmp_path):
    """Clean session names pass validation."""
    svc = TmuxSSHShareService(
        bind="127.0.0.1",
        authorized_keys=str(tmp_path / "authorized_keys"),
    )
    for name in ["qterm-1", "my_session.2", "Test-Session_3"]:
        assert svc._SAFE_SESSION.match(name), f"{name} should be valid"


# ---------------------------------------------------------------------------
# WebSocket: opcode handling
# ---------------------------------------------------------------------------

def test_ws_recv_text_handles_close_opcode():
    """Close frame (opcode 8) returns None."""
    from qterminator.plugins.tmux_web_share import _ws_recv_text
    import io

    frame = bytes([0x88, 0x00])

    class _FakeSock:
        def __init__(self, data):
            self._buf = io.BytesIO(data)
        def recv(self, n):
            return self._buf.read(n)

    assert _ws_recv_text(_FakeSock(frame)) is None


def test_ws_recv_text_handles_ping_opcode():
    """Ping frame (opcode 9) returns empty string (not None)."""
    from qterminator.plugins.tmux_web_share import _ws_recv_text

    frame = bytes([0x89, 0x00])

    class _FakeSock:
        def __init__(self, data):
            import io
            self._buf = io.BytesIO(data)
        def recv(self, n):
            return self._buf.read(n)

    assert _ws_recv_text(_FakeSock(frame)) == ""
