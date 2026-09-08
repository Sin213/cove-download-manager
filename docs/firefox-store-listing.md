# AMO listing - Cove Download Manager (Firefox) 1.4.8

Lives in `docs/` rather than `dist/`, which `scripts/build_extension.py`
deletes on every build.

Upload `dist/cove-firefox-1.4.8.zip` at
<https://addons.mozilla.org/developers/>.

The three blocks below map to the three AMO fields. They are plain text, not
markdown - AMO renders the description literally, so the bullets are real `•`
characters and there is no other formatting to strip.

Firefox ships the full feature set from the shared `extension/` source:
in-page video pill, video and audio context menus, HLS detection, the
detected-stream popup section, and the page extractor. Chrome ships the first
two from that same shared source; what it does not ship is the site-specific
part, meaning HLS detection, the detected-stream popup, and the page
extractor. See `docs/chrome-store-listing.md` for why the Chrome copy differs.

## Store listing -> Description

```text
Cove Download Manager (browser extension) hands your downloads off to the Cove desktop app so they download faster and stay organized.

When you start a download, the extension can intercept it and send the link to Cove, along with the page's cookies and referrer so logged-in and protected downloads still work. Cove then downloads the file using multiple connections and manages it in a real queue.

Features:

• Multi-connection downloads with up to 16 connections per file for higher speeds

• Real download queue with start, pause, and per-item controls

• Daily schedule window and a global speed cap

• In-page video pill: a small button appears on pages with video, and one click sends that video to Cove

• Right-click any link, image, video, or audio and choose "Download with Cove"

• Supported video pages are handed to Cove for extraction, including HLS (.m3u8) streams

• Sends cookies, referrer, and user-agent information so authenticated downloads work

• Toggle interception on or off with Alt+Shift+D, and set excluded domains and a minimum file size, which applies whenever Firefox gives a usable size at the start of a download

How interception works:

When you start a download, Cove can take it over instead of leaving it to the browser: installers, archives, documents, and other direct file links.

You decide what it touches. Interception can be switched off with a keyboard shortcut, restricted to specific file types, disabled entirely on domains you list, and limited by a minimum file size.

About that minimum: it can only be applied when Firefox gives a usable size at the moment a download starts. In the Firefox version and test files used for this release, Firefox reported the size as unknown at that moment for every download, including one whose server did declare a length, so the minimum did not filter any of them. Expect a download smaller than your minimum to be taken over anyway. Whether a size is available is Firefox's decision rather than the server's, and it may differ on other versions.

Nothing is downloaded without an action you took, and the extension never collects or transmits your browsing history.

What's new in version 1.4.8:

Downloads whose size Firefox does not report are handed to Cove correctly. Firefox reports an unknown size as -1, and that value used to be passed straight through to Cove, which rejected the handoff. It is now sent as an unknown size, and the download goes through.

The description of the minimum file size has been corrected to match what actually happens: the minimum can only be applied when Firefox gives a usable size at the start of a download, so a smaller file may still be taken over.

The free Cove Download Manager desktop app is required because it provides the download engine. Install Cove, launch it once, and then click "Test Connection to Cove" in the extension to link them.

Cove is open source:
https://github.com/Sin213/cove-download-manager
```

## Version -> Release Notes

Shown on the add-on's detail page under this version. Keep it to what changed
in 1.4.8 only.

```text
Fixed: downloads whose size Firefox does not report now reach Cove instead of being rejected.

Firefox reports an unknown size as -1, and that value was passed straight through to the desktop app, which rejected the handoff because a size cannot be negative. The size is now sent as unknown, and the download goes through. In testing, Firefox reported the size as unknown for every download, including one whose server declared a length, so this affected far more downloads than the wording "responses with no declared length" suggested.

Fixed: the video download button is more careful about which source it sends. It now waits for the player's own address to be usable rather than acting on a partly resolved one, and prefers the address the element itself names over one inferred around it.

Changed: the description of the minimum file size now matches what actually happens. The minimum can only be applied when Firefox gives a usable size at the moment a download starts, so a file smaller than your minimum may still be taken over. This is a wording correction, not a change in behaviour.

Internal: the add-on's media code is split into a browser-neutral part and the Firefox-only site handling, and the popup's detected-stream section moved into its own module. The scripts named in the manifest changed accordingly. Nothing the add-on does on a page changed because of it.

No permission changes in this version.
```

## Version -> Notes to Reviewer

