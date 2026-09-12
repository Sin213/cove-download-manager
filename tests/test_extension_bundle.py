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
    assert built["version"] == source["version"] == "1.3.11"
    assert built["manifest_version"] == 3


def test_firefox_manifest_permissions_and_version_are_untouched(bundles):
    """The Firefox half of the same guarantee, on its own version line.

    Chrome and Firefox are versioned independently - they are separate store
    items with separate histories - so a bump to one must not be readable as a
    bump to the other.
    """
    source = json.loads((ROOT / "extension" / "manifest.json").read_text())
    built = json.loads((bundles / "firefox" / "manifest.json").read_text())
    assert built["permissions"] == source["permissions"]
    assert built["version"] == source["version"] == "1.4.10"
    assert built["manifest_version"] == 2
    assert (built["browser_specific_settings"]["gecko"]["id"]
            == source["browser_specific_settings"]["gecko"]["id"]
            == "cove-dm@cove-download-manager.net")


def test_firefox_declares_the_data_it_sends_to_the_native_app(bundles):
    """`nativeMessaging` obliges a data-collection declaration; "none" is wrong.

    Mozilla's built-in consent model requires that "Data sent to native
    applications using NativeMessaging must be declared in the data collection
    consent and categorized in the appropriate consent model"
    (extensionworkshop.com/documentation/develop/
    best-practices-for-collecting-user-data-consents/, read 2026-09-12).

    This extension's whole purpose is handing a download to the local Cove
    application, passing the address, the referring page, cookies for that
    address, the filename, the user-agent and the size when known. Under the
    category definitions at extensionworkshop.com/documentation/develop/
    firefox-builtin-data-consent/ (same date) that is three categories:

    - `browsingActivity`, "Information about the websites users visit, such as
      specific URLs, domains, or categories of pages users view" - the download
      address and the referring page are exactly that, and the Chrome listing
      already discloses them as browsing activity.
    - `websiteContent`, which covers page content "and anything embedded, such
      as cookies, audio, page headers, and request and response information".
    - `websiteActivity`, user actions "such as saving and downloading".

    `authenticationInfo` is deliberately absent: Mozilla scopes it to
    credentials and account data - passwords, usernames, PINs, security
    questions, registration information - not to cookies.

    The browser's user-agent also travels, and that is browser information,
    which Mozilla puts under `technicalAndInteraction`. That category is
    special: it "cannot be required" and "must be optional", so it is declared
    in `optional` and `background.js` withholds the user-agent whenever the
    user has not currently granted it.

    The guard exists so a future release cannot quietly revert to
    `required: ["none"]` while `nativeMessaging` is still requested, nor
    promote the technical-data category into `required`, which AMO rejects.
    """
    source = json.loads((ROOT / "extension" / "manifest.json").read_text())
    built = json.loads((bundles / "firefox" / "manifest.json").read_text())

    for name, manifest in (("source", source), ("built bundle", built)):
        assert "nativeMessaging" in manifest["permissions"], name
        gecko = manifest["browser_specific_settings"]["gecko"]
        required = gecko["data_collection_permissions"]["required"]

        # "none" is the standalone "collects nothing" value and cannot be
        # combined, so its presence here would be a straight contradiction.
        assert "none" not in required, (
            f"{name}: manifest still declares no data collection while "
            "nativeMessaging is requested"
        )
        assert set(required) == {
            "browsingActivity",
            "websiteContent",
            "websiteActivity",
        }, name
        assert "authenticationInfo" not in required, name

        # Mozilla: technicalAndInteraction "cannot be required" and "must be
        # optional". Declaring it in `required` is an AMO rejection, and
        # leaving it out entirely while the user-agent still travels is the
        # under-declaration this slice exists to fix.
        optional = gecko["data_collection_permissions"]["optional"]
        assert optional == ["technicalAndInteraction"], name
        assert "technicalAndInteraction" not in required, (
            f"{name}: technicalAndInteraction cannot be a required category"
        )

    assert (built["browser_specific_settings"]["gecko"]
            ["data_collection_permissions"]
            == source["browser_specific_settings"]["gecko"]
            ["data_collection_permissions"])


@pytest.mark.parametrize("browser", ["chrome", "firefox"])
def test_each_zip_is_named_for_its_own_manifest_version(bundles, browser):
    """The zip name has to come from the manifest it contains.

    Both assertions matter. The first is the mechanism: a name derived from
    anything other than the bundled manifest would drift from it. The second
    is the outcome: a builder that emitted a name correctly but from a stale
    tree would still satisfy the first.
    """
    zips = list(bundles.glob(f"cove-{browser}-*.zip"))
    assert len(zips) == 1, f"expected exactly one {browser} zip, got {zips}"

    built = json.loads((bundles / browser / "manifest.json").read_text())
    source_manifest = "manifest.chrome.json" if browser == "chrome" else "manifest.json"
    source = json.loads((ROOT / "extension" / source_manifest).read_text())

    assert zips[0].name == f"cove-{browser}-{built['version']}.zip"
    assert built["version"] == source["version"]


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


