# Magnet Handler Self-Heal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users register Cove as a magnet handler from inside the app, and silently repair that registration when an update changes the executable path.

**Architecture:** A platform-agnostic facade (`cove/magnet_handler.py`) over two backends (`cove/magnet_win.py`, `cove/magnet_linux.py`) and a build-identity gate (`cove/magnet_identity.py`). The Windows registry primitives move out of `packaging/portable_launcher.py` into `cove/magnet_win.py`, which imports no Qt, so the portable launcher can keep using them before the GUI exists. Startup runs a repair off the UI thread; Settings exposes actions plus live status.

**Tech Stack:** Python 3.12, PySide6, `winreg` (Windows, injected in tests), `xdg-mime` / `update-desktop-database` (Linux, injected in tests), pytest.

**Spec:** `docs/superpowers/specs/2026-07-30-magnet-handler-self-heal-design.md`

## Global Constraints

- Do NOT change portable artifact naming, updater matching, packaging output names, or release workflow logic. A stable portable filename and a permanent launcher are explicitly out of scope.
- The portable/AppImage promise is exactly: "After the updated build is launched once, Cove repairs a stale registration it already owns. It does not silently reclaim a default the user changed elsewhere." Do not write copy that promises more.
- Never use em dashes or en dashes in code, comments, strings, commit messages, or output. Use plain hyphens.
- Migration sets `magnet_handler_enabled=True` only when the setting key is ABSENT and the existing registration is Cove-owned. An explicit `False` is always respected.
- Debian and AppImage use distinct desktop IDs: `cove-download-manager.desktop` and `cove-download-manager-appimage.desktop`.
- Windows Setup (`Cove.Magnet`) and portable (`Cove.Magnet.Portable`) identities must never modify one another.
- Registration status is read live from the OS, never from the stored preference.
- Windows UI must not claim Cove is the default until `UserChoice\ProgId` confirms it.
- Linux must verify with `xdg-mime query default` before claiming success.
- Startup repair updates stale paths only. It never calls `xdg-mime default` and never reasserts a default.
- Source/dev launches must never register a Python interpreter or a temporary AppImage mount path.
- No elevation, no `HKLM`, no machine-wide changes, no touching registrations Cove does not own.
- Tests must never touch the real registry, the real `~/.local/share/applications`, or the real MIME database.
- Run the full suite with `QT_QPA_PLATFORM=offscreen python -m pytest tests/ -q` and confirm the process exit status is 0, not just the summary line.

---

### Task 1: Build identity gate

**Files:**
- Create: `cove/magnet_identity.py`
- Test: `tests/test_magnet_identity.py`

**Interfaces:**
- Consumes: `cove.portable.is_portable` (existing).
- Produces: `WINDOWS_SETUP`, `WINDOWS_PORTABLE`, `DEBIAN`, `APPIMAGE`, `UNSUPPORTED` string constants; `build_identity() -> str`; `registration_path() -> str`; `supported() -> bool`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_magnet_identity.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen python -m pytest tests/test_magnet_identity.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'cove.magnet_identity'`

- [ ] **Step 3: Write minimal implementation**

Create `cove/magnet_identity.py`:

```python
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
        if getattr(sys, "frozen", False):
            # Frozen with no APPIMAGE: an extracted AppDir or an unknown
            # bundle. Its path is temporary, so refuse it.
            return UNSUPPORTED
        argv0 = (sys.argv[0] if sys.argv else "") or ""
        if os.path.realpath(argv0) == DEBIAN_LAUNCHER:
            return DEBIAN
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `QT_QPA_PLATFORM=offscreen python -m pytest tests/test_magnet_identity.py -q`
Expected: PASS, 7 passed

- [ ] **Step 5: Commit**

```bash
git add cove/magnet_identity.py tests/test_magnet_identity.py
git commit -m "Add build identity gate for magnet registration"
```

---

### Task 2: Windows backend, extracted from the portable launcher

**Files:**
- Create: `cove/magnet_win.py`
- Modify: `packaging/portable_launcher.py:22-194` (delete the moved primitives, delegate to the new module)
- Test: `tests/test_magnet_win.py`
- Verify unchanged: `tests/test_portable_magnet_registration.py` must keep passing untouched.

**Interfaces:**
- Consumes: `cove.magnet_identity.WINDOWS_SETUP`, `WINDOWS_PORTABLE`.
- Produces: `Keys` dataclass with fields `prog_id, prog_id_key, shell_key, shell_open_key, command_key, icon_key, capabilities_key, url_associations_key, app_name`; `keys_for(identity) -> Keys`; `register(winreg, keys, exe_path) -> None`; `unregister(winreg, keys, exe_path) -> bool`; `registered_command(winreg, keys) -> str`; `registered_executable(command) -> str`; `same_executable(a, b) -> bool`; `is_default(winreg, keys) -> bool`; `default_apps_url(app_name) -> str`; module constant `REGISTERED_APPS_KEY`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_magnet_win.py`. Reuse the fake registry already proven in `tests/test_portable_magnet_registration.py:29-60`:

```python
"""Windows magnet registry primitives.

The registry is faked end to end: these tests never import the real winreg
module and never touch the machine's registry or default handlers.
"""
import pytest

import cove.magnet_identity as mi
from cove import magnet_win


class FakeKey:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    KEY_WRITE = 2
    KEY_READ = 1
    REG_SZ = 1

    def __init__(self, initial=None):
        self.data = dict(initial or {})
        self.roots_used = set()

    def CreateKeyEx(self, root, path, reserved=0, access=0):
        self.roots_used.add(root)
        self.data.setdefault(path, {})
        return FakeKey(self.data[path])

    def OpenKey(self, root, path, reserved=0, access=0):
        self.roots_used.add(root)
        if path not in self.data:
            raise OSError("missing key: {}".format(path))
        return FakeKey(self.data[path])

    def SetValueEx(self, key, name, reserved, kind, value):
        key.store[name] = value

    def QueryValueEx(self, key, name):
        if name not in key.store:
            raise OSError("missing value: {}".format(name))
        return key.store[name], self.REG_SZ

    def DeleteKey(self, root, path):
        if path not in self.data:
            raise OSError("missing key: {}".format(path))
        del self.data[path]

    def DeleteValue(self, key, name):
        if name not in key.store:
            raise OSError("missing value: {}".format(name))
        del key.store[name]


