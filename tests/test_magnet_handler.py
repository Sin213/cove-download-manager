"""The facade the GUI talks to. Both backends are stubbed."""
import pytest

import cove.magnet_identity as mi
from cove import magnet_handler as mh


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

    assert mh.repair() is True
    # Path repaired, but the user's choice is untouched.
    assert linux_appimage["default"] == "org.qbittorrent.qBittorrent.desktop"
    assert ["xdg-mime", "default"] not in [c[:2] for c in linux_appimage["calls"][-2:]]


def test_repair_is_a_no_op_when_the_path_is_current(linux_appimage):
    mh.enable()
    before = len(linux_appimage["calls"])
    assert mh.repair() is True
    assert mh.status().stale is False
    # No further xdg-mime work was needed.
    assert len(linux_appimage["calls"]) == before


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
