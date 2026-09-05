"""Per-browser bundle contents built by scripts/build_extension.py.

The Chrome Web Store rejected 1.3.5 under "Malicious and Prohibited Products"
for facilitating downloads of copyrighted media, naming YouTube. What that
rejection was about is site handling: page extractors, site-specific media
discovery and stream-manifest observation. It was never about a media element
whose own address is an ordinary HTTP(S) file, which is the same download the
browser's own "Save video as" offers.

So the Chrome bundle ships the shared media mechanics and the shared in-page
pill, and still ships no site handling at all. The split that makes the
exclusion that fine is:

  media-core.js          browser-neutral mechanics, both bundles
  content/media-tab.js   the shared pill, both bundles
  media-chrome.js        Chrome's capability: no site hooks whatsoever
  media-sites.js         Firefox's background site/extractor/stream capability
  content/media-sites.js Firefox's in-page site capability

Chrome gets the first three, Firefox the first two plus the last two. Neither
gets the other's adapter.

These tests assert on the built bundles rather than on extension/ itself,
because the split is a build-time exclusion - the shared source legitimately
still contains the code only Firefox ships.
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


def _function_body(source: str, name: str) -> str:
    """The text of a top-level `function name(...)`, to its closing brace."""
    start = source.index(f"function {name}")
    end = source.index("\n}\n", start)
    return source[start:end]


# ---- Chrome: direct media is active, site handling stays absent ----

def test_chrome_bundle_ships_the_shared_media_pill(bundles):
    """The pill is shared code, so Chrome runs the same one Firefox does."""
    files = _files(bundles / "chrome")
    assert "content/media-tab.js" in files
    assert "content/media-tab.css" in files


def test_chrome_bundle_has_no_extractor_module(bundles):
    files = _files(bundles / "chrome")
    assert "media.js" not in files
    assert "media-sites.js" not in files


def test_chrome_bundle_content_directory_holds_the_shared_pill_only(bundles):
    """content/ ships, so its site adapter has to be excluded by name."""
    files = _files(bundles / "chrome")
    assert sorted(f for f in files if f.startswith("content/")) == [
        "content/media-tab.css",
        "content/media-tab.js",
    ]
    assert "content/media-sites.js" not in files


def test_chrome_bundle_ships_the_shared_core_and_its_own_capability(bundles):
    files = _files(bundles / "chrome")
    assert "media-core.js" in files
    assert "media-chrome.js" in files
    manifest = json.loads((bundles / "chrome" / "manifest.json").read_text())
    assert manifest["background"]["service_worker"] == "background.js"
    assert "scripts" not in manifest["background"]


def test_chrome_worker_loads_both_media_scripts_itself(bundles):
    """MV3 names one worker file, so background.js is the loader.

    The order matters: media-core.js publishes CoveMedia, media-chrome.js
    publishes the capability it resolves. Both must land before the context
    menu below them is registered, which is why this is an importScripts call
    in top-level worker evaluation and not an onInstalled handler.
    """
    source = (bundles / "chrome" / "background.js").read_text()
    match = re.search(r"importScripts\(\s*\"media-core\.js\",\s*\"media-chrome\.js\"\s*\)", source)
    assert match, "background.js must importScripts the core then the adapter"
    assert match.start() < source.index("registerContextMenu()")


def test_chrome_capability_supplies_no_site_hooks(bundles):
    """Chrome's adapter is the absence of site handling, spelled out."""
    source = (bundles / "chrome" / "media-chrome.js").read_text()
    for hook in ("sitePageUrl", "pageFallbackUrl", "titleCleanup",
                 "rejectExtension", "handleMessage", "webRequest"):
        assert not re.search(rf"^\s*{hook}\s*[:(]", source, re.M), (
            f"media-chrome.js must not implement {hook}"
        )


def test_chrome_capability_refuses_playlist_media_targets(bundles):
    """The one hook it does supply, and the seam that consults it.

    Chrome ships no stream handling, so the video/audio menu action this
    slice enabled must not forward a playlist address. The refusal lives on
    Chrome's capability so Firefox, which publishes its own, is unaffected.
    """
    capability = (bundles / "chrome" / "media-chrome.js").read_text()
    assert "function rejectMediaTarget" in capability
    for suffix in (".m3u8", ".m3u", ".mpd"):
        assert suffix in capability

    background = (bundles / "chrome" / "background.js").read_text()
    assert "CoveMediaCapability.rejectMediaTarget" in background
    # Consulted before the address is settled on, so nothing downstream can
    # put a link or the page in place of a refused media source.
    assert background.index("mediaPolicy(target)") < background.index("const fallbackUrl")