PORTABLE = magnet_win.keys_for(mi.WINDOWS_PORTABLE)
SETUP = magnet_win.keys_for(mi.WINDOWS_SETUP)


def test_portable_and_setup_use_separate_identities():
    assert PORTABLE.prog_id != SETUP.prog_id
    assert PORTABLE.command_key != SETUP.command_key
    assert PORTABLE.capabilities_key != SETUP.capabilities_key
    assert PORTABLE.app_name != SETUP.app_name


def test_register_records_the_executable_and_advertises_only():
    reg = FakeWinreg()
    magnet_win.register(reg, PORTABLE, r"C:\Cove\Cove.exe")

    assert reg.roots_used == {"HKCU"}  # never HKLM, never elevation
    command = reg.data[PORTABLE.command_key][None]
    assert command == '"C:\\Cove\\Cove.exe" "%1"'
    assert reg.data[PORTABLE.url_associations_key]["magnet"] == PORTABLE.prog_id
    assert (
        reg.data[magnet_win.REGISTERED_APPS_KEY][PORTABLE.app_name]
        == PORTABLE.capabilities_key
    )


def test_registering_portable_leaves_the_setup_registration_alone():
    reg = FakeWinreg()
    magnet_win.register(reg, SETUP, r"C:\Program Files\Cove\Cove.exe")
    magnet_win.register(reg, PORTABLE, r"D:\Portable\Cove.exe")

    assert reg.data[SETUP.command_key][None] == '"C:\\Program Files\\Cove\\Cove.exe" "%1"'
    assert reg.data[PORTABLE.command_key][None] == '"D:\\Portable\\Cove.exe" "%1"'


def test_unregister_refuses_a_registration_owned_by_another_copy():
    reg = FakeWinreg()
    magnet_win.register(reg, PORTABLE, r"D:\Other\Cove.exe")
    assert magnet_win.unregister(reg, PORTABLE, r"D:\Mine\Cove.exe") is False
    assert PORTABLE.command_key in reg.data


def test_unregister_removes_only_its_own_keys():
    reg = FakeWinreg()
    magnet_win.register(reg, SETUP, r"C:\Setup\Cove.exe")
    magnet_win.register(reg, PORTABLE, r"D:\Portable\Cove.exe")

    assert magnet_win.unregister(reg, PORTABLE, r"D:\Portable\Cove.exe") is True
    assert PORTABLE.command_key not in reg.data
    assert SETUP.command_key in reg.data
    assert PORTABLE.app_name not in reg.data[magnet_win.REGISTERED_APPS_KEY]


def test_registered_command_reports_nothing_when_absent():
    assert magnet_win.registered_command(FakeWinreg(), PORTABLE) == ""


def test_registered_executable_unwraps_a_quoted_command():
    assert (
        magnet_win.registered_executable('"C:\\Cove\\Cove.exe" "%1"')
        == r"C:\Cove\Cove.exe"
    )
    assert magnet_win.registered_executable("C:\\Cove\\Cove.exe %1") == r"C:\Cove\Cove.exe"
    assert magnet_win.registered_executable('"unterminated') == ""


def test_same_executable_ignores_case_and_separators():
    assert magnet_win.same_executable(r"C:\Cove\Cove.exe", r"c:\cove\COVE.EXE") is True
    assert magnet_win.same_executable(r"C:\Cove\Cove.exe", r"C:\Other\Cove.exe") is False


def test_is_default_only_when_userchoice_names_our_prog_id():
    reg = FakeWinreg({magnet_win.USER_CHOICE_KEY: {"ProgId": PORTABLE.prog_id}})
    assert magnet_win.is_default(reg, PORTABLE) is True
    assert magnet_win.is_default(reg, SETUP) is False


def test_is_default_is_false_when_no_choice_exists():
    assert magnet_win.is_default(FakeWinreg(), PORTABLE) is False


def test_default_apps_url_deep_links_to_the_named_application():
    url = magnet_win.default_apps_url("Cove Download Manager Portable")
    assert url.startswith("ms-settings:defaultapps?registeredAppUser=")
    assert " " not in url  # the name must be URL-escaped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen python -m pytest tests/test_magnet_win.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'cove.magnet_win'`

- [ ] **Step 3: Write minimal implementation**

Create `cove/magnet_win.py`:

```python
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
    """Windows-appropriate path comparison (case- and separator-insensitive)."""
    import os

    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `QT_QPA_PLATFORM=offscreen python -m pytest tests/test_magnet_win.py -q`
Expected: PASS, 11 passed

- [ ] **Step 5: Delegate from the portable launcher**

Replace `packaging/portable_launcher.py` lines 22-194 (everything from the `PROG_ID` constant block through `handle_registration_args`) with the delegating version below. Keep lines 1-21 (the docstring, imports, `os.environ["COVE_PORTABLE"] = "1"`, and the two flag constants) exactly as they are, and keep `main()` at the end unchanged.

```python
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
```

- [ ] **Step 6: Update the launcher's own tests for the delegation**

`tests/test_portable_magnet_registration.py` imports the launcher by file path, so it no longer sees `register`/`unregister` directly. Change every call of the form `launcher.register(reg, exe)` to go through the shared module instead:

```python
from cove import magnet_win
from cove.magnet_identity import WINDOWS_PORTABLE

KEYS = magnet_win.keys_for(WINDOWS_PORTABLE)
```

and replace `launcher.register(reg, path)` with `magnet_win.register(reg, KEYS, path)`, `launcher.unregister(reg, path)` with `magnet_win.unregister(reg, KEYS, path)`. Tests that exercise `handle_registration_args` argument parsing stay as they are. Do not delete any test: the flag-parsing, non-Windows-refusal, and usage-error cases must all survive.

- [ ] **Step 7: Run the affected suites**

Run:
```bash
QT_QPA_PLATFORM=offscreen python -m pytest \
  tests/test_magnet_win.py \
  tests/test_portable_magnet_registration.py \
  tests/test_magnet_registration.py \
  tests/test_portable.py -q
```
Expected: PASS, all tests, 0 failures

- [ ] **Step 8: Commit**

```bash
git add cove/magnet_win.py tests/test_magnet_win.py \
  packaging/portable_launcher.py tests/test_portable_magnet_registration.py
