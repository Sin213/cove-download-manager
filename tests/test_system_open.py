"""AppImage env scrubbing for external program launches.

Regression test for the bug where "Open File" / "Open Folder" launched
dolphin/mpv with the AppImage's LD_LIBRARY_PATH, so they loaded the
bundle's outdated liblzma and crashed with `version XZ_5.4 not found`.
"""

import os
import pytest
from unittest import mock
from types import SimpleNamespace

from cove.system_open import child_env, _SCRUB_VARS


@pytest.mark.parametrize("appimage", [False, True])
def test_open_url_linux_uses_detached_xdg_open(monkeypatch, appimage):
    from cove import system_open
    from PySide6.QtGui import QDesktopServices
    monkeypatch.delenv("APPDIR", raising=False)
    monkeypatch.delenv("APPIMAGE", raising=False)
    if appimage:
        monkeypatch.setenv("APPIMAGE", "/bundle/Cove.AppImage")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/bundle/lib")
    monkeypatch.setattr(system_open.sys, "platform", "linux")
    monkeypatch.setattr(system_open.shutil, "which", lambda name: "/usr/bin/xdg-open")
    url = "https://example.invalid/neutral"
    with mock.patch.object(system_open.subprocess, "Popen") as popen, \
            mock.patch.object(QDesktopServices, "openUrl") as desktop:
        assert system_open.open_url(url) is None
    assert popen.call_count == 1
    args, kwargs = popen.call_args
    assert args == (["xdg-open", url],)
    assert kwargs == {"env": child_env(), "stdin": system_open.subprocess.DEVNULL,
                      "stdout": system_open.subprocess.DEVNULL,
                      "stderr": system_open.subprocess.DEVNULL, "start_new_session": True}
    if appimage:
        assert "LD_LIBRARY_PATH" not in kwargs["env"]
    else:
        assert kwargs["env"] is None
    desktop.assert_not_called()


@pytest.mark.parametrize("platform,available", [("linux", False), ("darwin", True), ("win32", True)])
def test_open_url_desktop_fallback(monkeypatch, platform, available):
    from cove import system_open
    from PySide6.QtGui import QDesktopServices
    monkeypatch.setattr(system_open.sys, "platform", platform)
    monkeypatch.setattr(system_open.shutil, "which", lambda name: "/usr/bin/xdg-open" if available else None)
    with mock.patch.object(system_open.subprocess, "Popen") as popen, \
            mock.patch.object(QDesktopServices, "openUrl", return_value=True) as desktop:
        assert system_open.open_url("https://example.invalid/neutral") is None
    desktop.assert_called_once()
    assert desktop.call_args.args[0].toString() == "https://example.invalid/neutral"
    popen.assert_not_called()


def _clicked_message_box(label):
    class Box:
        ActionRole = RejectRole = AcceptRole = HelpRole = Information = 0

        def __init__(self, parent):
            self.chosen = None

        def setWindowTitle(self, text): pass
        def setText(self, text): pass
        def setIcon(self, icon): pass
        def setInformativeText(self, text): pass

        def addButton(self, text, role):
            button = object()
            if text == label:
                self.chosen = button
            return button

        def exec(self):
            assert self.chosen is not None

        def clickedButton(self):
            return self.chosen

    return Box


def _source_linux(monkeypatch):
    from cove import system_open
    monkeypatch.delenv("APPDIR", raising=False)
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.setattr(system_open.sys, "platform", "linux")
    monkeypatch.setattr(system_open.shutil, "which", lambda name: "/usr/bin/xdg-open")
    return system_open


@pytest.mark.parametrize("store", ["firefox", "chrome"])
def test_extension_help_uses_shared_linux_url_launcher(monkeypatch, store):
    from cove import main_window
    from PySide6.QtGui import QDesktopServices
    system_open = _source_linux(monkeypatch)
    assert main_window.open_url is system_open.open_url
    label, url = (("Firefox add-on", main_window.FIREFOX_EXTENSION_URL) if store == "firefox" else
                  ("Chrome Web Store", main_window.CHROME_EXTENSION_URL))
    monkeypatch.setattr(main_window, "QMessageBox", _clicked_message_box(label))
    with mock.patch.object(system_open.subprocess, "Popen") as popen, \
            mock.patch.object(QDesktopServices, "openUrl") as desktop:
        main_window.MainWindow._open_extension_help(object())
    popen.assert_called_once_with(["xdg-open", url], env=None,
                                  stdin=system_open.subprocess.DEVNULL,
                                  stdout=system_open.subprocess.DEVNULL,
                                  stderr=system_open.subprocess.DEVNULL, start_new_session=True)
    desktop.assert_not_called()


