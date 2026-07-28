"""Static consistency checks for the release metadata surfaces.

The version lives in three hand-maintained places (the package, the project
metadata, and the README badge) and the GitHub release body is a heredoc
inside the workflow. These checks compare everything against the single
application version so a partial bump cannot ship.
"""

import re
from pathlib import Path

try:  # tomllib is stdlib from 3.11; the project still declares >=3.9.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - depends on interpreter
    tomllib = None


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def app_version() -> str:
    text = (ROOT / "cove" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "cove/__init__.py does not define __version__"
    return match.group(1)


def release_notes_body() -> str:
    """The heredoc the publish job feeds to `gh release create`."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(
        r"<<'NOTESEOF'\n(.*?)^\s*NOTESEOF$",
        workflow,
        re.DOTALL | re.MULTILINE,
    )
    assert match, "release note heredoc is missing or unterminated"
    return match.group(1)


def project_version() -> str:
    path = ROOT / "pyproject.toml"
    if tomllib is not None:
        with path.open("rb") as handle:
            return tomllib.load(handle)["project"]["version"]

    # Python 3.9/3.10 fallback: read the [project] table's version by hand
    # rather than pulling in a TOML dependency for one field.
    text = path.read_text(encoding="utf-8")
    table = re.search(r"^\[project\]$(.*?)(?=^\[|\Z)", text, re.DOTALL | re.MULTILINE)
    assert table, "pyproject.toml has no [project] table"
    match = re.search(r'^version\s*=\s*"([^"]+)"', table.group(1), re.MULTILINE)
    assert match, "pyproject.toml [project] table has no version"
    return match.group(1)


def test_pyproject_version_matches_application_version():
    assert project_version() == app_version()


def test_readme_badge_matches_application_version():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert f"release-v{app_version()}-" in readme


def test_release_notes_heading_matches_application_version():
    assert f"## What's new in {app_version()}" in release_notes_body()


def test_release_notes_keep_dynamic_version_placeholders():
    # The publish job rewrites the literal VERSION token, so the downloads
    # table must not carry a hardcoded version instead.
    body = release_notes_body()

    for name in (
        "Cove-Download-Manager-VERSION-x86_64.AppImage",
        "cove-download-manager_VERSION_amd64.deb",
        "Cove-Download-Manager-VERSION-Setup.exe",
        "Cove-Download-Manager-VERSION-Portable.exe",
        "Cove-AI-Client-VERSION.zip",
    ):
        assert name in body

    assert app_version() not in body.split("## Downloads", 1)[1]


def test_publish_job_stays_tag_only():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "if: startsWith(github.ref, 'refs/tags/v')" in workflow


def test_workflow_artifact_names_stay_version_dynamic():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    # Artifact names are built from the resolved version, never a literal.
    assert f"Cove-Download-Manager-{app_version()}" not in workflow
    assert f"cove-download-manager_{app_version()}" not in workflow
