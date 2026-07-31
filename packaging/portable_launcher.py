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

# The registry primitives live in cove.magnet_win so the GUI and this
# launcher share one implementation. That module imports no Qt and does not
# import cove.entry, so importing it here still never starts the GUI.

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


def executable_path() -> str:
    """Absolute path of the running portable executable."""
    return os.path.abspath(sys.executable)


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

    from cove import magnet_win
    from cove.magnet_identity import WINDOWS_PORTABLE

    keys = magnet_win.keys_for(WINDOWS_PORTABLE)
    exe_path = executable_path()

    if flags[0] == REGISTER_FLAG:
        magnet_win.register(winreg, keys, exe_path)
        print("Registered Cove as a magnet link handler for the current user.")
        print("Pick Cove under Windows Default Apps to make it the default.")
        return EXIT_OK

    if magnet_win.unregister(winreg, keys, exe_path):
        print("Removed the Cove magnet link handler registration.")
        return EXIT_OK
    print(
        "Magnet registration belongs to another Cove installation; "
        "left unchanged."
    )
    return EXIT_ERROR


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
