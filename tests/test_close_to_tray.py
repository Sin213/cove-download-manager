"""Browser-download acceptance gate and close-to-tray window behavior.

Both halves of the emergency repair meet here: the gate decides whether the
*running* process may tell the browser "I took it", and close-to-tray is what
lets a user keep that process available after pressing X.

MainWindow's constructor is heavy (aria2, DB, dozens of widgets), so the
close/tray tests drive the real unbound methods against a light host object,
matching the existing convention in tests/test_dialogs.py. Tray availability
is always faked - a test must never depend on the desktop environment
actually providing a system tray.
"""
from collections import deque

import pytest
import shiboken6
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMainWindow

import cove.main_window as mw
from cove.app import BrowserDownloadGate

QApplication.instance() or QApplication([])

DUMMY_REQUEST = {
    "url": "https://example.invalid/dummy.bin",
    "filename": "dummy.bin",
    "directory": None,
    "cookies": "sid=dummy",
    "referrer": "https://example.invalid/page",
    "user_agent": "DummyAgent/1.0",
    "file_size": 0,
}


class _FakeQueue:
    def __init__(self, task_id=7):
        self.task_id = task_id
        self.calls = []

    def add_url(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.task_id, Exception):
            raise self.task_id
        return self.task_id


# --- acceptance gate ----------------------------------------------------


def test_gate_accepts_only_after_the_queue_is_ready():
    gate = BrowserDownloadGate()
    queue = _FakeQueue()
    gate.queue = queue

    # aria2 has not finished starting: refuse rather than buffer. The browser
    # keeps its own download and nothing is delivered later.
    assert gate.accept(DUMMY_REQUEST) is False
    assert queue.calls == []

    gate.ready = True
    assert gate.accept(DUMMY_REQUEST) is True
    assert len(queue.calls) == 1


def test_gate_forwards_the_exact_url_and_browser_headers():
    gate = BrowserDownloadGate()
    queue = _FakeQueue()
    gate.queue = queue
    gate.ready = True

    gate.accept(dict(DUMMY_REQUEST, directory="/tmp/dummy-dir"))

    url, kwargs = queue.calls[0]
    assert url == "https://example.invalid/dummy.bin"
    assert kwargs["filename"] == "dummy.bin"
    assert kwargs["out_dir"] == "/tmp/dummy-dir"
    assert kwargs["cookies"] == "sid=dummy"
    assert kwargs["referrer"] == "https://example.invalid/page"
    assert kwargs["user_agent"] == "DummyAgent/1.0"


def test_gate_leaves_routing_to_the_queue_when_no_directory_was_requested():
    """No explicit directory means category/debrid routing still applies."""
    gate = BrowserDownloadGate()
    queue = _FakeQueue()
    gate.queue = queue
    gate.ready = True

    gate.accept(DUMMY_REQUEST)
    assert queue.calls[0][1]["out_dir"] is None


def test_gate_reports_failure_when_the_queue_rejects_the_url():
    gate = BrowserDownloadGate()
    gate.queue = _FakeQueue(task_id=None)
    gate.ready = True
    assert gate.accept(DUMMY_REQUEST) is False


def test_gate_reports_failure_when_the_queue_raises():
    gate = BrowserDownloadGate()
    gate.queue = _FakeQueue(task_id=RuntimeError("dummy queue failure"))
    gate.ready = True
    assert gate.accept(DUMMY_REQUEST) is False


def test_gate_refuses_once_shutdown_has_begun():
    gate = BrowserDownloadGate()
    queue = _FakeQueue()
    gate.queue = queue
    gate.ready = True
    gate.shutting_down = True

    assert gate.accept(DUMMY_REQUEST) is False
    assert queue.calls == []


def test_gate_never_logs_the_url_cookies_or_referrer(caplog):
    gate = BrowserDownloadGate()
    gate.queue = _FakeQueue(task_id=RuntimeError("dummy queue failure"))
    gate.ready = True
    with caplog.at_level("DEBUG"):
        gate.accept(
            {
                "url": "https://example.invalid/?token=dummysecrettoken",
                "filename": "dummysecretname.bin",
                "cookies": "sid=dummysecretcookie",
                "referrer": "https://example.invalid/dummysecretreferrer",
                "user_agent": "DummyAgent/1.0",
            }
        )
    for needle in (
        "dummysecrettoken",
        "dummysecretcookie",
        "dummysecretreferrer",
        "dummysecretname",
    ):
        assert needle not in caplog.text


# --- close-to-tray ------------------------------------------------------


class _FakeTray:
    def __init__(self):
        self.hidden = 0
        self.menu = None

    def hide(self):
        self.hidden += 1

    def setContextMenu(self, menu):
        self.menu = menu


class _FakeSettings:
    def __init__(self, close_to_tray=False):
        self.close_to_tray = close_to_tray


_live_hosts = []


@pytest.fixture(autouse=True)
def _destroy_hosts():
    """Tear every fake window (and its tray menu) down inside the test.

    Left to the interpreter, these QMainWindows are destroyed after the
    QApplication itself during shutdown, which segfaults Qt.
    """
    yield
    while _live_hosts:
        host = _live_hosts.pop()
        host.close()
        # Destroy the C++ object now rather than queueing a deleteLater():
        # a pending deletion is processed during interpreter shutdown, after
        # the QApplication is gone, which segfaults Qt.
        shiboken6.delete(host)
    QApplication.processEvents()


