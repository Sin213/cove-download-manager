"""The facade the GUI talks to. Both backends are stubbed."""
import pytest

import cove.magnet_identity as mi
from cove import magnet_handler as mh
from cove import magnet_linux
from cove import magnet_win
from tests.conftest import FakeWinreg


@pytest.fixture
def linux_appimage(monkeypatch, tmp_path):
    """An AppImage run with an isolated applications directory."""
    state = {"default": "", "entry_dir": tmp_path, "calls": []}

    def fake_run(argv):
        state["calls"].append(list(argv))
        if argv[:3] == ["xdg-mime", "query", "default"]:
            return 0, state["default"] + "\n"
        if argv[:2] == ["xdg-mime", "default"]:
            state["default"] = argv[2]
            return 0, ""
        return 0, ""

    monkeypatch.setattr(mh, "_identity", lambda: mi.APPIMAGE)
    monkeypatch.setattr(mh, "_registration_path", lambda: "/opt/Cove-3.2.0.AppImage")
    monkeypatch.setattr(mh, "_run", fake_run)
    monkeypatch.setattr(mh, "_apps_dir", lambda: tmp_path)
    return state


@pytest.fixture
def windows_portable(monkeypatch):
    """A Windows portable build with an in-memory fake registry."""
    reg = FakeWinreg()

    monkeypatch.setattr(mh, "_identity", lambda: mi.WINDOWS_PORTABLE)
    monkeypatch.setattr(mh, "_registration_path", lambda: r"D:\Portable\Cove.exe")
    monkeypatch.setattr(mh, "_winreg", lambda: reg)
    return reg


def test_unsupported_build_refuses_everything(monkeypatch):
    monkeypatch.setattr(mh, "_identity", lambda: mi.UNSUPPORTED)
    monkeypatch.setattr(mh, "_registration_path", lambda: "")

    assert mh.status().supported is False
    assert mh.enable().ok is False
    assert mh.repair() is False


def test_enable_on_linux_sets_and_verifies_the_default(linux_appimage):
    result = mh.enable()
    assert result.ok is True
    assert mh.status().is_default is True
    assert linux_appimage["default"] == "cove-download-manager-appimage.desktop"


def test_enable_reports_honestly_when_the_desktop_declines(linux_appimage, monkeypatch):
    def stubborn(argv):
        if argv[:3] == ["xdg-mime", "query", "default"]:
            return 0, "org.qbittorrent.qBittorrent.desktop\n"
        return 0, ""

    monkeypatch.setattr(mh, "_run", stubborn)
    result = mh.enable()
    assert result.ok is False
    assert "did not make it the default" in result.message
    # The entry is still installed even though the default did not take.
    assert mh.status().registered is True


def test_repair_rewrites_a_stale_path(linux_appimage, monkeypatch):
    mh.enable()
    # The update renames the AppImage; the entry still names the old file.
    monkeypatch.setattr(mh, "_registration_path", lambda: "/opt/Cove-3.2.1.AppImage")

    assert mh.status().stale is True
    assert mh.repair() is True
    assert mh.status().stale is False
    assert mh.status().recorded_path == "/opt/Cove-3.2.1.AppImage"


def test_repair_never_reclaims_a_default_the_user_changed(linux_appimage, monkeypatch):
    mh.enable()
    linux_appimage["default"] = "org.qbittorrent.qBittorrent.desktop"
    monkeypatch.setattr(mh, "_registration_path", lambda: "/opt/Cove-3.2.1.AppImage")

    before = len(linux_appimage["calls"])
    assert mh.repair() is True
    # Path repaired, but the user's choice is untouched.
    assert linux_appimage["default"] == "org.qbittorrent.qBittorrent.desktop"
    assert ["xdg-mime", "default"] not in [
        c[:2] for c in linux_appimage["calls"][before:]
    ]


def test_repair_is_a_no_op_when_the_path_is_current(linux_appimage):
    mh.enable()
    before = len(linux_appimage["calls"])
    assert mh.repair() is True
    assert mh.status().stale is False
    # No further xdg-mime WORK was needed; a read-only query is fine.
    assert ["xdg-mime", "default"] not in [
        c[:2] for c in linux_appimage["calls"][before:]
    ]


def test_disable_on_linux_stops_repair_but_leaves_the_entry(linux_appimage):
    mh.enable()
    result = mh.disable()
    assert result.ok is True
    # Removing the entry that currently owns the default is what breaks
    # magnet links, so the entry stays and only self-heal stops.
    assert mh.status().registered is True


