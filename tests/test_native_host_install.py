"""Tests for native messaging host registration across platforms.

Covers the Windows path (registry + .bat launcher) and the Chromium
registration (allowed_origins manifest + per-browser registry keys / config
dirs), which differ from the Firefox path (allowed_extensions).
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from cove import native_host_install as nhi


def test_windows_command_parts_frozen():
    with patch.object(sys, "frozen", True, create=True), \
         patch.object(sys, "executable", r"C:\App\cove-download-manager.exe"):
        parts = nhi._windows_command_parts()
    assert parts == [r"C:\App\cove-download-manager.exe", "--native-messaging"]


def test_windows_command_parts_dev():
    with patch.object(sys, "frozen", False, create=True), \
         patch.object(sys, "executable", r"C:\Py\python.exe"):
        parts = nhi._windows_command_parts()
    assert parts == [r"C:\Py\python.exe", "-m", "cove", "--native-messaging"]


def test_windows_launcher_injects_flag_and_forwards_args():
    bat = nhi._windows_launcher([r"C:\App\cove.exe", "--native-messaging"])
    assert "--native-messaging" in bat
    # Forwards Firefox's manifest-path / extension-id args to the host.
    assert bat.rstrip().endswith("%*")
    assert "@echo off" in bat
    # Windows batch files need CRLF line endings.
    assert "\r\n" in bat


def test_manifest_fields():
    m = nhi._manifest(r"C:\hosts\cove_download_manager.bat")
    assert m["name"] == nhi.HOST_NAME
    assert m["type"] == "stdio"
    assert m["path"] == r"C:\hosts\cove_download_manager.bat"
    assert nhi.EXTENSION_ID in m["allowed_extensions"]
    assert "allowed_origins" not in m


def test_chrome_manifest_fields():
    m = nhi._chrome_manifest(r"C:\hosts\cove_download_manager.bat")
    assert m["name"] == nhi.HOST_NAME
    assert m["type"] == "stdio"
    assert m["path"] == r"C:\hosts\cove_download_manager.bat"
    # Chromium uses allowed_origins with chrome-extension:// URLs, not ids.
    assert "allowed_extensions" not in m
    for ext_id in nhi._CHROME_EXTENSION_IDS:
        assert f"chrome-extension://{ext_id}/" in m["allowed_origins"]


def test_install_windows_writes_both_manifests_and_all_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    fake_winreg = MagicMock()
    fake_winreg.HKEY_CURRENT_USER = 0
    fake_winreg.REG_SZ = 1

    with patch.dict(sys.modules, {"winreg": fake_winreg}), \
         patch.object(sys, "frozen", True, create=True), \
         patch.object(sys, "executable", r"C:\App\cove-download-manager.exe"):
        installed = nhi._install_windows()

    hosts_dir = tmp_path / "Cove" / "native-messaging-hosts"
    ff_manifest = hosts_dir / "cove_download_manager.json"
    chrome_manifest = hosts_dir / "cove_download_manager.chrome.json"
    launcher = hosts_dir / "cove_download_manager.bat"

    assert ff_manifest.is_file()
    assert chrome_manifest.is_file()
    assert launcher.is_file()
    assert str(hosts_dir) in installed

    # Firefox manifest -> allowed_extensions; Chrome manifest -> allowed_origins.
    assert "allowed_extensions" in json.loads(ff_manifest.read_text())
    assert "allowed_origins" in json.loads(chrome_manifest.read_text())

    bat = launcher.read_text()
    assert "cove-download-manager.exe" in bat
    assert "--native-messaging" in bat

    # Every registered key: Mozilla + each Chromium browser.
    created = [c.args[1] for c in fake_winreg.CreateKeyEx.call_args_list]
    expected = [f"{nhi._WIN_REGISTRY_KEY}\\{nhi.HOST_NAME}"] + [
        f"{k}\\{nhi.HOST_NAME}" for k in nhi._WIN_CHROMIUM_REGISTRY_KEYS
    ]
    assert created == expected

    # Mozilla key -> firefox manifest; chromium keys -> chrome manifest.
    set_values = [c.args[-1] for c in fake_winreg.SetValueEx.call_args_list]
    assert set_values[0] == str(ff_manifest)
    assert set_values[1:] == [str(chrome_manifest)] * len(
        nhi._WIN_CHROMIUM_REGISTRY_KEYS
    )


def test_install_posix_writes_chromium_manifests(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    # Simulate Chrome + Brave installed (config dirs exist), Firefox absent.
    (tmp_path / ".config" / "google-chrome").mkdir(parents=True)
    (tmp_path / ".config" / "BraveSoftware" / "Brave-Browser").mkdir(parents=True)

    installed = nhi._install_posix()

    chrome_dir = tmp_path / ".config" / "google-chrome" / "NativeMessagingHosts"
    brave_dir = (
        tmp_path / ".config" / "BraveSoftware" / "Brave-Browser"
        / "NativeMessagingHosts"
    )
    for d in (chrome_dir, brave_dir):
        manifest = d / "cove_download_manager.json"
        assert manifest.is_file()
        assert "allowed_origins" in json.loads(manifest.read_text())
        assert str(d) in installed

    # Firefox dir not created because ~/.mozilla doesn't exist.
    assert not (tmp_path / ".mozilla").exists()


def test_install_dispatch_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    with patch.object(nhi, "_install_windows", return_value=["WIN"]) as win, \
         patch.object(nhi, "_install_posix", return_value=["POSIX"]) as posix:
        assert nhi.install_native_hosts() == ["WIN"]
    win.assert_called_once()
    posix.assert_not_called()


def test_install_dispatch_posix(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with patch.object(nhi, "_install_windows", return_value=["WIN"]) as win, \
         patch.object(nhi, "_install_posix", return_value=["POSIX"]) as posix:
        assert nhi.install_native_hosts() == ["POSIX"]
    posix.assert_called_once()
    win.assert_not_called()


# --- Flatpak-only browsers --------------------------------------------------


def test_flatpak_only_firefox_gets_a_manifest_inside_its_sandbox_home(
    tmp_path, monkeypatch
):
    """A Flatpak browser with no conventional config dir was skipped entirely.

    Flatpak targets were only ever reached through the override step, which
    runs over directories discovered from ~/.mozilla and ~/.config. With
    neither present there was nothing to override and no manifest anywhere, so
    browser integration silently could not be installed.
    """
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(nhi.shutil, "which", lambda _n: None)
    (tmp_path / ".var" / "app" / "org.mozilla.firefox").mkdir(parents=True)

    installed = nhi._install_posix()

    hosts = (
        tmp_path / ".var" / "app" / "org.mozilla.firefox"
        / ".mozilla" / "native-messaging-hosts"
    )
    manifest = hosts / "cove_download_manager.json"
    assert manifest.is_file()
    assert "allowed_extensions" in json.loads(manifest.read_text())
    assert str(hosts) in installed


def test_flatpak_only_chromium_gets_the_chrome_shaped_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(nhi.shutil, "which", lambda _n: None)
    (tmp_path / ".var" / "app" / "com.brave.Browser").mkdir(parents=True)

    installed = nhi._install_posix()

    hosts = (
        tmp_path / ".var" / "app" / "com.brave.Browser" / "config"
        / "BraveSoftware" / "Brave-Browser" / "NativeMessagingHosts"
    )
    manifest = hosts / "cove_download_manager.json"
    assert manifest.is_file()
    assert "allowed_origins" in json.loads(manifest.read_text())
    assert str(hosts) in installed


def test_an_unknown_flatpak_app_is_never_written_to(tmp_path, monkeypatch):
    """Only browsers Cove recognises; ~/.var/app holds every Flatpak app."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(nhi.shutil, "which", lambda _n: None)
    (tmp_path / ".var" / "app" / "org.libreoffice.LibreOffice").mkdir(parents=True)

    installed = nhi._install_posix()

    assert installed == []
    assert list((tmp_path / ".var" / "app" / "org.libreoffice.LibreOffice").iterdir()) == []
