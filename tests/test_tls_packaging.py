"""Regression checks for the patched Windows aria2 packaging path."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_release_build_uses_patched_aria2_artifact():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "aria2-windows:" in workflow
    assert "bash scripts/build-aria2-windows.sh" in workflow
    assert "scripts/test-aria2-windows-tls.ps1" in workflow
    assert "aria2-1.37.0-cove.1-source.tar.gz" in workflow
    assert "aria2-1.37.0-win-64bit-build1.zip" not in workflow


def test_patched_aria2_source_is_pinned_and_soft_fails_only_offline_revocation():
    dockerfile = (ROOT / "packaging" / "aria2" / "Dockerfile.mingw").read_text(
        encoding="utf-8"
    )
    patch = (
        ROOT / "packaging" / "aria2" / "wintls-best-effort-revocation.patch"
    ).read_text(encoding="utf-8")

    assert "02f2d0d8472b3c38c29b4dba8c75ebd5fdd2899a" in dockerfile
    assert "SCH_CRED_IGNORE_REVOCATION_OFFLINE" in patch
    assert "SCH_CRED_MANUAL_CRED_VALIDATION" not in patch
    assert "SCH_CRED_NO_SERVERNAME_CHECK" not in patch


def test_wine_build_cannot_silently_download_stock_aria2():
    wine_script = (ROOT / "scripts" / "build-windows-wine.sh").read_text(
        encoding="utf-8"
    )

    assert "scripts/build-aria2-windows.sh" in wine_script
    assert "aria2-1.37.0-win-64bit-build1.zip" not in wine_script
