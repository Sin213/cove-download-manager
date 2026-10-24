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
import tempfile
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
from cove.aria2 import Aria2Error, Aria2InterfaceError, Aria2RPC as RealAria2RPC

calls = {"constructed": [], "add_url": [], "activated": 0, "forwarded": None,
         "boxes": [], "admission": [], "enabled": [], "drains": 0}
flags = __A7A_FLAGS__
choices = list(flags.get("interface_choices", []))


class FakeQMessageBox:
    Warning, ActionRole, RejectRole = 2, 0, 1
    def __init__(self, parent=None): self.buttons = {}
    def setIcon(self, v): pass
    def setWindowTitle(self, v): pass
    def setDefaultButton(self, v): pass
    def setEscapeButton(self, v): pass
    def setText(self, v): calls["boxes"].append(["interface", v])
    def addButton(self, label, role):
        self.buttons[label] = label
        return label
    def exec(self): self.choice = self.buttons[choices.pop(0) if choices else "Close"]
    def clickedButton(self): return self.choice
    @staticmethod
    def critical(parent, title, text):
        calls["constructed"].append("QMessageBox.critical")
        calls["boxes"].append(["critical", text])
    @staticmethod
    def warning(parent, title, text):
        calls["constructed"].append("warning")
        calls["boxes"].append(["warning", text])


class FakeSettings:
    rpc_port, rpc_secret = 16801, "PERSISTED_SECRET_0123456789"
    theme = "dark"
    auto_update_check = False
    api_enabled = flags.get("api_enabled", False)
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
        self.aria2_admission = k.get("aria2_admission", "open")

    def set_aria2_admission(self, state):
        calls["constructed"].append("admission:" + state)
        calls["admission"].append(state)
        if self.aria2_admission == "pending": self.aria2_admission = state

    @property
    def legacy_cookie_cleanup(self):
        from types import SimpleNamespace
        calls["constructed"].append("cleanup_read")
        return SimpleNamespace(status=__CLEANUP_STATUS__)

    def add_url(self, url):
        if url == __BAD_URL__:
            return None  # simulates a rejected/duplicate magnet
        calls["add_url"].append(url)
        return 1

    def resume_persisted(self):
        calls["constructed"].append("resume_persisted")
        if flags.get("resume_raises"): raise RuntimeError("SECRET-CANARY-123")


class FakeAria2Daemon:
    def __init__(self, *a, **k):
        calls["constructed"].append("Aria2Daemon")
        FakeAria2Daemon.instance = self; self.ready, self.dead = False, False

    def active_rpc_secret(self):
        return "ACTIVE_SECRET_0123456789" if self.ready and not self.dead else None

    def start(self):
        calls["constructed"].append("daemon_start")
        if flags.get("interface_errors") and not calls.get("interface_seen"):
            calls["interface_seen"] = True
            raise Aria2InterfaceError("Network interface 'x' was not found.")
        if __DAEMON_FAILS__:
            raise Aria2Error("aria2 missing")
        if flags.get("start_raises"): raise RuntimeError("SECRET-CANARY-123")
        self.ready = not flags.get("no_secret", False)

    def stop(self, *, kill_only=False):
        self.ready = False
        attempts = calls.setdefault("stop_attempts", [])
        attempts.append(kill_only)
        if flags.get("pending_handles"):
            import cove.aria2 as lifecycle
            from unittest.mock import MagicMock
            if not hasattr(self, "_proc"):
                self._proc = MagicMock(); self._proc.poll.return_value = 0
                self._unreleased = False
                self._secret = self._active_secret = self._launch_conf = None
                self._unlink_launch_conf = lambda: None
                pending = MagicMock(pid=424242)
                pending.poll.return_value = None
                pending._cove_unresolved_logged = False
                pending.wait.return_value = 0
                def kill():
                    calls["pending_kills"] = calls.get("pending_kills", 0) + 1
                    if calls["pending_kills"] < 3: raise PermissionError("controlled")
                    pending.poll.return_value = 0
                pending.kill.side_effect = kill
                lifecycle._owned_pending = pending
            result = lifecycle.Aria2Daemon.stop(self, kill_only=kill_only)
            calls["pending_cleared"] = lifecycle._owned_pending is None
            return result
        sequence = flags.get("stop_sequence", [True])
        value = sequence[min(len(attempts) - 1, len(sequence) - 1)]
        if value == "raise": raise RuntimeError("SECRET-CANARY-123")
        return value


