"""First-run "you need the extension" banner (issue #11, point 2).

The tray entry and the empty-state line only help a user who goes looking.
The banner puts the two store links in front of a new user once, and then
stays out of the way for good.

MainWindow's constructor is heavy, so the real unbound methods are driven
against a light host object, matching tests/test_close_to_tray.py.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import shiboken6
from PySide6.QtWidgets import QApplication, QMainWindow

import cove.main_window as mw
from cove.browser_extension import CHROME_EXTENSION_URL, FIREFOX_EXTENSION_URL
from cove.config import Settings

QApplication.instance() or QApplication([])

_live_hosts = []


@pytest.fixture(autouse=True)
def _destroy_hosts():
    yield
    while _live_hosts:
        host = _live_hosts.pop()
        host.close()
        shiboken6.delete(host)
    QApplication.processEvents()


class _FakeSettings:
    def __init__(self, prompt_shown=False):
        self.extension_prompt_shown = prompt_shown
        self.saves = 0

    def save(self):
        self.saves += 1


class _Host(QMainWindow):
    _build_extension_banner = mw.MainWindow._build_extension_banner
    _build_extension_section = mw.MainWindow._build_extension_section
    _set_extension_state = mw.MainWindow._set_extension_state
    _dismiss_extension_banner = mw.MainWindow._dismiss_extension_banner
    _open_extension_store = mw.MainWindow._open_extension_store
    note_extension_seen = mw.MainWindow.note_extension_seen
    note_extension_setup_failed = mw.MainWindow.note_extension_setup_failed
    _show_extension_setup_details = mw.MainWindow._show_extension_setup_details
    _open_extension_help = mw.MainWindow._open_extension_help

    def __init__(self, prompt_shown=False):
        QMainWindow.__init__(self)
        _live_hosts.append(self)
        self.settings = _FakeSettings(prompt_shown)
        self._extension_seen = False
        self._extension_setup_error = ""
        self.section = self._build_extension_section()
        self.banner = self._build_extension_banner()


@pytest.fixture
def opened(monkeypatch):
    urls = []
    monkeypatch.setattr(mw, "open_url", lambda url: urls.append(url))
    return urls


# ---- Setting -----------------------------------------------------------


def test_the_prompt_defaults_to_unshown():
    assert Settings().extension_prompt_shown is False


def _load_prompt_shown(tmp_path, raw: dict):
    """Load settings.json in a subprocess, as tests/test_config.py does."""
    script = r'''
import json, sys
from pathlib import Path
import cove.config as config

tmp = Path(sys.argv[1])
config.CONFIG_DIR = tmp
config.DATA_DIR = tmp
config.CONFIG_FILE = tmp / "settings.json"
config.CONFIG_FILE.write_text(sys.argv[2])

from cove.config import Settings
s = Settings.load()
print(json.dumps({"extension_prompt_shown": s.extension_prompt_shown}))
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), json.dumps(raw)],
        cwd=Path(__file__).resolve().parents[1],
        env=dict(os.environ),
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)["extension_prompt_shown"]


def test_the_prompt_flag_round_trips(tmp_path):
    assert _load_prompt_shown(tmp_path, {"extension_prompt_shown": True}) is True


@pytest.mark.parametrize("value", ["yes", 1, [], None])
def test_a_hand_edited_non_boolean_is_not_read_as_shown(tmp_path, value):
    """Same rule as close_to_tray: truthiness must not silence the prompt."""
    assert _load_prompt_shown(tmp_path, {"extension_prompt_shown": value}) is False


# ---- Banner ------------------------------------------------------------


def test_the_banner_is_shown_on_a_first_run():
    host = _Host(prompt_shown=False)
    assert host.banner.isVisibleTo(host) is True


def test_the_banner_stays_down_once_it_has_been_dismissed():
    host = _Host(prompt_shown=True)
    assert host.banner.isVisibleTo(host) is False


def test_dismissing_hides_it_and_remembers():
    host = _Host(prompt_shown=False)
    host._dismiss_extension_banner()
    assert host.banner.isVisibleTo(host) is False
    assert host.settings.extension_prompt_shown is True
    assert host.settings.saves == 1


def test_a_connected_extension_retires_the_banner():
    """Nothing to prompt for once the extension is demonstrably working."""
    host = _Host(prompt_shown=False)
    host.note_extension_seen()
    assert host.banner.isVisibleTo(host) is False
    assert host.settings.extension_prompt_shown is True


def test_the_firefox_button_opens_the_amo_listing(opened):
    host = _Host()
    host._open_extension_store(FIREFOX_EXTENSION_URL)
    assert opened == [FIREFOX_EXTENSION_URL]


def test_the_chrome_button_opens_the_web_store_listing(opened):
    host = _Host()
    host._open_extension_store(CHROME_EXTENSION_URL)
    assert opened == [CHROME_EXTENSION_URL]


def test_choosing_a_store_also_retires_the_banner(opened):
    """The user acted on it; showing it again next launch would be nagging."""
    host = _Host()
    host._open_extension_store(FIREFOX_EXTENSION_URL)
    assert host.banner.isVisibleTo(host) is False
    assert host.settings.extension_prompt_shown is True


def test_a_failed_save_does_not_keep_the_banner_up(monkeypatch):
    """A read-only settings file is not a reason to nag on every action."""
    host = _Host()

    def boom():
        raise OSError("settings.json is read-only")

    monkeypatch.setattr(host.settings, "save", boom)
    host._dismiss_extension_banner()
    assert host.banner.isVisibleTo(host) is False
