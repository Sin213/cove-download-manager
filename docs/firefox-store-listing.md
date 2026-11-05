# AMO listing - Cove Download Manager (Firefox) 1.4.13

Lives in `docs/` rather than `dist/`, which `scripts/build_extension.py`
deletes on every build.

Upload `dist/cove-firefox-1.4.13.zip` at
<https://addons.mozilla.org/developers/>.

That ZIP is the **submission input**, not a distributable add-on. AMO signs an
accepted upload and returns the signed file; nothing in this repository
produces a signed XPI. Source: the submission flow at
<https://extensionworkshop.com/documentation/publish/submitting-an-add-on/>,
read 2026-09-12, which takes a `.zip`, `.xpi`, or `.crx` and signs it.

1.4.13 is the next patch above 1.4.12, which the public AMO API reported as the
add-on's `current_version` with status `public` on 2026-11-03.

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

When you start a download, the extension can intercept it and send the link to Cove, along with the referrer. For Gofile, Firefox captures the accountToken cookie from that specific storage-server download request and forwards only that value to Cove on this computer for the transfer. No Gofile API key or Premium account is needed. Cove then downloads the file using multiple connections and manages it in a real queue.

Features:

• Multi-connection downloads with up to 16 connections per file for higher speeds

• Real download queue with start, pause, and per-item controls

• Daily schedule window and a global speed cap

• In-page video pill: a small button appears on pages with video, and one click sends that video to Cove. The pill is not shown on a site listed under Excluded Domains, and the list takes effect on pages that are already open

• Use the menu beside the pill to exclude the current site. Cove opens its own confirmation page, which names the site and asks before the exclusion is saved

• Right-click any link, image, video, or audio and choose "Download with Cove"

• Supported video pages are handed to Cove for extraction, including HLS (.m3u8) streams

• On ordinary handoffs, sends the referrer and user-agent information; browser cookies are not sent

• Downloads from private windows that Firefox reports as private are left to the browser; Cove does not take them over

• Fuckingfast.co links added in Cove are opened in Firefox, and the extension starts the download there and hands it back to Cove

• Gofile downloads started in Firefox are handed to Cove using the browser session that authorized them; no Gofile API key or Premium account is needed (requires an updated Cove desktop app)

• Toggle interception on or off with Alt+Shift+D, and set excluded domains and a minimum file size, which applies whenever Firefox gives a usable size at the start of a download

How interception works:

When you start a download, Cove can take it over instead of leaving it to the browser: installers, archives, documents, and other direct file links. Ordinary downloads are paused while Cove is asked and resumed if Cove cannot take them. Gofile downloads deliberately keep downloading, because Firefox cannot resume a Gofile download once paused. A very small or fast Gofile download can finish in Firefox before Cove takes over, leaving both copies.

You decide what it touches. Interception can be switched off with a keyboard shortcut, restricted to specific file types, disabled entirely on domains you list, and limited by a minimum file size.

About that minimum: it can only be applied when Firefox gives a usable size at the moment a download starts. In the Firefox version and test files used for this release, Firefox reported the size as unknown at that moment for every download, including one whose server did declare a length, so the minimum did not filter any of them. Expect a download smaller than your minimum to be taken over anyway. Whether a size is available is Firefox's decision rather than the server's, and it may differ on other versions.

Nothing is downloaded without an action you took, and the extension never collects or transmits your browsing history.

What's new in version 1.4.13:

Gofile downloads started in Firefox can now use the browser session that already authorized them in Cove, without a Gofile API key or Premium account. Requires a Cove desktop version that adds Gofile browser-session support (newer than 3.7.1). Firefox keeps the download if Cove cannot accept it.

The free Cove Download Manager desktop app is required because it provides the download engine. Install Cove, launch it once, and then click "Test Connection to Cove" in the extension to link them.