# ---- The popup: one shared shell, one Firefox-only stream module ----
#
# Detected streams are a Firefox feature - the detector that finds them ships
# only there - but the popup section that listed them, and the button that
# asked the native host to fetch one, shipped in both bundles. Chrome therefore
# advertised a list it could never fill and carried a send nothing could reach.
# The section, its stylesheet and its click handler are now one Firefox-only
# module, composed into the popup at build time.

_SCRIPT_RE = re.compile(r'<script\s+src="([^"]+)"\s*></script>')
_STYLE_RE = re.compile(r'<link\s+rel="stylesheet"\s+href="([^"]+)"\s*>')

_STREAM_MARKUP = ("streams-section", "streams-list", "Detected Streams")


def _popup_html(bundle: Path) -> str:
    return (bundle / "popup" / "popup.html").read_text()


def test_chrome_bundle_has_no_popup_stream_module(bundles):
    chrome = _files(bundles / "chrome")
    assert "popup/streams.js" not in chrome
    assert "popup/streams.css" not in chrome
    assert "popup/popup.js" in chrome, "the shared popup is not what is excluded"


def test_firefox_bundle_keeps_the_popup_stream_module(bundles):
    firefox = _files(bundles / "firefox")
    assert "popup/streams.js" in firefox
    assert "popup/streams.css" in firefox


def test_chrome_popup_markup_carries_no_stream_section(bundles):
    html = _popup_html(bundles / "chrome")
    for marker in _STREAM_MARKUP:
        assert marker not in html, f"Chrome popup still ships {marker}"
    assert not [s for s in _SCRIPT_RE.findall(html) if "streams" in s]
    assert not [s for s in _STYLE_RE.findall(html) if "streams" in s]


def test_the_stream_section_is_built_by_the_module_not_the_template(bundles):
    """Neither template carries the markup: the Firefox module creates it."""
    for browser in ("chrome", "firefox"):
        html = _popup_html(bundles / browser)
        for marker in _STREAM_MARKUP:
            assert marker not in html, f"{browser} popup.html should not hard-code {marker}"


def test_firefox_popup_loads_its_stream_module_before_the_shared_popup(bundles):
    scripts = _SCRIPT_RE.findall(_popup_html(bundles / "firefox"))
    assert scripts.count("streams.js") == 1
    assert scripts.count("popup.js") == 1
    # The module publishes the hook the shared popup looks for, so it has to be
    # evaluated first or the hook is simply not there when initialisation runs.
    assert scripts.index("streams.js") < scripts.index("popup.js")

    styles = _STYLE_RE.findall(_popup_html(bundles / "firefox"))
    assert styles.count("streams.css") == 1


@pytest.mark.parametrize("browser", ["chrome", "firefox"])
def test_every_popup_resource_exists_exactly_once(bundles, browser):
    bundle = bundles / browser
    html = _popup_html(bundle)
    popup_dir = bundle / "popup"
    references = _SCRIPT_RE.findall(html) + _STYLE_RE.findall(html)
    assert references, "the popup references something"
    for ref in references:
        assert references.count(ref) == 1, f"{ref} referenced more than once"
        assert not ref.startswith(("http://", "https://", "//")), "no remote resource"
        resolved = (popup_dir / ref).resolve()
        assert resolved.is_file(), f"{browser} popup references a missing {ref}"
        assert bundle.resolve() in resolved.parents or resolved.parent == bundle.resolve()


def test_the_popup_template_carries_one_of_each_composition_marker():
    html = (ROOT / "extension" / "popup" / "popup.html").read_text()
    for marker in (build_extension._POPUP_STYLE_MARKER,
                   build_extension._POPUP_MODULE_MARKER):
        assert html.count(marker) == 1


def _template(tmp_path: Path, body: str) -> Path:
    popup = tmp_path / "popup"
    popup.mkdir()
    (popup / "popup.html").write_text(body)
    return popup / "popup.html"


def test_popup_composition_swaps_a_marker_for_its_tag(tmp_path):
    marker = build_extension._POPUP_MODULE_MARKER
    html = _template(tmp_path, f"<body>\n  {marker}\n  <p></p>\n</body>\n")

    build_extension._compose_popup(tmp_path, {marker: '<script src="streams.js"></script>'})

    assert html.read_text() == (
        '<body>\n  <script src="streams.js"></script>\n  <p></p>\n</body>\n'
    )


