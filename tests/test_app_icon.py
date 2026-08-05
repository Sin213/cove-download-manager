"""Icon lookup prefers the download-manager artwork (issue #12).

Cove Download Manager shipped the shared suite skull, so its window, tray and
launcher were indistinguishable from the other Cove apps. It now ships its own
mark and must prefer it, while still falling back to the shared one so a build
missing the new asset keeps an icon instead of none.
"""
from pathlib import Path

import cove.widgets as widgets

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_the_download_manager_icon_ships_with_the_repo():
    assert (REPO_ROOT / "cove_dm_icon.png").is_file()


def test_the_windows_icon_is_generated_from_the_download_manager_png():
    """cove_dm_icon.ico is a build artifact, like cove_icon.ico before it."""
    workflow = (REPO_ROOT / ".github/workflows/release.yml").read_text()
    assert "Image.open('cove_dm_icon.png').save('cove_dm_icon.ico'" in workflow
    assert "cove_dm_icon.ico" in (REPO_ROOT / ".gitignore").read_text()


def test_the_download_manager_icon_wins_over_the_suite_icon(tmp_path, monkeypatch):
    (tmp_path / "cove_icon.png").write_bytes(b"suite")
    (tmp_path / "cove_dm_icon.png").write_bytes(b"download manager")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(widgets, "__file__", str(tmp_path / "cove" / "widgets.py"))

    assert widgets.find_icon() == tmp_path / "cove_dm_icon.png"


def test_the_suite_icon_is_the_fallback(tmp_path, monkeypatch):
    (tmp_path / "cove_icon.png").write_bytes(b"suite")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(widgets, "__file__", str(tmp_path / "cove" / "widgets.py"))

    assert widgets.find_icon() == tmp_path / "cove_icon.png"


def test_no_icon_at_all_is_reported_as_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(widgets, "__file__", str(tmp_path / "cove" / "widgets.py"))
    monkeypatch.delenv("APPDIR", raising=False)

    assert widgets.find_icon() is None


def test_an_installed_appimage_icon_is_still_found(tmp_path, monkeypatch):
    installed = tmp_path / "usr/share/icons/hicolor/256x256/apps"
    installed.mkdir(parents=True)
    (installed / "cove.png").write_bytes(b"installed")
    monkeypatch.chdir(tmp_path / "usr")
    monkeypatch.setattr(widgets, "__file__", str(tmp_path / "cove" / "widgets.py"))
    monkeypatch.setenv("APPDIR", str(tmp_path))

    assert widgets.find_icon() == installed / "cove.png"


def test_every_packaging_path_installs_the_download_manager_icon():
    """The icon only distinguishes the app if the packaging ships it."""
    installs = {
        "build.sh": "cp -f cove_dm_icon.png build/recipe/cove.png",
        "scripts/build-deb.sh": 'ICON_SRC="$ROOT/cove_dm_icon.png"',
        "packaging/installer.iss": '#define IconFile "..\\cove_dm_icon.ico"',
        ".github/workflows/release.yml": "--icon cove_dm_icon.ico",
        "scripts/build-windows.ps1": '"--icon", "cove_dm_icon.ico"',
        "scripts/build-windows-wine.sh": "--icon cove_dm_icon.ico",
        "pyproject.toml": 'cove = ["cove_dm_icon.png", "cove_icon.png"]',
    }
    for name, expected in installs.items():
        assert expected in (REPO_ROOT / name).read_text(), name


def test_the_suite_icon_stays_bundled_as_the_fallback():
    assert "cp -f cove_icon.png cove/cove_icon.png" in (REPO_ROOT / "build.sh").read_text()
    assert '--add-data "cove_icon.png:cove"' in (
        REPO_ROOT / "scripts/build-deb.sh"
    ).read_text()
    assert '"--add-data", "cove_icon.png;cove"' in (
        REPO_ROOT / "scripts/build-windows.ps1"
    ).read_text()
    assert 'FALLBACK_ASSET_DATA="cove_icon.png;cove"' in (
        REPO_ROOT / "scripts/build-windows-wine.sh"
    ).read_text()


def test_the_real_repo_lookup_returns_the_download_manager_icon(monkeypatch):
    monkeypatch.delenv("APPDIR", raising=False)
    monkeypatch.chdir(REPO_ROOT)
    found = widgets.find_icon()
    assert found is not None
    assert found.name == "cove_dm_icon.png"