class FakeAria2RPC(RealAria2RPC):
    def __init__(self, *a, **k):
        calls["constructed"].append("Aria2RPC")
        super().__init__(*a, **k); self.sends = []; self.probe("before")

    def _session(self):
        from types import SimpleNamespace
        import requests
        def post(url, **kw):
            self.sends.append(kw["json"]["params"])
            response = requests.Response(); response.status_code = 200
            response._content = b'{"result":{"version":"fake"}}'
            return response
        return SimpleNamespace(post=post, close=lambda: None)

    def probe(self, phase):
        before = len(self.sends)
        try: self.get_version()
        except Aria2Error as exc: assert "not ready" in str(exc).lower()
        calls.setdefault("rpc_phases", {})[phase] = self.sends[before:]

    def set_overall_speed_limit_kbps(self, v):
        calls["constructed"].append("speed_limit")
        if flags.get("speed_raises"): raise RuntimeError("speed")
        self.probe("ready"); FakeAria2Daemon.instance.dead = True; self.probe("dead")

    def shutdown(self):
        pass


class FakeScheduler:
    def __init__(self, *a, **k):
        calls["constructed"].append("Scheduler")


class FakeMainWindow(QObject):
    def __init__(self, *a, **k):
        super().__init__()
        FakeMainWindow.instance = self
        calls["constructed"].append("MainWindow")
        from unittest.mock import MagicMock
        self.titlebar = MagicMock()
        self._queue = a[1] if len(a) > 1 else k.get("queue")
        self.settings, self.queue = a[0], self._queue

    def add_url_interactive(self, url):
        # The real window runs its duplicate check here and then calls the
        # queue; the delivery contract under test is that the magnet still
        # lands in queue.add_url exactly once.
        calls["constructed"].append("handoff:" + url)
        if late := flags.pop("during_handoff", None):
            FakeSingleInstanceServer.instance.open_requested.emit([late])
        if flags.get("handoff_raises") and url == __BAD_URL__:
            raise RuntimeError("SECRET-CANARY-123 " + url)
        self._queue.add_url(url)

    def _open_settings(self):
        calls["constructed"].append("open_settings")
        if flags.get("settings_raises"): raise OSError("settings")
        if flags.get("real_queue"):
            from unittest.mock import MagicMock
            from cove import main_window as mw
            class Accepted:
                Accepted = 1
                def __init__(self, *a): pass
                def exec(self): return self.Accepted
            mw.SettingsDialog, mw.configure_speed_spin = Accepted, lambda *a: None
            for name in ("concurrent_spin", "speed_unit", "speed_spin", "speed_always_on"):
                setattr(self, name, MagicMock())
            self._apply_speed_limit = lambda: self.queue.set_overall_speed_limit(0)
            self._refresh_schedule_section = self._refresh_folder_chip = lambda: None
            mw.MainWindow._open_settings(self)
            settle(self.queue)
            calls["before_ready"] = [m for m, ready in calls.get("rpc", []) if m in ACTIVATING and not ready]

    def note_extension_seen(self):
        calls["constructed"].append("extension_seen")

    def note_extension_setup_failed(self, details):
        calls["constructed"].append("extension_setup_failed")

    def isMinimized(self):
        return False

    def show(self):
        calls["constructed"].append("window_show")

    def note_legacy_cookie_cleanup_incomplete(self):
        calls["constructed"].append("cleanup_notice")

    def raise_(self):
        pass

    def activateWindow(self):
        calls["activated"] += 1

    def setEnabled(self, v):
        calls["enabled"].append(v)

    def stop_ui_timers(self):
        pass