def test_popup_composition_drops_a_marker_it_has_no_tag_for(tmp_path):
    marker = build_extension._POPUP_MODULE_MARKER
    html = _template(tmp_path, f"<body>\n  {marker}\n  <p></p>\n</body>\n")

    build_extension._compose_popup(tmp_path, {marker: ""})

    assert html.read_text() == "<body>\n  <p></p>\n</body>\n"


def test_popup_composition_refuses_a_missing_marker(tmp_path):
    """A silently uncomposed popup ships without its module. Fail instead."""
    _template(tmp_path, "<body>\n  <p></p>\n</body>\n")

    with pytest.raises(ValueError, match="popup-modules"):
        build_extension._compose_popup(
            tmp_path, {build_extension._POPUP_MODULE_MARKER: ""})


def test_popup_composition_refuses_a_duplicated_marker(tmp_path):
    marker = build_extension._POPUP_MODULE_MARKER
    _template(tmp_path, f"<body>\n  {marker}\n  {marker}\n</body>\n")

    with pytest.raises(ValueError, match="popup-modules"):
        build_extension._compose_popup(tmp_path, {marker: ""})


# ---- The reproducibility claim the store listings make ----
#
# docs/firefox-store-listing.md tells an AMO reviewer that the bundle is the
# extension/ directory with exactly one composed file, and describes the two
# lines that differ. A reviewer diffs the XPI against the repository, so that
# claim has to keep being true rather than having been true once.

_COMPOSED = "popup/popup.html"


def test_firefox_bundle_is_the_source_tree_with_one_composed_file(bundles):
    bundle = bundles / "firefox"
    source = ROOT / "extension"

    for rel in sorted(_files(bundle)):
        if rel == _COMPOSED:
            continue
        assert (bundle / rel).read_bytes() == (source / rel).read_bytes(), rel

    built_lines = (bundle / _COMPOSED).read_text().splitlines()
    source_lines = (source / _COMPOSED).read_text().splitlines()
    assert len(built_lines) == len(source_lines), "composition replaces lines, never adds"

    differing = [(a, b) for a, b in zip(source_lines, built_lines) if a != b]
    assert [a.strip() for a, _ in differing] == [
        build_extension._POPUP_STYLE_MARKER,
        build_extension._POPUP_MODULE_MARKER,
    ]
    assert [b.strip() for _, b in differing] == [
        '<link rel="stylesheet" href="streams.css">',
        '<script src="streams.js"></script>',
    ]


def test_chrome_bundle_is_the_source_tree_with_its_markers_dropped(bundles):
    bundle = bundles / "chrome"
    source = ROOT / "extension"
    markers = {build_extension._POPUP_STYLE_MARKER, build_extension._POPUP_MODULE_MARKER}

    for rel in sorted(_files(bundle)):
        # manifest.json is the MV3 manifest swapped in, not a copy of the MV2 one.
        if rel in (_COMPOSED, "manifest.json"):
            continue
        assert (bundle / rel).read_bytes() == (source / rel).read_bytes(), rel

    built_lines = (bundle / _COMPOSED).read_text().splitlines()
    source_lines = (source / _COMPOSED).read_text().splitlines()
    kept = [line for line in source_lines if line.strip() not in markers]
    assert len(source_lines) - len(kept) == 2, "exactly the two marker lines"
    assert built_lines == kept


# ---- ZIP and unpacked bundle equivalence ----
#
# The ZIP is what a store receives and the unpacked directory is what the
# browser loads when testing. A file count or a version that happens to match
# is not equivalence, so these compare membership and bytes.


def _zip_members(zip_path: Path) -> dict[str, bytes]:
    from zipfile import ZipFile

    with ZipFile(zip_path) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


@pytest.mark.parametrize("browser", ["chrome", "firefox"])
def test_zip_membership_matches_the_unpacked_bundle(bundles, browser):
    zips = list(bundles.glob(f"cove-{browser}-*.zip"))
    assert len(zips) == 1
    assert set(_zip_members(zips[0])) == _files(bundles / browser)


def test_firefox_zip_bytes_match_the_unpacked_bundle(bundles):
    bundle = bundles / "firefox"
    zips = list(bundles.glob("cove-firefox-*.zip"))
    for name, data in _zip_members(zips[0]).items():
        assert data == (bundle / name).read_bytes(), f"{name} differs from the unpacked copy"


def test_chrome_zip_bytes_match_the_unpacked_bundle_but_for_the_key(bundles):
    bundle = bundles / "chrome"
    zips = list(bundles.glob("cove-chrome-*.zip"))
    members = _zip_members(zips[0])

    for name, data in members.items():
        if name == "manifest.json":
            continue
        assert data == (bundle / name).read_bytes(), f"{name} differs from the unpacked copy"

    # The one deliberate difference, and nothing else: the development key.
    stored = json.loads(members["manifest.json"])
    unpacked = json.loads((bundle / "manifest.json").read_text())
    assert "key" in unpacked and "key" not in stored
    assert stored == {k: v for k, v in unpacked.items() if k != "key"}


