"""Build hook that stages the branded icons into the `cove` package.

`cove/cove_dm_icon.png` and `cove/cove_icon.png` are build outputs, gitignored
and copied in from the repository root by `build.sh` for AppImage builds. A
plain `pip wheel .` never runs `build.sh`, so the wheel declared package data
that was not on disk: the built wheel shipped no icons at all and `find_icon()`
returned None for every installed copy.

Copying them here fixes every distribution format at once, without moving the
canonical files out from under `build.sh`, `scripts/build-deb.sh`, the
PyInstaller spec or the Windows release workflow - all of which read them from
the repository root.
"""
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

PACKAGE_ICONS = ("cove_dm_icon.png", "cove_icon.png")


def stage_package_icons(root: Path) -> list[str]:
    """Copy the root icons into the package. Returns the names staged.

    Missing sources are skipped rather than fatal: `find_icon()` already falls
    back through several locations, and a source checkout without one of the
    icons must still be installable.
    """
    staged = []
    package = Path(root) / "cove"
    for name in PACKAGE_ICONS:
        source = Path(root) / name
        if not source.is_file():
            continue
        # Always overwrite. These are ~100 KB and the copy happens once per
        # build, so there is nothing to save by guessing - and size is not
        # evidence two images are the same, which would ship a stale icon
        # whenever a redesign happened to land on the same byte count.
        shutil.copyfile(source, package / name)
        staged.append(name)
    return staged


class BuildPyWithIcons(build_py):
    def run(self):
        stage_package_icons(Path(__file__).resolve().parent)
        super().run()


# Guarded so the staging helper above can be imported and tested directly;
# setuptools always executes this file as __main__.
if __name__ == "__main__":
    setup(cmdclass={"build_py": BuildPyWithIcons})