class FakeLocalApiServer:
    def __init__(self, *a, **k):
        calls["constructed"].append("LocalApiServer")

    def start(self):
        calls["constructed"].append("api_start")

    def stop(self):
        pass


class FakeSingleInstanceServer(QObject):
    open_requested = Signal(list)
    activate_requested = Signal()
    extension_seen = Signal()

    def __init__(self, *a, **k):
        super().__init__()
        FakeSingleInstanceServer.instance = self
        calls["constructed"].append("SingleInstanceServer")
        early = __EARLY_IPC__
        if early:
            QTimer.singleShot(0, lambda: self.open_requested.emit([early]))
        late = __LATE_IPC__
        if late:
            QTimer.singleShot(200, lambda: self.open_requested.emit(late if isinstance(late, list) else [late]))

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
        if flags.get("recovery_actions") and False not in calls["enabled"]:
            FakeMainWindow.instance._open_settings()
            FakeMainWindow.instance.queue.remove(1)
            calls["constructed"].append("remove")
        if flags.get("recovery_schedule"):
            FakeMainWindow.instance._open_settings()
        # Simulate normal shutdown so run()'s aboutToQuit-connected cleanup
        # (which shuts down the single-instance server) actually runs.
        self.aboutToQuit.emit()
        self.processEvents()
        return flags.get("exec_status", 0)


app_mod.Settings = FakeSettings
app_mod.QueueManager = FakeQueueManager
app_mod.Aria2Daemon = FakeAria2Daemon
app_mod.Aria2RPC = FakeAria2RPC
if flags.get("real_queue"):
    from cove import db, config
    from cove.config import Settings as RealSettings, CategoryDirs
    from cove.queue import QueueManager as RealQueue
    from tests.test_queue_aria2_admission import ACTIVATING, settle, rows
    from pathlib import Path
    import requests
    path = Path.cwd() / "queue.sqlite3"
    init, connect = db.init, db.connect
    db.init, db.connect = lambda: init(path), lambda: connect(path)
    db.init()
    with db.connect() as conn:
        conn.execute("INSERT INTO downloads (url,out_dir,connections,speed_limit_kbps,status,created_at,category,backend) VALUES ('https://example.com/f.zip',?,4,0,'queued',1,'Other','aria2')", (str(Path.cwd()),))
    def no_http(*a, **k): raise AssertionError("unexpected HTTP request")
    requests.sessions.Session.request = no_http
    settings = RealSettings(download_dir=str(Path.cwd()), category_dirs=CategoryDirs(), intelligent_segments=False, auto_update_check=False)
    RealSettings.load, RealSettings.save = classmethod(lambda cls: settings), lambda self: None
    class StartupQueue(RealQueue):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._poll.stop(); self._ext_poll.stop()
            StartupQueue.instance = self
    class StartupRecorder(RealAria2RPC):
        def _call(self, method, params=(), *, with_token=True):
            calls.setdefault("rpc", []).append([method, bool(self.secret)])
            if self.secret is None: return super()._call(method, params, with_token=with_token)
            return "1234567890abcdef" if method in ACTIVATING else "OK"
        def shutdown(self): pass
    app_mod.Settings, app_mod.QueueManager, app_mod.Aria2RPC = RealSettings, StartupQueue, StartupRecorder
class FakeWatchdog:
    def __init__(self, *a): calls["constructed"].append("watchdog")
    def check(self): pass
    def stop(self): pass
app_mod.Aria2Watchdog = FakeWatchdog
FakeMainWindow.note_aria2_unavailable = lambda *a: None
# The two unrelated registration threads must not reach the host environment.
from unittest.mock import MagicMock
app_mod.threading.Thread = MagicMock()
app_mod.Scheduler = FakeScheduler
app_mod.MainWindow = FakeMainWindow
app_mod.LocalApiServer = FakeLocalApiServer
app_mod.SingleInstanceServer = FakeSingleInstanceServer
app_mod.send_to_primary = fake_send_to_primary
app_mod.QApplication = BoundedExecQApplication
app_mod.QMessageBox = FakeQMessageBox
app_mod.install_native_hosts = lambda: None
app_mod.shutdown_diagnostics = lambda: calls["constructed"].append("diagnostics_shutdown")

