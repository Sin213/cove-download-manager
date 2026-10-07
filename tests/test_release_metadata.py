"""Static consistency checks for the release metadata surfaces.

The version lives in three hand-maintained places (the package, the project
metadata, and the README badge) and the GitHub release body is a heredoc
inside the workflow. These checks compare everything against the single
application version so a partial bump cannot ship.
"""

import re
from pathlib import Path

import pytest

try:  # tomllib is stdlib from 3.11; the project still declares >=3.9.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - depends on interpreter
    tomllib = None


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
SECURITY_FLOOR = (2, 32, 4)
DEPENDENCY_SURFACES = (
    "requirements.txt",
    "pyproject.toml [project].dependencies",
    "build/recipe/requirements.txt",
    "build.sh generated-requirements heredoc",
)


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


def _pyproject_dependencies_fallback(text):
    # This fallback supports arrays of single-line quoted strings, without
    # basic-string escapes. Reject other syntax instead of guessing its meaning.
    table = re.search(
        r"^\[project\][ \t]*(?:#[^\n]*)?$(.*?)(?=^\[|\Z)",
        text,
        re.DOTALL | re.MULTILINE,
    )
    assert table, "pyproject.toml has no [project] table"
    body = table.group(1)
    array = re.search(r"^[ \t]*dependencies[ \t]*=[ \t]*\[", body, re.MULTILINE)
    assert array, "pyproject.toml [project] has no dependencies array"
    tokens = re.compile(r'''\s+|#[^\n]*|"[^"\\\r\n]*"|'[^'\r\n]*'|[,\]]''')
    dependencies = []
    position, need_value = array.end(), True
    while position < len(body):
        match = tokens.match(body, position)
        assert match, "pyproject.toml [project].dependencies: unsupported string or array syntax"
        token, position = match.group(0), match.end()
        if token.isspace() or token.startswith("#"):
            continue
        if token == "]":
            tail = body[position:].split("\n", 1)[0].strip()
            assert not tail or tail.startswith("#"), "pyproject.toml [project].dependencies: unexpected text after array"
            return dependencies
        if need_value:
            assert token[0] in "\"'", "pyproject.toml [project].dependencies: expected a quoted string"
            dependencies.append(token[1:-1])
        else:
            assert token == ",", "pyproject.toml [project].dependencies: expected comma or closing bracket"
        need_value = not need_value
    raise AssertionError("pyproject.toml [project].dependencies: unterminated array")


def _requests_floor_failures(surfaces):
    failures = []
    for surface, lines in surfaces.items():
        declarations = [
            line.split("#", 1)[0].strip()
            for line in lines
            if re.match(r"^\s*requests(?=\s|[<>=!~;\[@]|$)", line, re.IGNORECASE)
        ]
        if len(declarations) != 1:
            failures.append(f"{surface}: expected exactly one Requests declaration, found {len(declarations)}")
            continue
        declaration = declarations[0]
        # Accept only an explicit numeric lower bound; unknown specifiers fail closed.
        minimum = re.fullmatch(r"requests\s*>=\s*(\d+(?:\.\d+)*)", declaration, re.IGNORECASE)
        if minimum is None:
            failures.append(f"{surface}: {declaration!r} must declare a numeric Requests minimum with >=")
            continue
        version = tuple(int(part) for part in minimum.group(1).split("."))
        while len(version) > len(SECURITY_FLOOR) and version[-1] == 0:
            version = version[:-1]
        if version != SECURITY_FLOOR:
            failures.append(f"{surface}: {declaration!r} has minimum {version}, differs from approved floor {SECURITY_FLOOR}")
    return failures


