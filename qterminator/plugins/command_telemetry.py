"""command_telemetry — duration / CPU / memory per command.

Annotates every completed shell command with telemetry: wall-clock
duration, total CPU seconds, peak resident memory across the process
tree, and process count. Surfaces are:

* ``CommandRecord.telemetry`` on shell_integration's history dict (so
  agent_control's ``list_tabs[].last_command.telemetry`` carries it
  for free, and the MCP ``command_history`` tool exposes it).
* An optional ``[12.3s]`` suffix on the tab title (configurable via
  ``display = "tab_status"``), a dim grey inline line after command
  output (``display = "inline"``), or no UI at all (``display = "off"``).
* An optional JSONL append-log under ``~/.local/share/qterminator/``
  (configurable via ``log_path``).
* A push event broadcast through ``agent_control.broadcast_event``
  with ``event_type == "command_finished"`` whenever ``agent_control``
  is loaded — so attached agents are notified without polling.
* An ``rpc_command_telemetry`` method on agent_control returning the
  last N annotated command records for a tab.

Configuration (config.toml):

    [plugins.command_telemetry]
    enabled = false                    # default false; opt-in
    poll_interval_ms = 100             # /proc sampling cadence
    display = "tab_status"             # "tab_status", "inline", or "off"
    log_path = ""                      # empty/null = no JSONL log

SECURITY NOTE: the JSONL log captures ``rec.text`` — the shell command
line — when shell_integration's ``capture_command_text`` is also on.
That includes anything typed at the prompt: ``mysql -p secret``,
``export API_KEY=...``, etc. The log is opt-in (``log_path`` is empty
by default) and is written with ``0o600`` permissions, but anyone
enabling it is implicitly accepting credential persistence. Mirror the
warning from ``qterminator/layout.py`` if you're enabling both this
and ``capture_command_text``.

Requires the ``shell_integration`` plugin (OSC 133 ;C/;D).
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import time
from dataclasses import dataclass
from typing import Optional

from PyQt6.QtCore import QTimer

from qterminator.config import Config
from qterminator.plugin import Plugin


log = logging.getLogger("qterminator.command_telemetry")

#: Valid display modes.
VALID_DISPLAY_MODES = ("tab_status", "inline", "off")


# ---------------------------------------------------------------------------
# Telemetry data
# ---------------------------------------------------------------------------

@dataclass
class CommandTelemetry:
    """Telemetry data for a single command."""
    duration: float = 0.0
    cpu_seconds: float = 0.0
    peak_rss_bytes: int = 0
    process_count: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "duration": round(self.duration, 3),
            "cpu_seconds": round(self.cpu_seconds, 3),
            "peak_rss_bytes": self.peak_rss_bytes,
            "process_count": self.process_count,
        }

    def format_short(self) -> str:
        """Human-readable short form for the tab title."""
        parts = [f"{self.duration:.1f}s"]
        if self.peak_rss_bytes > 0:
            mb = self.peak_rss_bytes / (1024 * 1024)
            parts.append(f"{mb:.0f}MB")
        if self.cpu_seconds > 0:
            parts.append(f"{self.cpu_seconds:.1f}s CPU")
        return " · ".join(parts)


# ---------------------------------------------------------------------------
# Process tree introspection (/proc, Linux-only)
# ---------------------------------------------------------------------------

_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


class ProcTreeSampler:
    """Walk /proc to collect CPU time and RSS for a process tree.

    Encapsulates all /proc parsing logic with a configurable ``proc_root``
    for testability. Instantiate with a custom path to point at a fake
    /proc tree in tests.

    The ``proc_root`` parameter (default ``"/proc"``) allows tests to
    point the sampler at a fake filesystem tree.
    """

    def __init__(self, proc_root: str = "/proc"):
        self.proc_root = proc_root

    def read_children(self, pid: int) -> list[int]:
        """Return immediate child PIDs of ``pid``.

        Prefers ``/proc/<pid>/task/<pid>/children`` (cheap; one read).
        Falls back to scanning all ``/proc/*/stat`` entries for matching
        ppid (slower but works on kernels without ``CONFIG_PROC_CHILDREN``).
        """
        try:
            path = f"{self.proc_root}/{pid}/task/{pid}/children"
            with open(path) as f:
                line = f.read().strip()
            if line:
                return [int(c) for c in line.split() if c.isdigit()]
            return []
        except (FileNotFoundError, PermissionError, ValueError, OSError):
            pass
        out: list[int] = []
        try:
            entries = os.listdir(self.proc_root)
        except OSError:
            return out
        for entry in entries:
            if not entry.isdigit():
                continue
            try:
                with open(f"{self.proc_root}/{entry}/stat") as f:
                    line = f.read()
            except (FileNotFoundError, PermissionError, OSError):
                continue
            close = line.rfind(")")
            if close == -1:
                continue
            fields = line[close + 2:].split()
            if len(fields) < 2:
                continue
            try:
                ppid = int(fields[1])
            except ValueError:
                continue
            if ppid == pid:
                try:
                    out.append(int(entry))
                except ValueError:
                    pass
        return out

    def walk_tree(self, root_pid: int) -> list[int]:
        """BFS over the process tree rooted at ``root_pid``."""
        seen = {root_pid}
        frontier = [root_pid]
        while frontier:
            next_frontier: list[int] = []
            for pid in frontier:
                for child in self.read_children(pid):
                    if child not in seen:
                        seen.add(child)
                        next_frontier.append(child)
            frontier = next_frontier
        return list(seen)

    def read_cpu_seconds(self, pid: int) -> float:
        """utime + stime for a single PID, in seconds. 0 on any failure."""
        try:
            with open(f"{self.proc_root}/{pid}/stat") as f:
                line = f.read()
        except (FileNotFoundError, PermissionError, OSError):
            return 0.0
        close = line.rfind(")")
        if close == -1:
            return 0.0
        fields = line[close + 2:].split()
        try:
            utime = int(fields[11])
            stime = int(fields[12])
        except (IndexError, ValueError):
            return 0.0
        return (utime + stime) / _CLK_TCK

    def read_rss_bytes(self, pid: int) -> int:
        """VmRSS for a single PID, in bytes. 0 on any failure."""
        try:
            with open(f"{self.proc_root}/{pid}/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        parts = line.split()
                        if len(parts) >= 2:
                            try:
                                return int(parts[1]) * 1024
                            except ValueError:
                                return 0
                        return 0
        except (FileNotFoundError, PermissionError, OSError):
            pass
        return 0

    def sample(self, root_pid: int) -> dict:
        """One-shot snapshot of the process tree rooted at ``root_pid``.

        Returns ``{"cpu_seconds", "peak_rss_bytes", "process_count"}``.
        ``peak_rss_bytes`` is the SUM across the tree at this instant
        (the caller maintains running max across samples).
        """
        if root_pid <= 0:
            return {"cpu_seconds": 0.0, "peak_rss_bytes": 0, "process_count": 0}
        pids = self.walk_tree(root_pid)
        total_cpu = 0.0
        total_rss = 0
        counted = 0
        for pid in pids:
            cpu = self.read_cpu_seconds(pid)
            total_cpu += cpu
            rss = self.read_rss_bytes(pid)
            if rss > 0:
                total_rss += rss
                counted += 1
        if counted == 0:
            counted = 1 if self.read_rss_bytes(root_pid) > 0 else len(pids)
        return {
            "cpu_seconds": total_cpu,
            "peak_rss_bytes": total_rss,
            "process_count": max(counted, len(pids)),
        }


# Default sampler instance pointing at the real /proc.
_default_sampler = ProcTreeSampler()


# Module-level convenience wrappers (backward compat + used by _TabTracker).
def _read_children(pid: int) -> list[int]:
    return _default_sampler.read_children(pid)


def _walk_proc_tree(root_pid: int) -> list[int]:
    return _default_sampler.walk_tree(root_pid)


def _read_cpu_seconds(pid: int) -> float:
    return _default_sampler.read_cpu_seconds(pid)


def _read_rss_bytes(pid: int) -> int:
    return _default_sampler.read_rss_bytes(pid)


def sample_tree(root_pid: int) -> dict:
    return _default_sampler.sample(root_pid)


# ---------------------------------------------------------------------------
# Per-tab tracker
# ---------------------------------------------------------------------------

class _TabTracker:
    """Per-terminal in-flight telemetry. One instance per tab; reused
    across commands. Holds a QTimer only while a command is running."""

    def __init__(self, poll_interval_ms: int = 100):
        self._poll_interval_ms = poll_interval_ms
        self._timer: Optional[QTimer] = None
        self._reset()
        # Most recent finalized telemetry — exposed via
        # ``CommandTelemetryService.get_last_telemetry``.
        self.last_telemetry: Optional[CommandTelemetry] = None

    def _reset(self) -> None:
        self._started_at: Optional[float] = None
        self._started_monotonic: Optional[float] = None
        self._root_pid: int = 0
        self._cpu_last: float = 0.0
        self._peak_rss: int = 0
        self._peak_count: int = 0

    def on_start(self, terminal, started_at: float,
                 started_at_monotonic: float) -> None:
        """Called from the ;C subscription."""
        self.stop_timer()
        self._reset()
        self._started_at = started_at
        self._started_monotonic = started_at_monotonic
        try:
            self._root_pid = int(terminal.foreground_pid() or 0)
        except Exception:
            self._root_pid = 0
        # Take one immediate sample so even sub-poll-interval commands
        # produce non-zero counts.
        self._sample()
        if self._root_pid > 0:
            self._timer = QTimer()
            self._timer.setSingleShot(False)
            self._timer.timeout.connect(self._sample)
            self._timer.start(self._poll_interval_ms)

    def _sample(self) -> None:
        if self._root_pid <= 0:
            return
        snap = sample_tree(self._root_pid)
        self._cpu_last = max(self._cpu_last, snap["cpu_seconds"])
        if snap["peak_rss_bytes"] > self._peak_rss:
            self._peak_rss = snap["peak_rss_bytes"]
        if snap["process_count"] > self._peak_count:
            self._peak_count = snap["process_count"]

    def stop_timer(self) -> None:
        if self._timer is not None:
            try:
                self._timer.stop()
            except RuntimeError:
                pass
            self._timer = None

    def on_finish(self) -> Optional[CommandTelemetry]:
        """Called from the ;D subscription. Returns the finalized
        telemetry (also cached on ``last_telemetry``) or None if we
        never saw a matching ;C."""
        if self._started_at is None:
            self.stop_timer()
            return None
        # Final sample.
        self._sample()
        self.stop_timer()

        finished_at = time.time()
        finished_monotonic = time.monotonic()
        if self._started_monotonic is not None:
            duration = finished_monotonic - self._started_monotonic
        else:
            duration = max(0.0, finished_at - self._started_at)

        tele = CommandTelemetry(
            duration=duration,
            cpu_seconds=self._cpu_last,
            peak_rss_bytes=self._peak_rss,
            process_count=self._peak_count,
            started_at=self._started_at,
            finished_at=finished_at,
        )
        self.last_telemetry = tele
        self._reset()
        return tele


# ---------------------------------------------------------------------------
# JSONL log writer
# ---------------------------------------------------------------------------

class _TelemetryLogger:
    """Append-only JSONL writer with 0o600 perms (the log captures
    command lines, which can include credentials — see module note)."""

    def __init__(self, path: str):
        self.path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)

    def append(self, record: dict) -> None:
        line = json.dumps(record, default=str) + "\n"
        try:
            fd = os.open(self.path,
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                         0o600)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
        except OSError as e:
            log.warning("command_telemetry: write failed: %s", e)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

#: Regex matching the trailing ``  [12.3s · 412MB]`` suffix we add to
#: tab titles, so repeated commands don't compound the suffix.
_TAB_SUFFIX_RE = re.compile(r"\s*\[[^\]]*?s(?:\s*·[^\]]*)?\]\s*$")

#: ANSI SGR dim grey used by the inline display mode.
_INLINE_DIM = "\x1b[2;37m"
_INLINE_RESET = "\x1b[0m"


class CommandTelemetryService:
    """Per-window service. Subscribes to shell_integration's ;C and
    ;D events, samples /proc on a per-command timer, and annotates
    each completed CommandRecord."""

    def __init__(self, window, poll_interval_ms: int = 100,
                 display: str = "tab_status",
                 log_path: Optional[str] = None,
                 tab_status_ms: int = 30_000):
        self._window = window
        self._poll_interval_ms = poll_interval_ms
        if display not in VALID_DISPLAY_MODES:
            log.warning(
                "command_telemetry: invalid display mode %r, falling back "
                "to 'tab_status'", display,
            )
            display = "tab_status"
        self._display = display
        self._tab_status_ms = tab_status_ms
        self._trackers: dict[int, _TabTracker] = {}
        self._logger: Optional[_TelemetryLogger] = None
        if log_path:
            try:
                self._logger = _TelemetryLogger(log_path)
            except OSError as e:
                log.warning("command_telemetry: cannot open log %r: %s",
                            log_path, e)
        # Subscriptions registered on shell_integration; stashed so
        # ``deactivate`` can unwind them.
        self._sub_started = None
        self._sub_finished = None
        # Pending fade-tab-title QTimer instances. We hold strong
        # references so ``detach`` can stop them — otherwise the
        # ``QTimer.singleShot`` callback fires up to 30 s after the
        # window was destroyed and crashes on a deleted QTabWidget.
        self._fade_timers: list[QTimer] = []

    @property
    def display(self) -> str:
        """Current display mode."""
        return self._display

    # -- subscription wiring --

    def attach(self, shell_int) -> None:
        if self._sub_started is None:
            self._sub_started = self._on_started
            shell_int.subscribe_command_started(self._sub_started)
        if self._sub_finished is None:
            self._sub_finished = self._on_finished
            shell_int.subscribe_command_finished(self._sub_finished)

    def detach(self, shell_int) -> None:
        if self._sub_started is not None:
            try:
                shell_int.unsubscribe_command_started(self._sub_started)
            except Exception:
                pass
            self._sub_started = None
        if self._sub_finished is not None:
            try:
                shell_int.unsubscribe_command_finished(self._sub_finished)
            except Exception:
                pass
            self._sub_finished = None
        for tracker in self._trackers.values():
            tracker.stop_timer()
        self._trackers.clear()
        # Cancel any pending tab-title fade timers so they don't fire
        # after the window is gone.
        for t in self._fade_timers:
            try:
                t.stop()
            except RuntimeError:
                pass
        self._fade_timers.clear()

    # -- shell_integration callbacks --

    def _tracker_for(self, terminal) -> _TabTracker:
        tid = id(terminal)
        tracker = self._trackers.get(tid)
        if tracker is None:
            tracker = _TabTracker(self._poll_interval_ms)
            self._trackers[tid] = tracker
        return tracker

    def _on_started(self, terminal, ev) -> None:
        tracker = self._tracker_for(terminal)
        tracker.on_start(
            terminal,
            started_at=ev.started_at,
            started_at_monotonic=ev.started_at_monotonic,
        )

    def _on_finished(self, terminal, rec) -> None:
        tracker = self._tracker_for(terminal)
        tele = tracker.on_finish()
        if tele is None:
            return
        # Annotate the CommandRecord so agent_control / MCP surfaces
        # pick it up via shell_integration.serialize_*.
        rec.telemetry = tele.to_dict()
        # Display.
        if self._display == "tab_status":
            self._update_tab_status(terminal, tele)
        elif self._display == "inline":
            self._inject_inline(terminal, tele)
        # Log.
        if self._logger is not None:
            self._logger.append({
                "tab_id": id(terminal),
                "started_at": tele.started_at,
                "finished_at": tele.finished_at,
                "duration": round(tele.duration, 3),
                "exit_status": rec.exit_status,
                "command": rec.text,
                "cwd": rec.cwd,
                **tele.to_dict(),
            })
        # Push event to attached agents. Prefer the agent_event_channel
        # ``publish`` API when it's loaded so the user's per-event
        # filter (``set_enabled_events``) still applies. Fall back to
        # the raw ``agent_control.broadcast_event`` only when no event
        # channel is configured — that path skips filtering, which is
        # acceptable as a last resort but not the default.
        payload = {
            "command": rec.text,
            "exit_status": rec.exit_status,
            "cwd": rec.cwd,
            "telemetry": tele.to_dict(),
        }
        events_svc = getattr(self._window, "agent_events", None)
        if events_svc is not None:
            try:
                events_svc.publish(id(terminal), "command_finished", payload)
                return
            except Exception:
                pass
        ac = getattr(self._window, "agent_control", None)
        if ac is not None:
            try:
                ac.broadcast_event(id(terminal), "command_finished", payload)
            except Exception:
                pass

    # -- display helpers --

    def _update_tab_status(self, terminal, tele: CommandTelemetry) -> None:
        if self._window is None:
            return
        tabs = getattr(self._window, "_tabs", None)
        if tabs is None:
            return
        for i in range(tabs.count()):
            split = tabs.widget(i)
            if split is None or not hasattr(split, "find_terminals"):
                continue
            if terminal not in split.find_terminals():
                continue
            title = tabs.tabText(i)
            # Strip a prior ``  [Xs · YMB]`` suffix before appending
            # the new one — otherwise repeated commands compound:
            # ``foo  [1s]  [2s]  [3s]``.
            base = _TAB_SUFFIX_RE.sub("", title)
            short = tele.format_short()
            try:
                tabs.setTabText(i, f"{base}  [{short}]")
                tabs.setTabToolTip(i, short)
            except Exception:
                pass
            # Fade after a configurable interval. Use a stateful
            # QTimer (not the QTimer.singleShot static call) so we
            # keep a handle and can cancel from ``detach``. Without
            # that the closure fires up to ``_tab_status_ms`` later
            # — long after the window may have been destroyed in a
            # test teardown — and crashes the event loop on a deleted
            # C++ QTabWidget.
            timer = QTimer()
            timer.setSingleShot(True)
            timer.timeout.connect(
                lambda t=terminal, tm=timer: self._fade_fire(t, tm)
            )
            self._fade_timers.append(timer)
            timer.start(self._tab_status_ms)
            return

    def _inject_inline(self, terminal, tele: CommandTelemetry) -> None:
        """Inject a dim grey telemetry line into the terminal output.

        QTermWidget has no public API to write to the display layer
        without going through the PTY master (which the shell would
        interpret as input and try to execute). Until a display-only
        write API is available, fall back to tab_status display.
        """
        self._show_tab_status(terminal, tele)

    def _fade_fire(self, terminal, timer: "QTimer") -> None:
        """Bridge slot for fade timers: clear the title and drop the
        timer from the pending list so ``detach`` has nothing stale
        to stop."""
        try:
            self._fade_timers.remove(timer)
        except ValueError:
            pass
        self._clear_tab_status(terminal)

    def _clear_tab_status(self, terminal) -> None:
        """Strip the trailing ``  [Xs]`` suffix. Safe against a
        destroyed C++ QTabWidget — fade timers can fire long after
        the window is gone (tests, window-close-mid-fade)."""
        if self._window is None:
            return
        tabs = getattr(self._window, "_tabs", None)
        if tabs is None:
            return
        try:
            count = tabs.count()
        except RuntimeError:
            self._window = None
            return
        for i in range(count):
            try:
                split = tabs.widget(i)
            except RuntimeError:
                return
            if split is None or not hasattr(split, "find_terminals"):
                continue
            if terminal not in split.find_terminals():
                continue
            try:
                title = tabs.tabText(i)
                tabs.setTabText(i, _TAB_SUFFIX_RE.sub("", title))
            except (RuntimeError, Exception):
                pass
            return

    # -- read API --

    def get_last_telemetry(self, terminal) -> Optional[dict]:
        """Return the last command's telemetry dict for ``terminal``,
        or ``None`` if no command has completed yet."""
        tracker = self._trackers.get(id(terminal))
        if tracker is None or tracker.last_telemetry is None:
            return None
        return tracker.last_telemetry.to_dict()

    def get_telemetry_history(self, terminal, limit: int = 10) -> list[dict]:
        """Return the last ``limit`` annotated command records for a tab.

        Delegates to shell_integration's history; only records that carry
        a ``telemetry`` dict are included (i.e. commands that completed
        while this plugin was active).
        """
        shell_int = getattr(self._window, "shell_integration", None)
        if shell_int is None:
            return []
        hist = shell_int.get_history(terminal)
        if hist is None:
            return []
        out = []
        for rec in reversed(hist.history):
            if rec.telemetry is not None:
                entry = {
                    "text": rec.text,
                    "exit_status": rec.exit_status,
                    "started_at": rec.started_at,
                    "finished_at": rec.finished_at,
                    "cwd": rec.cwd,
                    "telemetry": rec.telemetry,
                }
                out.append(entry)
                if len(out) >= limit:
                    break
        out.reverse()
        return out


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

class CommandTelemetryPlugin(Plugin):
    name = "command_telemetry"
    description = (
        "Track duration, CPU time, and peak memory for each command "
        "run in a terminal tab; annotate CommandRecord.telemetry."
    )
    version = "0.1"
    capabilities = ["command_telemetry"]

    def __init__(self):
        super().__init__()
        self._window = None
        self._service: Optional[CommandTelemetryService] = None

    def activate(self, app_controller):
        cfg = Config()
        enabled = bool(cfg.get(
            "plugins", "command_telemetry", "enabled", default=False,
        ))
        if not enabled:
            return

        shell_int = getattr(app_controller, "shell_integration", None)
        if shell_int is None:
            # Documented dependency — don't silently degrade.
            raise RuntimeError(
                "command_telemetry requires the shell_integration plugin"
            )

        poll_ms = int(cfg.get(
            "plugins", "command_telemetry", "poll_interval_ms", default=100,
        ))
        display = cfg.get(
            "plugins", "command_telemetry", "display", default="tab_status",
        )
        log_path = cfg.get(
            "plugins", "command_telemetry", "log_path", default="",
        )
        tab_status_ms = int(cfg.get(
            "plugins", "command_telemetry", "tab_status_ms", default=30_000,
        ))

        self._window = app_controller
        self._service = CommandTelemetryService(
            app_controller,
            poll_interval_ms=poll_ms,
            display=display,
            log_path=log_path or None,
            tab_status_ms=tab_status_ms,
        )
        if not hasattr(app_controller, "command_telemetry"):
            app_controller.command_telemetry = self._service
        self._service.attach(shell_int)

    def deactivate(self):
        shell_int = (
            getattr(self._window, "shell_integration", None)
            if self._window else None
        )
        if self._service is not None and shell_int is not None:
            self._service.detach(shell_int)
        if (self._window is not None
                and getattr(self._window, "command_telemetry", None) is self._service):
            try:
                del self._window.command_telemetry
            except AttributeError:
                pass
        self._service = None
        self._window = None