@pytest.mark.parametrize("browser", ["chrome", "firefox"])
def test_no_signing_key_reaches_either_artifact(bundles, browser):
    zips = list(bundles.glob(f"cove-{browser}-*.zip"))
    assert "chrome-key.pem" not in _zip_members(zips[0])
    assert "chrome-key.pem" not in _files(bundles / browser)


# ---- Store listing copy tracks the manifest it describes ----
#
# The two listing documents are what gets pasted into the dashboards, and each
# names the version and the ZIP filename the reviewer will receive. A manifest
# bump without a copy update hands a reviewer notes for the previous release,
# so the candidate version is read from the manifest rather than written twice.

_LISTINGS = {
    "chrome": (ROOT / "docs" / "chrome-store-listing.md", "manifest.chrome.json"),
    "firefox": (ROOT / "docs" / "firefox-store-listing.md", "manifest.json"),
}

# Versions that have already gone to a store from this repository and can
# therefore never be a candidate again. Per browser, because the two are
# versioned independently against separate stores.
#
# 1.3.9 and 1.4.8 were uploaded. Chrome 1.3.10 and Firefox 1.4.9 went further
# and were confirmed *published* on 2026-09-12: the public Store listing
# reported version 1.3.10, and the public AMO API reported current_version
# 1.4.9 with status public. Chrome requires each update to carry a strictly
# larger version and forbids reusing a published number.
_SHIPPED = {
    "chrome": ("1.3.9", "1.3.10"),
    "firefox": ("1.4.8", "1.4.9"),
}

# The latest version confirmed published per store, on the evidence above. A
# candidate must be strictly greater than this. Membership in _SHIPPED alone
# is not enough: it is an exhaustive blocklist, so a rollback to a number that
# predates the list - Chrome 1.3.8, Firefox 1.4.7 - would pass it while still
# being unuploadable.
_PUBLISHED = {"chrome": "1.3.10", "firefox": "1.4.9"}


def _version_tuple(version):
    return tuple(int(part) for part in version.split("."))


@pytest.mark.parametrize("browser", ["chrome", "firefox"])
def test_store_listing_names_the_current_candidate_version(browser):
    doc, manifest_name = _LISTINGS[browser]
    version = json.loads((ROOT / "extension" / manifest_name).read_text())["version"]
    text = doc.read_text(encoding="utf-8")

    assert f"dist/cove-{browser}-{version}.zip" in text
    assert f"What's new in version {version}" in text or f"Release notes for {version}" in text


@pytest.mark.parametrize("browser", ["chrome", "firefox"])
def test_candidate_version_is_not_an_already_shipped_version(browser):
    """The candidate itself must not reuse a shipped version.

    Kept separate from the document check below on purpose. The two are
    different requirements, and an earlier version of this guard skipped its
    own document assertions whenever a shipped version equalled the manifest
    version - which meant the one mistake it existed to catch, reusing a
    shipped number, made it pass.
    """
    _, manifest_name = _LISTINGS[browser]
    version = json.loads((ROOT / "extension" / manifest_name).read_text())["version"]

    assert version not in _SHIPPED[browser], (
        f"{browser} candidate {version} has already gone to the store; "
        "a shipped version number can never be reused"
    )


@pytest.mark.parametrize("browser", ["chrome", "firefox"])
def test_candidate_version_is_above_the_published_one(browser):
    """Ordering, not just membership in a blocklist.

    Both stores require an update to carry a strictly larger version than the
    one currently published. A blocklist can only ever name versions somebody
    remembered to add; an ordering assertion covers every older number,
    including ones that predate the list entirely.
    """
    _, manifest_name = _LISTINGS[browser]
    version = json.loads((ROOT / "extension" / manifest_name).read_text())["version"]
    published = _PUBLISHED[browser]

    assert _version_tuple(version) > _version_tuple(published), (
        f"{browser} candidate {version} is not above the published {published}; "
        "the store will reject it"
    )


@pytest.mark.parametrize("browser", ["chrome", "firefox"])
def test_store_listing_does_not_offer_an_already_shipped_version(browser):
    """No listing document may still be offering a shipped version."""
    doc, _ = _LISTINGS[browser]
    text = doc.read_text(encoding="utf-8")

    for shipped in _SHIPPED[browser]:
        assert f"dist/cove-{browser}-{shipped}.zip" not in text
        assert f"What's new in version {shipped}" not in text
        assert f"Release notes for {shipped}" not in text