```text
Source code and build process

This add-on is not minified, obfuscated, transpiled, bundled, or generated by any build tool. Every .js, .css, and .html file in the XPI is hand-written source, readable as-is.

The packaging step is a file copy, one deterministic edit to a single HTML file, and a zip. scripts/build_extension.py in the repository copies extension/ to dist/firefox/ and zips it, using only the Python standard library (json, shutil, pathlib, zipfile). No compiler, minifier, transpiler, or package manager is involved, and no dependencies are downloaded or vendored.

The one edit: popup/popup.html carries two HTML comment markers, "<!-- cove:popup-styles -->" and "<!-- cove:popup-modules -->", each on its own line. The Firefox build replaces them with a stylesheet link for popup/streams.css and a script tag for popup/streams.js, which are the Firefox-only detected-stream section of the popup. The Chrome build removes both marker lines instead, because that bundle does not ship stream detection. Nothing else in any file is rewritten, and no JavaScript is transformed. The build fails loudly if either marker is missing or appears more than once.

The uploaded XPI holds 21 files. Twenty of them are byte-for-byte identical to their counterpart in the extension/ directory of the public repository. The twenty-first, popup/popup.html, differs only by that marker substitution. You can verify this by diffing the XPI contents against that directory: popup/popup.html should be the only file that differs, and the difference should be exactly the two lines described above.

To reproduce the uploaded file:

  git clone https://github.com/Sin213/cove-download-manager
  cd cove-download-manager
  python scripts/build_extension.py

This writes dist/cove-firefox-1.4.8.zip. Requires Python 3.9 or later, no other tooling. Three things differ from the Chrome bundle produced by the same script: manifest.json (MV2 vs MV3); the browser-specific modules, which are media-sites.js, content/media-sites.js, popup/streams.js and popup/streams.css in the Firefox bundle only, and media-chrome.js in the Chrome bundle only; and the popup composition described above. That is why the Firefox bundle holds 21 files and the Chrome bundle holds 18. Chrome is not without video handling: it ships the same shared in-page button and the same video and audio context-menu entries for a media element whose own address is an ordinary HTTP(S) file. What the Chrome build excludes is the site-specific part, meaning page extractors, HLS stream detection, and the detected-stream popup section.

Public source: https://github.com/Sin213/cove-download-manager

What changed in 1.4.8

No new permissions, no new APIs, no new hosts. The permission set in manifest.json is byte-identical to 1.4.7; only the version and the script lists differ.

A file reorganisation, so the manifest's script lists changed:

extension/media.js was split into extension/media-core.js (browser-neutral mechanics: filename derivation, the in-page button's handoff, the media message surface) and extension/media-sites.js (the Firefox-only site handling: page extractors, site title rules, HLS stream observation). The manifest's background.scripts therefore names media-core.js, media-sites.js, background.js instead of media.js, background.js. Likewise the detected-stream part of the content script moved to content/media-sites.js, which now precedes content/media-tab.js, and the popup's stream section moved to popup/streams.js and popup/streams.css. This is a split of existing code, not new capability: the Firefox bundle still ships every part of it.

The split exists because the same source tree also builds a Chrome bundle that ships none of the site handling. Files whose names end in -sites are the Firefox-only half, and the Chrome build excludes them.

Behaviour fixes:

1. extension/background.js - a download size the browser reports as unknown is normalised before the handoff. Firefox reports -1 for an unknown size; that value reached the desktop app, which rejects a negative size, so those downloads were refused outright. It is now sent as 0, meaning unknown, which the app accepts.

2. extension/content/media-tab.js - the in-page button now requires the player's own address to be usable before acting on it, and takes the address the element names over one inferred around it. This removes a case where a partly resolved player produced a handoff for the wrong source.

These are covered by tests/extension_background.test.js, tests/extension_media_tab.test.js, and tests/test_extension_bundle.py in the repository, which run under node --test and pytest with no third-party dependencies.

How the add-on works

Cove Download Manager is the browser half of a desktop download manager. The extension does not download anything itself. It observes a download the user started, and hands the URL to the local desktop app over native messaging, which performs the download with multiple connections and queue management.

Permission justifications, unchanged from previous versions:

• nativeMessaging - the entire purpose of the add-on. It passes download requests to the Cove desktop app via the native host cove_dm_host. Without this the add-on does nothing.

• downloads - to observe downloads the user starts and cancel the browser's copy after Cove has taken it over, so the file is not downloaded twice.

• cookies - authenticated and paywalled downloads fail without the session cookies for the originating site. These are read for the download's own URL and passed to the local desktop app only. They are never sent to any remote server, and never stored by the extension.

• webRequest and <all_urls> - downloads and media can originate from any site, so the add-on cannot enumerate hosts ahead of time. Used to observe request headers for the download being handed off, and to detect media on the page for the in-page pill.

• contextMenus - the "Download with Cove" right-click entry on links, images, video, and audio.

• notifications - to tell the user when a handoff to Cove succeeded or failed.

• storage - to persist the user's own settings: the interception toggle, minimum file size, file type filters, and excluded domains.

Privacy

No analytics, no telemetry, no remote endpoints. The add-on communicates only with the Cove desktop app on the same machine, over native messaging. Browsing history is never collected or transmitted. The manifest declares data_collection_permissions: ["none"].

The extension keeps a small local diagnostics ring in browser storage so a user can report a failed handoff while the desktop app is closed. It records event names and outcomes only - no page URLs, no media URLs, no tab titles, and no cookie values. This is asserted by tests/extension_diagnostics.test.js in the repository.

Testing the add-on

The desktop app is required to exercise the handoff, and is free and open source. Linux, Windows, and macOS builds are at:
https://github.com/Sin213/cove-download-manager/releases

Install Cove, launch it once, then click "Test Connection to Cove" in the extension popup. Without the desktop app the extension installs and its UI works, but every download handoff will correctly report that Cove is unavailable.
```

