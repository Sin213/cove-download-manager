"""PyInstaller entry point for the one-file Windows portable build.

Portable mode must be selected before importing Cove because configuration
paths are resolved at module import time. A freshly downloaded single EXE has
no adjacent data directory or marker yet, so presence-based detection alone
would incorrectly write its first-run state into the user profile.

The portable build also owns the opt-in magnet-handler registration flags.
They are handled here, before cove.entry is imported, so a registration run
never starts the GUI and the flags never reach the GUI argument parser.
Registration is HKCU-only, needs no elevation, and only advertises Cove as a
capable handler -- Windows still asks the user to pick the default.
"""
import os
import sys

os.environ["COVE_PORTABLE"] = "1"

REGISTER_FLAG = "--register-magnet-handler"
UNREGISTER_FLAG = "--unregister-magnet-handler"

# The portable build deliberately owns a *different* ProgID and capability
# identity from the Setup installer (which uses Cove.Magnet). A portable copy
# and an installed copy can coexist on one machine, and sharing the identity
# would let either one's unregister/uninstall wipe the other's registration.
PROG_ID = "Cove.Magnet.Portable"
PROG_ID_KEY = r"Software\Classes\Cove.Magnet.Portable"
SHELL_KEY = r"Software\Classes\Cove.Magnet.Portable\shell"
SHELL_OPEN_KEY = r"Software\Classes\Cove.Magnet.Portable\shell\open"
COMMAND_KEY = r"Software\Classes\Cove.Magnet.Portable\shell\open\command"
ICON_KEY = r"Software\Classes\Cove.Magnet.Portable\DefaultIcon"
CAPABILITIES_KEY = (
    r"Software\Cove\Cove Download Manager Portable\Capabilities"
)
URL_ASSOCIATIONS_KEY = (
    r"Software\Cove\Cove Download Manager Portable\Capabilities\URLAssociations"
)
REGISTERED_APPS_KEY = r"Software\RegisteredApplications"
APP_NAME = "Cove Download Manager Portable"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


def executable_path() -> str:
    """Absolute path of the running portable executable."""
    return os.path.abspath(sys.executable)


def open_command(exe_path: str) -> str:
    """Shell open command; both the executable and %1 stay quoted."""
    return '"{}" "%1"'.format(exe_path)


def _same_executable(left: str, right: str) -> bool:
    """Windows-appropriate path comparison (case- and separator-insensitive)."""
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def _registered_executable(command: str) -> str:
    """Pull the executable out of a `"exe" "%1"` command string."""
    command = command.strip()
    if command.startswith('"'):
        end = command.find('"', 1)
        if end == -1:
            return ""
        return command[1:end]
    return command.split(" ")[0]


def register(winreg, exe_path: str) -> int:
    """Advertise Cove as a magnet-capable application for the current user."""
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, PROG_ID_KEY, 0, winreg.KEY_WRITE
    ) as key:
        winreg.SetValueEx(
            key,
            None,
            0,
            winreg.REG_SZ,
            "Magnet Link (Cove Download Manager Portable)",
        )
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, ICON_KEY, 0, winreg.KEY_WRITE
    ) as key:
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, '"{}",0'.format(exe_path))
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, COMMAND_KEY, 0, winreg.KEY_WRITE
    ) as key:
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, open_command(exe_path))
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, CAPABILITIES_KEY, 0, winreg.KEY_WRITE
    ) as key:
        winreg.SetValueEx(key, "ApplicationName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(
            key,
            "ApplicationDescription",
            0,
            winreg.REG_SZ,
            "Multi-connection download manager with magnet link support",
        )
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, URL_ASSOCIATIONS_KEY, 0, winreg.KEY_WRITE
    ) as key:
        winreg.SetValueEx(key, "magnet", 0, winreg.REG_SZ, PROG_ID)
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, REGISTERED_APPS_KEY, 0, winreg.KEY_WRITE
    ) as key:
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, CAPABILITIES_KEY)

    print("Registered Cove as a magnet link handler for the current user.")
    print("Pick Cove under Windows Default Apps to make it the default.")
    return EXIT_OK


def _delete_key(winreg, path: str) -> None:
    """Delete a key, ignoring an already-absent one (keeps this idempotent)."""
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except OSError:
        pass


def unregister(winreg, exe_path: str) -> int:
    """Remove the registration, but only when this executable owns it."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, COMMAND_KEY, 0, winreg.KEY_READ
        ) as key:
            command, _ = winreg.QueryValueEx(key, None)
    except OSError:
        print("No Cove magnet registration found for the current user.")
        return EXIT_OK

    owner = _registered_executable(str(command))
    if not owner or not _same_executable(owner, exe_path):
        print(
            "Magnet registration belongs to another Cove installation; "
            "left unchanged."
        )
        return EXIT_ERROR

    _delete_key(winreg, COMMAND_KEY)
    _delete_key(winreg, SHELL_OPEN_KEY)
    _delete_key(winreg, SHELL_KEY)
    _delete_key(winreg, ICON_KEY)
    _delete_key(winreg, PROG_ID_KEY)
    _delete_key(winreg, URL_ASSOCIATIONS_KEY)
    _delete_key(winreg, CAPABILITIES_KEY)
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, REGISTERED_APPS_KEY, 0, winreg.KEY_WRITE
        ) as key:
            winreg.DeleteValue(key, APP_NAME)
    except OSError:
        pass

    print("Removed the Cove magnet link handler registration.")
    return EXIT_OK


def handle_registration_args(argv):
    """Run a registration flag if present; return None for a normal launch."""
    flags = [arg for arg in argv[1:] if arg in (REGISTER_FLAG, UNREGISTER_FLAG)]
    if not flags:
        return None

    others = [
        arg for arg in argv[1:] if arg not in (REGISTER_FLAG, UNREGISTER_FLAG)
    ]
    if len(flags) > 1 or others:
        print(
            "Use {} or {} on its own.".format(REGISTER_FLAG, UNREGISTER_FLAG),
            file=sys.stderr,
        )
        return EXIT_USAGE

    if sys.platform != "win32":
        print(
            "Magnet handler registration is only supported on Windows.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    import winreg  # Windows-only; never imported on other platforms.

    exe_path = executable_path()
    if flags[0] == REGISTER_FLAG:
        return register(winreg, exe_path)
    return unregister(winreg, exe_path)


def main(argv=None):
    argv = sys.argv if argv is None else argv
    result = handle_registration_args(argv)
    if result is not None:
        return result

    # Imported lazily so a registration run never loads the GUI stack.
    from cove.entry import main as cove_main

    return cove_main()


if __name__ == "__main__":
    raise SystemExit(main())
