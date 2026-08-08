"""Per-browser bundle contents built by scripts/build_extension.py.

The Chrome Web Store rejected 1.3.5 under "Malicious and Prohibited Products"
for facilitating downloads of copyrighted media, naming YouTube. The Chrome
bundle therefore ships no video handling at all: no in-page pill content
script, no video/audio context menus, and no page-extractor code. Firefox
(AMO) keeps the full feature set from the same shared source.

These tests assert on the built bundles rather than on extension/ itself,
because the split is a build-time exclusion - the shared source legitimately
still contains the code Firefox ships.
"""
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_extension  # noqa: E402


@pytest.fixture(scope="module")
def bundles(tmp_path_factory):
    """Build both bundles into a temp dist so the repo's dist/ is untouched."""
    dist = tmp_path_factory.mktemp("dist")
    build_extension.build(dist=dist)
    return dist


def _files(bundle: Path):
    return {p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file()}


# ---- Chrome: video handling must be absent ----

def test_chrome_bundle_has_no_media_pill_content_script(bundles):
    files = _files(bundles / "chrome")
    assert "content/media-tab.js" not in files
    assert "content/media-tab.css" not in files


def test_chrome_bundle_has_no_extractor_module(bundles):
    assert "media.js" not in _files(bundles / "chrome")


def test_chrome_manifest_registers_no_content_scripts(bundles):
    manifest = json.loads((bundles / "chrome" / "manifest.json").read_text())
    assert "content_scripts" not in manifest


def test_chrome_bundle_mentions_no_video_site(bundles):
    """A reviewer reading the zip must find no site-specific media code."""
    pattern = re.compile(r"youtube|youtu\.be|yt-dlp|extractorPageUrl", re.I)
    offenders = []
    for path in sorted((bundles / "chrome").rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".js", ".json", ".html", ".css"}:
            continue
        for lineno, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(bundles / 'chrome')}:{lineno}: {line.strip()}")
    assert not offenders, "video-site references in the Chrome bundle:\n" + "\n".join(offenders)


def test_chrome_context_menu_offers_links_and_images_only(bundles):
    """Contexts are registered in background.js, not the manifest."""
    source = (bundles / "chrome" / "background.js").read_text()
    match = re.search(r"contexts:\s*(\[[^\]]*\]|[A-Za-z_$][\w$]*)", source)
    assert match, "no contexts: found in background.js"
    # The shared source derives contexts from whether media.js loaded, so the
    # literal list in the file must not name video or audio.
    assert "video" not in source.split("registerContextMenu")[1][:400]
    assert "audio" not in source.split("registerContextMenu")[1][:400]


# ---- Firefox: the full feature set survives the split ----

def test_firefox_bundle_keeps_the_media_pill(bundles):
    files = _files(bundles / "firefox")
    assert "content/media-tab.js" in files
    assert "content/media-tab.css" in files
    assert "media.js" in files


def test_firefox_manifest_loads_the_extractor_module(bundles):
    manifest = json.loads((bundles / "firefox" / "manifest.json").read_text())
    assert "media.js" in manifest["background"]["scripts"]
    assert manifest["content_scripts"], "the pill content script must stay on Firefox"


def test_firefox_bundle_still_handles_video_pages(bundles):
    assert "youtube" in (bundles / "firefox" / "media.js").read_text().lower()


# ---- Both: unrelated guarantees the build already made ----

def test_neither_bundle_ships_the_signing_key(bundles):
    for browser in ("chrome", "firefox"):
        assert not list((bundles / browser).rglob("chrome-key.pem"))


def test_chrome_store_zip_manifest_has_no_key(bundles):
    from zipfile import ZipFile

    zips = list(bundles.glob("cove-chrome-*.zip"))
    assert len(zips) == 1
    with ZipFile(zips[0]) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert "key" not in manifest
    assert "content_scripts" not in manifest
