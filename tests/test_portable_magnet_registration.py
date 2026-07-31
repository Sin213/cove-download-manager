"""Unit tests for the portable build's opt-in magnet-handler registration.

The registry is faked end to end -- these tests never import the real winreg
module and never touch the machine's registry or default handlers.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

from cove import magnet_win
from cove.magnet_identity import WINDOWS_PORTABLE
from tests.conftest import FakeWinreg

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_PATH = ROOT / "packaging" / "portable_launcher.py"

KEYS = magnet_win.keys_for(WINDOWS_PORTABLE)


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


@pytest.fixture
def windows(monkeypatch):
    """Pretend to run on Windows with a fake registry installed."""
    fake = FakeWinreg()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake)
    return fake


EXE = "/opt/cove/Cove-Portable.exe"
OTHER_EXE = "/somewhere/else/Cove-Portable.exe"


def _register(fake, exe=EXE):
    return magnet_win.register(fake, KEYS, exe)


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
    _register(windows)
    assert windows.roots_used == {"HKCU"}


def test_register_writes_a_fully_quoted_open_command(launcher, windows):
    _register(windows)
    command = windows.data[KEYS.command_key][None]
    assert command == '"{}" "%1"'.format(EXE)
    assert command.endswith('"%1"')


def test_register_advertises_magnet_capability(launcher, windows):
    _register(windows)
    assert windows.data[KEYS.prog_id_key]["URL Protocol"] == ""
    assert windows.data[KEYS.url_associations_key]["magnet"] == KEYS.prog_id
    registered = windows.data[magnet_win.REGISTERED_APPS_KEY]
    assert registered[KEYS.app_name] == KEYS.capabilities_key


def test_register_does_not_force_the_active_default(launcher, windows):
    _register(windows)
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

    assert KEYS.prog_id == "Cove.Magnet.Portable"
    assert KEYS.app_name != "Cove Download Manager"

    _register(windows)
    for path in windows.data:
        assert not path.startswith("Software\\Classes\\Cove.Magnet\\")
        assert path != "Software\\Classes\\Cove.Magnet"
        assert "Software\\Cove\\Cove Download Manager\\" not in path
    registered = windows.data[magnet_win.REGISTERED_APPS_KEY]
    assert "Cove Download Manager" not in registered


def test_register_is_idempotent(launcher, windows):
    _register(windows)
    first = {path: dict(values) for path, values in windows.data.items()}
    _register(windows)
    assert windows.data == first


def test_register_after_a_move_rewrites_the_command(launcher, windows):
    _register(windows)
    _register(windows, exe=OTHER_EXE)
    assert windows.data[KEYS.command_key][None] == '"{}" "%1"'.format(OTHER_EXE)


# ------------------------------------------------------------ unregistration


def test_unregister_removes_keys_this_executable_owns(launcher, windows):
    _register(windows)
    assert magnet_win.unregister(windows, KEYS, EXE) is True
    assert KEYS.prog_id_key not in windows.data
    assert KEYS.command_key not in windows.data
    assert KEYS.capabilities_key not in windows.data
    assert KEYS.app_name not in windows.data[magnet_win.REGISTERED_APPS_KEY]


def test_unregister_leaves_another_executables_registration_alone(
    launcher, windows
):
    _register(windows, exe=OTHER_EXE)
    before = {path: dict(values) for path, values in windows.data.items()}

    assert magnet_win.unregister(windows, KEYS, EXE) is False
    assert windows.data == before


def test_unregister_is_idempotent(launcher, windows):
    _register(windows)
    assert magnet_win.unregister(windows, KEYS, EXE) is True
    assert magnet_win.unregister(windows, KEYS, EXE) is True


def test_unregister_on_a_clean_machine_succeeds(launcher, windows):
    assert magnet_win.unregister(windows, KEYS, EXE) is True


def test_ownership_check_reads_the_quoted_executable(launcher):
    assert magnet_win.registered_executable('"C:\\a b\\cove.exe" "%1"') == (
        "C:\\a b\\cove.exe"
    )
    assert magnet_win.registered_executable("") == ""


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
    launcher.handle_registration_args(["cove.exe", launcher.REGISTER_FLAG])
    out = capsys.readouterr().out
    assert "magnet:?" not in out
    assert "HKEY" not in out
    assert "Software\\" not in out
    assert len(out.splitlines()) == 2
