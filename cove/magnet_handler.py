"""One surface over the platform magnet-handler backends.

Everything here is best-effort and returns a Result rather than raising: a
magnet association is never important enough to interrupt startup or a
download. Status is always read from the operating system, never from a
stored preference, so a default changed outside Cove shows up immediately.
"""
from dataclasses import dataclass

from . import magnet_identity, magnet_linux


@dataclass(frozen=True)
class HandlerStatus:
    """Status is always read fresh from the OS, never cached or lazy.

    A fresh status() call always reflects the current default, so two
    statuses that differ only in who holds the default compare unequal.
    """

    supported: bool = False
    identity: str = magnet_identity.UNSUPPORTED
    registered: bool = False
    owned_by_cove: bool = False
    stale: bool = False
    recorded_path: str = ""
    is_default: bool = False


@dataclass(frozen=True)
class Result:
    ok: bool
    message: str


# Indirections so tests can drive either platform without faking a machine.
def _identity() -> str:
    return magnet_identity.build_identity()


def _registration_path() -> str:
    return magnet_identity.registration_path()


def _run(argv):
    return magnet_linux.run_command(argv)


def _apps_dir():
    return magnet_linux.user_apps_dir()


def _winreg():
    import winreg  # Windows-only; never imported on other platforms.

    return winreg


_WINDOWS = (magnet_identity.WINDOWS_SETUP, magnet_identity.WINDOWS_PORTABLE)


def status() -> HandlerStatus:
    identity = _identity()
    if identity == magnet_identity.UNSUPPORTED:
        return HandlerStatus()
    current = _registration_path()
    if identity in _WINDOWS:
        return _windows_status(identity, current)
    return _linux_status(identity, current)


def _windows_status(identity: str, current: str) -> HandlerStatus:
    from . import magnet_win

    keys = magnet_win.keys_for(identity)
    try:
        winreg = _winreg()
        command = magnet_win.registered_command(winreg, keys)
    except Exception:
        return HandlerStatus(supported=True, identity=identity)
    recorded = magnet_win.registered_executable(command) if command else ""

    try:
        is_default = magnet_win.is_default(_winreg(), keys)
    except Exception:
        is_default = False

    return HandlerStatus(
        supported=True,
        identity=identity,
        registered=bool(recorded),
        owned_by_cove=bool(recorded),
        stale=bool(recorded) and not magnet_win.same_executable(recorded, current),
        recorded_path=recorded,
        is_default=is_default,
    )


def _linux_status(identity: str, current: str) -> HandlerStatus:
    desktop_id = magnet_linux.desktop_id(identity)

    try:
        is_default = magnet_linux.query_default(_run) == desktop_id
    except Exception:
        is_default = False

    if identity == magnet_identity.DEBIAN:
        # The package owns the entry; its path is stable and never stale.
        return HandlerStatus(
            supported=True,
            identity=identity,
            registered=True,
            owned_by_cove=True,
            stale=False,
            recorded_path=current,
            is_default=is_default,
        )
    path = magnet_linux.user_entry_path(desktop_id, _apps_dir())
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return HandlerStatus(supported=True, identity=identity, is_default=is_default)
    recorded = magnet_linux.entry_exec_path(text)
    return HandlerStatus(
        supported=True,
        identity=identity,
        registered=bool(recorded),
        owned_by_cove=bool(recorded),
        stale=bool(recorded) and recorded != current,
        recorded_path=recorded,
        is_default=is_default,
    )


def enable() -> Result:
    """Register, and on Linux also set and verify the default."""
    identity = _identity()
    if identity == magnet_identity.UNSUPPORTED:
        return Result(False, "Magnet registration needs an installed or portable build.")
    current = _registration_path()

    if identity in _WINDOWS:
        from . import magnet_win

        try:
            magnet_win.register(_winreg(), magnet_win.keys_for(identity), current)
        except Exception:
            return Result(False, "Could not register Cove for magnet links.")
        # Windows does not allow an application to assign the default.
        return Result(True, "Registered. Choose Cove in the window that opened.")

    desktop_id = magnet_linux.desktop_id(identity)
    if identity == magnet_identity.APPIMAGE:
        try:
            magnet_linux.write_user_entry(desktop_id, current, _apps_dir(), _run)
        except OSError:
            return Result(False, "Could not install Cove's desktop entry.")
    if magnet_linux.set_default(_run, desktop_id):
        return Result(True, "Cove is now your magnet handler.")
    return Result(
        False, "Cove was registered, but your desktop did not make it the default."
    )


def disable() -> Result:
    """Stop self-healing without leaving a broken association behind.

    On Linux the desktop entry stays: removing the entry that currently owns
    the default is exactly what breaks magnet links. On Windows the keys are
    removed only when Cove is not the current default.
    """
    identity = _identity()
    if identity == magnet_identity.UNSUPPORTED:
        return Result(True, "")
    if identity not in _WINDOWS:
        return Result(True, "Cove will no longer repair its magnet registration.")

    from . import magnet_win

    keys = magnet_win.keys_for(identity)
    try:
        winreg = _winreg()
        if magnet_win.is_default(winreg, keys):
            return Result(
                False,
                "Cove is currently your default magnet handler. Choose another "
                "handler in Windows Settings first, then remove the registration.",
            )
        removed = magnet_win.unregister(winreg, keys, _registration_path())
    except Exception:
        return Result(False, "Could not remove Cove's magnet registration.")
    if not removed:
        return Result(
            False,
            "The recorded registration belongs to a different copy of Cove "
            "and was left unchanged.",
        )
    return Result(True, "Removed Cove's magnet registration.")


def repair() -> bool:
    """Re-point a stale, Cove-owned registration. Never reclaims a default.

    Returns True when nothing needed doing or the repair succeeded.
    """
    state = status()
    if not state.supported or not state.registered or not state.owned_by_cove:
        return False
    if not state.stale:
        return True
    current = _registration_path()
    identity = state.identity
    try:
        if identity in _WINDOWS:
            from . import magnet_win

            magnet_win.register(_winreg(), magnet_win.keys_for(identity), current)
            return True
        if identity == magnet_identity.APPIMAGE:
            magnet_linux.write_user_entry(
                magnet_linux.desktop_id(identity), current, _apps_dir(), _run
            )
            return True
    except Exception:
        return False
    return False


def default_apps_url() -> str:
    """Windows deep link to Cove's page in Settings > Default apps."""
    identity = _identity()
    if identity not in _WINDOWS:
        return ""
    from . import magnet_win

    return magnet_win.default_apps_url(magnet_win.keys_for(identity).app_name)
