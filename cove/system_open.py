"""Environment hygiene for launching external programs from an AppImage.

The AppImage runtime exports LD_LIBRARY_PATH (and similar loader vars)
pointing at the bundle's own libraries. Children spawned by Cove - xdg-open
and whatever it launches (dolphin, mpv, a browser) - inherit those vars and
load the bundle's outdated libraries instead of the system ones, crashing
on startup with errors like `liblzma.so.5: version XZ_5.4 not found`.

Spawn external programs with `env=child_env()`. Bundled binaries (aria2c)
must keep the AppImage env, so this never mutates os.environ.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

_SCRUB_VARS = (
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "PYTHONHOME",
    "PYTHONPATH",
    "GDK_PIXBUF_MODULE_FILE",
    "GDK_PIXBUF_MODULEDIR",
)


def child_env() -> dict[str, str] | None:
    """Environment for spawning external (non-bundled) programs.

    Returns None when not running from an AppImage - pass it to
    subprocess.Popen(env=...) either way; None means "inherit unchanged".
    Inside an AppImage, returns a copy of os.environ with the bundle's
    loader vars removed.
    """
    if not (os.environ.get("APPDIR") or os.environ.get("APPIMAGE")):
        return None
    env = dict(os.environ)
    for key in _SCRUB_VARS:
        env.pop(key, None)
    return env


def open_url(url: str) -> None:
    """Open `url` in the default browser without leaking AppImage env.

    QDesktopServices.openUrl spawns xdg-open with the current environment,
    which inside an AppImage carries LD_LIBRARY_PATH into the browser and
    crashes it. Spawn xdg-open ourselves with a scrubbed env in that case.
    """
    env = child_env()
    if env is not None and sys.platform.startswith("linux") and shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", url], env=env)
        return
    # Imported here so this module stays importable without a Qt GUI stack.
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    QDesktopServices.openUrl(QUrl(url))