class _Host(QMainWindow):
    """The real MainWindow close/tray methods without its heavy constructor."""

    closeEvent = mw.MainWindow.closeEvent
    discard_torrent_preflights = mw.MainWindow.discard_torrent_preflights
    request_quit = mw.MainWindow.request_quit
    show_from_tray = mw.MainWindow.show_from_tray
    _install_tray_menu = mw.MainWindow._install_tray_menu
    _on_tray_activated = mw.MainWindow._on_tray_activated
    _open_extension_help = mw.MainWindow._open_extension_help

    def __init__(self, close_to_tray=False, tray=True, tray_available=True):
        QMainWindow.__init__(self)
        _live_hosts.append(self)
        self.settings = _FakeSettings(close_to_tray)
        self._tray = _FakeTray() if tray else None
        self._tray_available_flag = tray_available
        self._force_quit = False
        # A real window always has this; closing drains it so a `.torrent`
        # preflight the user never answered cannot leave a copy behind.
        self._torrent_preflights = deque()
        self.quit_calls = 0
        self.super_close_calls = 0

    def _system_tray_available(self):
        return self._tray is not None and self._tray_available_flag


def _close(host):
    """Deliver a real QCloseEvent and report whether it was intercepted."""
    event = QCloseEvent()
    mw.MainWindow.closeEvent(host, event)
    return not event.isAccepted()


def test_close_quits_normally_when_the_setting_is_disabled():
    host = _Host(close_to_tray=False)
    assert _close(host) is False
    assert host.isVisible() is False


def test_close_hides_to_tray_when_enabled_and_a_tray_is_available():
    host = _Host(close_to_tray=True)
    host.show()
    assert _close(host) is True
    assert host.isVisible() is False
    # Hiding is not quitting: nothing was torn down.
    assert host._tray.hidden == 0


def test_close_quits_normally_when_no_tray_is_available():
    """Never hide with no way to get the window back."""
    host = _Host(close_to_tray=True, tray_available=False)
    host.show()
    assert _close(host) is False


def test_close_quits_normally_when_the_tray_icon_was_never_created():
    host = _Host(close_to_tray=True, tray=False)
    host.show()
    assert _close(host) is False


def test_toggling_the_setting_applies_without_a_restart():
    host = _Host(close_to_tray=False)
    host.show()
    assert _close(host) is False

    # Same window object, setting flipped on the shared Settings instance.
    host.settings.close_to_tray = True
    host.show()
    assert _close(host) is True

    host.settings.close_to_tray = False
    host.show()
    assert _close(host) is False


def test_force_quit_bypasses_close_to_tray_interception():
    host = _Host(close_to_tray=True)
    host.show()
    host._force_quit = True
    assert _close(host) is False
    assert host._tray.hidden == 1


def test_request_quit_sets_force_quit_and_hides_the_tray_icon(monkeypatch):
    host = _Host(close_to_tray=True)
    quits = []
    monkeypatch.setattr(
        mw.QApplication, "quit", staticmethod(lambda: quits.append(True))
    )
    mw.MainWindow.request_quit(host)
    assert host._force_quit is True
    assert host._tray.hidden == 1
    assert quits == [True]
    # And a close event arriving during that quit must not hide again.
    assert _close(host) is False


def test_show_from_tray_restores_the_same_window():
    host = _Host(close_to_tray=True)
    host.show()
    _close(host)
    assert host.isVisible() is False

    mw.MainWindow.show_from_tray(host)
    assert host.isVisible() is True


def test_tray_menu_is_installed_once_and_offers_open_and_quit():
    host = _Host(close_to_tray=True)
    mw.MainWindow._install_tray_menu(host)
    first = host._tray.menu
    assert first is not None
    labels = [a.text() for a in first.actions() if not a.isSeparator()]
    assert labels == ["Open Cove", "Get the browser extension", "Quit Cove"]

    # Idempotent: no second controller, no second icon.
    mw.MainWindow._install_tray_menu(host)
    assert host._tray.menu is first


def test_tray_menu_install_is_a_no_op_without_a_tray_icon():
    host = _Host(tray=False)
    mw.MainWindow._install_tray_menu(host)  # must not raise


# --- Settings dialog ----------------------------------------------------


def _settings_dialog(close_to_tray):
    from cove.config import Settings
    from cove.dialogs import SettingsDialog

    settings = Settings()
    settings.close_to_tray = close_to_tray
    dlg = SettingsDialog(settings, None)
    _live_hosts.append(dlg)
    return settings, dlg


def test_settings_checkbox_reflects_the_stored_value(monkeypatch):
    monkeypatch.setattr(
        mw.QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True)
    )
    for stored in (True, False):
        _settings, dlg = _settings_dialog(stored)
        assert dlg.close_to_tray.isChecked() is stored


def test_saving_settings_writes_the_checkbox_value(monkeypatch):
    monkeypatch.setattr(
        mw.QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True)
    )
    settings, dlg = _settings_dialog(False)
    monkeypatch.setattr(type(settings), "save", lambda self: None)
    dlg.close_to_tray.setChecked(True)
    dlg._on_accept()
    assert settings.close_to_tray is True

    dlg.close_to_tray.setChecked(False)
    dlg._on_accept()
    assert settings.close_to_tray is False


def test_settings_disables_the_option_when_no_tray_exists(monkeypatch):
    monkeypatch.setattr(
        "cove.dialogs.QSystemTrayIcon.isSystemTrayAvailable", staticmethod(lambda: False)
    )
    settings, dlg = _settings_dialog(True)
    monkeypatch.setattr(type(settings), "save", lambda self: None)
    assert dlg.close_to_tray.isEnabled() is False
    assert dlg.close_to_tray.isChecked() is False
    # And saving must not leave a setting enabled that cannot be honoured.
    dlg._on_accept()
    assert settings.close_to_tray is False
