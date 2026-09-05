#!/usr/bin/env python3
"""Build per-browser extension bundles from the shared extension/ source.

Approach A (see docs/superpowers/specs/2026-06-17-chrome-extension-support
-design.md): one shared codebase, the manifest swapped per browser.

  dist/firefox/  copy of extension/ (manifest.json is the MV2 manifest)
  dist/chrome/   copy of extension/ with manifest.chrome.json -> manifest.json

Each is also zipped as dist/cove-<browser>-<version>.zip. The private
signing key (chrome-key.pem) is never copied into a bundle.

Usage: python scripts/build_extension.py
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "extension"
DIST = ROOT / "dist"

# Files/dirs in extension/ that must never ship in a bundle. Entries are
# POSIX paths relative to extension/, so a nested file can be named as
# "content/media-sites.js" and a whole directory as "content".
_EXCLUDE = {"manifest.chrome.json", "chrome-key.pem"}

# Each browser gets the shared media runtime plus its own capability, and
# never the other's.
#
# The Chrome Web Store rejected 1.3.5 under "Malicious and Prohibited
# Products" for facilitating downloads of copyrighted media, naming YouTube.
# What that rejection was about is site handling - page extractors, site
# media discovery, stream observation - which is exactly the two files
# excluded below. A media element whose own address is an ordinary HTTP(S)
# file is not that, so Chrome keeps the browser-neutral mechanics
# (media-core.js) and the shared in-page pill (content/media-tab.js and its
# stylesheet) and gets media-chrome.js, a capability that supplies no site
# hooks at all.
#
# Firefox keeps both site modules and, symmetrically, must not receive
# media-chrome.js: two capabilities would fight over the same global.
#
# content/ is no longer excluded wholesale, so content/media-sites.js is now
# doing real work in this set rather than documenting an intent.
# Guarded by tests/test_extension_bundle.py.
#
# popup/streams.js and popup/streams.css are the same decision one level up.
# Detected streams come from media-sites.js, so a Chrome popup could only ever
# show an empty list - and the Download button on a stream row was a send with
# no detector behind it. The section, its styling and its click handler are one
# Firefox-only module, composed into that bundle's popup below.
_CHROME_EXCLUDE = {"media-sites.js", "content/media-sites.js",
                   "popup/streams.js", "popup/streams.css"}
_FIREFOX_EXCLUDE = {"media-chrome.js"}

# Browser-specific popup resources are composed into the shared popup at build
# time, so both source manifests keep naming the one popup/popup.html. The
# markers sit on their own lines in the template and are replaced by a real tag
# or removed outright.
_POPUP_HTML = "popup/popup.html"
_POPUP_STYLE_MARKER = "<!-- cove:popup-styles -->"
_POPUP_MODULE_MARKER = "<!-- cove:popup-modules -->"

_CHROME_POPUP = {_POPUP_STYLE_MARKER: "", _POPUP_MODULE_MARKER: ""}
_FIREFOX_POPUP = {
    _POPUP_STYLE_MARKER: '<link rel="stylesheet" href="streams.css">',
    _POPUP_MODULE_MARKER: '<script src="streams.js"></script>',
}


def _is_excluded(rel: str, exclude) -> bool:
    """True when `rel` is an excluded path or sits under an excluded one."""
    for pattern in (*_EXCLUDE, *exclude):
        if rel == pattern or rel.startswith(f"{pattern}/"):
            return True
    return False


def _copy_shared(dest: Path, exclude: set[str] = frozenset()) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for item in sorted(SRC.rglob("*")):
        rel = item.relative_to(SRC).as_posix()
        if _is_excluded(rel, exclude):
            continue
        target = dest / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def _compose_popup(dest: Path, replacements: dict[str, str]) -> None:
    """Swap the popup's browser-specific include markers for real tags.

    Each marker must sit on a line of its own and appear exactly once. A
    missing or duplicated marker means the template moved out from under this
    builder, and the failure it would otherwise produce is silent: a Firefox
    popup shipped with no stream module, or a Chrome popup shipped with one.
    """
    path = dest / _POPUP_HTML
    seen = {marker: 0 for marker in replacements}
    out: list[str] = []
    for line in path.read_text().splitlines(keepends=True):
        marker = line.strip()
        if marker in seen:
            seen[marker] += 1
            tag = replacements[marker]
            if tag:
                out.append(line[: len(line) - len(line.lstrip())] + tag + "\n")
            continue
        out.append(line)

    wrong = {marker: count for marker, count in seen.items() if count != 1}
    if wrong:
        found = ", ".join(f"{marker} x{count}" for marker, count in sorted(wrong.items()))
        raise ValueError(f"{_POPUP_HTML}: markers not present exactly once: {found}")

    path.write_text("".join(out))


def _zip_dir(src_dir: Path, zip_path: Path, manifest_override: str | None = None) -> None:
    with ZipFile(zip_path, "w", ZIP_DEFLATED) as zf:
        for path in sorted(src_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(src_dir)
            if manifest_override is not None and rel.as_posix() == "manifest.json":
                zf.writestr("manifest.json", manifest_override)
            else:
                zf.write(path, rel)


def build(dist: Path | None = None) -> None:
    DIST = Path(dist) if dist is not None else globals()["DIST"]
    if DIST.exists():
        shutil.rmtree(DIST)

    # Firefox: manifest.json is already the MV2 manifest.
    firefox = DIST / "firefox"
    _copy_shared(firefox, exclude=_FIREFOX_EXCLUDE)
    _compose_popup(firefox, _FIREFOX_POPUP)
    ff_version = json.loads((firefox / "manifest.json").read_text())["version"]
    _zip_dir(firefox, DIST / f"cove-firefox-{ff_version}.zip")

    # Chrome: swap in the MV3 manifest as manifest.json, minus site handling.
    chrome = DIST / "chrome"
    _copy_shared(chrome, exclude=_CHROME_EXCLUDE)
    _compose_popup(chrome, _CHROME_POPUP)
    mv3 = json.loads((SRC / "manifest.chrome.json").read_text())
    # Unpacked dir keeps `key` so the dev extension id is stable and matches
    # the native host whitelist when loaded unpacked for local testing.
    (chrome / "manifest.json").write_text(json.dumps(mv3, indent=2) + "\n")
    # The Web Store upload must NOT contain `key` (Google rejects it and
    # assigns the permanent id itself), so strip it from the zipped manifest.
    store_manifest = {k: v for k, v in mv3.items() if k != "key"}
    _zip_dir(
        chrome,
        DIST / f"cove-chrome-{mv3['version']}.zip",
        manifest_override=json.dumps(store_manifest, indent=2) + "\n",
    )

    print(f"firefox: {firefox}  (v{ff_version})")
    print(f"chrome:  {chrome}  (v{mv3['version']})")
    for z in sorted(DIST.glob("*.zip")):
        print(f"zip:     {z}")


if __name__ == "__main__":
    build()
