"""Which packaged build is running, and what path a registration records.

Magnet registration stores an absolute path. A source checkout would store
the Python interpreter and an extracted AppDir would store a temporary
mount point, so neither may register: the recorded path would be wrong the
moment the process exits. Only the four shipped builds are eligible.
"""
import os
import sys

WINDOWS_SETUP = "windows-setup"
WINDOWS_PORTABLE = "windows-portable"
DEBIAN = "debian"
APPIMAGE = "appimage"
UNSUPPORTED = "unsupported"

# The wrapper script the .deb installs (see scripts/build-deb.sh:89).
DEBIAN_LAUNCHER = "/usr/bin/cove-download-manager"

# The PyInstaller bundle the /usr/bin wrapper execs (scripts/build-deb.sh:89-91).
DEBIAN_BINARY = "/usr/lib/cove-download-manager/cove-download-manager"


def _is_portable() -> bool:
    # Indirected so tests can choose the branch without faking a data dir.
    from .portable import is_portable

    return is_portable()


def _appimage_path() -> str:
    """The live AppImage path, or "" when this is not an AppImage run.

    The AppImage runtime sets APPIMAGE to the resolved absolute path of the
    image itself, which is the one path that stays correct across an update
    because it is read fresh on every launch.
    """
    path = os.environ.get("APPIMAGE", "")
    if path and os.path.isabs(path) and os.path.isfile(path):
        return path
    return ""


def build_identity() -> str:
    if sys.platform == "win32":
        if not getattr(sys, "frozen", False):
            return UNSUPPORTED
        return WINDOWS_PORTABLE if _is_portable() else WINDOWS_SETUP
    if sys.platform.startswith("linux"):
        if _appimage_path():
            return APPIMAGE
        # Debian must be checked before the frozen-means-unsupported rule
        # below: the packaged build IS frozen (a PyInstaller bundle exec'd
        # by the /usr/bin wrapper), so treating "frozen" as disqualifying
        # would misclassify every real Debian install as UNSUPPORTED.
        argv0 = (sys.argv[0] if sys.argv else "") or ""
        executable = getattr(sys, "executable", "") or ""
        if (
            os.path.realpath(argv0) == DEBIAN_LAUNCHER
            or os.path.realpath(executable) == DEBIAN_BINARY
        ):
            return DEBIAN
        if getattr(sys, "frozen", False):
            # Frozen, no APPIMAGE, and not the Debian bundle: an extracted
            # AppDir or an unknown bundle. Its path is temporary, so refuse it.
            return UNSUPPORTED
        return UNSUPPORTED
    return UNSUPPORTED


def registration_path() -> str:
    """Absolute path a registration must record, or "" if ineligible."""
    identity = build_identity()
    if identity == APPIMAGE:
        return _appimage_path()
    if identity in (WINDOWS_SETUP, WINDOWS_PORTABLE):
        return os.path.abspath(sys.executable)
    if identity == DEBIAN:
        return DEBIAN_LAUNCHER
    return ""


def supported() -> bool:
    return build_identity() != UNSUPPORTED