sys.argv = ["cove"] + __ARGV__

def profile(frame, event, arg):
    if event == "call" and frame.f_code.co_name == "_drain_startup_inbox": calls["drains"] += 1
sys.setprofile(profile)
rc = app_mod.run()
sys.setprofile(None)
if flags.get("real_queue"):
    settle(StartupQueue.instance)
    calls["saved"] = rows(path)
    restarted = StartupQueue(settings, StartupRecorder(settings, secret_provider=lambda: None))
    calls["restored"] = sorted(restarted.tasks)
    calls["saved_after_restart"] = rows(path)
calls["returncode"] = rc
print(json.dumps(calls))
'''


_CHILD_GUARD = '''import os,sys
from pathlib import Path
assert not any(n == 'cove' or n.startswith('cove.') for n in sys.modules)
sentinel = Path(os.environ['S08B2_CHILD_SENTINEL']).resolve()
assert 'COVE_PORTABLE' not in os.environ and not getattr(sys, 'frozen', False)
for key in ('HOME','XDG_DATA_HOME','XDG_CONFIG_HOME','USERPROFILE','APPDATA','LOCALAPPDATA'):
    if key in os.environ:
        assert os.environ[key] and Path(os.environ[key]).resolve().is_relative_to(sentinel)
assert Path.cwd().resolve().is_relative_to(sentinel)
for directory in (Path(__file__).resolve().parent, Path(sys.argv[0]).resolve().parent, Path.cwd()):
    assert not (directory / 'portable.marker').exists()
    assert not (directory / 'cove-app-data').exists()
