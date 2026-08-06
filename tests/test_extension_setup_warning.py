"""Native-host registration failure is visible, not log-only (issue #11, point 5).

Registration failing is the usual root cause of "the extension can't connect",
and it used to reach nothing but the log file. It now reports to the window,
which shows a warning in the Browser extension section with the details behind
a button.
"""
import pytest
import shiboken6
from PySide6.QtWidgets import QApplication, QMainWindow

import cove.app as app_mod
import cove.main_window as mw
from cove.browser_extension import setup_failure_text

QApplication.instance() or QApplication([])


# ---- Reporting off the GUI thread --------------------------------------


def test_successful_registration_reports_nothing(monkeypatch):
    monkeypatch.setattr(app_mod, "install_native_hosts", lambda: ["/somewhere"])
    registration = app_mod.NativeHostRegistration()
    failures = []
    registration.failed.connect(failures.append)

    assert registration.run() is True
    QApplication.processEvents()
    assert failures == []


def test_a_failed_registration_reports_the_reason(monkeypatch):
    def boom():
        raise PermissionError("cannot write /home/u/.mozilla/native-messaging-hosts")

    monkeypatch.setattr(app_mod, "install_native_hosts", boom)
    registration = app_mod.NativeHostRegistration()
    failures = []
    registration.failed.connect(failures.append)

    assert registration.run() is False
    QApplication.processEvents()
    assert len(failures) == 1
    assert "PermissionError" in failures[0]
    assert "native-messaging-hosts" in failures[0]


def test_a_failure_is_still_logged(monkeypatch, caplog):
    def boom():
        raise RuntimeError("dummy failure")

    monkeypatch.setattr(app_mod, "install_native_hosts", boom)
    registration = app_mod.NativeHostRegistration()
    with caplog.at_level("WARNING", logger="cove"):
        registration.run()
    assert any("registration failed" in r.message for r in caplog.records)


def test_the_failure_text_names_the_error():
    text = setup_failure_text(OSError("disk is read-only"))
    assert "OSError" in text
    assert "disk is read-only" in text


# ---- Window surface ----------------------------------------------------

class _FakeSettings:
    extension_prompt_shown = False

    def save(self):
        pass


_live_hosts = []


@pytest.fixture(autouse=True)
def _destroy_hosts():
    yield
    while _live_hosts:
        host = _live_hosts.pop()
        host.close()
        shiboken6.delete(host)
    QApplication.processEvents()


class _FakeBox:
    instances = []
    Warning = "warning"

    def __init__(self, parent=None):
        self.text = ""
        self.detailed = ""
        self.title = ""
        self.executed = False
        _FakeBox.instances.append(self)

    def setWindowTitle(self, title):
        self.title = title

    def setText(self, text):
        self.text = text

    def setDetailedText(self, text):
        self.detailed = text

    def setIcon(self, icon):
        pass

    def exec(self):
        self.executed = True
        return 0


class _Host(QMainWindow):
    _build_extension_section = mw.MainWindow._build_extension_section
    _build_extension_banner = mw.MainWindow._build_extension_banner
    _dismiss_extension_banner = mw.MainWindow._dismiss_extension_banner
    _set_extension_state = mw.MainWindow._set_extension_state
    note_extension_seen = mw.MainWindow.note_extension_seen
    note_extension_setup_failed = mw.MainWindow.note_extension_setup_failed
    _show_extension_setup_details = mw.MainWindow._show_extension_setup_details
    _open_extension_help = mw.MainWindow._open_extension_help

    def __init__(self):
        QMainWindow.__init__(self)
        _live_hosts.append(self)
        self._extension_seen = False
        self._extension_setup_error = ""
        # The real window always has both; note_extension_seen retires the
        # banner, so a host without one would not exercise the real path.
        self.settings = _FakeSettings()
        self.section = self._build_extension_section()
        self.banner = self._build_extension_banner()


def test_no_warning_is_shown_when_registration_worked():
    host = _Host()
    assert host.extension_problem.isVisibleTo(host.section) is False


def test_a_failure_shows_the_warning():
    host = _Host()
    host.note_extension_setup_failed("OSError: disk is read-only")
    assert host.extension_problem.isVisibleTo(host.section) is True


def test_the_details_button_shows_the_reported_reason(monkeypatch):
    _FakeBox.instances = []
    monkeypatch.setattr(mw, "QMessageBox", _FakeBox)
    host = _Host()
    host.note_extension_setup_failed("OSError: disk is read-only")
    host._show_extension_setup_details()

    box = _FakeBox.instances[-1]
    assert box.executed
    assert "OSError: disk is read-only" in box.detailed


def test_the_presence_indicator_is_independent_of_the_warning():
    """A failed registration on one browser does not mean nothing connected."""
    host = _Host()
    host.note_extension_setup_failed("OSError: disk is read-only")
    host.note_extension_seen()
    assert host.extension_pill.text() == "CONNECTED"
    assert host.extension_problem.isVisibleTo(host.section) is True