def test_chrome_media_action_selects_the_element_source_over_a_link(bundles):
    """A player inside a hyperlink must hand over the player, not the page."""
    background = (bundles / "chrome" / "background.js").read_text()
    body = background.split("contextMenus.onClicked")[1][:2000]
    assert "info.srcUrl || info.linkUrl" in body, "media actions take the source first"
    assert "info.linkUrl || info.srcUrl" in body, "link and image keep link-first"
    assert "mediaPolicy && mediaAction" in body, "and only where a policy is published"

    # Firefox's adapter must stay clear of it.
    assert "rejectMediaTarget" not in (bundles / "firefox" / "media-sites.js").read_text()


def test_chrome_manifest_registers_the_shared_pill_content_script(bundles):
    manifest = json.loads((bundles / "chrome" / "manifest.json").read_text())
    entries = manifest["content_scripts"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["js"] == ["content/media-tab.js"], "no site adapter in Chrome"
    assert entry["css"] == ["content/media-tab.css"]
    assert entry["matches"] == ["http://*/*", "https://*/*"]
    assert entry["run_at"] == "document_idle"
    assert entry["all_frames"] is True
    assert entry["match_about_blank"] is True


def test_chrome_manifest_permissions_and_version_are_untouched(bundles):
    """Activating shared code must not widen what Chrome asks the user for."""
    source = json.loads((ROOT / "extension" / "manifest.chrome.json").read_text())
    built = json.loads((bundles / "chrome" / "manifest.json").read_text())
    assert built["permissions"] == source["permissions"]
    assert built["host_permissions"] == source["host_permissions"]
    assert built["version"] == source["version"] == "1.3.8"
    assert built["manifest_version"] == 3


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


def test_chrome_context_menu_derives_media_contexts_from_the_capability(bundles):
    """Contexts are registered in background.js, not the manifest.

    The literal list in the file still names links and images only; video and
    audio arrive from CoveMedia, which is exactly what makes the menu describe
    whichever media scripts the bundle actually loaded.
    """
    source = (bundles / "chrome" / "background.js").read_text()
    body = _function_body(source, "registerContextMenu")
    assert re.search(r"contexts:\s*\[\"link\", \"image\"\]\.concat\(", body)
    assert "CoveMedia.contexts" in body
    assert '"video"' not in body and '"audio"' not in body
    assert ["video", "audio"] == json.loads(
        re.search(r"contexts:\s*(\[[^\]]*\])",
                  (bundles / "chrome" / "media-core.js").read_text()).group(1)
    )


def test_chrome_context_menu_registration_survives_an_upgrade(bundles):
    """Chrome keeps menu items across worker restarts and across updates.

    create() on a second evaluation fails with a duplicate id, so an install
    upgrading from the links-and-images build would keep those contexts
    forever if the registration did not clear first.
    """
    source = (bundles / "chrome" / "background.js").read_text()
    body = _function_body(source, "registerContextMenu")
    assert "removeAll" in body, "registration must reconcile, not just create"


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


def test_firefox_bundle_excludes_the_chrome_capability(bundles):
    """Firefox has its own adapter; two would fight over the same global."""
    assert "media-chrome.js" not in _files(bundles / "firefox")


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
    # The unpacked directory keeps `key` for a stable development id; only the
    # upload drops it. Everything else is the same manifest.
    unpacked = json.loads((bundles / "chrome" / "manifest.json").read_text())
    assert "key" in unpacked
    assert manifest == {k: v for k, v in unpacked.items() if k != "key"}


def test_chrome_store_zip_ships_the_same_media_boundary(bundles):
    """The upload is what a reviewer reads, so check it, not only the dir."""
    from zipfile import ZipFile

    zips = list(bundles.glob("cove-chrome-*.zip"))
    with ZipFile(zips[0]) as zf:
        names = set(zf.namelist())
    assert {"media-core.js", "media-chrome.js",
            "content/media-tab.js", "content/media-tab.css"} <= names
    assert "media-sites.js" not in names
    assert "content/media-sites.js" not in names
    assert "chrome-key.pem" not in names


def test_firefox_zip_ships_its_own_media_boundary(bundles):
    from zipfile import ZipFile

    zips = list(bundles.glob("cove-firefox-*.zip"))
    assert len(zips) == 1
    with ZipFile(zips[0]) as zf:
        names = set(zf.namelist())
    assert {"media-core.js", "media-sites.js", "content/media-sites.js",
            "content/media-tab.js", "content/media-tab.css"} <= names
    assert "media-chrome.js" not in names
    assert "chrome-key.pem" not in names