print('S08B2_GUARD_BEFORE_IMPORT', flush=True)
from cove import config
from cove.portable import is_portable
assert not is_portable()
assert all(Path(p).resolve().is_relative_to(sentinel) for p in (config.DB_FILE,config.DATA_DIR,config.CONFIG_FILE))
'''


def _assert_child_clean(*roots):
    for root in roots:
        assert not [p for p in root.rglob('*') if p.name in ('cove.db', 'cove-app-data', 'portable.marker')], 'child data artifact'


def _isolated_child(script, *, control="", input=None):
    # TEST-DISCIPLINE: choose the sentinel before any child filesystem operation.
    with tempfile.TemporaryDirectory(prefix="s08b2-app-") as temporary:
        root = Path(temporary)
        for name in ('home', 'data', 'config', 'work', 'scripts', 'guard', 'profile', 'appdata', 'localappdata'):
            (root / name).mkdir()
        target = root / 'scripts' / 'child.py'
        env = {k: os.environ[k] for k in ('PATH','LD_LIBRARY_PATH','LANG','DISPLAY',
               'SYSTEMROOT','SYSTEMDRIVE','PATHEXT','COMSPEC') if k in os.environ}
        env.update(HOME=str(root / 'home'), XDG_DATA_HOME=str(root / 'data'),
                   XDG_CONFIG_HOME=str(root / 'config'), S08B2_CHILD_SENTINEL=str(root),
                   USERPROFILE=str(root / 'profile'), APPDATA=str(root / 'appdata'),
                   LOCALAPPDATA=str(root / 'localappdata'),
                   PYTHONPATH=str(REPO_ROOT), PYTHONDONTWRITEBYTECODE='1', QT_QPA_PLATFORM='offscreen')
        if control == 'marker':
            (target.parent / 'portable.marker').touch()
        if control == 'portable':
            env['COVE_PORTABLE'] = '1'
        if control == 'outside':
            env['XDG_DATA_HOME'] = '/'
        if control == 'outside-profile':
            env['USERPROFILE'] = '/'
        guard = root / 'guard' / 'guard.py'
        guard.write_text("import sys\nsys.argv = sys.argv[1:]\n" + _CHILD_GUARD +
                         "import runpy\nrunpy.run_path(sys.argv[0], run_name='__main__')\n")
        target.write_text(script)
        result = subprocess.run([sys.executable, '-B', str(guard), str(target)], cwd=root / 'work',
                                env=env, capture_output=True, text=True, timeout=20, input=input)
        if control == 'marker':
            (target.parent / 'portable.marker').unlink()
        _assert_child_clean(root, root / 'work', target.parent)
        return result


def _run_harness(argv, is_primary, daemon_fails=False, early_ipc=None, late_ipc=None,
                  forward_ok=True, bad_url="", return_raw=False,
                  settings_unreadable=False, cleanup_status="not_needed", **flags):
    script = (
        _HARNESS
        .replace("__A7A_FLAGS__", repr(flags))
        .replace("__ARGV__", repr(argv))
        .replace("__IS_PRIMARY__", repr(is_primary))
        .replace("__SETTINGS_UNREADABLE__", repr(settings_unreadable))
        .replace("__DAEMON_FAILS__", repr(daemon_fails))
        .replace("__EARLY_IPC__", repr(early_ipc))
        .replace("__LATE_IPC__", repr(late_ipc))
        .replace("__FORWARD_OK__", repr(forward_ok))
        .replace("__BAD_URL__", repr(bad_url))
        .replace("__CLEANUP_STATUS__", repr(cleanup_status))
    )
    result = _isolated_child(script)
    assert result.returncode in (0, 1), result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    if return_raw:
        return out, result
    return out


@pytest.mark.parametrize("sequence,status,expected,count", [
    ([False, False, True], 0, 0, 3),  # D1: bounded recovery
    ([False], 0, 70, 3),             # D2/D5: unresolved and exact bound
    (["raise"], 0, 70, 3),          # D3: sanitized exception, cleanup continues
    ([True], 0, 0, 1),              # D4: successful status
    ([True], 17, 17, 1),            # D4: preserve app failure
    ([False], 17, 17, 3),           # preserve failure even if unresolved
    ([False, True], 0, 0, 2),
])
def test_lifecycle_cleanup_status(sequence, status, expected, count):
    out, raw = _run_harness([], True, stop_sequence=sequence,
                            exec_status=status, return_raw=True)
    assert out["returncode"] == expected
    assert len(out["stop_attempts"]) == count, "one retry round, at most two additional calls"
    assert out["stop_attempts"] == [False] + [True] * (count - 1)
    assert out["constructed"][-2:] == ["shutdown", "diagnostics_shutdown"]
    assert "SECRET-CANARY-123" not in raw.stderr
    if "raise" in sequence:
        assert "RuntimeError" in raw.stderr


def test_lifecycle_cleanup_retries_pending_handle_behind_exited_child():
    out, raw = _run_harness([], True, pending_handles=True, return_raw=True)
    assert out["returncode"] == 0 and out["pending_cleared"]
    assert out["stop_attempts"] == [False, True, True]
    assert out["pending_kills"] == 3
    assert raw.stderr.count("aria2_child_unresolved") == 1
    assert out["constructed"][-2:] == ["shutdown", "diagnostics_shutdown"]


def test_p1_OP16_app_rpc_tracks_daemon_readiness():
    phases = _run_harness(argv=[], is_primary=True)["rpc_phases"]
    assert phases == {"before": [], "ready": [["token:ACTIVE_SECRET_0123456789"]], "dead": []}

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
    result = _isolated_child(script, input="")
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


@pytest.mark.parametrize('status', ['not_needed', 'complete', 'incomplete', 'blocked'])
def test_s08b2_K_launch_order(status):
    out, result = _run_harness([MAGNET], True, cleanup_status=status, return_raw=True)
    events = out['constructed']
    assert events.count('cleanup_read') == 1
    assert events.index('window_show') < events.index('cleanup_read') < events.index('daemon_start') < events.index('resume_persisted')
    assert events.count('cleanup_notice') == int(status in ('incomplete', 'blocked'))
    assert out['add_url'] == [MAGNET] and out['returncode'] == 0 and result.returncode == 0
    assert 'S08B2_GUARD_BEFORE_IMPORT' in result.stdout


@pytest.mark.parametrize('control', ['marker', 'portable', 'outside', 'outside-profile'])
def test_s08b2_M_guard_refusal(control):
    result = _isolated_child("raise AssertionError('import reached')\n", control=control)
    assert result.returncode != 0
    assert 'S08B2_GUARD_BEFORE_IMPORT' not in result.stdout
    assert 'import reached' not in result.stderr


def test_s08b2_M_parent_detection():
    for script in ("from cove import db\nwith db.connect(): pass\n",
                   "import importlib,os\nos.environ['COVE_PORTABLE']='1'\nfrom cove import config\nimportlib.reload(config)\nfrom cove import db\nwith db.connect(): pass\n",
                   "from pathlib import Path\nPath('cove-app-data').mkdir()\n",
                   "from pathlib import Path\nPath('portable.marker').touch()\n"):
        with pytest.raises(AssertionError, match='child data artifact'):
            _isolated_child(script)


def test_s08b2_U_M_structure():
    import ast
    for filename in ('queue.py', 'app.py', 'main_window.py'):
        tree = ast.parse((REPO_ROOT / 'cove' / filename).read_text())
        if filename != 'queue.py':
            assert not any(isinstance(n, ast.ImportFrom) and n.module in ('cove.db', 'db') for n in ast.walk(tree))
            assert not any(isinstance(n, ast.ImportFrom) and any(a.name == 'db' for a in n.names) for n in ast.walk(tree))
            assert not any(isinstance(n, ast.Import) and any(a.name == 'cove.db' for a in n.names) for n in ast.walk(tree))
            assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == 'clear_legacy_cookies' for n in ast.walk(tree))
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                calls = [ast.unparse(n.func) for n in ast.walk(node) if isinstance(n, ast.Call)]
                assert 'QueueManager' not in calls
                if filename == 'queue.py':
                    assert 'db.init' not in calls and 'self._load_persisted' not in calls


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


def test_settings_recovery_never_activates_aria2_before_ready():
    out = _run_harness([], True, real_queue=True, interface_errors=True, interface_choices=["Open Settings"])
    assert out["before_ready"] == []
    assert [m for m, ready in out["rpc"] if m == "aria2.addUri" and ready] == ["aria2.addUri"]
    assert out["constructed"].count("daemon_start") == 2


def test_failed_start_keeps_settings_remove_and_quit_available():
    out = _run_harness([], True, daemon_fails=True, real_queue=True, recovery_actions=True)
    assert out["enabled"] == []
    assert "open_settings" in out["constructed"] and "remove" in out["constructed"]
    assert out["saved"] == [] and out["restored"] == [] and "shutdown" in out["constructed"]
    assert not [m for m, _ in out.get("rpc", []) if m in {"aria2.addUri", "aria2.unpause"}]


@pytest.mark.parametrize("phase", ["buffered", "late"])
@pytest.mark.parametrize("count", [1, 2])
def test_handoff_exceptions_report_one_sanitized_warning_per_batch(phase, count):
    batch = [MAGNET_BAD] * count + [MAGNET]
    out, raw = _run_harness(batch if phase == "buffered" else [], True,
                           late_ipc=batch if phase == "late" else None, bad_url=MAGNET_BAD,
                           handoff_raises=True, api_enabled=True, return_raw=True)
    assert out["add_url"] == [MAGNET]
    text = f"{count} magnet link{' was' if count == 1 else 's were'} not added. Open the link again; if that fails, restart Cove."
    assert out["boxes"] == [["warning", text]]
    assert "SECRET-CANARY-123" not in raw.stderr and MAGNET_BAD not in raw.stderr
    order = out["constructed"]
    assert order.index("api_start") < order.index("watchdog") < order.index("warning")


def test_modal_handoff_order_is_drain_then_api_then_watchdog():
    out = _run_harness([MAGNET, MAGNET_2], True, api_enabled=True)
    assert out["add_url"] == [MAGNET, MAGNET_2] and out["drains"] == 1
    order = out["constructed"]
    assert order.index("resume_persisted") < order.index("handoff:" + MAGNET) < order.index("handoff:" + MAGNET_2) < order.index("api_start") < order.index("watchdog")


@pytest.mark.parametrize("fault", ["resume_raises", "speed_raises"])
def test_recovery_exception_still_drains_once_and_keeps_admission_open(fault):
    out = _run_harness([MAGNET], True, **{fault: True})
    assert out["add_url"] == [MAGNET]
    assert out["admission"] == ["open"] and out["drains"] == 1
    assert out["constructed"].count("resume_persisted") == 1
    expected = [["warning", "Cove started the download engine but couldn't resume your saved downloads. They are still in your list. Restart Cove to try again."]] if fault == "resume_raises" else []
    assert out["boxes"] == expected


@pytest.mark.parametrize("fault", ["daemon", "start_raises", "no_secret", "interface_close", "settings_raises"])
def test_startup_failure_reports_buffered_and_late_links_without_draining(fault):
    flags = {fault: True} if fault in ("start_raises", "no_secret") else {}
    if fault in ("interface_close", "settings_raises"):
        flags.update(interface_errors=True, interface_choices=["Open Settings"] if fault == "settings_raises" else ["Close"], settings_raises=fault == "settings_raises")
    out, raw = _run_harness([MAGNET], True, daemon_fails=fault == "daemon", late_ipc=[MAGNET_2, MAGNET_BAD], return_raw=True, **flags)
    assert out["add_url"] == []
    assert out["admission"] == ["failed"] and out["drains"] == 0
    warnings = [text for kind, text in out["boxes"] if kind == "warning"]
    assert warnings == ["1 magnet link was not added. Open the link again; if that fails, restart Cove.", "2 magnet links were not added. Open the link again; if that fails, restart Cove."]
    assert out["enabled"] == [] and "SECRET-CANARY-123" not in raw.stdout + raw.stderr
    if fault in ("daemon", "no_secret"):
        assert out["boxes"][0] == ["critical", "aria2 missing" if fault == "daemon" else "aria2 did not report authenticated readiness."]
    if fault in ("interface_close", "settings_raises"):
        assert out["boxes"][0] == ["interface", "Network interface 'x' was not found."]
    if fault in ("start_raises", "settings_raises"):
        assert out["boxes"][-3] == ["critical", "Cove couldn't start the download engine. Restart Cove and try again."]
    if fault not in ("interface_close", "settings_raises"):
        order = out["constructed"]
        assert order.index("admission:failed") < order.index("QMessageBox.critical")


def test_ready_opens_before_continuation_and_successful_batches_have_no_warning():
    out = _run_harness([MAGNET], True, early_ipc=MAGNET_2, late_ipc=MAGNET_BAD)
    assert out["add_url"] == [MAGNET, MAGNET_2, MAGNET_BAD] and out["boxes"] == []
    order = out["constructed"]
    assert out["admission"] == ["open"] and out["drains"] == 1
    assert order.index("daemon_start") < order.index("admission:open") < order.index("speed_limit") < order.index("resume_persisted")


@pytest.mark.parametrize("failed", [False, True])
def test_startup_failure_preserves_saved_row_for_restart(failed):
    out = _run_harness([], True, real_queue=True, interface_errors=failed, interface_choices=["Close"], recovery_schedule=failed)
    # A later accepted Settings action is a real scheduling entry, even after Close.
    if failed:
        assert out["saved"] == [[1, "queued", None, None]]
        assert out["restored"] == [1]
        assert out["saved_after_restart"] == out["saved"]
    else:
        assert [m for m, ready in out["rpc"] if m == "aria2.addUri" and ready] == ["aria2.addUri"]


def test_reentrant_ipc_during_startup_drain_is_delivered_once():
    out = _run_harness([MAGNET], True, during_handoff=MAGNET_2)
    assert out["add_url"] == [MAGNET_2, MAGNET]
    assert out["drains"] == 1 and out["boxes"] == []