def test_requests_security_floor_across_dependency_surfaces():
    project_path = ROOT / "pyproject.toml"
    dependencies = _pyproject_dependencies_fallback(project_path.read_text(encoding="utf-8"))
    if tomllib is not None:
        with project_path.open("rb") as handle:
            parsed = tomllib.load(handle)["project"].get("dependencies", [])
        assert dependencies == parsed, "pyproject.toml dependency parsers disagree"
        dependencies = parsed

    script = (ROOT / "build.sh").read_text(encoding="utf-8")
    heredocs = re.findall(
        r'^cat > "\$STAGED_RECIPE/requirements\.txt" <<EOF\n(.*?)^EOF$',
        script,
        re.DOTALL | re.MULTILINE,
    )
    assert len(heredocs) == 1, "build.sh must have exactly one generated-requirements heredoc"
    failures = _requests_floor_failures({
        "requirements.txt": (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines(),
        "pyproject.toml [project].dependencies": dependencies,
        "build/recipe/requirements.txt": (
            ROOT / "build" / "recipe" / "requirements.txt"
        ).read_text(encoding="utf-8").splitlines(),
        "build.sh generated-requirements heredoc": heredocs[0].splitlines(),
    })
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("surface", DEPENDENCY_SURFACES)
@pytest.mark.parametrize("minimum", ("2.32.3", "2.34.2"))
def test_requests_floor_rejects_unapproved_minimum(surface, minimum):
    surfaces = {name: ["requests>=2.32.4"] for name in DEPENDENCY_SURFACES}
    surfaces[surface] = [f"requests>={minimum}"]
    failures = _requests_floor_failures(surfaces)
    assert len(failures) == 1, f"{surface}: expected rejection of {minimum}, got {failures}"
    assert failures[0].startswith(f"{surface}:")
    assert "approved floor" in failures[0]


@pytest.mark.parametrize("minimum", ("2.32.4", "2.32.4.0", "2.32.4.0.0"))
def test_requests_floor_accepts_equivalent_approved_minimum(minimum):
    assert not _requests_floor_failures({"requirements.txt": [f"requests>={minimum}"]})


@pytest.mark.parametrize("lines, message", (
    ([], "found 0"),
    (["requests>=2.32.4", "requests>=2.32.4"], "found 2"),
    (["requests==2.32.4"], "numeric Requests minimum"),
    (["requests[socks]>=2.32.4"], "numeric Requests minimum"),
))
def test_requests_floor_fails_closed(lines, message):
    failures = _requests_floor_failures({"requirements.txt": lines})
    assert len(failures) == 1
    assert failures[0].startswith("requirements.txt:")
    assert message in failures[0]


def test_dependencies_fallback_excludes_keywords():
    text = '[project]\ndependencies = ["PySide6>=6.5"]\nkeywords = [\n"requests>=2.32.4",\n]\n'
    dependencies = _pyproject_dependencies_fallback(text)
    assert dependencies == ["PySide6>=6.5"]
    failures = _requests_floor_failures({"pyproject.toml [project].dependencies": dependencies})
    assert failures == ["pyproject.toml [project].dependencies: expected exactly one Requests declaration, found 0"]


@pytest.mark.parametrize("array, expected", (
    pytest.param('dependencies = ["PySide6>=6.5", "requests>=2.32.4"]',
                 ["PySide6>=6.5", "requests>=2.32.4"], id="single-line"),
    pytest.param('dependencies = [\n"PySide6>=6.5",\n"requests>=2.32.4",\n]',
                 ["PySide6>=6.5", "requests>=2.32.4"], id="multi-line"),
    pytest.param('dependencies = [ # "ignored" ]\n"requests>=2.32.4", # ]\n\'yt-dlp>=2025.1.0\',\n] # ]',
                 ["requests>=2.32.4", "yt-dlp>=2025.1.0"], id="comments"),
    pytest.param("dependencies = ['PySide6>=6.5', 'requests>=2.32.4']",
                 ["PySide6>=6.5", "requests>=2.32.4"], id="single-quotes"),
    pytest.param('dependencies = ["requests[socks]>=2.32.4"]',
                 ["requests[socks]>=2.32.4"], id="quoted-bracket"),
))
def test_dependencies_fallback_parses_supported_arrays(array, expected):
    assert _pyproject_dependencies_fallback(f"[project]\n{array}\n") == expected


def test_requests_security_floor_without_tomllib(monkeypatch):
    monkeypatch.setitem(test_requests_security_floor_across_dependency_surfaces.__globals__, "tomllib", None)
    test_requests_security_floor_across_dependency_surfaces()


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


def test_release_notes_keep_the_browser_extension_separate():
    # Desktop releases neither contain nor update the extension, and the two
    # ship on different schedules. Dropping this line while editing the notes
    # would let a desktop release look like it carries extension fixes.
    body = release_notes_body()

    assert "browser extension is published separately" in body
    assert "neither contains nor updates it" in body


def test_github_release_is_created_as_a_draft():
    # The user decides when a release goes public; the workflow only stages it.
    workflow = WORKFLOW.read_text(encoding="utf-8")

    create = re.search(r"gh release create .*?(?=\n\s*\n|\Z)", workflow, re.DOTALL)
    assert create, "publish job no longer creates a release"
    assert "--draft" in create.group(0)


def test_publish_job_stays_tag_only():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "if: startsWith(github.ref, 'refs/tags/v')" in workflow


def test_workflow_artifact_names_stay_version_dynamic():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    # Artifact names are built from the resolved version, never a literal.
    assert f"Cove-Download-Manager-{app_version()}" not in workflow
    assert f"cove-download-manager_{app_version()}" not in workflow