Cove is open source:
https://github.com/Sin213/cove-download-manager
```

## Version -> Release Notes

Shown on the add-on's detail page under this version. Keep it to what changed
in 1.4.13 only.

```text
New: Gofile downloads started in Firefox can now be handed to Cove using the browser session that already authorized them. No Gofile API key and no Premium subscription are needed.

When you click Download on a Gofile page, the extension reads only the accountToken cookie from that specific download request and sends it to the Cove desktop app running on your computer. Cove keeps it in memory for the transfer and uses it only to fetch that file from Gofile through a local relay. It is never given to the download engine, never written to disk or logs, and never sent anywhere except Gofile.

Firefox keeps its Gofile copy downloading while Cove is asked and cancels it only after Cove confirms it has taken the download. Gofile downloads are deliberately not paused, because Firefox cannot resume a Gofile download once paused. If Cove is not running, is too old, cannot verify the file, or anything else goes wrong, Firefox keeps downloading. A very small or fast Gofile download can finish in Firefox before Cove takes over, leaving both copies. Both copies can also remain if Cove accepted but the extension could not confirm it in time, such as a reply after the 30-second deadline or two tabs asking for the same file.

Requires a Cove desktop version that adds Gofile browser-session support (newer than 3.7.1). With older Cove versions the extension leaves Gofile downloads to Firefox.

A Gofile download in progress in Cove cannot resume after you restart Cove, because the session credential is deliberately not stored. Download it again from Firefox.

Privacy: this version declares authenticationInfo in its data-collection permissions because the Gofile accountToken is passed to the local Cove app. Firefox may ask you to consent when you update. No new browser permissions are added.

Nothing is sent to the developer, there is no analytics or tracking, and nothing is sold or shared.
```

1.4.12 and earlier, already published (Fuckingfast through Firefox,
private-window handling, exclude-site action), are not re-announced here.

## Version -> Notes to Reviewer

```text
Source code and build process

This add-on is not minified, obfuscated, transpiled, bundled, or generated by any build tool. Every .js, .css, and .html file in the XPI is hand-written source, readable as-is.

The packaging step is a file copy, one deterministic edit to a single HTML file, and a zip. scripts/build_extension.py in the repository copies extension/ to dist/firefox/ and zips it, using only the Python standard library (json, shutil, pathlib, zipfile). No compiler, minifier, transpiler, or package manager is involved, and no dependencies are downloaded or vendored.

The one edit: popup/popup.html carries two HTML comment markers, "<!-- cove:popup-styles -->" and "<!-- cove:popup-modules -->", each on its own line. The Firefox build replaces them with a stylesheet link for popup/streams.css and a script tag for popup/streams.js, which are the Firefox-only detected-stream section of the popup. The Chrome build removes both marker lines instead, because that bundle does not ship stream detection. Nothing else in any file is rewritten, and no JavaScript is transformed. The build fails loudly if either marker is missing or appears more than once.

The uploaded ZIP holds 25 files. Twenty-four of them are byte-for-byte identical to their counterpart in the extension/ directory of the public repository. The twenty-fifth, popup/popup.html, differs only by that marker substitution. You can verify this by diffing the ZIP contents against that directory: popup/popup.html should be the only file that differs, and the difference should be exactly the two lines described above.

To reproduce the uploaded file:

  git clone https://github.com/Sin213/cove-download-manager
  cd cove-download-manager
  python scripts/build_extension.py

This writes dist/cove-firefox-1.4.13.zip. Requires Python 3.9 or later, no other tooling. Three things differ from the Chrome bundle produced by the same script: manifest.json (MV2 vs MV3); the browser-specific modules, which are media-sites.js, content/media-sites.js, fuckingfast-fetch.js, content/fuckingfast.js, popup/streams.js and popup/streams.css in the Firefox bundle only, and media-chrome.js in the Chrome bundle only; and the popup composition described above. That is why the Firefox bundle holds 25 files and the Chrome bundle holds 20. Chrome is not without video handling: it ships the same shared in-page button and the same video and audio context-menu entries for a media element whose own address is an ordinary HTTP(S) file. What the Chrome build excludes is the site-specific part, meaning page extractors, HLS stream detection, and the detected-stream popup section.

