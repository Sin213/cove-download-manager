"""In-app discovery of the browser extension (issue #11).

Nothing in the app used to say that an extension exists, so a new user had no
in-app path to it. These tests pin the two surfaces that now say so: the
empty-state hint and a permanent "Get the browser extension" entry.

MainWindow's constructor is heavy, so the real unbound methods are driven
against a light host object, matching tests/test_close_to_tray.py.
"""
import pytest
import shiboken6
from PySide6.QtWidgets import QApplication, QMainWindow

import cove.main_window as mw
from cove.browser_extension import (
    CHROME_EXTENSION_URL,
    FIREFOX_EXTENSION_URL,
    EXTENSION_HELP_TEXT,
)

QApplication.instance() or QApplication([])


class _FakeTray:
    def __init__(self):
        self.menu = None

    def setContextMenu(self, menu):
        self.menu = menu


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
    _install_tray_menu = mw.MainWindow._install_tray_menu
    _on_tray_activated = mw.MainWindow._on_tray_activated
    _open_extension_help = mw.MainWindow._open_extension_help
    show_from_tray = mw.MainWindow.show_from_tray
    request_quit = mw.MainWindow.request_quit

    def __init__(self):
        QMainWindow.__init__(self)
        _live_hosts.append(self)
        self._tray = _FakeTray()
        self._tray_menu = None
        self._force_quit = False


class _FakeBox:
    """Stands in for QMessageBox; `picked` names the button to report."""

    picked = None
    instances = []
    ActionRole = "action"
    RejectRole = "reject"

    def __init__(self, parent=None):
        self.parent = parent
        self.title = ""
        self.text = ""
        self.buttons = {}
        self.executed = False
        _FakeBox.instances.append(self)

    def setWindowTitle(self, title):
        self.title = title

    def setText(self, text):
        self.text = text

    def addButton(self, label, role):
        button = object()
        self.buttons[label] = button
        return button

    def exec(self):
        self.executed = True
        return 0

    def clickedButton(self):
        return self.buttons.get(_FakeBox.picked)


@pytest.fixture
def help_box(monkeypatch):
    _FakeBox.instances = []
    _FakeBox.picked = None
    monkeypatch.setattr(mw, "QMessageBox", _FakeBox)
    opened = []
    monkeypatch.setattr(mw, "open_url", lambda url: opened.append(url))
    return opened


def _menu_labels(menu):
    return [action.text() for action in menu.actions()]


def test_the_empty_state_points_at_the_browser_extension():
    tree = mw.DownloadTree()
    try:
        assert "extension" in tree._empty_sub.lower()
    finally:
        shiboken6.delete(tree)


def test_the_empty_state_still_explains_manual_adds():
    tree = mw.DownloadTree()
    try:
        assert "Ctrl+N" in tree._empty_sub
    finally:
        shiboken6.delete(tree)


def test_the_tray_menu_offers_the_extension_permanently():
    host = _Host()
    host._install_tray_menu()
    assert "Get the browser extension" in _menu_labels(host._tray.menu)


def test_the_help_dialog_names_both_stores(help_box):
    host = _Host()
    host._open_extension_help()
    box = _FakeBox.instances[-1]
    assert box.executed
    assert box.text == EXTENSION_HELP_TEXT
    assert "Firefox add-on" in box.buttons
    assert "Chrome Web Store" in box.buttons


def test_choosing_firefox_opens_the_amo_listing(help_box):
    host = _Host()
    _FakeBox.picked = "Firefox add-on"
    host._open_extension_help()
    assert help_box == [FIREFOX_EXTENSION_URL]


def test_choosing_chrome_opens_the_web_store_listing(help_box):
    host = _Host()
    _FakeBox.picked = "Chrome Web Store"
    host._open_extension_help()
    assert help_box == [CHROME_EXTENSION_URL]


def test_closing_the_dialog_opens_nothing(help_box):
    host = _Host()
    _FakeBox.picked = "Close"
    host._open_extension_help()
    assert help_box == []


def test_the_store_urls_are_the_published_listings():
    assert FIREFOX_EXTENSION_URL.startswith("https://addons.mozilla.org/")
    assert CHROME_EXTENSION_URL.startswith("https://chromewebstore.google.com/")