git commit -m "Move Windows magnet registry primitives into a shared module"
```

---

### Task 3: Linux backend

**Files:**
- Create: `cove/magnet_linux.py`
- Test: `tests/test_magnet_linux.py`

**Interfaces:**
- Consumes: `cove.magnet_identity.APPIMAGE`, `DEBIAN`.
- Produces: `DESKTOP_ID_DEBIAN`, `DESKTOP_ID_APPIMAGE`, `MAGNET_MIME` constants; `desktop_id(identity) -> str`; `escape_exec(path) -> str`; `entry_text(exec_path) -> str`; `entry_exec_path(text) -> str`; `user_entry_path(desktop_id, apps_dir) -> Path`; `write_user_entry(desktop_id, exec_path, apps_dir, run) -> None`; `set_default(run, desktop_id) -> bool`; `query_default(run) -> str`; `remove_user_entry(desktop_id, apps_dir) -> None`.

`run` is a callable `(argv: list[str]) -> tuple[int, str]` returning exit code and stdout, injected everywhere so tests never shell out.

- [ ] **Step 1: Write the failing test**

Create `tests/test_magnet_linux.py`:

```python
"""Linux magnet handler: desktop entry authoring and xdg-mime calls.

Every subprocess is injected. These tests never run xdg-mime and never
write to the real ~/.local/share/applications.
"""
import cove.magnet_identity as mi
from cove import magnet_linux as ml


class FakeRunner:
    """Records argv lists and replays scripted (exit_code, stdout) results."""

    def __init__(self, results=None, missing=False):
        self.calls = []
        self.results = dict(results or {})
        self.missing = missing

    def __call__(self, argv):
        self.calls.append(list(argv))
        if self.missing:
            raise FileNotFoundError(argv[0])
        return self.results.get(argv[0], (0, ""))


def test_debian_and_appimage_ids_never_collide():
    assert ml.desktop_id(mi.DEBIAN) == "cove-download-manager.desktop"
    assert ml.desktop_id(mi.APPIMAGE) == "cove-download-manager-appimage.desktop"
    assert ml.desktop_id(mi.DEBIAN) != ml.desktop_id(mi.APPIMAGE)


def test_exec_escaping_handles_spaces_quotes_and_percent():
    assert ml.escape_exec("/home/a b/Cove.AppImage") == '"/home/a b/Cove.AppImage"'
    # A literal percent must be doubled or the desktop file is unlaunchable.
    assert ml.escape_exec("/tmp/100%/Cove") == '"/tmp/100%%/Cove"'
    assert ml.escape_exec('/tmp/we"ird/Cove') == '"/tmp/we\\"ird/Cove"'
    assert ml.escape_exec("/tmp/do$llar/Cove") == '"/tmp/do\\$llar/Cove"'
    assert ml.escape_exec("/tmp/back\\slash/Cove") == '"/tmp/back\\\\slash/Cove"'


def test_entry_declares_the_magnet_scheme_and_round_trips_the_path():
    text = ml.entry_text("/opt/Cove 3.2.0.AppImage")
    assert "MimeType=x-scheme-handler/magnet;" in text
    assert 'Exec="/opt/Cove 3.2.0.AppImage" %u' in text
    assert ml.entry_exec_path(text) == "/opt/Cove 3.2.0.AppImage"


def test_entry_exec_path_round_trips_a_percent():
    text = ml.entry_text("/tmp/100%/Cove.AppImage")
    assert ml.entry_exec_path(text) == "/tmp/100%/Cove.AppImage"


def test_entry_exec_path_of_junk_is_empty():
    assert ml.entry_exec_path("[Desktop Entry]\nName=x\n") == ""


def test_write_user_entry_creates_the_file_and_refreshes_the_database(tmp_path):
    run = FakeRunner()
    ml.write_user_entry(
        ml.desktop_id(mi.APPIMAGE), "/opt/Cove.AppImage", tmp_path, run
    )
    written = tmp_path / "cove-download-manager-appimage.desktop"
    assert written.is_file()
    assert 'Exec="/opt/Cove.AppImage" %u' in written.read_text()
    assert ["update-desktop-database", str(tmp_path)] in run.calls


def test_a_missing_update_desktop_database_is_not_fatal(tmp_path):
    run = FakeRunner(missing=True)
    ml.write_user_entry(
        ml.desktop_id(mi.APPIMAGE), "/opt/Cove.AppImage", tmp_path, run
    )
    assert (tmp_path / "cove-download-manager-appimage.desktop").is_file()


def test_set_default_reports_only_what_the_query_confirms():
    desktop_id = ml.desktop_id(mi.APPIMAGE)
    confirmed = FakeRunner(results={"xdg-mime": (0, desktop_id + "\n")})
    assert ml.set_default(confirmed, desktop_id) is True
    assert ["xdg-mime", "default", desktop_id, ml.MAGNET_MIME] in confirmed.calls
    assert ["xdg-mime", "query", "default", ml.MAGNET_MIME] in confirmed.calls


def test_set_default_is_false_when_the_desktop_declined():
    desktop_id = ml.desktop_id(mi.APPIMAGE)
    declined = FakeRunner(results={"xdg-mime": (0, "org.qbittorrent.qBittorrent.desktop\n")})
    assert ml.set_default(declined, desktop_id) is False


def test_set_default_is_false_when_xdg_mime_is_missing():
    assert ml.set_default(FakeRunner(missing=True), ml.desktop_id(mi.APPIMAGE)) is False


def test_query_default_is_empty_when_xdg_mime_is_missing():
    assert ml.query_default(FakeRunner(missing=True)) == ""


