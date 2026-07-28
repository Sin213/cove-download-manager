"""Static checks for the packaged magnet-handler registration.

These read the packaging sources directly. Nothing here touches the real
registry, the real desktop database, or the user's default handler.
"""
import re
import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

APPIMAGE_DESKTOP = ROOT / "build" / "recipe" / "cove.desktop"
ENTRYPOINT = ROOT / "build" / "recipe" / "entrypoint.sh"
BUILD_DEB = ROOT / "scripts" / "build-deb.sh"
POSTINST = ROOT / "packaging" / "debian" / "postinst"
POSTRM = ROOT / "packaging" / "debian" / "postrm"
INSTALLER = ROOT / "packaging" / "installer.iss"

MAGNET_MIME_LINE = "MimeType=x-scheme-handler/magnet;"


def _lines(path):
    return path.read_text(encoding="utf-8").splitlines()


# ----------------------------------------------------------------- AppImage


def test_appimage_desktop_entry_accepts_a_single_uri():
    assert "Exec=AppRun %u" in _lines(APPIMAGE_DESKTOP)


def test_appimage_desktop_entry_declares_the_magnet_scheme_once():
    mime_lines = [
        line for line in _lines(APPIMAGE_DESKTOP) if line.startswith("MimeType=")
    ]
    assert mime_lines == [MAGNET_MIME_LINE]


def test_appimage_desktop_entry_uses_no_shell_wrapper():
    text = APPIMAGE_DESKTOP.read_text(encoding="utf-8")
    assert "sh -c" not in text
    assert "bash -c" not in text


def test_appimage_desktop_entry_is_syntactically_valid():
    lines = [line for line in _lines(APPIMAGE_DESKTOP) if line.strip()]
    assert lines[0] == "[Desktop Entry]"
    for line in lines[1:]:
        assert re.match(r"^[A-Za-z0-9\-\[\]@_.]+=", line), line


def test_appimage_entrypoint_forwards_every_argument():
    assert 'exec {{ python-executable }} -m cove "$@"' in ENTRYPOINT.read_text(
        encoding="utf-8"
    )


# --------------------------------------------------------------- Debian .deb


def _deb_desktop_template():
    text = BUILD_DEB.read_text(encoding="utf-8")
    body = text.split("$APP_NAME.desktop\" <<EOF", 1)[1]
    return body.split("EOF", 1)[0]


def test_deb_desktop_template_accepts_a_single_uri():
    assert "Exec=$APP_NAME %u" in _deb_desktop_template()


def test_deb_desktop_template_declares_the_magnet_scheme_once():
    template = _deb_desktop_template()
    assert template.count(MAGNET_MIME_LINE) == 1


def test_deb_desktop_template_is_syntactically_valid():
    lines = [line for line in _deb_desktop_template().splitlines() if line.strip()]
    assert lines[0] == "[Desktop Entry]"
    for line in lines[1:]:
        assert re.match(r"^[A-Za-z0-9\-\[\]@_.]+=", line), line


def test_deb_launcher_forwards_every_argument():
    assert 'exec /usr/lib/$APP_NAME/$APP_NAME "\\$@"' in BUILD_DEB.read_text(
        encoding="utf-8"
    )


def test_maintainer_scripts_exist_and_are_executable():
    for script in (POSTINST, POSTRM):
        assert script.is_file()
        mode = script.stat().st_mode
        assert mode & stat.S_IXUSR
        assert script.read_text(encoding="utf-8").startswith("#!/bin/sh")


def test_build_script_installs_maintainer_scripts_with_mode_0755():
    text = BUILD_DEB.read_text(encoding="utf-8")
    assert 'install -m 0755 "$ROOT/packaging/debian/postinst"' in text
    assert 'install -m 0755 "$ROOT/packaging/debian/postrm"' in text
    assert '"$PKG_ROOT/DEBIAN/postinst"' in text
    assert '"$PKG_ROOT/DEBIAN/postrm"' in text


def test_maintainer_scripts_refresh_caches_only_when_tools_exist():
    for script in (POSTINST, POSTRM):
        text = script.read_text(encoding="utf-8")
        for tool in ("update-desktop-database", "gtk-update-icon-cache"):
            assert "command -v {} >/dev/null 2>&1".format(tool) in text
        # Cache refresh must never fail the install or the removal.
        assert text.count("|| true") == 2


def test_no_packaging_script_forces_the_user_default_handler():
    for path in (POSTINST, POSTRM, BUILD_DEB, APPIMAGE_DESKTOP, ENTRYPOINT):
        text = path.read_text(encoding="utf-8")
        assert "xdg-mime default" not in text
        assert "gio mime" not in text


# --------------------------------------------------------- Windows installer


def _registry_section():
    text = INSTALLER.read_text(encoding="utf-8")
    body = text.split("[Registry]", 1)[1]
    return body.split("\n[", 1)[0]