def test_updater_release_prompt_uses_shared_linux_url_launcher(monkeypatch):
    from cove import updater
    from PySide6.QtGui import QDesktopServices
    system_open = _source_linux(monkeypatch)
    assert updater._open_url is updater.open_url is system_open.open_url
    monkeypatch.setattr(updater, "QMessageBox", _clicked_message_box("View release"))
    monkeypatch.setattr(updater, "bundle_kind", lambda: "source")
    controller = SimpleNamespace(_parent=None, _display_name="Cove", _current="1.0")
    info = updater.UpdateInfo("2.0", "https://github.com/example/cove/releases/tag/v2.0")
    with mock.patch.object(system_open.subprocess, "Popen") as popen, \
            mock.patch.object(QDesktopServices, "openUrl") as desktop:
        updater.UpdateController._prompt(controller, info)
    popen.assert_called_once_with(["xdg-open", info.release_url], env=None,
                                  stdin=system_open.subprocess.DEVNULL,
                                  stdout=system_open.subprocess.DEVNULL,
                                  stderr=system_open.subprocess.DEVNULL, start_new_session=True)
    desktop.assert_not_called()


def test_child_env_is_none_outside_appimage(monkeypatch):
    monkeypatch.delenv("APPDIR", raising=False)
    monkeypatch.delenv("APPIMAGE", raising=False)
    assert child_env() is None


def test_child_env_strips_loader_vars_inside_appimage(monkeypatch):
    monkeypatch.setenv("APPDIR", "/tmp/.mount_CoveXXXX")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/.mount_CoveXXXX/usr/lib:")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = child_env()
    assert env is not None
    assert "LD_LIBRARY_PATH" not in env
    for key in _SCRUB_VARS:
        assert key not in env
    assert env["PATH"] == "/usr/bin"
    # os.environ itself must be untouched (aria2c still needs bundle libs)
    assert os.environ["LD_LIBRARY_PATH"] == "/tmp/.mount_CoveXXXX/usr/lib:"


def test_child_env_triggers_on_appimage_var_alone(monkeypatch):
    monkeypatch.delenv("APPDIR", raising=False)
    monkeypatch.setenv("APPIMAGE", "/x/Cove.AppImage")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/x/usr/lib")
    env = child_env()
    assert env is not None and "LD_LIBRARY_PATH" not in env


def test_open_path_spawns_xdg_open_with_clean_env(monkeypatch, tmp_path):
    from cove import main_window

    monkeypatch.setenv("APPDIR", "/tmp/.mount_CoveXXXX")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/.mount_CoveXXXX/usr/lib:")
    monkeypatch.setattr(main_window.sys, "platform", "linux")
    monkeypatch.setattr(main_window.os, "name", "posix")
    monkeypatch.setattr(main_window.shutil, "which", lambda _: "/usr/bin/xdg-open")
    with mock.patch.object(main_window.subprocess, "Popen") as popen:
        assert main_window._open_path(tmp_path) is True
    args, kwargs = popen.call_args
    assert args[0][0] == "xdg-open"
    assert kwargs["env"] is not None
    assert "LD_LIBRARY_PATH" not in kwargs["env"]


def test_reveal_in_folder_spawns_with_clean_env(monkeypatch, tmp_path):
    from cove import main_window

    monkeypatch.setenv("APPDIR", "/tmp/.mount_CoveXXXX")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/.mount_CoveXXXX/usr/lib:")
    monkeypatch.setattr(main_window.sys, "platform", "linux")
    monkeypatch.setattr(main_window.os, "name", "posix")
    monkeypatch.setattr(main_window.shutil, "which", lambda _: "/usr/bin/xdg-open")
    f = tmp_path / "file.bin"
    f.write_bytes(b"x")
    with mock.patch.object(main_window.subprocess, "Popen") as popen:
        assert main_window._reveal_in_folder(f) is True
    args, kwargs = popen.call_args
    assert args[0] == ["xdg-open", str(tmp_path)]
    assert "LD_LIBRARY_PATH" not in kwargs["env"]