def test_status_is_read_from_the_system_not_a_preference(linux_appimage):
    assert mh.status().registered is False
    mh.enable()
    assert mh.status().registered is True


def test_enable_on_windows_registers_but_never_sets_a_default(windows_portable):
    result = mh.enable()
    assert result.ok is True
    keys = magnet_win.keys_for(mi.WINDOWS_PORTABLE)
    # The positive side: the registration really was written, so a no-op
    # enable() could not pass this test.
    assert keys.command_key in windows_portable.data
    # Windows forbids an app assigning its own default; only the user can.
    assert magnet_win.USER_CHOICE_KEY not in windows_portable.data


def test_disable_on_windows_refuses_while_cove_is_the_default(windows_portable):
    mh.enable()
    windows_portable.data[magnet_win.USER_CHOICE_KEY] = {
        "ProgId": magnet_win.keys_for(mi.WINDOWS_PORTABLE).prog_id
    }
    result = mh.disable()
    assert result.ok is False
    assert "choose another" in result.message.lower()
    keys = magnet_win.keys_for(mi.WINDOWS_PORTABLE)
    assert keys.command_key in windows_portable.data


def test_disable_on_windows_removes_registration_when_not_the_default(windows_portable):
    mh.enable()
    result = mh.disable()
    assert result.ok is True
    keys = magnet_win.keys_for(mi.WINDOWS_PORTABLE)
    assert keys.command_key not in windows_portable.data


def test_disable_on_windows_leaves_a_foreign_registration_unchanged(
    windows_portable, monkeypatch
):
    """After a portable update the recorded exe is the OLD copy: unregister()
    returns False and disable() must say so, not report success while
    leaving the stale registration in place."""
    mh.enable()
    # Simulate the update: the exe Cove is running from now differs from
    # what was registered, so the running copy no longer owns the entry.
    monkeypatch.setattr(mh, "_registration_path", lambda: r"D:\Portable\Cove-new.exe")

    result = mh.disable()

    assert result.ok is False
    assert "different copy of cove" in result.message.lower()
    keys = magnet_win.keys_for(mi.WINDOWS_PORTABLE)
    # Nothing was removed: the stale registration is left exactly as it was.
    assert keys.command_key in windows_portable.data


def test_status_on_windows_is_default_only_when_userchoice_names_cove(windows_portable):
    mh.enable()
    assert mh.status().is_default is False
    windows_portable.data[magnet_win.USER_CHOICE_KEY] = {
        "ProgId": magnet_win.keys_for(mi.WINDOWS_PORTABLE).prog_id
    }
    assert mh.status().is_default is True


# ---- Debian: status must describe the live system, not the package's intent


@pytest.fixture
def debian(monkeypatch, tmp_path):
    """A .deb install whose system applications dir is redirected to tmp."""
    state = {"default": ""}

    def fake_run(argv):
        if argv[:3] == ["xdg-mime", "query", "default"]:
            return 0, state["default"] + "\n"
        if argv[:2] == ["xdg-mime", "default"]:
            state["default"] = argv[2]
            return 0, ""
        return 0, ""

    monkeypatch.setattr(mh, "_identity", lambda: mi.DEBIAN)
    monkeypatch.setattr(mh, "_registration_path", lambda: mi.DEBIAN_LAUNCHER)
    monkeypatch.setattr(mh, "_run", fake_run)
    monkeypatch.setattr(magnet_linux, "SYSTEM_APPS_DIR", str(tmp_path))
    state["dir"] = tmp_path
    return state


def test_debian_reports_registered_only_when_the_entry_is_installed(debian):
    # A removed or half-installed package must not be reported as registered.
    assert mh.status().registered is False
    assert mh.status().owned_by_cove is False
    assert mh.status().recorded_path == ""

    (debian["dir"] / magnet_linux.DESKTOP_ID_DEBIAN).write_text("[Desktop Entry]\n")
    state = mh.status()
    assert state.registered is True
    assert state.owned_by_cove is True
    assert state.recorded_path == mi.DEBIAN_LAUNCHER
    # The packaged path is stable, so it can never be stale.
    assert state.stale is False


def test_debian_default_detection_is_independent_of_the_entry(debian):
    debian["default"] = magnet_linux.DESKTOP_ID_DEBIAN
    assert mh.status().is_default is True


def test_repair_does_nothing_on_debian(debian):
    (debian["dir"] / magnet_linux.DESKTOP_ID_DEBIAN).write_text("[Desktop Entry]\n")
    # Never stale, so there is nothing to repair and nothing to reclaim.
    assert mh.repair() is True
    assert debian["default"] == ""
