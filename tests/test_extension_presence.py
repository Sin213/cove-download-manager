"""Live "is the extension talking to us?" indicator (issue #11, point 4).

The native host answers `ping` entirely on its own, so the running GUI used to
have no way of knowing an extension existed until a download arrived. A ping
now also notifies the primary over the existing IPC socket, and the window
reports what it has heard.

The IPC half mirrors tests/test_single_instance.py: the client runs in a
subprocess while this process pumps the Qt loop for the in-process server.
"""
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication, QMainWindow

import cove.main_window as mw
import cove.native_messaging as nm
from cove.single_instance import SingleInstanceServer

REPO_ROOT = Path(__file__).resolve().parent.parent
QApplication.instance() or QApplication([])

BROWSER_URL = "https://example.invalid/dummy.bin"


def _unique_name() -> str:
    return f"cove-ext-{uuid.uuid4().hex[:12]}"


def _ping_in_subprocess(name: str, timeout: float = 5.0) -> bool:
    app = QCoreApplication.instance()
    script = (
        "from cove.single_instance import send_extension_ping\n"
        f"ok = send_extension_ping({name!r})\n"
        'print("OK" if ok else "FAIL")\n'
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + timeout
    while proc.poll() is None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    out, _ = proc.communicate(timeout=2)
    return out.strip() == "OK"


def _browser_download_in_subprocess(name: str, timeout: float = 5.0) -> bool:
    app = QCoreApplication.instance()
    payload = {"url": BROWSER_URL}
    script = (
        "from cove.single_instance import send_browser_download\n"
        f"ok = send_browser_download({name!r}, {payload!r})\n"
        'print("OK" if ok else "FAIL")\n'
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + timeout
    while proc.poll() is None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    out, _ = proc.communicate(timeout=2)
    return out.strip() == "OK"


# ---- IPC ---------------------------------------------------------------


def test_a_ping_reaches_the_running_primary():
    name = _unique_name()
    server = SingleInstanceServer()
    seen = []
    try:
        assert server.try_become_primary(name) is True
        server.extension_seen.connect(lambda: seen.append(True))
        assert _ping_in_subprocess(name) is True
        assert seen == [True]
    finally:
        server.shutdown()


def test_a_ping_without_a_primary_is_not_an_error():
    assert _ping_in_subprocess(_unique_name()) is False


def test_a_ping_never_activates_the_window():
    """A background browser heartbeat must not raise Cove to the front."""
    name = _unique_name()
    server = SingleInstanceServer()
    activates = []
    opens = []
    try:
        assert server.try_become_primary(name) is True
        server.activate_requested.connect(lambda: activates.append(True))
        server.open_requested.connect(lambda urls: opens.append(urls))
        assert _ping_in_subprocess(name) is True
        assert activates == []
        assert opens == []
    finally:
        server.shutdown()


def test_an_accepted_download_also_counts_as_a_sighting():
    name = _unique_name()
    server = SingleInstanceServer()
    seen = []
    try:
        assert server.try_become_primary(name) is True
        server.browser_download_handler = lambda req: True
        server.extension_seen.connect(lambda: seen.append(True))
        assert _browser_download_in_subprocess(name) is True
        assert seen == [True]
    finally:
        server.shutdown()


def test_a_rejected_download_is_not_reported_as_connected():
    """A refused download says nothing the indicator should claim: the ping
    path is what proves presence, and this keeps the two consistent."""
    name = _unique_name()
    server = SingleInstanceServer()
    seen = []
    try:
        assert server.try_become_primary(name) is True
        server.browser_download_handler = lambda req: False
        server.extension_seen.connect(lambda: seen.append(True))
        assert _browser_download_in_subprocess(name) is False
        assert seen == []
    finally:
        server.shutdown()


def _raw_send_in_subprocess(name: str, message: dict, timeout: float = 5.0) -> bool:
    """Send an arbitrary IPC message, bypassing the typed client helpers."""
    app = QCoreApplication.instance()
    script = (
        "from cove.single_instance import _request\n"
        f"ok = _request({name!r}, {message!r}, 1500, 1500)\n"
        'print("OK" if ok else "FAIL")\n'
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + timeout
    while proc.poll() is None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    out, _ = proc.communicate(timeout=2)
    return out.strip() == "OK"


@pytest.mark.parametrize(
    "extra",
    [
        {"urls": ["https://example.invalid/x"]},
        {"url": BROWSER_URL},
        {"directory": "/tmp"},
    ],
)
def test_a_ping_carrying_a_payload_is_rejected(extra):
    """The ping is payload-free; anything else is a different message."""
    from cove.single_instance import EXTENSION_PING_ACTION, PROTOCOL_VERSION

    name = _unique_name()
    server = SingleInstanceServer()
    seen = []
    try:
        assert server.try_become_primary(name) is True
        server.extension_seen.connect(lambda: seen.append(True))
        message = {
            "version": PROTOCOL_VERSION,
            "action": EXTENSION_PING_ACTION,
            **extra,
        }
        assert _raw_send_in_subprocess(name, message) is False
        assert seen == []
    finally:
        server.shutdown()


# ---- Native host -------------------------------------------------------


def test_the_host_tells_the_primary_about_a_ping(monkeypatch):
    calls = []
    monkeypatch.setattr(nm, "notify_primary_extension_seen", lambda: calls.append(True))
    reply = nm.handle_message({"action": "ping"}, None, None)
    assert reply["status"] == "ok"
    assert calls == [True]


def test_a_ping_still_succeeds_when_no_cove_is_running(monkeypatch):
    """The host answers for itself; the notification is best effort."""

    def boom():
        raise RuntimeError("no primary")

    monkeypatch.setattr(nm, "notify_primary_extension_seen", boom)
    assert nm.handle_message({"action": "ping"}, None, None)["status"] == "ok"


# ---- Window indicator --------------------------------------------------

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


class _Host(QMainWindow):
    """The real indicator methods without MainWindow's heavy constructor."""

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


def test_the_indicator_starts_as_not_detected():
    host = _Host()
    assert host.extension_pill.text() == "NOT DETECTED"
    assert host.extension_pill.property("state") == "off"


def test_a_sighting_flips_the_indicator_to_connected():
    host = _Host()
    host.note_extension_seen()
    assert host.extension_pill.text() == "CONNECTED"
    assert host.extension_pill.property("state") == "ok"


def test_repeated_sightings_are_harmless():
    host = _Host()
    host.note_extension_seen()
    host.note_extension_seen()
    assert host.extension_pill.text() == "CONNECTED"