def test_installer_registration_is_hkcu_only():
    section = _registry_section()
    entries = [line for line in section.splitlines() if line.startswith("Root:")]
    assert entries
    for line in entries:
        assert line.startswith("Root: HKCU;"), line


def test_installer_never_registers_magnet_under_hklm():
    text = INSTALLER.read_text(encoding="utf-8")
    assert "HKLM" not in text
    assert "HKEY_LOCAL_MACHINE" not in text


def test_installer_stays_per_user():
    assert "PrivilegesRequired=lowest" in INSTALLER.read_text(encoding="utf-8")


def test_installer_declares_the_cove_progid_with_url_protocol():
    section = _registry_section()
    assert 'Subkey: "Software\\Classes\\Cove.Magnet"' in section
    assert 'ValueName: "URL Protocol"' in section


def test_installer_open_command_quotes_the_executable_and_the_argument():
    section = _registry_section()
    command_lines = [
        line for line in section.splitlines() if "shell\\open\\command" in line
    ]
    assert len(command_lines) == 1
    command = command_lines[0]
    assert 'ValueData: """{app}\\cove-download-manager.exe"" ""%1"""' in command


def test_installer_has_no_unquoted_command_pattern():
    section = _registry_section()
    # An unquoted %1 (exactly one doubled quote pair around it is required).
    assert not re.search(r'[^"]%1', section)
    assert ".exe %1" not in section


def test_installer_advertises_capabilities_for_default_apps():
    section = _registry_section()
    assert 'Subkey: "Software\\RegisteredApplications"' in section
    assert 'ValueName: "Cove Download Manager"' in section
    assert (
        'Subkey: "Software\\Cove\\Cove Download Manager\\Capabilities"' in section
    )
    assert 'ValueName: "ApplicationName"' in section
    assert 'ValueName: "ApplicationDescription"' in section


def test_installer_maps_magnet_to_the_cove_progid():
    section = _registry_section()
    assoc = [
        line for line in section.splitlines() if "URLAssociations" in line
    ]
    assert len(assoc) == 1
    assert 'ValueName: "magnet"' in assoc[0]
    assert 'ValueData: "Cove.Magnet"' in assoc[0]


def test_installer_registration_is_scoped_to_the_optional_task():
    section = _registry_section()
    for line in section.splitlines():
        if line.startswith("Root:"):
            assert "Tasks: magnetassoc" in line, line
    assert 'Name: "magnetassoc";' in INSTALLER.read_text(encoding="utf-8")


def test_installer_never_deletes_registration_unconditionally():
    # The portable build shares this ProgID, so an unconditional
    # uninsdeletekey/uninsdeletevalue would wipe a portable registration.
    entries = [
        line for line in _registry_section().splitlines() if line.startswith("Root:")
    ]
    assert entries
    for line in entries:
        assert "uninsdelete" not in line, line


def test_installer_uninstall_removes_only_cove_owned_registration():
    text = INSTALLER.read_text(encoding="utf-8")
    code = text.split("[Code]", 1)[1]

    # Deletion is gated on the stored command still pointing at this install.
    assert "function CoveOwnsMagnetRegistration" in code
    assert "RegQueryStringValue(HKEY_CURRENT_USER, CoveCommandKey" in code
    assert "'\"' + ExpandConstant('{app}\\cove-download-manager.exe') + '\" \"%1\"'" in code
    assert "if CoveOwnsMagnetRegistration() then" in code

    deletes = [
        line.strip()
        for line in code.splitlines()
        if "RegDelete" in line.strip() and not line.strip().startswith("{")
    ]
    assert deletes
    for line in deletes:
        assert "Cove" in line, line
        assert "HKEY_CURRENT_USER" in line, line
    # Only the Cove-owned ProgID and Capabilities trees are removed, and the
    # shared RegisteredApplications key loses only Cove's own value.
    assert "RegDeleteKeyIncludingSubkeys(HKEY_CURRENT_USER, CoveProgIdKey)" in code
    assert (
        "RegDeleteKeyIncludingSubkeys(HKEY_CURRENT_USER, CoveCapabilitiesKey)" in code
    )
    assert (
        "RegDeleteValue(HKEY_CURRENT_USER, CoveRegisteredAppsKey, CoveAppName)" in code
    )
    assert "RegDeleteKeyIncludingSubkeys(HKEY_CURRENT_USER, CoveRegisteredAppsKey)" not in code


def test_installer_does_not_take_over_the_generic_magnet_association():
    section = _registry_section()
    assert 'Software\\Classes\\magnet"' not in section
    assert 'Software\\Classes\\magnet\\' not in section


def test_installer_declares_changed_associations():
    assert "ChangesAssociations=yes" in INSTALLER.read_text(encoding="utf-8")
