"""Regression tests for build.sh's AppImage recipe staging.

build.sh used to write its generated recipe state (the absolute checkout path
in requirements.txt, and the AppImage icon) straight into the tracked
build/recipe/ template, so every release build dirtied committed source.

These tests drive the real build.sh with fake python-appimage / appimagetool
executables, against a fixture checkout whose build/recipe/ is chmod'd
read-only. Read-only enforcement is deliberate: it proves no write to the
template is even attempted, so a "dirty the source then git-restore it"
workaround cannot pass.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_SH = REPO_ROOT / "build.sh"
RECIPE_TEMPLATE = REPO_ROOT / "build" / "recipe"
APP_ICON = REPO_ROOT / "cove_dm_icon.png"
SUITE_ICON = REPO_ROOT / "cove_icon.png"

FIXTURE_VERSION = "9.9.9"
ARCH = platform.machine()

FAKE_PYAPPIMAGE = """#!/usr/bin/env bash
set -e
RECIPE="${!#}"
# Record the path as build.sh's own working directory resolves it, so a
# relative tracked path cannot masquerade as somewhere outside the checkout.
RECIPE="$(cd "$RECIPE" && pwd -P)"
mkdir -p "$COVE_TEST_RECORD"
printf '%s\\n' "$@" > "$COVE_TEST_RECORD/argv.txt"
printf '%s' "$RECIPE" > "$COVE_TEST_RECORD/recipe-path.txt"
rm -rf "$COVE_TEST_RECORD/recipe-snapshot"
cp -a "$RECIPE" "$COVE_TEST_RECORD/recipe-snapshot"
mkdir -p "Cove Download Manager-$(uname -m)/usr/bin"
printf 'appdir\\n' > "Cove Download Manager-$(uname -m)/AppRun"
"""

FAKE_APPIMAGETOOL = """#!/usr/bin/env bash
set -e
OUT="${!#}"
mkdir -p "$COVE_TEST_RECORD"
printf '%s\\n' "$@" > "$COVE_TEST_RECORD/appimagetool-argv.txt"
printf 'fake-appimage-payload\\n' > "$OUT"
chmod +x "$OUT"
"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_exe(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


def _unlock(recipe_dir: Path) -> None:
    recipe_dir.chmod(0o755)
    for child in recipe_dir.iterdir():
        child.chmod(0o644)


def _lock(recipe_dir: Path) -> None:
    """Make the tracked recipe template unwritable, files first."""
    for child in recipe_dir.iterdir():
        child.chmod(0o444)
    recipe_dir.chmod(0o555)


class BuildFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.checkout = root / "checkout"
        self.home = root / "home"
        self.record_seq = 0

    @property
    def recipe(self) -> Path:
        return self.checkout / "build" / "recipe"

    def build(self) -> subprocess.CompletedProcess:
        self.record_seq += 1
        record = self.root / f"record-{self.record_seq}"
        record.mkdir()
        env = dict(os.environ)
        env.update(
            HOME=str(self.home),
            PYAPPIMG=str(self.root / "bin" / "python-appimage"),
            COVE_TEST_RECORD=str(record),
        )
        env.pop("TMPDIR", None)
        self.record = record
        return subprocess.run(
            ["bash", str(self.checkout / "build.sh")],
            cwd=self.checkout,
            env=env,
            capture_output=True,
            text=True,
        )

    def recipe_path_used(self, record: Path | None = None) -> Path:
        record = record or self.record
        return Path((record / "recipe-path.txt").read_text())

    def staged_snapshot(self, record: Path | None = None) -> Path:
        record = record or self.record
        return (record or self.record) / "recipe-snapshot"


@pytest.fixture
def build_fixture(tmp_path):
    fx = BuildFixture(tmp_path)
    checkout = fx.checkout
    (checkout / "cove").mkdir(parents=True)
    (checkout / "build").mkdir(parents=True)

    shutil.copy2(BUILD_SH, checkout / "build.sh")
    shutil.copytree(RECIPE_TEMPLATE, fx.recipe)
    shutil.copy2(APP_ICON, checkout / "cove_dm_icon.png")
    shutil.copy2(SUITE_ICON, checkout / "cove_icon.png")
    (checkout / "cove" / "__init__.py").write_text(
        f'__version__ = "{FIXTURE_VERSION}"\n'
    )

    appimagetool = (
        fx.home
        / ".cache"
        / "python-appimage"
        / "bin"
        / f".appimagetool-continuous.appdir.{ARCH}"
        / "AppRun"
    )
    _write_exe(appimagetool, FAKE_APPIMAGETOOL)
    _write_exe(tmp_path / "bin" / "python-appimage", FAKE_PYAPPIMAGE)

    _lock(fx.recipe)
    try:
        yield fx
    finally:
        _unlock(fx.recipe)


