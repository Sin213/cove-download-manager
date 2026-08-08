"""Auto-install native messaging host registration for Firefox-based browsers.

Called once on app startup. Refreshes whatever the platform needs so the
Cove extension can connect without manual setup.

POSIX (Linux/macOS): all Firefox-based browsers (Firefox, Zen, LibreWolf,
Waterfox, Floorp) read native-messaging-host manifests from
~/.mozilla/native-messaging-hosts/ (hardcoded in libxul). Some forks also
check their own config dir, so we write there too when it exists. For
Flatpak browsers, the sandbox hides the real ~/.mozilla/ behind an
ephemeral overlay, so we apply a user-level flatpak override granting
read-only access to the manifest directory and the org.freedesktop.Flatpak
portal so the wrapper can re-exec on the host via flatpak-spawn.

Windows: Firefox does NOT read any manifest directory. It discovers native
hosts only through the registry key
HKEY_CURRENT_USER\\SOFTWARE\\Mozilla\\NativeMessagingHosts\\<host>, whose
default value is the absolute path to the manifest JSON. Firefox launches
the manifest's `path` directly without arguments, so we point it at a .bat
wrapper that injects the --native-messaging flag the app needs.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

HOST_NAME = "cove_download_manager"

# Firefox identifies the extension by its gecko id (allowed_extensions);
# Chromium browsers identify it by extension id via allowed_origins.
EXTENSION_ID = "cove-dm@cove-download-manager.net"

# Chrome extension ids allowed to talk to the host.
#   1. pinned dev id (from extension/chrome-key.pem) for unpacked testing
#   2. Chrome Web Store id (permanent, assigned at item creation)
_CHROME_EXTENSION_IDS = [
    "jnemjlhecpicblbjjhbhjbbbmjhplfal",
    "liakghhamogjcmmgnmcpephlfecmilnf",
]

# Firefox on Windows discovers native messaging hosts ONLY through this
# registry key (the ~/.mozilla manifest directory is never consulted there).
_WIN_REGISTRY_KEY = r"SOFTWARE\Mozilla\NativeMessagingHosts"

# Chromium browsers each read their own HKCU NativeMessagingHosts key.
_WIN_CHROMIUM_REGISTRY_KEYS = [
    r"SOFTWARE\Google\Chrome\NativeMessagingHosts",
    r"SOFTWARE\Microsoft\Edge\NativeMessagingHosts",
    r"SOFTWARE\Chromium\NativeMessagingHosts",
    r"SOFTWARE\BraveSoftware\Brave-Browser\NativeMessagingHosts",
    r"SOFTWARE\Vivaldi\NativeMessagingHosts",
    r"SOFTWARE\Opera Software\NativeMessagingHosts",
]

_FORK_CONFIG_DIRS = [".librewolf", ".waterfox", ".floorp"]

# Chromium browsers read manifests from ~/.config/<dir>/NativeMessagingHosts/
# (note the camel-case dir name, unlike Firefox's native-messaging-hosts).
_CHROMIUM_CONFIG_DIRS = [
    "google-chrome",
    "chromium",
    "microsoft-edge",
    "BraveSoftware/Brave-Browser",
    "vivaldi",
    "opera",
]

# macOS equivalents under ~/Library/Application Support/<dir>/.
_CHROMIUM_MACOS_DIRS = [
    "Google/Chrome",
    "Chromium",
    "Microsoft Edge",
    "BraveSoftware/Brave-Browser",
    "Vivaldi",
    "com.operasoftware.Opera",
]

_KNOWN_FLATPAK_IDS = {
    "org.mozilla.firefox",
    "app.zen_browser.zen",
    "io.github.nicoth.zen",
    "io.gitlab.librewolf-community",
    "net.waterfox.waterfox",
    "one.nicothin.nicothin",
    # Chromium browsers
    "com.google.Chrome",
    "org.chromium.Chromium",
    "com.microsoft.Edge",
    "com.brave.Browser",
    "com.vivaldi.Vivaldi",
    "com.opera.Opera",
}

# Where each Flatpak browser looks for manifests *inside its own sandbox home*
# (~/.var/app/<id>/). A Flatpak-only install has no ~/.mozilla or
# ~/.config/<browser> on the host, so the override mechanism has nothing to
# grant access to and the manifest has to be written here directly.
#
# Firefox-family: the same hardcoded ~/.mozilla path, relative to the sandbox
# home. Chromium-family: config/<browser dir>/NativeMessagingHosts, because
# the sandbox maps its XDG_CONFIG_HOME to ~/.var/app/<id>/config.
_FLATPAK_FIREFOX_IDS = {
    "org.mozilla.firefox",
    "app.zen_browser.zen",
    "io.github.nicoth.zen",
    "io.gitlab.librewolf-community",
    "net.waterfox.waterfox",
    "one.nicothin.nicothin",
}
_FLATPAK_CHROMIUM_CONFIG_DIRS = {
    "com.google.Chrome": "google-chrome",
    "org.chromium.Chromium": "chromium",
    "com.microsoft.Edge": "microsoft-edge",
    "com.brave.Browser": "BraveSoftware/Brave-Browser",
    "com.vivaldi.Vivaldi": "vivaldi",
    "com.opera.Opera": "opera",
}


def _host_command_parts() -> list[str]:
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        return [appimage, "--native-messaging"]

    python = sys.executable or "python3"
    return [python, "-c", "from cove.native_messaging import main; main()"]


def _wrapper_script(parts: list[str]) -> str:
    import shlex
    # shlex.quote, not bare double quotes: a path containing ", $, or `
    # would otherwise break or expand inside the generated script.
    quoted = " ".join(shlex.quote(p) for p in parts)
    return (
        "#!/usr/bin/env bash\n"
        f"target=({quoted})\n"
        "if [ -e /.flatpak-info ] && command -v flatpak-spawn >/dev/null 2>&1; then\n"
        '    exec flatpak-spawn --host "${target[@]}"\n'
        "fi\n"
        'exec "${target[@]}"\n'
    )


def _manifest(wrapper_path: str) -> dict:
    return {
        "name": HOST_NAME,
        "description": "Cove Download Manager native messaging host",
        "path": wrapper_path,
        "type": "stdio",
        "allowed_extensions": [EXTENSION_ID],
    }


def _chrome_manifest(wrapper_path: str) -> dict:
    return {
        "name": HOST_NAME,
        "description": "Cove Download Manager native messaging host",
        "path": wrapper_path,
        "type": "stdio",
        "allowed_origins": [
            f"chrome-extension://{ext_id}/" for ext_id in _CHROME_EXTENSION_IDS
        ],
    }


def _write_manifest(hosts_dir: Path, wrapper_content: str, manifest_fn=_manifest) -> None:
    hosts_dir.mkdir(parents=True, exist_ok=True)

    wrapper_path = hosts_dir / HOST_NAME
    wrapper_path.write_text(wrapper_content)
    wrapper_path.chmod(wrapper_path.stat().st_mode | stat.S_IEXEC)

    manifest_path = hosts_dir / f"{HOST_NAME}.json"
    manifest_path.write_text(
        json.dumps(manifest_fn(str(wrapper_path)), indent=2) + "\n"
    )


def _browser_dirs() -> list[Path]:
    """Firefox-family manifest dirs for installed browsers only, so we
    don't create stray directories for browsers that aren't there."""
    home = Path.home()
    dirs: list[Path] = []

    if sys.platform == "darwin":
        app_support = home / "Library" / "Application Support"
        if (app_support / "Firefox").is_dir() or (app_support / "Mozilla").is_dir():
            dirs.append(app_support / "Mozilla" / "NativeMessagingHosts")
        return dirs

    # Primary: all Firefox-based browsers check ~/.mozilla/
    if (home / ".mozilla").exists():
        dirs.append(home / ".mozilla" / "native-messaging-hosts")

    # Fork-specific dirs (patched libxul builds)
    for name in _FORK_CONFIG_DIRS:
        candidate = home / name / "native-messaging-hosts"
        if (home / name).is_dir():
            dirs.append(candidate)

    return dirs


def _chromium_browser_dirs() -> list[Path]:
    """NativeMessagingHosts dirs for installed Chromium browsers.

    Only includes a browser whose config dir already exists, so we don't
    create stray directories for browsers that aren't installed.
    """
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
        names = _CHROMIUM_MACOS_DIRS
    else:
        base = Path.home() / ".config"
        names = _CHROMIUM_CONFIG_DIRS
    dirs: list[Path] = []
    for name in names:
        browser_dir = base / name
        if browser_dir.is_dir():
            dirs.append(browser_dir / "NativeMessagingHosts")
    return dirs


def _installed_flatpak_ids() -> list[str]:
    """Recognised browser app ids that actually have a Flatpak data directory.

    ~/.var/app holds a directory per installed Flatpak app, so this is checked
    against the known-browser set - Cove must never write into an unrelated
    application's private home.
    """
    root = Path.home() / ".var" / "app"
    if not root.is_dir():
        return []
    try:
        entries = sorted(p.name for p in root.iterdir() if p.is_dir())
    except OSError:
        return []
    return [name for name in entries if name in _KNOWN_FLATPAK_IDS]


def _flatpak_browser_dirs() -> tuple[list[Path], list[Path]]:
    """(Firefox-family, Chromium-family) manifest dirs inside Flatpak homes.

    Discovered independently of the conventional ~/.mozilla and ~/.config
    locations: a Flatpak-only browser has neither, and depending on them meant
    such a user got no manifest at all.
    """
    root = Path.home() / ".var" / "app"
    firefox: list[Path] = []
    chromium: list[Path] = []
    for app_id in _installed_flatpak_ids():
        if app_id in _FLATPAK_FIREFOX_IDS:
            firefox.append(root / app_id / ".mozilla" / "native-messaging-hosts")
        config_dir = _FLATPAK_CHROMIUM_CONFIG_DIRS.get(app_id)
        if config_dir:
            chromium.append(
                root / app_id / "config" / config_dir / "NativeMessagingHosts"
            )
    return firefox, chromium


def _apply_flatpak_overrides(manifest_dir: str) -> None:
    if not shutil.which("flatpak"):
        return

    flatpak_root = Path.home() / ".var" / "app"
    if not flatpak_root.is_dir():
        return

    for app_dir in flatpak_root.iterdir():
        if not app_dir.is_dir():
            continue
        app_id = app_dir.name
        if app_id not in _KNOWN_FLATPAK_IDS:
            continue
        try:
            subprocess.run(
                [
                    "flatpak", "override", "--user",
                    "--talk-name=org.freedesktop.Flatpak",
                    f"--filesystem={manifest_dir}:ro",
                    app_id,
                ],
                check=False,
                capture_output=True,
                timeout=10,
            )
        except Exception:
            pass


def _install_posix() -> list[str]:
    """Install manifests and apply Flatpak overrides (Linux/macOS).

    Returns list of directories where manifests were written.
    """
    parts = _host_command_parts()
    wrapper = _wrapper_script(parts)
    installed: list[str] = []

    # Flatpak targets are enumerated independently of the conventional ones:
    # a Flatpak-only browser has no ~/.mozilla or ~/.config/<browser>, and
    # deriving its manifest location from those left it with nothing.
    flatpak_firefox, flatpak_chromium = _flatpak_browser_dirs()

    # Firefox-based browsers (allowed_extensions). _browser_dirs() already
    # filters to installed browsers.
    for hosts_dir in [*_browser_dirs(), *flatpak_firefox]:
        _write_manifest(hosts_dir, wrapper)
        installed.append(str(hosts_dir))

    # Chromium-based browsers (allowed_origins). Same wrapper, different
    # manifest shape and location.
    for hosts_dir in [*_chromium_browser_dirs(), *flatpak_chromium]:
        _write_manifest(hosts_dir, wrapper, _chrome_manifest)
        installed.append(str(hosts_dir))

    # Overrides only matter for manifests on the host side; one written inside
    # a sandbox home is already visible to that browser.
    host_side = {str(p) for p in [*flatpak_firefox, *flatpak_chromium]}
    for manifest_dir in installed:
        if manifest_dir in host_side:
            continue
        try:
            _apply_flatpak_overrides(manifest_dir)
        except Exception:
            pass

    return installed


def _windows_host_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "Cove" / "native-messaging-hosts"


def _windows_command_parts() -> list[str]:
    if getattr(sys, "frozen", False):
        # Frozen build: the exe IS the host; re-launch it in host mode.
        return [sys.executable, "--native-messaging"]

    python = sys.executable or "python"
    return [python, "-m", "cove", "--native-messaging"]


def _windows_launcher(parts: list[str]) -> str:
    # Firefox launches the manifest `path` directly and cannot pass args, so
    # the .bat injects --native-messaging. %* forwards Firefox's own args
    # (manifest path + extension id), which the host ignores. CRLF required.
    quoted = " ".join(f'"{p}"' for p in parts)
    return "@echo off\r\n" + quoted + " %*\r\n"


def _win_set_host_key(winreg, base_key: str, manifest_path: str) -> None:
    """Point HKCU\\<base_key>\\<host> at the given manifest path."""
    key = winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        f"{base_key}\\{HOST_NAME}",
        0,
        winreg.KEY_WRITE,
    )
    try:
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, manifest_path)
    finally:
        winreg.CloseKey(key)


def _install_windows() -> list[str]:
    """Register the native messaging host on Windows.

    Writes a .bat launcher plus a Firefox manifest (allowed_extensions) and
    a Chrome manifest (allowed_origins) under %LOCALAPPDATA%, then points the
    Mozilla registry key at the Firefox manifest and each Chromium browser's
    registry key at the Chrome manifest. Returns the host directory.
    """
    import winreg

    hosts_dir = _windows_host_dir()
    hosts_dir.mkdir(parents=True, exist_ok=True)

    launcher_path = hosts_dir / f"{HOST_NAME}.bat"
    launcher_path.write_text(_windows_launcher(_windows_command_parts()))

    # Firefox manifest + Mozilla registry key.
    ff_manifest = hosts_dir / f"{HOST_NAME}.json"
    ff_manifest.write_text(
        json.dumps(_manifest(str(launcher_path)), indent=2) + "\n"
    )
    _win_set_host_key(winreg, _WIN_REGISTRY_KEY, str(ff_manifest))

    # Chrome manifest + per-browser Chromium registry keys.
    chrome_manifest = hosts_dir / f"{HOST_NAME}.chrome.json"
    chrome_manifest.write_text(
        json.dumps(_chrome_manifest(str(launcher_path)), indent=2) + "\n"
    )
    for base_key in _WIN_CHROMIUM_REGISTRY_KEYS:
        _win_set_host_key(winreg, base_key, str(chrome_manifest))

    return [str(hosts_dir)]


def install_native_hosts() -> list[str]:
    """Register the native messaging host for the current platform.

    Returns list of directories/locations that were written.
    """
    if sys.platform == "win32":
        return _install_windows()
    return _install_posix()
