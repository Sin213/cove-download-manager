"""Tests for cove.app: launch-argument parsing, window activation, and the
single-instance election / startup-inbox integration in run().

The election/startup-inbox scenarios drive the real `cove.app.run()` in a
subprocess with every heavy service (Settings, aria2, the queue, the
window, the API server, single-instance IPC) replaced by fakes, and with
QApplication.exec() overridden to pump the event loop for a bounded window
instead of actually blocking. This is the only way to exercise run()'s real
control flow (construction order, closures, QTimer.singleShot scheduling)
without starting a real GUI or aria2 daemon.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cove.app import activate_window, parse_launch_urls

REPO_ROOT = Path(__file__).resolve().parents[1]

MAGNET = "magnet:?xt=urn:btih:" + "a" * 40
MAGNET_2 = "magnet:?xt=urn:btih:" + "b" * 40
MAGNET_BAD = "magnet:?xt=urn:btih:" + "c" * 40


# --- parse_launch_urls ----------------------------------------------------

def test_parse_launch_urls_empty():
    assert parse_launch_urls(["cove"]) == []


def test_parse_launch_urls_single_magnet():
    assert parse_launch_urls(["cove", MAGNET]) == [MAGNET]


def test_parse_launch_urls_multiple_magnets():
    assert parse_launch_urls(["cove", MAGNET, MAGNET_2]) == [MAGNET, MAGNET_2]


def test_parse_launch_urls_ignores_flags_and_other_schemes():
    assert parse_launch_urls(["cove", "--some-flag", "https://example.com"]) == []


def test_parse_launch_urls_ignores_file_paths():
    assert parse_launch_urls(["cove", "/home/user/file.torrent"]) == []


def test_parse_launch_urls_bounded_count():
    from cove.single_instance import MAX_URLS_PER_MESSAGE

    many = [MAGNET] * (MAX_URLS_PER_MESSAGE + 5)
    assert len(parse_launch_urls(["cove", *many])) == MAX_URLS_PER_MESSAGE


def test_parse_launch_urls_several_valid_magnets_preserve_order():
    magnet3 = "magnet:?xt=urn:btih:" + "d" * 40
    assert parse_launch_urls(["cove", MAGNET, MAGNET_2, magnet3]) == [
        MAGNET,
        MAGNET_2,
        magnet3,
    ]


# --- command-line validation shares the IPC path's policy ------------------
#
# cove.app.parse_launch_urls() and cove.single_instance.validate_message()
# (the IPC `open` path) both delegate to the same
# cove.single_instance.is_valid_launch_url() helper - these tests prove that
# sharing, not just that each path happens to reject similar things.

def _ipc_accepts(url: str) -> bool:
    from cove.single_instance import MessageError, validate_message

    try:
        validate_message({"version": 1, "action": "open", "urls": [url]})
        return True
    except MessageError:
        return False


def test_command_line_max_length_matches_ipc():
    from cove.torrent import MAX_MAGNET_LENGTH

    base = "magnet:?xt=urn:btih:" + "a" * 40 + "&tr="
    padding = "x" * (MAX_MAGNET_LENGTH - len(base))
    at_limit = base + padding
    assert len(at_limit) == MAX_MAGNET_LENGTH
    assert parse_launch_urls(["cove", at_limit]) == [at_limit]
    assert _ipc_accepts(at_limit) is True


def test_command_line_oversized_magnet_rejected():
    from cove.torrent import MAX_MAGNET_LENGTH

    oversized = "magnet:?xt=urn:btih:" + "a" * 40 + "&tr=" + "x" * MAX_MAGNET_LENGTH
    assert parse_launch_urls(["cove", oversized]) == []
    assert _ipc_accepts(oversized) is False


def test_command_line_nul_magnet_rejected():
    tainted = MAGNET + "\x00"
    assert parse_launch_urls(["cove", tainted]) == []
    assert _ipc_accepts(tainted) is False


def test_command_line_other_control_character_magnet_rejected():
    tainted = MAGNET + "\x1b[31m"
    assert parse_launch_urls(["cove", tainted]) == []
    assert _ipc_accepts(tainted) is False


def test_command_line_unsupported_scheme_rejected():
    assert parse_launch_urls(["cove", "https://example.com/x"]) == []
    assert _ipc_accepts("https://example.com/x") is False


@pytest.mark.parametrize(
    "candidate",
    [
        MAGNET,
        "https://example.com",
        "",
        "magnet:?xt=urn:btih:" + "a" * 40 + "\x00",
        "magnet:?xt=urn:btih:" + "a" * 40 + "\x07",
        "/home/user/file.torrent",
    ],
)
def test_command_line_and_ipc_validation_agree(candidate):
    cli_accepted = parse_launch_urls(["cove", candidate]) == [candidate]
    assert cli_accepted == _ipc_accepts(candidate)


# --- activate_window --------------------------------------------------------

class _FakeWindow:
    def __init__(self, minimized=False):
        self._minimized = minimized
        self.calls = []

    def isMinimized(self):
        return self._minimized

    def showNormal(self):
        self.calls.append("showNormal")

    def show(self):
        self.calls.append("show")

    def raise_(self):
        self.calls.append("raise")

    def activateWindow(self):
        self.calls.append("activateWindow")


def test_activate_window_minimized_becomes_visible():
    w = _FakeWindow(minimized=True)
    activate_window(w)
    assert w.calls == ["showNormal", "show", "raise", "activateWindow"]


def test_activate_window_normal_stays_normal():
    w = _FakeWindow(minimized=False)
    activate_window(w)
    assert w.calls == ["show", "raise", "activateWindow"]


def test_activate_window_never_raises_on_failure():
    class Bad:
        def isMinimized(self):
            raise RuntimeError("compositor refused")

    activate_window(Bad())  # must not propagate


# --- run() integration, via subprocess with faked services -----------------

_HARNESS = r'''
import json
import sys
import time

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication as _RealQApplication

import cove.app as app_mod
from cove.aria2 import Aria2Error

calls = {"constructed": [], "add_url": [], "activated": 0, "forwarded": None}


class FakeQMessageBox:
    @staticmethod
    def critical(parent, title, text):
        calls["constructed"].append("QMessageBox.critical")


class FakeSettings:
    theme = "dark"
    auto_update_check = False
    api_enabled = False
    schedule = None
    overall_speed_limit_kbps = 0
    speed_limiter_enabled = False

    @classmethod
    def load(cls):
        calls["constructed"].append("Settings")
        if __SETTINGS_UNREADABLE__:
            raise PermissionError(13, "Access is denied")
        return cls()


class FakeQueueManager:
    def __init__(self, *a, **k):
        calls["constructed"].append("QueueManager")
        from unittest.mock import MagicMock
        self.error = MagicMock()

    def add_url(self, url):
        if url == __BAD_URL__:
            return None  # simulates a rejected/duplicate magnet
        calls["add_url"].append(url)
        return 1

    def resume_persisted(self):
        calls["constructed"].append("resume_persisted")


class FakeAria2Daemon:
    def __init__(self, *a, **k):
        calls["constructed"].append("Aria2Daemon")

    def start(self):
        if __DAEMON_FAILS__:
            raise Aria2Error("aria2 missing")

    def stop(self):
        pass


class FakeAria2RPC:
    def __init__(self, *a, **k):
        calls["constructed"].append("Aria2RPC")

    def set_overall_speed_limit_kbps(self, v):
        pass

    def shutdown(self):
        pass


class FakeScheduler:
    def __init__(self, *a, **k):
        calls["constructed"].append("Scheduler")


class FakeMainWindow:
    def __init__(self, *a, **k):
        calls["constructed"].append("MainWindow")
        from unittest.mock import MagicMock
        self.titlebar = MagicMock()
        self._queue = a[1] if len(a) > 1 else k.get("queue")

    def add_url_interactive(self, url):
        # The real window runs its duplicate check here and then calls the
        # queue; the delivery contract under test is that the magnet still
        # lands in queue.add_url exactly once.
        self._queue.add_url(url)

    def note_extension_seen(self):
        calls["constructed"].append("extension_seen")

    def isMinimized(self):
        return False

    def show(self):
        pass

    def raise_(self):
        pass

    def activateWindow(self):
        calls["activated"] += 1

    def setEnabled(self, v):
        pass

    def stop_ui_timers(self):
        pass


class FakeLocalApiServer:
    def __init__(self, *a, **k):
        calls["constructed"].append("LocalApiServer")

    def start(self):
        pass

    def stop(self):
        pass


class FakeSingleInstanceServer(QObject):
    open_requested = Signal(list)
    activate_requested = Signal()
    extension_seen = Signal()

    def __init__(self, *a, **k):
        super().__init__()
        calls["constructed"].append("SingleInstanceServer")
        early = __EARLY_IPC__
        if early:
            QTimer.singleShot(0, lambda: self.open_requested.emit([early]))
        late = __LATE_IPC__
        if late:
            QTimer.singleShot(200, lambda: self.open_requested.emit([late]))

    def try_become_primary(self, name):
        return __IS_PRIMARY__

    def shutdown(self):
        calls["constructed"].append("shutdown")


def fake_send_to_primary(name, urls, **kwargs):
    calls["forwarded"] = list(urls)
    return __FORWARD_OK__


class BoundedExecQApplication(_RealQApplication):
    def exec(self):
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            self.processEvents()
            time.sleep(0.01)
        # Simulate normal shutdown so run()'s aboutToQuit-connected cleanup
        # (which shuts down the single-instance server) actually runs.
        self.aboutToQuit.emit()
        self.processEvents()
        return 0


app_mod.Settings = FakeSettings
app_mod.QueueManager = FakeQueueManager
app_mod.Aria2Daemon = FakeAria2Daemon
app_mod.Aria2RPC = FakeAria2RPC
app_mod.Scheduler = FakeScheduler
app_mod.MainWindow = FakeMainWindow
app_mod.LocalApiServer = FakeLocalApiServer
app_mod.SingleInstanceServer = FakeSingleInstanceServer
app_mod.send_to_primary = fake_send_to_primary
app_mod.QApplication = BoundedExecQApplication
app_mod.QMessageBox = FakeQMessageBox
app_mod.install_native_hosts = lambda: None

sys.argv = ["cove"] + __ARGV__

rc = app_mod.run()
calls["returncode"] = rc
print(json.dumps(calls))
'''


def _run_harness(argv, is_primary, daemon_fails=False, early_ipc=None, late_ipc=None,
                  forward_ok=True, bad_url="", return_raw=False,
                  settings_unreadable=False):
    script = (
        _HARNESS
        .replace("__ARGV__", repr(argv))
        .replace("__IS_PRIMARY__", repr(is_primary))
        .replace("__SETTINGS_UNREADABLE__", repr(settings_unreadable))
        .replace("__DAEMON_FAILS__", repr(daemon_fails))
        .replace("__EARLY_IPC__", repr(early_ipc))
        .replace("__LATE_IPC__", repr(late_ipc))
        .replace("__FORWARD_OK__", repr(forward_ok))
        .replace("__BAD_URL__", repr(bad_url))
    )
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode in (0, 1), result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    if return_raw:
        return out, result
    return out


def test_primary_launch_continues_normal_construction():
    out = _run_harness(argv=[], is_primary=True)
    assert "Settings" in out["constructed"]
    assert "QueueManager" in out["constructed"]
    assert "Aria2Daemon" in out["constructed"]
    assert "MainWindow" in out["constructed"]
    assert "LocalApiServer" in out["constructed"]
    assert "resume_persisted" in out["constructed"]
    assert out["forwarded"] is None


def test_secondary_no_argument_sends_activate_and_builds_nothing():
    out = _run_harness(argv=[], is_primary=False)
    assert out["forwarded"] == []
    for heavy in ("Settings", "QueueManager", "Aria2Daemon", "Aria2RPC", "MainWindow", "LocalApiServer"):
        assert heavy not in out["constructed"]
    assert out["returncode"] == 0


def test_secondary_with_magnet_sends_open_and_builds_nothing():
    out = _run_harness(argv=[MAGNET], is_primary=False)
    assert out["forwarded"] == [MAGNET]
    for heavy in ("Settings", "QueueManager", "Aria2Daemon", "Aria2RPC", "MainWindow", "LocalApiServer"):
        assert heavy not in out["constructed"]


def test_secondary_failed_forward_exits_nonzero_without_starting_daemon():
    out = _run_harness(argv=[MAGNET], is_primary=False, forward_ok=False)
    assert out["returncode"] == 1
    assert "Aria2Daemon" not in out["constructed"]


def test_command_line_magnet_drained_after_daemon_ready():
    out = _run_harness(argv=[MAGNET], is_primary=True)
    assert out["add_url"] == [MAGNET]
    assert "resume_persisted" in out["constructed"]


def test_early_ipc_magnet_buffered_and_drained_once_ready():
    # "Early" here means before the window exists at all (the IPC handlers
    # are wired up immediately so a racing secondary still gets a prompt
    # ack - see cove/app.py's processEvents() calls through construction).
    # Activation genuinely cannot happen yet with no window to raise; the
    # magnet must still make it into the queue once ready regardless.
    out = _run_harness(argv=[], is_primary=True, early_ipc=MAGNET)
    assert out["add_url"] == [MAGNET]


def test_arrival_order_preserved_across_cli_and_ipc():
    out = _run_harness(argv=[MAGNET], is_primary=True, early_ipc=MAGNET_2)
    assert out["add_url"] == [MAGNET, MAGNET_2]


def test_late_ipc_magnet_goes_directly_through_add_url():
    out = _run_harness(argv=[], is_primary=True, late_ipc=MAGNET)
    assert out["add_url"] == [MAGNET]


def test_daemon_start_failure_does_not_drain_buffered_magnets():
    out = _run_harness(argv=[MAGNET], is_primary=True, daemon_fails=True)
    assert out["add_url"] == []
    assert "resume_persisted" not in out["constructed"]


def test_rejected_magnet_does_not_block_other_buffered_magnets():
    out = _run_harness(
        argv=[MAGNET_BAD, MAGNET], is_primary=True, bad_url=MAGNET_BAD
    )
    assert out["add_url"] == [MAGNET]


def test_shape_invalid_command_line_magnet_never_enters_startup_inbox():
    """An oversized/malformed magnet fails `is_valid_launch_url()` inside
    `parse_launch_urls()` itself - it never becomes part of `launch_urls`,
    so it can't reach `startup_inbox` or `queue.add_url()` at all (unlike
    `test_rejected_magnet_does_not_block_other_buffered_magnets` above,
    which covers a *shape-valid* magnet the queue itself rejects)."""
    from cove.torrent import MAX_MAGNET_LENGTH

    oversized = "magnet:?xt=urn:btih:" + "a" * 40 + "&tr=" + "x" * MAX_MAGNET_LENGTH
    out = _run_harness(argv=[oversized, MAGNET], is_primary=True)
    assert out["add_url"] == [MAGNET]


def test_no_rejected_magnet_contents_in_logs_or_exceptions():
    tainted = MAGNET + "\x00"
    out, result = _run_harness(argv=[tainted, MAGNET], is_primary=True, return_raw=True)
    assert out["add_url"] == [MAGNET]
    assert result.returncode == 0
    assert tainted not in result.stdout
    assert tainted not in result.stderr
    assert "\x00" not in result.stdout
    assert "\x00" not in result.stderr


def test_activation_helper_called_on_ipc_open():
    # Use a late-arriving IPC request (after the window definitely exists)
    # to prove activate_window() actually gets called on a real open.
    out = _run_harness(argv=[], is_primary=True, late_ipc=MAGNET)
    assert out["activated"] >= 1


def test_single_instance_server_shutdown_on_exit():
    out = _run_harness(argv=[], is_primary=True)
    assert "shutdown" in out["constructed"]


# --- native-messaging isolation --------------------------------------------

def test_native_messaging_bypasses_single_instance_and_gui():
    """`--native-messaging` must dispatch before any of Settings, aria2,
    the queue, the window, or single-instance IPC are touched."""
    script = (
        "import sys\n"
        "sys.argv = ['cove', '--native-messaging']\n"
        "import cove.app as app_mod\n"
        "def boom(*a, **k):\n"
        "    raise AssertionError('single-instance logic entered under --native-messaging')\n"
        "app_mod.SingleInstanceServer = boom\n"
        "app_mod.QApplication = boom\n"
        "import cove.native_messaging as nm\n"
        "nm.main = lambda: None\n"
        "sys.modules['cove.native_messaging'] = nm\n"
        "rc = app_mod.run()\n"
        "print('OK', rc)\n"
    )
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        input="",
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_unreadable_settings_exits_without_constructing_services():
    """An unreadable settings.json must stop startup, not reset the file.

    Settings.load() fails closed on a read error rather than falling back to
    defaults, because the fallback regenerates rpc_secret and api_token and
    discards every stored setting. run() has to honour that: exit non-zero
    before aria2, the queue, or the window exist, so nothing downstream runs
    against half-initialised state and the file is left recoverable.
    """
    out, result = _run_harness(
        argv=[], is_primary=True, settings_unreadable=True, return_raw=True
    )

    assert out["returncode"] == 1
    assert "Settings" in out["constructed"]
    for heavy in ("Aria2Daemon", "Aria2RPC", "QueueManager", "MainWindow", "LocalApiServer"):
        assert heavy not in out["constructed"], heavy
    assert "settings_unreadable" in result.stderr