@pytest.fixture
def completed_build(build_fixture):
    result = build_fixture.build()
    assert result.returncode == 0, (
        "build.sh failed against a read-only tracked recipe template:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    return build_fixture


def test_build_leaves_tracked_requirements_byte_identical(build_fixture):
    """RED GROUP A: build.sh must not write build/recipe/requirements.txt."""
    template = build_fixture.recipe / "requirements.txt"
    before = _sha256(template)

    result = build_fixture.build()

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert _sha256(template) == before


def test_build_leaves_tracked_icon_byte_identical(build_fixture):
    """RED GROUP B: build.sh must not write build/recipe/cove.png."""
    template = build_fixture.recipe / "cove.png"
    before = _sha256(template)

    result = build_fixture.build()

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert _sha256(template) == before
    # The committed template is the suite icon, not the AppImage icon.
    assert _sha256(template) == _sha256(SUITE_ICON)


def test_pyappimage_receives_a_staged_recipe(completed_build):
    """RED GROUP C: the recipe handed to python-appimage is not the template."""
    used = completed_build.recipe_path_used()
    template = completed_build.recipe

    assert used.resolve() != template.resolve()
    assert template.resolve() not in used.resolve().parents
    assert completed_build.checkout.resolve() not in used.resolve().parents


def test_staged_requirements_carries_the_checkout_path(completed_build):
    """RED GROUP D: staging must keep the build-specific source entry."""
    staged = completed_build.staged_snapshot() / "requirements.txt"
    lines = staged.read_text().split()

    assert lines[-1] == str(completed_build.checkout.resolve())
    assert "PySide6>=6.5" in lines
    assert "requests>=2.31" in lines
    assert "yt-dlp>=2025.1.0" in lines


def test_staged_recipe_keeps_the_rest_of_the_template(completed_build):
    """Staging copies the whole template, not a hand-rebuilt subset."""
    staged = completed_build.staged_snapshot()
    template = completed_build.recipe

    for name in ("cove.desktop", "entrypoint.sh"):
        assert _sha256(staged / name) == _sha256(template / name)


def test_staged_icon_is_the_appimage_icon(completed_build):
    """RED GROUP E: the repair must still ship cove_dm_icon.png."""
    staged = completed_build.staged_snapshot() / "cove.png"

    assert _sha256(staged) == _sha256(APP_ICON)
    assert _sha256(staged) != _sha256(SUITE_ICON)


def test_output_artifact_contract_unchanged(completed_build):
    """RED GROUP F: release filename/path must not drift."""
    out = (
        completed_build.checkout
        / "release"
        / f"Cove-Download-Manager-{FIXTURE_VERSION}-{ARCH}.AppImage"
    )

    assert out.is_file()
    assert out.stat().st_size > 0
    assert os.access(out, os.X_OK)
    assert (out.parent / f"{out.name}.sha256").is_file()


def test_staging_directory_is_unique_per_build(build_fixture):
    """RED GROUP G: no fixed global staging path shared between builds."""
    first = build_fixture.build()
    assert first.returncode == 0, f"{first.stdout}\n{first.stderr}"
    first_path = build_fixture.recipe_path_used()

    second = build_fixture.build()
    assert second.returncode == 0, f"{second.stdout}\n{second.stderr}"
    second_path = build_fixture.recipe_path_used()

    assert first_path != second_path


def test_staging_directory_is_cleaned_up(completed_build):
    """RED GROUP H: a successful build leaves no staging directory behind."""
    assert not completed_build.recipe_path_used().exists()


def test_build_script_does_not_repair_source_with_git(completed_build):
    """No git restore/checkout/reset/clean workaround in the build script."""
    script = (completed_build.checkout / "build.sh").read_text()

    for forbidden in ("git restore", "git checkout", "git reset", "git clean"):
        assert forbidden not in script


def test_build_creates_no_new_files_in_the_recipe_template(completed_build):
    """Staging must not land inside the tracked template directory."""
    names = sorted(p.name for p in completed_build.recipe.iterdir())

    assert names == ["cove.desktop", "cove.png", "entrypoint.sh", "requirements.txt"]
    mode = stat.S_IMODE(completed_build.recipe.stat().st_mode)
    assert mode == 0o555