Public source: https://github.com/Sin213/cove-download-manager

What changed in 1.4.13

No new API permissions, no new host permissions, no new content scripts and no new files. Relative to the public 1.4.12 file, after normalising its CRLF line endings against this build's LF, only two members differ:

• background.js - captures and correlates the Gofile browser-session value and hands the download to the local Cove relay. Ordinary downloads retain the pause-then-cancel flow from public 1.4.12; Gofile downloads deliberately keep running until a proven takeover. Refuses to send Gofile targets as generic downloads.
• manifest.json - version 1.4.13; data_collection_permissions.required gains authenticationInfo. The permissions array, background scripts, content scripts, strict_min_version (140.0) and Gecko ID are unchanged.

Gofile capture and handoff

The webRequest.onBeforeSendHeaders listener is non-blocking and read-only. Its URL filter is https://*.gofile.io/download/web/* and its request type is main_frame only. It reads only accountToken from that request's Cookie header, retaining that value in extension memory for at most 10 seconds, with at most 8 entries and single use. Correlation uses the download URL, container and tab. No cookies API or cookies permission is used, and the listener does not change the request.

The native message contains only {action: "download_gofile_relay", url, gofileToken, filename}. Ordinary downloads are paused while Cove is asked and resumed if Cove cannot take them, as in public 1.4.12. Gofile downloads are deliberately not paused: verified on Firefox 159, a Gofile download paused at handoff start cannot be resumed. Firefox keeps downloading while the captured session is checked and Cove is asked. Firefox cancels its Gofile copy only after Cove responds with {status: "ok", proof: "gofile-relay-v1"}, within a 30-second deadline and with correlation still current. Missing, expired or ambiguous capture, refusal, an old or unavailable Cove app, failed verification and timeouts leave Firefox untouched. A very small or fast Gofile download can finish in Firefox before Cove takes over, leaving both copies. Both copies can also remain if Cove accepted but the extension could not confirm it in time, such as a reply after the deadline or two tabs asking for the same file.

Cove holds the session value in memory and fetches the file through a relay on 127.0.0.1 with a random capability URL. The value is sent only to Gofile, never to aria2, disk or logs. After a Cove restart, the download cannot resume; start a fresh download from Firefox.

Reviewers can search background.js for onBeforeSendHeaders and download_gofile_relay to inspect capture, correlation and handoff. The node --test suites under tests/ (extension_background.test.js, extension_gofile.test.js and extension_diagnostics.test.js) cover these paths without third-party dependencies.

Browser support: no minimum version is changed in this release.

How the add-on works

Cove Download Manager is the browser half of a desktop download manager. The extension does not download anything itself. It observes a download the user started, and hands the URL to the local desktop app over native messaging, which performs the download with multiple connections and queue management.

Permission justifications (the permission list is unchanged from public 1.4.12; the downloads and webRequest justifications are extended for the Gofile handoff; no cookies permission is requested):

• nativeMessaging - the entire purpose of the add-on. It passes download requests to the Cove desktop app via the native host cove_dm_host. Without this the add-on does nothing.

• downloads - to observe downloads the user starts and cancel the browser copy after Cove confirms takeover. Ordinary downloads are paused while Cove is asked and resumed if Cove cannot take them. Gofile downloads are deliberately not paused because Firefox cannot resume them once paused, so Firefox keeps downloading. A very small or fast Gofile download can finish in Firefox before Cove takes over, leaving both copies. Both copies can also remain after a late acceptance reply or two tabs asking for the same file.

• webRequest and <all_urls> - downloads and media can originate from any site, so the add-on cannot enumerate hosts ahead of time. Used to read the Cookie header of the specific Gofile storage-server download request, retaining only accountToken for the local handoff, and to detect media on the page for the in-page pill. No cookies API is used and no permissions are added.