## Facts behind the copy

| Claim | Source |
| --- | --- |
| 16 connections per file | `cove/config.py:30`, `cove/config.py:133` |
| link, image, video, audio contexts | `extension/background.js:428` plus `extension/media.js:230` `contexts: ["video", "audio"]` |
| in-page pill ships on Firefox | `content/media-tab.js` and `.css` present in the bundle and registered in `content_scripts`, asserted by `tests/test_extension_bundle.py` |
| extractor and HLS ship on Firefox | `media.js` present in the bundle, `manifest.background.scripts` loads it |
| Alt+Shift+D toggle | `extension/manifest.json` `commands.toggle-intercept` |
| "No video found" instead of the page address | `content/media-tab.js` `onPillClick`, guarded by `tests/extension_media_tab.test.js` |
| the pill can hide again after that | `downloadPending` set only once an address resolves, guarded by the same file |
| stream lookup is ancestor-scoped | `content/media-tab.js` `embeddedStreamUrl` uses `closest()` only |
| intercepted ids pruned against the browser | `extension/background.js` `pruneInterceptedIds`, `tests/extension_background.test.js` |
| badge OFF outranks a media count | `extension/media.js` calls `renderBadge` rather than painting directly |
| diagnostics record no URLs or cookies | `tests/extension_diagnostics.test.js` |
| bundle is a copy of `extension/` with one composed HTML file | `scripts/build_extension.py` `_copy_shared` and `_compose_popup`; 20 of 21 files sha256-identical, `popup/popup.html` differing only by the two marker substitutions, asserted by `tests/test_extension_bundle.py` |
| no permission changes | `git diff af4afbc..HEAD -- extension/manifest.json` shows only the version line |

## Before submitting

- The feature bullets in the description carry over unchanged except for the
  minimum-file-size bullet, which was corrected. The "What's new" paragraph, the
  release notes, and the reviewer notes are new copy for 1.4.8.
- Screenshots do not need replacing. This build still ships the video pill, so
  existing pill screenshots remain accurate.
- `nativeMessaging`, `cookies`, `webRequest`, and `<all_urls>` are still
  requested and still need their justifications - they are reproduced in the
  reviewer notes above.
- **Not yet done: the manual load check.** Load `dist/firefox/` as a temporary
  add-on and confirm two things by hand before uploading - right-clicking a
  video offers "Download with Cove", and the in-page pill appears on hover.
  This has been outstanding since `0be8c3e` changed the script load order and
  no browser has executed the new layout yet. See the note in
  `project-firefox-release-check`.
- 1.4.7 carried extension fixes from `c445cb1` that were written before the
  1.4.6 upload but never given a version bump, so the published 1.4.6 and the
  repository's 1.4.6 source were not the same code. That was corrected there.
- 1.4.7 is the version currently public on AMO (verified 2026-09-07 against the
  AMO API for add-on `cove-dm@cove-download-manager.net`), and the repository
  source was also 1.4.7, so 1.4.8 is the next patch above both. That check reads
  public versions only; it cannot see an upload awaiting review or an unlisted
  one, so confirm on the developer dashboard before uploading.
