"""Wire QTerminator into the qdistro App1 launcher contract.

Imported once from ``qterminator.__main__`` after the main window is
built. The receiver claims ``org.qdistro.QTerminator.uid<NNNN>`` on
the session bus so PodApps' broker-driven scan sees the running app
and the "Send To… → QTerminator" entry in peer apps' menus works.

``ReceivePayload`` / ``Receive`` dispatch onto the active terminal as
typed text via ``MainWindow.send_text_to_active``; that lets a peer
app drop a snippet straight into the user's shell. If no terminal is
open we drop the payload with a status-bar message rather than
silently losing it.

This module degrades gracefully when ``dbus-python`` is missing or
the session bus isn't reachable (e.g. CI runs with no logind seat);
``maybe_install`` returns ``None`` and the rest of the app behaves
identically to a pre-P03 build.
"""
from __future__ import annotations

import os
import sys

from PyQt6.QtCore import QTimer

try:  # pragma: no cover — exercised in VM, not host unit tests
    from qdistro_app import app_receiver as _app_receiver
except ImportError:
    _app_receiver = None  # type: ignore[assignment]


APP_FRIENDLY_NAME = "QTerminator"
APP_SUPPORTED_KINDS = ("text/*", "application/octet-stream")


def maybe_install(window) -> object | None:
    """Register the running QTerminator on the session bus.

    Returns the AppReceiver (caller must keep a reference for the
    process lifetime to hold the bus name) or ``None`` when the SDK
    isn't importable / the bus isn't reachable.
    """
    if _app_receiver is None:
        print("[qterminator/qdistro] qdistro_app SDK not importable; "
              "App1 registration skipped",
              file=sys.stderr, flush=True)
        return None

    def on_receive(kind: str, payload: str) -> None:
        # The D-Bus dispatcher runs on its own thread; bounce to Qt's
        # main thread before touching widgets.
        QTimer.singleShot(0, lambda: _deliver_to_active_terminal(window, kind, payload))

    receiver = _app_receiver.register_app(
        APP_FRIENDLY_NAME,
        on_receive=on_receive,
        friendly_name=APP_FRIENDLY_NAME,
        supported_kinds=APP_SUPPORTED_KINDS,
    )
    if receiver is None:
        return None
    print(f"[qterminator/qdistro] App1 receiver registered as "
          f"{receiver.service_name} (silo={receiver.silo!r})",
          flush=True)
    return receiver


def _deliver_to_active_terminal(window, kind: str, payload: str) -> None:
    """Append received payload as typed text to the active terminal.

    Best-effort: if the window doesn't expose a usable target we
    surface the drop via the status bar rather than raising into
    dbus.service.Object.Receive (which would log a noisy back-trace
    in the broker's audit thread).
    """
    try:
        term = getattr(window, "_active_terminal", None)
        if term is not None and hasattr(term, "send_text"):
            text = payload if payload.endswith("\n") else payload + "\n"
            term.send_text(text)
            return
        bar = window.statusBar() if hasattr(window, "statusBar") else None
        if bar is not None:
            bar.showMessage(f"qdistro: dropped {kind} payload "
                            f"({len(payload)} bytes) — no active terminal", 4000)
    except Exception as e:  # noqa: BLE001
        print(f"[qterminator/qdistro] deliver failed: {e}",
              file=sys.stderr, flush=True)


def send_to_targets(*, kind: str = "text/plain") -> list[dict]:
    """Build the Send-To menu rows from the broker. Returns ``[]``
    when the SDK isn't importable, which lets the menu code degrade
    to a single "(no targets)" disabled entry without crashing."""
    if _app_receiver is None:
        return []
    try:
        self_service = f"org.qdistro.{APP_FRIENDLY_NAME}.uid{os.geteuid()}"
        return _app_receiver.send_to_menu_targets(
            self_service=self_service, kind=kind)
    except Exception as e:  # noqa: BLE001
        print(f"[qterminator/qdistro] send_to_menu_targets failed: {e}",
              file=sys.stderr, flush=True)
        return []


def send_payload(target_uid: int, target_service: str, payload: str, *,
                 kind: str = "text/plain") -> bool:
    """Send the given payload via the broker (admin-gated for
    cross-silo; same-silo bypass is honoured server-side).

    Returns True on delivery, False on any failure (including
    admin denial). Caller surfaces via status bar / toast."""
    if _app_receiver is None:
        return False
    try:
        return bool(_app_receiver.send_to(int(target_uid),
                                          str(target_service),
                                          str(kind), str(payload)))
    except Exception as e:  # noqa: BLE001
        print(f"[qterminator/qdistro] send_to({target_service}) failed: {e}",
              file=sys.stderr, flush=True)
        return False
