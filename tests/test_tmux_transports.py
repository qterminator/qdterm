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
