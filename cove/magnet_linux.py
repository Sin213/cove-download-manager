"""Linux magnet handler: desktop entry authoring and xdg-mime calls.

Every subprocess goes through an injected `run` callable so the GUI, the
tests, and any future caller share one code path and the tests never touch
the real MIME database.
"""
import os
import subprocess
from pathlib import Path

from .magnet_identity import APPIMAGE, DEBIAN

MAGNET_MIME = "x-scheme-handler/magnet"

# The .deb ships this system-wide (scripts/build-deb.sh:95). The AppImage
# must NOT reuse the basename: a user entry with the same ID shadows the
# packaged one on a machine that has both.
DESKTOP_ID_DEBIAN = "cove-download-manager.desktop"
DESKTOP_ID_APPIMAGE = "cove-download-manager-appimage.desktop"


def desktop_id(identity: str) -> str:
    if identity == DEBIAN:
        return DESKTOP_ID_DEBIAN
    if identity == APPIMAGE:
        return DESKTOP_ID_APPIMAGE
    raise ValueError("no desktop id for identity {!r}".format(identity))


def run_command(argv) -> tuple[int, str]:
    """Default runner: exit code and stdout, stderr discarded.

    These are short query and set operations, but they run synchronously
    during Settings dialog construction, so a wedged xdg-mime would freeze
    the dialog for as long as the timeout. Kept short (5s) rather than the
    15s previously used elsewhere.
    """
    proc = subprocess.run(
        list(argv), capture_output=True, text=True, timeout=5, check=False
    )
    return proc.returncode, proc.stdout


def escape_exec(path: str) -> str:
    """Quote a path for a Desktop Entry Exec field.

    Desktop Entry rules, not shell rules: inside double quotes the
    characters \\ " ` and $ are escaped with a backslash, and a literal
    percent is written %% anywhere in the field. An AppImage in a directory
    containing a percent sign is otherwise silently unlaunchable.
    """
    escaped = path.replace("%", "%%")
    for ch in ("\\", '"', "`", "$"):
        escaped = escaped.replace(ch, "\\" + ch)
    return '"{}"'.format(escaped)


def _unescape_exec(field: str) -> str:
    """Inverse of escape_exec for a single quoted field."""
    field = field.strip()
    if not (field.startswith('"') and field.endswith('"') and len(field) >= 2):
        return ""
    body = field[1:-1]
    out = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            out.append(body[i + 1])
            i += 2
            continue
        if ch == "%" and i + 1 < len(body) and body[i + 1] == "%":
            out.append("%")
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def entry_text(exec_path: str) -> str:
    """A desktop entry declaring Cove as a magnet handler."""
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Cove Download Manager\n"
        "GenericName=Download Manager\n"
        "Comment=Multi-connection downloads with a queue, schedule, "
        "and global speed cap\n"
        "Exec={} %u\n"
        "Icon=cove-download-manager\n"
        "Terminal=false\n"
        "Categories=Network;FileTransfer;Qt;\n"
        "MimeType=x-scheme-handler/magnet;\n"
        "StartupNotify=true\n"
        "StartupWMClass=Cove\n"
    ).format(escape_exec(exec_path))


def entry_exec_path(text: str) -> str:
    """The executable path recorded in an entry's Exec line, or ""."""
    for line in (text or "").splitlines():
        if not line.startswith("Exec="):
            continue
        value = line[len("Exec=") :].strip()
        if value.endswith(" %u"):
            value = value[: -len(" %u")]
        return _unescape_exec(value)
    return ""


def user_entry_path(desktop_id_value: str, apps_dir) -> Path:
    return Path(apps_dir) / desktop_id_value


def write_user_entry(desktop_id_value: str, exec_path: str, apps_dir, run) -> None:
    """Install the entry and refresh the desktop database.

    A missing update-desktop-database is not fatal: the entry itself is what
    matters, and many desktops pick it up without the cache refresh.
    """
    apps_dir = Path(apps_dir)
    apps_dir.mkdir(parents=True, exist_ok=True)
    target = user_entry_path(desktop_id_value, apps_dir)
    target.write_text(entry_text(exec_path), encoding="utf-8")
    try:
        run(["update-desktop-database", str(apps_dir)])
    except (OSError, subprocess.SubprocessError):
        pass


def remove_user_entry(desktop_id_value: str, apps_dir) -> None:
    try:
        user_entry_path(desktop_id_value, apps_dir).unlink()
    except OSError:
        pass


def query_default(run) -> str:
    """The desktop ID currently handling magnet links, or ""."""
    try:
        code, out = run(["xdg-mime", "query", "default", MAGNET_MIME])
    except (OSError, subprocess.SubprocessError):
        return ""
    if code != 0:
        return ""
    return out.strip()


def set_default(run, desktop_id_value: str) -> bool:
    """Ask for the default, then verify it actually took.

    xdg-mime reports success for the request, not the outcome: desktop
    policy can decline it. Nothing is claimed without reading it back.
    """
    try:
        run(["xdg-mime", "default", desktop_id_value, MAGNET_MIME])
    except (OSError, subprocess.SubprocessError):
        return False
    return query_default(run) == desktop_id_value


def user_apps_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return Path(base) / "applications"
