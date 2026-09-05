"""Per-browser bundle contents built by scripts/build_extension.py.

The Chrome Web Store rejected 1.3.5 under "Malicious and Prohibited Products"
for facilitating downloads of copyrighted media, naming YouTube. The Chrome
bundle therefore ships no video handling at all: no in-page pill content
script, no video/audio context menus, and no page-extractor code. Firefox
(AMO) keeps the full feature set from the same shared source.

The media source is split so that exclusion can be finer than "all video
handling": media-core.js holds browser-neutral mechanics and media-sites.js
holds the site/extractor/stream capability that the Chrome bundle must not
contain. media-core.js is copied into the Chrome bundle but the MV3 manifest
does not load it, so it is inert there.

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
    files = _files(bundles / "chrome")
    assert "media.js" not in files
    assert "media-sites.js" not in files


def test_chrome_bundle_has_no_content_directory_at_all(bundles):
    """content/ stays excluded wholesale, including its site adapter."""
    files = _files(bundles / "chrome")
    assert not [f for f in files if f.startswith("content/")]
    assert "content/media-sites.js" not in files


def test_chrome_bundle_ships_the_shared_media_core_inertly(bundles):
    """The neutral core is copied but the MV3 manifest never loads it."""
    assert "media-core.js" in _files(bundles / "chrome")
    manifest = json.loads((bundles / "chrome" / "manifest.json").read_text())
    assert manifest["background"]["service_worker"] == "background.js"
    assert "scripts" not in manifest["background"]


def test_chrome_manifest_registers_no_content_scripts(bundles):
    manifest = json.loads((bundles / "chrome" / "manifest.json").read_text())
    assert "content_scripts" not in manifest


def test_chrome_manifest_still_requests_no_webrequest(bundles):
    manifest = json.loads((bundles / "chrome" / "manifest.json").read_text())
    granted = set(manifest.get("permissions", [])) | set(manifest.get("host_permissions", []))
    assert "webRequest" not in granted


def test_chrome_bundle_mentions_no_video_site(bundles):
    """A reviewer reading the zip must find no site-specific media code.

    Two terms are deliberately absent from this pattern, both because a file
    outside this slice already contains them and structural file absence -
    not this scan - is the primary boundary:

    - `detectedStreams`: the getDetectedStreams message type belongs to the
      shared background protocol in background.js, which ships in the Chrome
      bundle and answers it with an empty list.
    - `m3u8`: popup/popup.js still derives a filename from one for its
      stream list. Removing that dead Chrome UI is a later slice.
    """
    pattern = re.compile(
        r"youtube|youtu\.be|yt-dlp|googlevideo|extractorPageUrl"
        r"|mpegurl|HLS_CONTENT_TYPES|data-hls-url",
        re.I,
    )
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
    assert "content/media-sites.js" in files
    assert "media-core.js" in files
    assert "media-sites.js" in files
    # Superseded by the core/sites split; nothing may resurrect it.
    assert "media.js" not in files


def test_firefox_manifest_loads_the_extractor_module(bundles):
    manifest = json.loads((bundles / "firefox" / "manifest.json").read_text())
    scripts = manifest["background"]["scripts"]
    assert scripts == ["media-core.js", "media-sites.js", "background.js"]
    assert manifest["content_scripts"], "the pill content script must stay on Firefox"
    # The site adapter publishes its capability before the shared pill reads it.
    assert manifest["content_scripts"][0]["js"] == [
        "content/media-sites.js",
        "content/media-tab.js",
    ]


def test_firefox_bundle_still_handles_video_pages(bundles):
    assert "youtube" in (bundles / "firefox" / "media-sites.js").read_text().lower()


def test_firefox_bundle_keeps_stream_detection(bundles):
    source = (bundles / "firefox" / "media-sites.js").read_text()
    assert "HLS_CONTENT_TYPES" in source
    assert "onHeadersReceived" in source


# ---- The build helper's exclusion contract ----

def test_copy_shared_by_default_copies_the_whole_content_directory(tmp_path):
    """The normal, no-exclusion call every Firefox build makes."""
    dest = tmp_path / "default"
    build_extension._copy_shared(dest)
    copied = _files(dest)
    assert "content/media-tab.js" in copied
    assert "content/media-tab.css" in copied
    # _EXCLUDE still applies with no caller exclusions.
    assert "manifest.chrome.json" not in copied


def test_copy_shared_excludes_a_nested_file_without_its_directory(tmp_path):
    """A relative POSIX path must exclude one file, not its whole directory."""
    dest = tmp_path / "nested"
    build_extension._copy_shared(dest, exclude={"content/media-tab.css"})
    copied = _files(dest)
    assert "content/media-tab.css" not in copied
    assert "content/media-tab.js" in copied, "the parent directory must survive"


def test_copy_shared_still_excludes_a_whole_directory(tmp_path):
    dest = tmp_path / "dir"
    build_extension._copy_shared(dest, exclude={"content"})
    assert not [f for f in _files(dest) if f.startswith("content/")]


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
