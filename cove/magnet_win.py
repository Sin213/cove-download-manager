"""Windows magnet-handler registry primitives.

Imports no Qt and nothing from cove.entry, so packaging/portable_launcher.py
can use it before the GUI stack exists and a registration run never starts
the GUI. Every key is under HKEY_CURRENT_USER: registration needs no
elevation and only advertises Cove as capable. Windows itself decides the
default, and since Windows 10 no application may assign that for itself.
"""
from dataclasses import dataclass
from urllib.parse import quote

from .magnet_identity import WINDOWS_PORTABLE, WINDOWS_SETUP

REGISTERED_APPS_KEY = r"Software\RegisteredApplications"
# Where Windows records the user's own choice of magnet handler. Read-only
# for us: the value is hash-protected and only the user may set it.
USER_CHOICE_KEY = (
    r"Software\Microsoft\Windows\Shell\Associations"
    r"\UrlAssociations\magnet\UserChoice"
)


@dataclass(frozen=True)
class Keys:
    """Every registry path for one Cove identity."""

    prog_id: str
    prog_id_key: str
    shell_key: str
    shell_open_key: str
    command_key: str
    icon_key: str
    capabilities_key: str
    url_associations_key: str
    app_name: str


def _keys(prog_id: str, vendor_subkey: str, app_name: str) -> Keys:
    base = r"Software\Classes\{}".format(prog_id)
    capabilities = r"Software\Cove\{}\Capabilities".format(vendor_subkey)
    return Keys(
        prog_id=prog_id,
        prog_id_key=base,
        shell_key=base + r"\shell",
        shell_open_key=base + r"\shell\open",
        command_key=base + r"\shell\open\command",
        icon_key=base + r"\DefaultIcon",
        capabilities_key=capabilities,
        url_associations_key=capabilities + r"\URLAssociations",
        app_name=app_name,
    )


# A portable copy and an installed copy can coexist on one machine. They keep
# separate identities so neither one's unregister can wipe the other's.
_PORTABLE = _keys(
    "Cove.Magnet.Portable",
    "Cove Download Manager Portable",
    "Cove Download Manager Portable",
)
_SETUP = _keys(
    "Cove.Magnet",
    "Cove Download Manager",
    "Cove Download Manager",
)


def keys_for(identity: str) -> Keys:
    if identity == WINDOWS_PORTABLE:
        return _PORTABLE
    if identity == WINDOWS_SETUP:
        return _SETUP
    raise ValueError("no Windows keys for identity {!r}".format(identity))


def open_command(exe_path: str) -> str:
    """Shell open command; both the executable and %1 stay quoted."""
    return '"{}" "%1"'.format(exe_path)


def register(winreg, keys: Keys, exe_path: str) -> None:
    """Advertise Cove as a magnet-capable application for the current user."""
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, keys.prog_id_key, 0, winreg.KEY_WRITE
    ) as key:
        winreg.SetValueEx(
            key, None, 0, winreg.REG_SZ, "Magnet Link ({})".format(keys.app_name)
        )
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, keys.icon_key, 0, winreg.KEY_WRITE
    ) as key:
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, '"{}",0'.format(exe_path))
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, keys.command_key, 0, winreg.KEY_WRITE
    ) as key:
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, open_command(exe_path))
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, keys.capabilities_key, 0, winreg.KEY_WRITE
    ) as key:
        winreg.SetValueEx(key, "ApplicationName", 0, winreg.REG_SZ, keys.app_name)
        winreg.SetValueEx(
            key,
            "ApplicationDescription",
            0,
            winreg.REG_SZ,
            "Multi-connection download manager with magnet link support",
        )
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, keys.url_associations_key, 0, winreg.KEY_WRITE
    ) as key:
        winreg.SetValueEx(key, "magnet", 0, winreg.REG_SZ, keys.prog_id)
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, REGISTERED_APPS_KEY, 0, winreg.KEY_WRITE
    ) as key:
        winreg.SetValueEx(key, keys.app_name, 0, winreg.REG_SZ, keys.capabilities_key)


def registered_command(winreg, keys: Keys) -> str:
    """The recorded open command, or "" when nothing is registered."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, keys.command_key, 0, winreg.KEY_READ
        ) as key:
            command, _ = winreg.QueryValueEx(key, None)
    except OSError:
        return ""
    return str(command)


def registered_executable(command: str) -> str:
    """Pull the executable out of a `"exe" "%1"` command string."""
    command = (command or "").strip()
    if command.startswith('"'):
        end = command.find('"', 1)
        if end == -1:
            return ""
        return command[1:end]
    return command.split(" ")[0]


def same_executable(left: str, right: str) -> bool:
    """Windows-appropriate path comparison (case- and separator-insensitive).

    Windows paths are compared manually rather than through os.path, because
    this module (and its tests) also run on non-Windows platforms where
    os.path would not treat backslashes as separators or ignore case.
    """

    def _normalize(path: str) -> str:
        return (path or "").replace("/", "\\").lower()

    return _normalize(left) == _normalize(right)


def _delete_key(winreg, path: str) -> None:
    """Delete a key, ignoring an already-absent one (keeps this idempotent)."""
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except OSError:
        pass


def unregister(winreg, keys: Keys, exe_path: str) -> bool:
    """Remove the registration, but only when this executable owns it.

    Returns False without changing anything when the registration belongs to
    another Cove copy, so a portable run can never strip an installed one.
    """
    command = registered_command(winreg, keys)
    if not command:
        return True  # nothing registered: already in the desired state
    owner = registered_executable(command)
    if not owner or not same_executable(owner, exe_path):
        return False

    for path in (
        keys.command_key,
        keys.shell_open_key,
        keys.shell_key,
        keys.icon_key,
        keys.prog_id_key,
        keys.url_associations_key,
        keys.capabilities_key,
    ):
        _delete_key(winreg, path)
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, REGISTERED_APPS_KEY, 0, winreg.KEY_WRITE
        ) as key:
            winreg.DeleteValue(key, keys.app_name)
    except OSError:
        pass
    return True


def is_default(winreg, keys: Keys) -> bool:
    """True only when Windows records this ProgID as the user's choice."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, USER_CHOICE_KEY, 0, winreg.KEY_READ
        ) as key:
            prog_id, _ = winreg.QueryValueEx(key, "ProgId")
    except OSError:
        return False
    return str(prog_id) == keys.prog_id


def default_apps_url(app_name: str) -> str:
    """Deep link to this application's page in Settings > Default apps.

    Windows 11 honours the registeredAppUser parameter for entries under
    HKCU\\Software\\RegisteredApplications. Older builds ignore the parameter
    and open the generic page, which is an acceptable fallback.
    """
    return "ms-settings:defaultapps?registeredAppUser={}".format(quote(app_name))
