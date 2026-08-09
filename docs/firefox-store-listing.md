# AMO listing - Cove Download Manager (Firefox) 1.4.7

Lives in `docs/` rather than `dist/`, which `scripts/build_extension.py`
deletes on every build.

Upload `dist/cove-firefox-1.4.7.zip` at
<https://addons.mozilla.org/developers/>.

The three blocks below map to the three AMO fields. They are plain text, not
markdown - AMO renders the description literally, so the bullets are real `•`
characters and there is no other formatting to strip.

Unlike the Chrome bundle, Firefox ships the full feature set from the shared
`extension/` source: in-page video pill, video and audio context menus, HLS
detection, and the page extractor. See `docs/chrome-store-listing.md` for why
the Chrome copy differs.

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

• Toggle interception on or off with Alt+Shift+D, and set a minimum file size and excluded domains

How interception works:

When you start a download, Cove can take it over instead of leaving it to the browser: installers, archives, documents, and other direct file links.

You decide what it touches. Interception can be switched off with a keyboard shortcut, limited by a minimum file size, restricted to specific file types, and disabled entirely on domains you list.

Nothing is downloaded without an action you took, and the extension never collects or transmits your browsing history.

What's new in version 1.4.7:

The download button on videos is more accurate about what it can do. On a page where the video's address cannot be worked out, it now says "No video found" instead of quietly sending the page itself to Cove, which produced a download that failed for no visible reason. The button also stops getting stuck after that happens, and a video on a page with several players can no longer be sent using a different player's stream.

Interception also cleans up after itself more reliably. A download the browser finished or cancelled while the extension was asleep used to stay on the tracking list for the rest of the session, which could misclassify a later download that reused the same id. And the toolbar badge now shows OFF whenever interception is disabled, instead of being overwritten by a count of videos found on the page.

The free Cove Download Manager desktop app is required because it provides the download engine. Install Cove, launch it once, and then click "Test Connection to Cove" in the extension to link them.

Cove is open source:
https://github.com/Sin213/cove-download-manager
```

## Version -> Release Notes

Shown on the add-on's detail page under this version. Keep it to what changed
in 1.4.7 only.

```text
Fixed: the video download button no longer sends the page instead of the video.

On a page where the video's address cannot be determined - a player that streams through the browser rather than from a plain file address - the button used to fall back to sending the page's own address. Cove then tried to download a web page as if it were a video, which failed with an unhelpful error or, on sites that refuse unfamiliar clients, no explanation at all. The button now reports "No video found", which is the honest answer.

Fixed: the button no longer sticks on the page after that happens. It could previously stay pinned over the page until a reload, and ignore every later click.

Fixed: on a page with several video players, a player with no stream of its own can no longer be sent using the first player's stream. That produced a download that appeared to succeed and fetched the wrong video.

Fixed: intercepted downloads are cleaned up even when the browser's completion event is missed, which can happen while the extension is suspended. A leftover entry used to persist for the whole session and could suppress or misclassify a later download that reused the same id.

Fixed: the toolbar badge shows OFF while interception is disabled, rather than being replaced by a count of videos detected on the page.

No permission changes in this version.
```

## Version -> Notes to Reviewer

```text
Source code and build process

This add-on is not minified, obfuscated, transpiled, bundled, or generated by any build tool. Every .js, .css, and .html file in the XPI is hand-written source, readable as-is.

The packaging step is a file copy and a zip, nothing more. scripts/build_extension.py in the repository copies extension/ to dist/firefox/ and zips it, using only the Python standard library (json, shutil, pathlib, zipfile). No compiler, minifier, transpiler, or package manager is involved, and no dependencies are downloaded or vendored.

Every one of the 17 files in the uploaded XPI is byte-for-byte identical to its counterpart in the extension/ directory of the public repository. You can verify this by diffing the XPI contents against that directory.

To reproduce the uploaded file:

  git clone https://github.com/Sin213/cove-download-manager
  cd cove-download-manager
  python scripts/build_extension.py

This writes dist/cove-firefox-1.4.7.zip. Requires Python 3.9 or later, no other tooling. Only two things differ from the Chrome bundle produced by the same script: manifest.json (MV2 vs MV3) and the exclusion of video handling from the Chrome build.

Public source: https://github.com/Sin213/cove-download-manager

What changed in 1.4.7

Five fixes, in two files. No new permissions, no new APIs, no new hosts.

extension/content/media-tab.js - the in-page video button:

1. It no longer falls back to sending the page's own address when it cannot determine the video's address. On a player that streams through the browser (a blob: source with no separate stream visible on the page), there is genuinely nothing to download, and sending the page address made the desktop app fetch an HTML page as if it were a video. It now reports "No video found" and sends nothing.

2. The in-flight flag is released when there is no video to send. It was previously set before the address was resolved and cleared only on a path that this case returned before reaching, so the button pinned itself over the page until a reload and rejected every later click.

3. The lookup for a player's embedded stream address is now scoped to that player's own ancestors. It previously fell back to the first matching element anywhere in the document, so on a page with several players every one of them resolved to the first player's stream - a download that looked successful and fetched the wrong video.

extension/background.js and extension/media.js - interception bookkeeping:

4. Intercepted download ids are pruned against the browser's own download list. Cleanup previously depended entirely on catching a terminal onChanged event, which is missed when it races the insertion or fires while the extension is suspended. A leftover id persisted for the whole session and could suppress or misclassify a later event for a reused id. Writes to the persisted set are chained rather than fired independently, because two overlapping writes could complete out of order and resurrect ids that had just been cleared.

5. The toolbar badge is rendered through a single function so the disabled OFF state takes priority over a media count. media.js previously painted the badge directly, which overwrote OFF and made a disabled extension look active.

These are covered by tests/extension_background.test.js and tests/extension_media_tab.test.js in the repository, which run under node --test with no dependencies.

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
| bundle is a verbatim copy of `extension/` | `scripts/build_extension.py` `_copy_shared`; all 17 files verified sha256-identical for 1.4.7 |
| no permission changes | `git diff af4afbc..HEAD -- extension/manifest.json` shows only the version line |

## Before submitting

- The feature bullets in the description carry over from 1.4.6 unchanged. Only
  the "What's new" paragraph, the release notes, and the reviewer notes are new
  copy for 1.4.7.
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
- 1.4.7 carries extension fixes from `c445cb1` that were written before the
  1.4.6 upload but never given a version bump, so the published 1.4.6 and the
  repository's 1.4.6 source were not the same code. That is corrected here.
