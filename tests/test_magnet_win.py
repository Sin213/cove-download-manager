"""Windows magnet registry primitives.

The registry is faked end to end: these tests never import the real winreg
module and never touch the machine's registry or default handlers.
"""
import cove.magnet_identity as mi
from cove import magnet_win
from tests.conftest import FakeWinreg

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


def test_same_executable_canonicalizes_dot_and_redundant_segments():
    """Ownership decides whether Cove may delete a registration, so two
    spellings of one path must not read as two different programs."""
    target = r"C:\Cove\Cove.exe"
    for spelling in (
        r"C:\Cove\.\Cove.exe",
        r"C:\Cove\sub\..\Cove.exe",
        "C:/Cove/Cove.exe",
        "C:\\Cove\\\\Cove.exe",
    ):
        assert magnet_win.same_executable(spelling, target) is True, spelling
    assert magnet_win.same_executable("", target) is False


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