• contextMenus - the "Download with Cove" right-click entry on links, images, video, and audio.

• notifications - to tell the user when a handoff to Cove succeeded or failed.

• storage - to persist the user's own settings: the interception toggle, minimum file size, file type filters, and excluded domains.

Privacy

No analytics, no telemetry, no remote endpoints. The add-on communicates only with the Cove desktop app on the same machine, over native messaging. Nothing is sent to the developer, and nothing is sold or shared for advertising.

Data collection declaration

Data sent to native applications using NativeMessaging must be declared in the data collection consent model. authenticationInfo is NEW in 1.4.13 for the Gofile session handoff; the other categories were already declared in public 1.4.12. Existing users will be asked to consent to this new category on update. The current declaration is:

  "data_collection_permissions": {
    "required": ["browsingActivity", "websiteContent", "websiteActivity", "authenticationInfo"],
    "optional": ["technicalAndInteraction"]
  }

browsingActivity, because the handoff carries the address being downloaded and the address of the page it came from. websiteContent, because the handoff carries the media address and the specific Gofile download's accountToken cookie. websiteActivity, because the action being carried out is a download. authenticationInfo, because accountToken authenticates the Gofile session. All four are required for these handoffs.

technicalAndInteraction is optional, and it covers exactly one field: the browser user-agent, which Cove passes so the download can use browser-compatible request headers. Mozilla requires this category to be optional and states that it cannot be required, so the extension reads the user's current grant through permissions.getAll() before every handoff and simply omits the user-agent when it is not granted. The download itself still proceeds. Revoking the permission in about:addons takes effect on the next handoff, with no restart. The extension never calls permissions.request() during a download; the install and about:addons consent flows are the only way it is granted.

For Gofile, the extension reads accountToken from the specific browser download request to a Gofile storage server (store*.gofile.io) and forwards only that value to the Cove app running on this computer for that download. Cove keeps it in memory for the transfer; it is not written to disk or logs, is not sent to aria2 or any host other than Gofile, and after a Cove restart a fresh download from Firefox is needed. No Gofile API key or Premium account is needed. authenticationInfo is required; strict_min_version is unchanged at 140.0.

What this declaration does not mean: none of this data goes to the add-on's developer, there is no analytics or tracking, nothing is sold or shared for advertising, and no cookie is stored on any remote server. The recipient is the Cove Download Manager application on the user's own computer, which then contacts the site the user asked to download from. The add-on does not read, collect or transmit your browsing history: what it passes is the address of the file you chose to download and the page you were on when you chose it, at the moment you chose it.

The extension keeps a small local diagnostics ring in browser storage so a user can report a failed handoff while the desktop app is closed. It records event names and outcomes only - no page URLs, no media URLs, no tab titles, and no cookie values. This is asserted by tests/extension_diagnostics.test.js in the repository.

Existing exclude-site behaviour: while an exclusion is awaiting confirmation, the add-on holds one record in storage.session - a random token, the hostname being confirmed, the time it was created, and the id of the tab it came from. storage.session is held in memory and is never written to disk, and it is not exposed to content scripts. The record stops being valid two minutes after it is created, and it is removed when the user confirms or cancels, and otherwise the next time the add-on reads or sweeps the pending set after that point. It is not removed by a timer at the two-minute mark. A hostname the user does confirm is then stored under Excluded Domains in storage.local, which is the user's own settings and persists until they remove it.

Testing the add-on

The desktop app is required to exercise the handoff, and is free and open source. Linux, Windows, and macOS builds are at:
https://github.com/Sin213/cove-download-manager/releases

Install Cove, launch it once, then click "Test Connection to Cove" in the extension popup. Without the desktop app the extension installs and its UI works, but every download handoff will correctly report that Cove is unavailable.

