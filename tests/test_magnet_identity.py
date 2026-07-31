"""The gate that decides whether this process may register anything.

A source checkout would record the Python interpreter and an extracted
AppDir would record a temporary mount point, so both must be refused.
"""
import cove.magnet_identity as mi


def test_source_checkout_is_unsupported(monkeypatch):
    monkeypatch.setattr(mi.sys, "platform", "linux")
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.setattr(mi.sys, "frozen", False, raising=False)
    monkeypatch.setattr(mi.sys, "argv", ["/home/dev/venv/bin/python"])
    assert mi.build_identity() == mi.UNSUPPORTED
    assert mi.supported() is False
    assert mi.registration_path() == ""


def test_appimage_uses_the_live_appimage_path(monkeypatch, tmp_path):
    image = tmp_path / "Cove-3.2.0-x86_64.AppImage"
    image.write_text("")
    monkeypatch.setattr(mi.sys, "platform", "linux")
    monkeypatch.setenv("APPIMAGE", str(image))
    assert mi.build_identity() == mi.APPIMAGE
    assert mi.registration_path() == str(image)


def test_extracted_appdir_is_refused(monkeypatch):
    # APPIMAGE unset but frozen: an extracted AppDir mount, whose path is
    # temporary and must never be recorded.
    monkeypatch.setattr(mi.sys, "platform", "linux")
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.setattr(mi.sys, "frozen", True, raising=False)
    assert mi.build_identity() == mi.UNSUPPORTED


def test_appimage_env_pointing_at_a_missing_file_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(mi.sys, "platform", "linux")
    monkeypatch.setenv("APPIMAGE", str(tmp_path / "gone.AppImage"))
    monkeypatch.setattr(mi.sys, "frozen", False, raising=False)
    monkeypatch.setattr(mi.sys, "argv", ["/usr/bin/python3"])
    assert mi.build_identity() == mi.UNSUPPORTED


def test_debian_install_is_recognized(monkeypatch):
    monkeypatch.setattr(mi.sys, "platform", "linux")
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.setattr(mi.sys, "frozen", False, raising=False)
    monkeypatch.setattr(mi.sys, "argv", [mi.DEBIAN_LAUNCHER])
    assert mi.build_identity() == mi.DEBIAN
    assert mi.registration_path() == mi.DEBIAN_LAUNCHER


def test_debian_frozen_bundle_is_recognized(monkeypatch):
    # The .deb's /usr/bin wrapper execs the PyInstaller bundle at
    # DEBIAN_BINARY, so sys.executable (not argv[0]) is the frozen bundle
    # path. Registration must still record the stable /usr/bin wrapper
    # path, not the /usr/lib bundle path, since that is what the desktop
    # entry's Exec= line points at.
    monkeypatch.setattr(mi.sys, "platform", "linux")
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.setattr(mi.sys, "frozen", True, raising=False)
    monkeypatch.setattr(mi.sys, "executable", mi.DEBIAN_BINARY)
    monkeypatch.setattr(mi.sys, "argv", [mi.DEBIAN_BINARY])
    assert mi.build_identity() == mi.DEBIAN
    assert mi.registration_path() == mi.DEBIAN_LAUNCHER


def test_frozen_linux_with_unrelated_executable_is_unsupported(monkeypatch):
    # A frozen process whose executable matches neither the live AppImage
    # nor the Debian bundle path is an extracted AppDir or unknown bundle
    # whose path is temporary, so it must still be refused.
    monkeypatch.setattr(mi.sys, "platform", "linux")
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.setattr(mi.sys, "frozen", True, raising=False)
    monkeypatch.setattr(mi.sys, "executable", "/tmp/.mount_CoveXYZ/AppRun")
    monkeypatch.setattr(mi.sys, "argv", ["/tmp/.mount_CoveXYZ/AppRun"])
    assert mi.build_identity() == mi.UNSUPPORTED


def test_windows_portable_and_setup_are_distinguished(monkeypatch):
    monkeypatch.setattr(mi.sys, "platform", "win32")
    monkeypatch.setattr(mi.sys, "frozen", True, raising=False)
    monkeypatch.setattr(mi.sys, "executable", r"C:\Cove\Cove.exe")

    monkeypatch.setattr(mi, "_is_portable", lambda: True)
    assert mi.build_identity() == mi.WINDOWS_PORTABLE

    monkeypatch.setattr(mi, "_is_portable", lambda: False)
    assert mi.build_identity() == mi.WINDOWS_SETUP


def test_unfrozen_windows_is_unsupported(monkeypatch):
    monkeypatch.setattr(mi.sys, "platform", "win32")
    monkeypatch.setattr(mi.sys, "frozen", False, raising=False)
    assert mi.build_identity() == mi.UNSUPPORTED