def test_remove_user_entry_is_idempotent(tmp_path):
    desktop_id = ml.desktop_id(mi.APPIMAGE)
    ml.remove_user_entry(desktop_id, tmp_path)  # absent: must not raise
    (tmp_path / desktop_id).write_text("x")
    ml.remove_user_entry(desktop_id, tmp_path)
    assert not (tmp_path / desktop_id).exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen python -m pytest tests/test_magnet_linux.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'cove.magnet_linux'`

- [ ] **Step 3: Write minimal implementation**

Create `cove/magnet_linux.py`:

```python
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
    """Default runner: exit code and stdout, stderr discarded."""
    proc = subprocess.run(
        list(argv), capture_output=True, text=True, timeout=15, check=False
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `QT_QPA_PLATFORM=offscreen python -m pytest tests/test_magnet_linux.py -q`
Expected: PASS, 12 passed

- [ ] **Step 5: Commit**

```bash
git add cove/magnet_linux.py tests/test_magnet_linux.py
git commit -m "Add Linux magnet handler backend"
```

---

### Task 4: Platform-agnostic facade

**Files:**
- Create: `cove/magnet_handler.py`
- Test: `tests/test_magnet_handler.py`

**Interfaces:**
- Consumes: `cove.magnet_identity`, `cove.magnet_win`, `cove.magnet_linux`.
- Produces: `HandlerStatus` dataclass with fields `supported: bool, identity: str, registered: bool, owned_by_cove: bool, is_default: bool, stale: bool, recorded_path: str`; `Result` dataclass with `ok: bool, message: str`; `status() -> HandlerStatus`; `enable() -> Result`; `disable() -> Result`; `repair() -> bool`; `default_apps_url() -> str`.

Windows `enable()` never sets the default (it cannot). Linux `enable()` sets it and verifies. `repair()` rewrites paths only.

- [ ] **Step 1: Write the failing test**

Create `tests/test_magnet_handler.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen python -m pytest tests/test_magnet_handler.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'cove.magnet_handler'`

- [ ] **Step 3: Write minimal implementation**

Create `cove/magnet_handler.py`:

```python
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
    supported: bool = False
    identity: str = magnet_identity.UNSUPPORTED
    registered: bool = False
    owned_by_cove: bool = False
    is_default: bool = False
    stale: bool = False
    recorded_path: str = ""


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
        default = magnet_win.is_default(winreg, keys)
    except Exception:
        return HandlerStatus(supported=True, identity=identity)
    recorded = magnet_win.registered_executable(command) if command else ""
    return HandlerStatus(
        supported=True,
        identity=identity,
        registered=bool(recorded),
        owned_by_cove=bool(recorded),
        is_default=default,
        stale=bool(recorded) and not magnet_win.same_executable(recorded, current),
        recorded_path=recorded,
    )


def _linux_status(identity: str, current: str) -> HandlerStatus:
    desktop_id = magnet_linux.desktop_id(identity)
    default = magnet_linux.query_default(_run) == desktop_id
    if identity == magnet_identity.DEBIAN:
        # The package owns the entry; its path is stable and never stale.
        return HandlerStatus(
            supported=True,
            identity=identity,
            registered=True,
            owned_by_cove=True,
            is_default=default,
            stale=False,
            recorded_path=current,
        )
    path = magnet_linux.user_entry_path(desktop_id, _apps_dir())
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return HandlerStatus(supported=True, identity=identity, is_default=default)
    recorded = magnet_linux.entry_exec_path(text)
    return HandlerStatus(
        supported=True,
        identity=identity,
        registered=bool(recorded),
        owned_by_cove=bool(recorded),
        is_default=default,
        stale=bool(recorded) and recorded != current,
        recorded_path=recorded,
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
        magnet_win.unregister(winreg, keys, _registration_path())
    except Exception:
        return Result(False, "Could not remove Cove's magnet registration.")
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
    return True


def default_apps_url() -> str:
    """Windows deep link to Cove's page in Settings > Default apps."""
    identity = _identity()
    if identity not in _WINDOWS:
        return ""
    from . import magnet_win

    return magnet_win.default_apps_url(magnet_win.keys_for(identity).app_name)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `QT_QPA_PLATFORM=offscreen python -m pytest tests/test_magnet_handler.py -q`
Expected: PASS, 8 passed

- [ ] **Step 5: Commit**

```bash
git add cove/magnet_handler.py tests/test_magnet_handler.py
git commit -m "Add platform-agnostic magnet handler facade"
```

---

### Task 5: Settings fields and migration of existing opt-ins

**Files:**
- Modify: `cove/config.py:151` (add two fields near `close_to_tray`), `cove/config.py:228-245` (add the migration in `load`)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `cove.magnet_handler.status`.
- Produces: `Settings.magnet_handler_enabled: bool`, `Settings.magnet_prompt_shown: bool`, and `Settings.magnet_setting_missing: bool` (a non-persisted marker set by `load()` so Task 6 can run the one-time migration).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_magnet_fields_default_off_and_flag_absence(tmp_path, monkeypatch):
    from cove import config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text("{}")

    s = config.Settings.load()
    assert s.magnet_handler_enabled is False
    assert s.magnet_prompt_shown is False
    # Absent, so the one-time migration is allowed to consider it.
    assert s.magnet_setting_missing is True


def test_explicit_false_is_never_treated_as_absent(tmp_path, monkeypatch):
    from cove import config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text('{"magnet_handler_enabled": false}')

    s = config.Settings.load()
    assert s.magnet_handler_enabled is False
    assert s.magnet_setting_missing is False


def test_hand_edited_non_boolean_is_not_read_as_enabled(tmp_path, monkeypatch):
    from cove import config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text('{"magnet_handler_enabled": "yes"}')

    s = config.Settings.load()
    assert s.magnet_handler_enabled is False


def test_magnet_setting_missing_is_not_persisted(tmp_path, monkeypatch):
    import json

    from cove import config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text("{}")

    s = config.Settings.load()
    s.save()
    raw = json.loads((tmp_path / "settings.json").read_text())
    assert "magnet_setting_missing" not in raw
```

- [ ] **Step 2: Run test to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen python -m pytest tests/test_config.py -q`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'magnet_handler_enabled'`

- [ ] **Step 3: Add the fields**

In `cove/config.py`, immediately after the `close_to_tray: bool = False` line (currently line 151), insert:

```python
    # Whether Cove keeps its magnet-handler registration repaired after an
    # update changes the executable path. This is NOT a claim that Cove is
    # the current default: only the OS knows that, and it is read live.
    magnet_handler_enabled: bool = False
    # Records that the one-time "make Cove your magnet handler" offer has
    # been made. Like torrent_ip_disclosure_shown, this stores a decision,
    # it is not a user-facing option.
    magnet_prompt_shown: bool = False
```

- [ ] **Step 4: Add the absence marker and validation**

`Settings.save()` serialises the dataclass, so the marker must not be a dataclass field. Add it as a plain attribute.

In `cove/config.py`, next to the existing `speed_limit_unit_missing = "speed_limit_unit" not in raw` line (currently line 228), add:

```python
        magnet_setting_missing = "magnet_handler_enabled" not in raw
```

Then next to the existing `close_to_tray` validation (currently lines 243-245), add:

```python
        # Same reasoning as close_to_tray: a hand-edited non-boolean must not
        # be read as "enabled" via Python truthiness.
        if not isinstance(s.magnet_handler_enabled, bool):
            s.magnet_handler_enabled = False
        if not isinstance(s.magnet_prompt_shown, bool):
            s.magnet_prompt_shown = False
        # Not a dataclass field, so it is never written back to settings.json.
        # Task 6 consumes it once, at startup, to migrate existing opt-ins.
        s.magnet_setting_missing = magnet_setting_missing
```

Also set the marker on both early-return paths in `load()` (the no-config-file path and the corrupted-file path), immediately before each `return s`:

```python
            s.magnet_setting_missing = False
```

A brand new config is not an existing opt-in, so migration must not fire for it.

- [ ] **Step 5: Run test to verify it passes**

Run: `QT_QPA_PLATFORM=offscreen python -m pytest tests/test_config.py -q`
Expected: PASS, all tests

- [ ] **Step 6: Commit**

```bash
git add cove/config.py tests/test_config.py
git commit -m "Add magnet handler settings fields"
```

---

### Task 6: Startup migration and self-heal

**Files:**
- Modify: `cove/app.py:320-327` (add a daemon thread next to the existing `_register_native_hosts` one)
- Create: `cove/magnet_startup.py`
- Test: `tests/test_magnet_startup.py`

**Interfaces:**
- Consumes: `cove.magnet_handler.status`, `cove.magnet_handler.repair`; `Settings.magnet_handler_enabled`, `magnet_prompt_shown`, `magnet_setting_missing`.
- Produces: `migrate_and_repair(settings, status_fn=None, repair_fn=None) -> None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_magnet_startup.py`:

```python
"""Startup migration and self-heal. Never touches a real registry or MIME db."""
from dataclasses import dataclass

from cove import magnet_startup
from cove.magnet_handler import HandlerStatus


@dataclass
class FakeSettings:
    magnet_handler_enabled: bool = False
    magnet_prompt_shown: bool = False
    magnet_setting_missing: bool = False
    saved: bool = False

    def save(self):
        self.saved = True


def test_absent_setting_with_cove_registration_migrates_and_repairs():
    settings = FakeSettings(magnet_setting_missing=True)
    calls = []
    magnet_startup.migrate_and_repair(
        settings,
        status_fn=lambda: HandlerStatus(
            supported=True, registered=True, owned_by_cove=True, stale=True
        ),
        repair_fn=lambda: calls.append("repair") or True,
    )
    assert settings.magnet_handler_enabled is True
    assert settings.magnet_prompt_shown is True
    assert settings.saved is True
    assert calls == ["repair"]


def test_absent_setting_without_a_registration_does_not_migrate():
    settings = FakeSettings(magnet_setting_missing=True)
    calls = []
    magnet_startup.migrate_and_repair(
        settings,
        status_fn=lambda: HandlerStatus(supported=True, registered=False),
        repair_fn=lambda: calls.append("repair") or True,
    )
    assert settings.magnet_handler_enabled is False
    assert settings.magnet_prompt_shown is False
    assert calls == []


def test_absent_setting_with_a_foreign_registration_does_not_migrate():
    settings = FakeSettings(magnet_setting_missing=True)
    magnet_startup.migrate_and_repair(
        settings,
        status_fn=lambda: HandlerStatus(
            supported=True, registered=True, owned_by_cove=False
        ),
        repair_fn=lambda: True,
    )
    assert settings.magnet_handler_enabled is False


def test_explicit_false_is_respected_and_never_repairs():
    settings = FakeSettings(magnet_handler_enabled=False, magnet_setting_missing=False)
    calls = []
    magnet_startup.migrate_and_repair(
        settings,
        status_fn=lambda: HandlerStatus(
            supported=True, registered=True, owned_by_cove=True, stale=True
        ),
        repair_fn=lambda: calls.append("repair") or True,
    )
    assert settings.magnet_handler_enabled is False
    assert calls == []


def test_enabled_setting_repairs_a_stale_registration():
    settings = FakeSettings(magnet_handler_enabled=True)
    calls = []
    magnet_startup.migrate_and_repair(
        settings,
        status_fn=lambda: HandlerStatus(
            supported=True, registered=True, owned_by_cove=True, stale=True
        ),
        repair_fn=lambda: calls.append("repair") or True,
    )
    assert calls == ["repair"]


def test_a_failing_repair_never_propagates():
    settings = FakeSettings(magnet_handler_enabled=True)

    def boom():
        raise OSError("registry unavailable")

    magnet_startup.migrate_and_repair(
        settings,
        status_fn=lambda: HandlerStatus(
            supported=True, registered=True, owned_by_cove=True, stale=True
        ),
        repair_fn=boom,
    )  # must not raise


def test_a_failing_status_never_propagates():
    def boom():
        raise OSError("registry unavailable")

    magnet_startup.migrate_and_repair(FakeSettings(), status_fn=boom)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen python -m pytest tests/test_magnet_startup.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'cove.magnet_startup'`

- [ ] **Step 3: Write minimal implementation**

Create `cove/magnet_startup.py`:

```python
"""One-time migration plus the self-heal run, both done at startup.

Runs off the GUI thread. Nothing here may raise into the caller: a magnet
association is never worth blocking the window or failing a launch.
"""
from . import magnet_handler


def migrate_and_repair(settings, status_fn=None, repair_fn=None) -> None:
    status_fn = status_fn or magnet_handler.status
    repair_fn = repair_fn or magnet_handler.repair

    try:
        state = status_fn()
    except Exception:
        return

    # Migration: users who registered through the installer or the CLI flag
    # have no setting at all. Reading that absence as False would strand them
    # without self-heal despite having opted in. An explicit False is a real
    # decision and is never overridden.
    if getattr(settings, "magnet_setting_missing", False):
        if state.supported and state.registered and state.owned_by_cove:
            settings.magnet_handler_enabled = True
            settings.magnet_prompt_shown = True
            try:
                settings.save()
            except Exception:
                return
        settings.magnet_setting_missing = False

    if not getattr(settings, "magnet_handler_enabled", False):
        return
    if not (state.supported and state.registered and state.owned_by_cove):
        return
    if not state.stale:
        return
    try:
        repair_fn()
    except Exception:
        return
```

- [ ] **Step 4: Run test to verify it passes**

Run: `QT_QPA_PLATFORM=offscreen python -m pytest tests/test_magnet_startup.py -q`
Expected: PASS, 7 passed

- [ ] **Step 5: Wire it into startup**

In `cove/app.py`, immediately after the existing native-host thread block (currently lines 323-327), add:

```python
    # Same reasoning as the native-host thread above: registry and xdg-mime
    # work can block, and a magnet association must never delay the window.
    def _heal_magnet_handler() -> None:
        from .magnet_startup import migrate_and_repair

        migrate_and_repair(settings)

    threading.Thread(
        target=_heal_magnet_handler, name="magnet-self-heal", daemon=True
    ).start()
```

- [ ] **Step 6: Run the startup suite**

Run: `QT_QPA_PLATFORM=offscreen python -m pytest tests/test_app_launch.py tests/test_magnet_startup.py -q`
Expected: PASS, all tests

- [ ] **Step 7: Commit**

```bash
git add cove/magnet_startup.py cove/app.py tests/test_magnet_startup.py
git commit -m "Migrate existing magnet opt-ins and self-heal stale paths at startup"
```

---

### Task 7: Settings UI row

**Files:**
- Modify: `cove/dialogs.py:606-625` (add a "Magnet links" row after the close-to-tray row)
- Test: `tests/test_dialogs.py`

**Interfaces:**
- Consumes: `cove.magnet_handler.status`, `enable`, `disable`, `default_apps_url`.
- Produces: `SettingsDialog.magnet_status_label`, `SettingsDialog.magnet_action_btn`, `SettingsDialog.magnet_remove_btn`, `SettingsDialog.magnet_repair_check`, `SettingsDialog._magnet_status_text(state) -> str`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_dialogs.py`:

```python
def test_magnet_status_text_never_claims_default_without_confirmation(qtbot, settings):
    from cove.dialogs import SettingsDialog
    from cove.magnet_handler import HandlerStatus
    import cove.magnet_identity as mi

    dialog = SettingsDialog(settings)
    qtbot.addWidget(dialog)

    registered_only = HandlerStatus(
        supported=True, identity=mi.WINDOWS_PORTABLE, registered=True,
        owned_by_cove=True, is_default=False,
    )
    text = dialog._magnet_status_text(registered_only)
    assert "not currently selected as default" in text
    assert "Cove is the current default" not in text

    confirmed = HandlerStatus(
        supported=True, identity=mi.WINDOWS_PORTABLE, registered=True,
        owned_by_cove=True, is_default=True,
    )
    assert "Cove is the current default" in dialog._magnet_status_text(confirmed)


def test_magnet_row_explains_an_unsupported_build(qtbot, settings):
    from cove.dialogs import SettingsDialog
    from cove.magnet_handler import HandlerStatus

    dialog = SettingsDialog(settings)
    qtbot.addWidget(dialog)

    text = dialog._magnet_status_text(HandlerStatus(supported=False))
    assert "installed or portable build" in text
```

Use the existing fixtures in `tests/test_dialogs.py` for `qtbot` and `settings`; match how the neighbouring settings tests construct `SettingsDialog`.

- [ ] **Step 2: Run test to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen python -m pytest tests/test_dialogs.py -k magnet -q`
Expected: FAIL with `AttributeError: 'SettingsDialog' object has no attribute '_magnet_status_text'`

- [ ] **Step 3: Write minimal implementation**

In `cove/dialogs.py`, after the close-to-tray block (currently ending at line 625), add:

```python
        # Magnet links. Actions plus live status, deliberately not a
        # checkbox: on Windows a checkbox would stay ticked after the user
        # closed Settings without choosing Cove, stating something false.
        from . import magnet_handler
        from .magnet_identity import WINDOWS_PORTABLE, WINDOWS_SETUP

        self._magnet_handler = magnet_handler
        magnet_state = magnet_handler.status()
        self.magnet_status_label = QLabel(self._magnet_status_text(magnet_state))
        self.magnet_status_label.setProperty("role", "muted")
        self.magnet_status_label.setWordWrap(True)

        is_windows = magnet_state.identity in (WINDOWS_SETUP, WINDOWS_PORTABLE)
        self.magnet_action_btn = QPushButton(
            "Choose Cove as default" if is_windows else "Make Cove default"
        )
        self.magnet_remove_btn = QPushButton("Remove Cove registration")
        self.magnet_repair_check = QCheckBox(
            "Repair Cove's magnet registration after updates"
        )
        self.magnet_repair_check.setChecked(
            bool(getattr(settings, "magnet_handler_enabled", False))
        )

        magnet_buttons = QHBoxLayout()
        magnet_buttons.addWidget(self.magnet_action_btn)
        magnet_buttons.addWidget(self.magnet_remove_btn)
        magnet_buttons.addStretch(1)

        magnet_box = QVBoxLayout()
        magnet_box.addWidget(self.magnet_status_label)
        magnet_box.addLayout(magnet_buttons)
        magnet_box.addWidget(self.magnet_repair_check)
        form.addRow("Magnet links", magnet_box)

        if not magnet_state.supported:
            self.magnet_action_btn.setEnabled(False)
            self.magnet_remove_btn.setEnabled(False)
            self.magnet_repair_check.setEnabled(False)
            self.magnet_repair_check.setChecked(False)

        self.magnet_action_btn.clicked.connect(self._on_magnet_enable)
        self.magnet_remove_btn.clicked.connect(self._on_magnet_disable)
```

Then add these methods to `SettingsDialog`:

```python
    def _magnet_status_text(self, state) -> str:
        """Wording derived from the system, never from the stored setting."""
        if not state.supported:
            return (
                "Magnet registration needs an installed or portable build. "
                "Running Cove from source cannot register a stable path."
            )
        if state.is_default:
            return "Status: Cove is the current default"
        if state.registered:
            return "Status: Registered, but not currently selected as default"
        return "Status: Not registered"

    def _refresh_magnet_status(self) -> None:
        self.magnet_status_label.setText(
            self._magnet_status_text(self._magnet_handler.status())
        )

    def _on_magnet_enable(self) -> None:
        result = self._magnet_handler.enable()
        url = self._magnet_handler.default_apps_url()
        if url:
            QDesktopServices.openUrl(QUrl(url))
        self._refresh_magnet_status()
        if not result.ok and result.message:
            QMessageBox.information(self, "Magnet links", result.message)

    def _on_magnet_disable(self) -> None:
        result = self._magnet_handler.disable()
        self._refresh_magnet_status()
        if not result.ok and result.message:
            QMessageBox.information(self, "Magnet links", result.message)
            return
        self.magnet_repair_check.setChecked(False)
```

Add any missing imports (`QPushButton`, `QHBoxLayout`, `QVBoxLayout`, `QMessageBox`, `QDesktopServices`, `QUrl`) to the existing import blocks at the top of `cove/dialogs.py`; several are already imported, so check before adding.

- [ ] **Step 4: Persist the repair preference**

In `cove/dialogs.py`, directly after the existing line 1033:

```python
        self.settings.close_to_tray = self.close_to_tray.isChecked()
```

add:

```python
        self.settings.magnet_handler_enabled = self.magnet_repair_check.isChecked()
```

Note this writes to `self.settings`, the shared `Settings` object `MainWindow`
already holds, matching the neighbouring lines.

- [ ] **Step 5: Run test to verify it passes**

Run: `QT_QPA_PLATFORM=offscreen python -m pytest tests/test_dialogs.py -q`
Expected: PASS, all tests

- [ ] **Step 6: Commit**

```bash
git add cove/dialogs.py tests/test_dialogs.py
git commit -m "Add magnet links row to settings"
```

---

### Task 8: One-time contextual offer

**Files:**
- Modify: `cove/main_window.py` (the handler that adds a magnet or torrent by hand)
- Test: `tests/test_magnet_prompt.py`

**Interfaces:**
- Consumes: `cove.magnet_handler.status`, `enable`; `Settings.magnet_prompt_shown`.
- Produces: `MainWindow._maybe_offer_magnet_handler() -> bool` (True when the offer was shown).

- [ ] **Step 1: Write the failing test**

Create `tests/test_magnet_prompt.py`:

```python
"""The one-time offer made on the first hand-added magnet or torrent."""
from cove.magnet_handler import HandlerStatus, Result


class FakeSettings:
    def __init__(self, shown=False):
        self.magnet_prompt_shown = shown
        self.magnet_handler_enabled = False
        self.saved = False

    def save(self):
        self.saved = True


def _offer(monkeypatch, settings, state, answer):
    """Drive MainWindow._maybe_offer_magnet_handler with everything stubbed."""
    from cove import main_window as mw

    monkeypatch.setattr(mw.magnet_handler, "status", lambda: state)
    monkeypatch.setattr(mw.magnet_handler, "enable", lambda: Result(True, "ok"))
    monkeypatch.setattr(mw, "_ask_magnet_offer", lambda parent: answer)
    return mw.MainWindow._maybe_offer_magnet_handler(
        type("Stub", (), {"settings": settings})()
    )


def test_offer_is_made_once_and_records_that_it_was(monkeypatch):
    settings = FakeSettings()
    state = HandlerStatus(supported=True, registered=False, is_default=False)
    assert _offer(monkeypatch, settings, state, True) is True
    assert settings.magnet_prompt_shown is True
    assert settings.saved is True


def test_declining_still_records_that_the_offer_was_made(monkeypatch):
    settings = FakeSettings()
    state = HandlerStatus(supported=True, registered=False, is_default=False)
    assert _offer(monkeypatch, settings, state, False) is True
    assert settings.magnet_prompt_shown is True


def test_offer_never_repeats(monkeypatch):
    settings = FakeSettings(shown=True)
    state = HandlerStatus(supported=True, registered=False, is_default=False)
    assert _offer(monkeypatch, settings, state, True) is False


def test_no_offer_when_cove_is_already_the_default(monkeypatch):
    settings = FakeSettings()
    state = HandlerStatus(supported=True, registered=True, is_default=True)
    assert _offer(monkeypatch, settings, state, True) is False
    assert settings.magnet_prompt_shown is False


def test_no_offer_on_an_unsupported_build(monkeypatch):
    settings = FakeSettings()
    assert _offer(monkeypatch, settings, HandlerStatus(supported=False), True) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen python -m pytest tests/test_magnet_prompt.py -q`
Expected: FAIL with `AttributeError: module 'cove.main_window' has no attribute 'magnet_handler'`

- [ ] **Step 3: Write minimal implementation**

In `cove/main_window.py`, add the import near the other `cove` imports:

```python
from . import magnet_handler
```

Add the module-level helper (kept separate so tests can replace the dialog):

```python
def _ask_magnet_offer(parent) -> bool:
    """Ask whether Cove should handle magnet links. True when accepted."""
    answer = QMessageBox.question(
        parent,
        "Magnet links",
        "Open magnet links with Cove from now on?\n\n"
        "You can change this later in Settings.",
        QMessageBox.Yes | QMessageBox.No,
        QMessageBox.Yes,
    )
    return answer == QMessageBox.Yes
```

Add the method to `MainWindow`:

```python
    def _maybe_offer_magnet_handler(self) -> bool:
        """Offer once, the first time the user adds a magnet or torrent.

        A first-run prompt would arrive before the user knows what Cove does.
        Someone who just pasted a magnet has demonstrated the exact need.
        Returns True when the offer was shown.
        """
        settings = self.settings
        if getattr(settings, "magnet_prompt_shown", False):
            return False
        try:
            state = magnet_handler.status()
        except Exception:
            return False
        if not state.supported or state.is_default:
            return False

        accepted = _ask_magnet_offer(self)
        settings.magnet_prompt_shown = True
        if accepted:
            magnet_handler.enable()
            # Set regardless of whether enable() could confirm the default.
            # On Windows it never can: the user still has to choose Cove in
            # Settings. The preference means "keep the registration
            # repaired", which is what an accepting user wants either way.
            settings.magnet_handler_enabled = True
        try:
            settings.save()
        except Exception:
            pass
        return True
```

- [ ] **Step 4: Call it from the manual add path**

The only call site is `MainWindow._add_download` (`cove/main_window.py:928`), which
is the Add dialog's accept path. It has two successful branches.

In the `.torrent` branch, after the existing `self.queue.add_torrent_file(...)`
call and before its `return`:

```python
            self._maybe_offer_magnet_handler()
```

In the URL branch, after the existing `self.add_urls_checked(urls)` line:

```python
        from . import torrent

        if any(torrent.is_magnet(u) for u in urls):
            self._maybe_offer_magnet_handler()
```

`cove/main_window.py` does not import `torrent` at module level and does not use
`is_magnet` anywhere today, so the local import above is required. Do not add a
module-level import for it.

Do NOT call this from `_add_from_clipboard` (`cove/main_window.py:974`), the
native-messaging path, or the local API. A modal question is unwelcome during a
batch paste and impossible to answer during a background handoff.

- [ ] **Step 5: Run test to verify it passes**

Run: `QT_QPA_PLATFORM=offscreen python -m pytest tests/test_magnet_prompt.py -q`
Expected: PASS, 5 passed

- [ ] **Step 6: Commit**

```bash
git add cove/main_window.py tests/test_magnet_prompt.py
git commit -m "Offer the magnet handler once on a hand-added magnet"
```

---

### Task 9: Documentation and full verification

**Files:**
- Modify: `README.md:158-207` (the "Opening magnet links from your browser" section)

- [ ] **Step 1: Rewrite the magnet section**

Replace `README.md` lines 170-207 (from `**Linux (AppImage)**` through the end of the portable block) with:

```markdown
Cove can register itself from **Settings -> Magnet links**. The row shows
what the operating system currently reports, not what Cove would prefer, so
a default changed elsewhere is visible immediately.

**Keeping it working after an update**

The portable executable and the AppImage carry their version in the file
name, so an update changes the path the registration points at. When
"Repair Cove's magnet registration after updates" is on, Cove repairs its
own registration the next time the updated build is launched.

The precise promise: after the updated build is launched once, Cove repairs
a stale registration it already owns. It does not silently reclaim a default
you changed to another application. If you delete the old file and click a
magnet link before ever launching the new one, Cove has not run yet and
cannot repair anything.

Cove only ever repairs a registration it owns. A portable copy and an
installed copy keep separate identities and never modify each other.

**Linux**

The AppImage installs its own desktop entry
(`cove-download-manager-appimage.desktop`) and sets the default directly.
The `.deb` ships its entry system-wide, so Cove only needs to set the
default. Cove verifies the result afterwards and reports honestly if your
desktop declined the change.

**Windows**

Since Windows 10, no application may make itself the default handler. Cove
registers itself as capable and opens Settings at its own entry; the final
choice is yours. Registration is per-user and needs no administrator rights.

The command-line flags still work for scripted setups:

```powershell
.\Cove-Download-Manager-<version>-Portable.exe --register-magnet-handler
.\Cove-Download-Manager-<version>-Portable.exe --unregister-magnet-handler
```

**Running from source**

A source checkout cannot register: the path it would record belongs to the
Python interpreter, not to Cove. Settings explains this rather than failing
silently.
```

- [ ] **Step 2: Verify no em dashes or en dashes**

Run: `grep -n "—\|–" README.md cove/magnet_*.py tests/test_magnet_*.py`
Expected: no output

- [ ] **Step 3: Run the full suite and check the exit status**

Run:
```bash
set +e
QT_QPA_PLATFORM=offscreen python -m pytest tests/ -q
status=$?
echo "pytest exit=$status"
test "$status" -eq 0
```
Expected: all tests pass AND `pytest exit=0`

- [ ] **Step 4: Static checks**

Run:
```bash
python -m py_compile cove/magnet_identity.py cove/magnet_win.py \
  cove/magnet_linux.py cove/magnet_handler.py cove/magnet_startup.py \
  cove/config.py cove/dialogs.py cove/main_window.py cove/app.py \
  packaging/portable_launcher.py
ruff check cove/magnet_identity.py cove/magnet_win.py cove/magnet_linux.py \
  cove/magnet_handler.py cove/magnet_startup.py
git diff --check
```
Expected: no errors

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "Document in-app magnet registration and self-heal"
```

---

## Manual verification

Automated tests never touch a real registry or MIME database, so these rows must
be run by hand before release. Record every unexecuted row honestly.

**Windows 11, portable**
1. Launch the portable exe, Settings -> Magnet links, click "Choose Cove as default".
2. Confirm Settings opens **at Cove's own entry**, not the generic Default apps list. This verifies the `registeredAppUser` deep link.
3. Pick Cove, reopen Cove's Settings, confirm the status reads "Cove is the current default".
4. Rename the exe to simulate an update, relaunch, confirm the registry command now names the new file and magnet links still open Cove.

**Windows 10, portable**
5. Repeat step 2. If the deep link is not honoured, confirm it falls back to the generic Default apps page rather than failing.

**Windows, installed**
6. With both an installed and a portable copy present, register the portable one and confirm the installed copy's registration is untouched, and the reverse.

**Linux, AppImage**
7. Settings -> Magnet links -> "Make Cove default". Confirm the status becomes "Cove is the current default" and a magnet link in the browser opens Cove.
8. Confirm `~/.local/share/applications/cove-download-manager-appimage.desktop` exists and the Debian entry, if present, is untouched.
9. Rename the AppImage to simulate an update, relaunch, confirm the entry's `Exec` now names the new file.
10. Set another handler as default outside Cove, relaunch Cove, confirm Cove does NOT take the default back and Settings reports the truth.

**Both platforms**
11. Confirm the one-time offer appears on the first hand-added magnet and never again, whichever way it is answered.
12. Confirm an existing user who registered before this change gets migrated: with a Cove-owned registration and no setting in `settings.json`, launching once sets `magnet_handler_enabled` to true.
13. Confirm the pre-first-launch gap is real and documented: delete the old build, click a magnet before launching the new one, and confirm nothing repairs it. This limitation is expected.

## Out of scope

Stable portable naming and a permanent launcher stub are a separate, isolated
design decision. This plan must not change portable artifact naming, updater
matching, packaging output names, or release workflow logic.