Exercising the Gofile handoff requires a Cove desktop build containing the Gofile relay, newer than 3.7.1. Run from the public repository main branch with python -m cove, which registers its native host at start. Cove 3.7.1 lacks the relay. Without a desktop build containing it, the extension refuses the Gofile handoff, shows "Update Cove ...", and Firefox keeps downloading.
```

## Facts behind the copy

| Claim | Source |
| --- | --- |
| 16 connections per file | `cove/config.py` `connections_per_server` |
| link, image, video, audio contexts | `extension/background.js` link/image contexts plus `extension/media-core.js` `contexts: ["video", "audio"]` |
| in-page pill ships on Firefox | `content/media-tab.js` and `.css` present in the bundle and registered in `content_scripts`, asserted by `tests/test_extension_bundle.py` |
| extractor and HLS ship on Firefox | `media-sites.js` present in the bundle, `manifest.background.scripts` loads it |
| Alt+Shift+D toggle | `extension/manifest.json` `commands.toggle-intercept` |
| "No video found" instead of the page address | `content/media-tab.js` `onPillClick`, guarded by `tests/extension_media_tab.test.js` |
| the pill can hide again after that | `downloadPending` set only once an address resolves, guarded by the same file |
| stream lookup is ancestor-scoped | `content/media-tab.js` `embeddedStreamUrl` uses `closest()` only |
| intercepted ids pruned against the browser | `extension/background.js` `pruneInterceptedIds`, `tests/extension_background.test.js` |
| badge OFF outranks a media count | `extension/media-sites.js` calls `renderBadge` rather than painting directly |
| diagnostics record no URLs or cookies | `tests/extension_diagnostics.test.js` |
| bundle is a copy of `extension/` with one composed HTML file | `scripts/build_extension.py` `_copy_shared` and `_compose_popup`; 24 of 25 files sha256-identical, `popup/popup.html` differing only by the two marker substitutions, asserted by `tests/test_extension_bundle.py` |
| the page can only request, never persist, an exclusion | `extension/background.js` `fromExtensionPage` gates `getExcludeConfirmation`, `confirmExcludeSite` and `cancelExcludeConfirmation`; `tests/extension_confirm_exclude.test.js` |
| the excluded host is the top-level page's | `extension/background.js` `excludableHost` reads `sender.tab.url`; `tests/extension_confirm_exclude.test.js` |
| no `cookies` permission or cookies API | `extension/manifest.json` retains existing API and host permissions; Gofile capture uses the existing `webRequest` permission. Required `authenticationInfo` describes the relay payload (`action`, `url`, `gofileToken`, `filename`); `strict_min_version`, the Gecko ID and `gecko_android` are unchanged. |
| the declared categories match ordinary handoffs | `extension/background.js` native payload (`url`, `referrer`, `filename`, `userAgent`, `fileSize`); asserted by `test_firefox_declares_the_data_it_sends_to_the_native_app` |
| downloads Firefox reports as private are left to the browser | `extension/background.js` `handleCreated`, `pillIdentityAllowed`, context-menu handler; `extension/media-sites.js` `downloadStream`; `tests/extension_background.test.js`; manual proof pending |

## Before submitting

- Ship or identify the Cove desktop release containing the Gofile relay before
  or together with extension 1.4.13. Cove 3.7.1 lacks it; Gofile requires a
  Cove desktop version that adds Gofile browser-session support (newer than 3.7.1).
- The manifest now declares required authenticationInfo, and existing users
  will be asked to consent on update. Match the AMO dashboard's data-collection
  and privacy disclosure and privacy-policy link to PRIVACY.md, last updated
  2026-11-02 or later.
- Paste the Description, Release Notes and Notes to Reviewer blocks above
  into the corresponding AMO fields for 1.4.13.
- The published 1.4.12 file uses CRLF line endings; this build uses LF.
  Ignore line endings when comparing the reviewer diff.
- Screenshots are unchanged.
