"""Unit tests for the portable build's opt-in magnet-handler registration.

The registry is faked end to end -- these tests never import the real winreg
module and never touch the machine's registry or default handlers.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_PATH = ROOT / "packaging" / "portable_launcher.py"


@pytest.fixture
def launcher(monkeypatch):
    """Import portable_launcher without leaking COVE_PORTABLE into the suite."""
    monkeypatch.setenv("COVE_PORTABLE", "0")
    spec = importlib.util.spec_from_file_location(
        "cove_portable_launcher_under_test", LAUNCHER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeKey:
    def __init__(self, store, path):
        self.store = store
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWinreg:
    """Minimal in-memory stand-in for the winreg module."""

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
        return FakeKey(self.data[path], path)

    def OpenKey(self, root, path, reserved=0, access=0):
        self.roots_used.add(root)
        if path not in self.data:
            raise OSError("missing key: {}".format(path))
        return FakeKey(self.data[path], path)

    def SetValueEx(self, key, name, reserved, value_type, value):
        key.store[name] = value

    def QueryValueEx(self, key, name):
        if name not in key.store:
            raise OSError("missing value")
        return key.store[name], self.REG_SZ

    def DeleteKey(self, root, path):
        self.roots_used.add(root)
        if path not in self.data:
            raise OSError("missing key: {}".format(path))
        del self.data[path]

    def DeleteValue(self, key, name):
        if name not in key.store:
            raise OSError("missing value")
        del key.store[name]


@pytest.fixture
def windows(monkeypatch):
    """Pretend to run on Windows with a fake registry installed."""
    fake = FakeWinreg()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake)
    return fake


EXE = "/opt/cove/Cove-Portable.exe"
OTHER_EXE = "/somewhere/else/Cove-Portable.exe"


def _register(launcher, fake, exe=EXE):
    return launcher.register(fake, exe)


# ------------------------------------------------------------ normal launch


def test_normal_launch_does_not_register_anything(launcher):
    assert launcher.handle_registration_args(["cove.exe"]) is None


def test_ordinary_arguments_still_reach_cove(launcher):
    argv = ["cove.exe", "magnet:?xt=urn:btih:" + "0" * 40]
    assert launcher.handle_registration_args(argv) is None


def test_register_flag_does_not_launch_the_gui(launcher, windows, monkeypatch):
    def explode():
        raise AssertionError("GUI must not start during registration")

    monkeypatch.setitem(
        sys.modules, "cove.entry", type(sys)("cove.entry")
    )
    sys.modules["cove.entry"].main = explode
    assert launcher.main(["cove.exe", launcher.REGISTER_FLAG]) == launcher.EXIT_OK


def test_unregister_flag_does_not_launch_the_gui(launcher, windows, monkeypatch):
    def explode():
        raise AssertionError("GUI must not start during unregistration")

    monkeypatch.setitem(sys.modules, "cove.entry", type(sys)("cove.entry"))
    sys.modules["cove.entry"].main = explode
    assert launcher.main(["cove.exe", launcher.UNREGISTER_FLAG]) == launcher.EXIT_OK


# -------------------------------------------------------------- registration


def test_register_writes_hkcu_only(launcher, windows):
    _register(launcher, windows)
    assert windows.roots_used == {"HKCU"}


def test_register_writes_a_fully_quoted_open_command(launcher, windows):
    _register(launcher, windows)
    command = windows.data[launcher.COMMAND_KEY][None]
    assert command == '"{}" "%1"'.format(EXE)
    assert command.endswith('"%1"')


def test_register_advertises_magnet_capability(launcher, windows):
    _register(launcher, windows)
    assert windows.data[launcher.PROG_ID_KEY]["URL Protocol"] == ""
    assert windows.data[launcher.URL_ASSOCIATIONS_KEY]["magnet"] == launcher.PROG_ID
    registered = windows.data[launcher.REGISTERED_APPS_KEY]
    assert registered[launcher.APP_NAME] == launcher.CAPABILITIES_KEY


def test_register_does_not_force_the_active_default(launcher, windows):
    _register(launcher, windows)
    for path in windows.data:
        assert not path.lower().startswith("software\\classes\\magnet")
        assert "UserChoice" not in path


def test_portable_identity_is_separate_from_the_installer(launcher, windows):
    # The Setup installer owns Cove.Magnet and the non-portable capability
    # path. Sharing them would let either build delete the other's
    # registration on unregister/uninstall.
    installer_iss = (
        Path(__file__).resolve().parents[1] / "packaging" / "installer.iss"
    ).read_text(encoding="utf-8")
    assert 'Subkey: "Software\\Classes\\Cove.Magnet"' in installer_iss
    assert "Cove.Magnet.Portable" not in installer_iss

    assert launcher.PROG_ID == "Cove.Magnet.Portable"
    assert launcher.APP_NAME != "Cove Download Manager"

    _register(launcher, windows)
    for path in windows.data:
        assert not path.startswith("Software\\Classes\\Cove.Magnet\\")
        assert path != "Software\\Classes\\Cove.Magnet"
        assert "Software\\Cove\\Cove Download Manager\\" not in path
    registered = windows.data[launcher.REGISTERED_APPS_KEY]
    assert "Cove Download Manager" not in registered


def test_register_is_idempotent(launcher, windows):
    _register(launcher, windows)
    first = {path: dict(values) for path, values in windows.data.items()}
    _register(launcher, windows)
    assert windows.data == first


def test_register_after_a_move_rewrites_the_command(launcher, windows):
    _register(launcher, windows)
    _register(launcher, windows, exe=OTHER_EXE)
    assert windows.data[launcher.COMMAND_KEY][None] == '"{}" "%1"'.format(OTHER_EXE)


# ------------------------------------------------------------ unregistration


def test_unregister_removes_keys_this_executable_owns(launcher, windows):
    _register(launcher, windows)
    assert launcher.unregister(windows, EXE) == launcher.EXIT_OK
    assert launcher.PROG_ID_KEY not in windows.data
    assert launcher.COMMAND_KEY not in windows.data
    assert launcher.CAPABILITIES_KEY not in windows.data
    assert launcher.APP_NAME not in windows.data[launcher.REGISTERED_APPS_KEY]


def test_unregister_leaves_another_executables_registration_alone(
    launcher, windows
):
    _register(launcher, windows, exe=OTHER_EXE)
    before = {path: dict(values) for path, values in windows.data.items()}

    assert launcher.unregister(windows, EXE) == launcher.EXIT_ERROR
    assert windows.data == before


def test_unregister_is_idempotent(launcher, windows):
    _register(launcher, windows)
    assert launcher.unregister(windows, EXE) == launcher.EXIT_OK
    assert launcher.unregister(windows, EXE) == launcher.EXIT_OK


def test_unregister_on_a_clean_machine_succeeds(launcher, windows):
    assert launcher.unregister(windows, EXE) == launcher.EXIT_OK


def test_ownership_check_reads_the_quoted_executable(launcher):
    assert launcher._registered_executable('"C:\\a b\\cove.exe" "%1"') == (
        "C:\\a b\\cove.exe"
    )
    assert launcher._registered_executable("") == ""


# -------------------------------------------------------------- argument use


def test_conflicting_registration_flags_are_rejected(launcher):
    argv = ["cove.exe", launcher.REGISTER_FLAG, launcher.UNREGISTER_FLAG]
    assert launcher.handle_registration_args(argv) == launcher.EXIT_USAGE


def test_repeated_registration_flag_is_rejected(launcher):
    argv = ["cove.exe", launcher.REGISTER_FLAG, launcher.REGISTER_FLAG]
    assert launcher.handle_registration_args(argv) == launcher.EXIT_USAGE


def test_registration_flag_with_a_magnet_argument_is_rejected(launcher):
    argv = [
        "cove.exe",
        launcher.REGISTER_FLAG,
        "magnet:?xt=urn:btih:" + "0" * 40,
    ]
    assert launcher.handle_registration_args(argv) == launcher.EXIT_USAGE


def test_non_windows_invocation_fails_without_importing_winreg(
    launcher, monkeypatch
):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "winreg", None)
    argv = ["cove.exe", launcher.REGISTER_FLAG]
    assert launcher.handle_registration_args(argv) == launcher.EXIT_ERROR


# ------------------------------------------------------------------- logging


def test_registration_output_is_short_and_leaks_nothing(
    launcher, windows, capsys
):
    _register(launcher, windows)
    out = capsys.readouterr().out
    assert "magnet:?" not in out
    assert "HKEY" not in out
    assert "Software\\" not in out
    assert len(out.splitlines()) == 2
