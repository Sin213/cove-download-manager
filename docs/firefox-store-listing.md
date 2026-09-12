# AMO listing - Cove Download Manager (Firefox) 1.4.10

Lives in `docs/` rather than `dist/`, which `scripts/build_extension.py`
deletes on every build.

Upload `dist/cove-firefox-1.4.10.zip` at
<https://addons.mozilla.org/developers/>.

That ZIP is the **submission input**, not a distributable add-on. AMO signs an
accepted upload and returns the signed file; nothing in this repository
produces a signed XPI. Source: the submission flow at
<https://extensionworkshop.com/documentation/publish/submitting-an-add-on/>,
read 2026-09-12, which takes a `.zip`, `.xpi`, or `.crx` and signs it.

1.4.10 is the next patch above 1.4.9, which the public AMO API reported as the
add-on's `current_version` with status `public` on 2026-09-12 (see
[Before submitting](#before-submitting)).

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

• In-page video pill: a small button appears on pages with video, and one click sends that video to Cove. The pill is not shown on a site listed under Excluded Domains, and the list takes effect on pages that are already open

• Use the menu beside the pill to exclude the current site. Cove opens its own confirmation page, which names the site and asks before the exclusion is saved

• Right-click any link, image, video, or audio and choose "Download with Cove"

• Supported video pages are handed to Cove for extraction, including HLS (.m3u8) streams

• Sends cookies, referrer, and user-agent information so authenticated downloads work

• Toggle interception on or off with Alt+Shift+D, and set excluded domains and a minimum file size, which applies whenever Firefox gives a usable size at the start of a download

How interception works:

When you start a download, Cove can take it over instead of leaving it to the browser: installers, archives, documents, and other direct file links.

You decide what it touches. Interception can be switched off with a keyboard shortcut, restricted to specific file types, disabled entirely on domains you list, and limited by a minimum file size.

About that minimum: it can only be applied when Firefox gives a usable size at the moment a download starts. In the Firefox version and test files used for this release, Firefox reported the size as unknown at that moment for every download, including one whose server did declare a length, so the minimum did not filter any of them. Expect a download smaller than your minimum to be taken over anyway. Whether a size is available is Firefox's decision rather than the server's, and it may differ on other versions.

Nothing is downloaded without an action you took, and the extension never collects or transmits your browsing history.

What's new in version 1.4.10:

You can now exclude a site from the in-page button without opening the options page. Use the menu beside "Download with Cove" and choose "Exclude <this site>". Cove opens its own confirmation page, names the site, and changes no setting until you choose "Exclude site" there. Cancelling leaves your settings unchanged. A confirmed exclusion hides the button on pages that are already open, without a reload, and you can undo it by removing the entry under Excluded Domains.

The free Cove Download Manager desktop app is required because it provides the download engine. Install Cove, launch it once, and then click "Test Connection to Cove" in the extension to link them.

Cove is open source:
https://github.com/Sin213/cove-download-manager
```

## Version -> Release Notes

Shown on the add-on's detail page under this version. Keep it to what changed
in 1.4.10 only.

```text
New: exclude a site straight from the in-page button.

Use the menu beside "Download with Cove" and choose "Exclude <this site>". Cove opens its own confirmation page before the exclusion is saved. That page names the site it is about to exclude, and the exclusion is only written when you choose "Exclude site" there. Choosing "Cancel", or simply closing the page, leaves your settings exactly as they were.

A confirmed exclusion takes effect on pages that are already open, without a reload: the in-page button disappears from that site. To undo it, remove the entry under Excluded Domains in the add-on's options.

The site that gets excluded is the one in the address bar. A video player embedded from another site, and the server the video itself comes from, cannot put their own name on the exclusion.

Saving the options page keeps an exclusion that was confirmed from another tab while the options page was open, and still saves the unrelated change you made there.

No new Firefox API or host permissions are requested.

The Firefox data-collection declaration is updated to describe the information Cove transmits to the locally installed Cove application. Earlier versions declared none, which was not accurate for an add-on that hands downloads to a local application, so Firefox may ask you to confirm this on update.

Browser and device technical information is optional. The browser user-agent is only included in a handoff when Firefox reports that you currently grant the technical-data permission, and you can turn it off at any time in about:addons. When it is not granted, Cove does not transmit it, and your downloads keep working either way.

Nothing is sent to the developer, there is no analytics or tracking, and nothing is sold or shared.
```

1.4.9, already published, is where the in-page button began respecting
Excluded Domains. That change is not re-announced here.

## Version -> Notes to Reviewer

```text
Source code and build process

This add-on is not minified, obfuscated, transpiled, bundled, or generated by any build tool. Every .js, .css, and .html file in the XPI is hand-written source, readable as-is.

The packaging step is a file copy, one deterministic edit to a single HTML file, and a zip. scripts/build_extension.py in the repository copies extension/ to dist/firefox/ and zips it, using only the Python standard library (json, shutil, pathlib, zipfile). No compiler, minifier, transpiler, or package manager is involved, and no dependencies are downloaded or vendored.

The one edit: popup/popup.html carries two HTML comment markers, "<!-- cove:popup-styles -->" and "<!-- cove:popup-modules -->", each on its own line. The Firefox build replaces them with a stylesheet link for popup/streams.css and a script tag for popup/streams.js, which are the Firefox-only detected-stream section of the popup. The Chrome build removes both marker lines instead, because that bundle does not ship stream detection. Nothing else in any file is rewritten, and no JavaScript is transformed. The build fails loudly if either marker is missing or appears more than once.

The uploaded ZIP holds 23 files. Twenty-two of them are byte-for-byte identical to their counterpart in the extension/ directory of the public repository. The twenty-third, popup/popup.html, differs only by that marker substitution. You can verify this by diffing the ZIP contents against that directory: popup/popup.html should be the only file that differs, and the difference should be exactly the two lines described above.

Two of the 23 are new in this version: confirm-exclude.html and confirm-exclude.js, the confirmation page described under "What changed in 1.4.10" below. Both are hand-written source and both are byte-identical to the repository.

To reproduce the uploaded file:

  git clone https://github.com/Sin213/cove-download-manager
  cd cove-download-manager
  python scripts/build_extension.py

This writes dist/cove-firefox-1.4.10.zip. Requires Python 3.9 or later, no other tooling. Three things differ from the Chrome bundle produced by the same script: manifest.json (MV2 vs MV3); the browser-specific modules, which are media-sites.js, content/media-sites.js, popup/streams.js and popup/streams.css in the Firefox bundle only, and media-chrome.js in the Chrome bundle only; and the popup composition described above. That is why the Firefox bundle holds 23 files and the Chrome bundle holds 20. Chrome is not without video handling: it ships the same shared in-page button and the same video and audio context-menu entries for a media element whose own address is an ordinary HTTP(S) file. What the Chrome build excludes is the site-specific part, meaning page extractors, HLS stream detection, and the detected-stream popup section.

Public source: https://github.com/Sin213/cove-download-manager

What changed in 1.4.10

No new API permissions, no new hosts, and no new content scripts. The "permissions" array in manifest.json is byte-identical to 1.4.9. One thing changes in the manifest besides the version: the data-collection declaration, which is corrected as described under "Data collection declaration" below, and which Firefox presents to users as a consent change. Two new files are added to the package, and four existing files change, background.js among them - it now withholds the user-agent unless the optional technical-data permission is currently granted.

New feature: exclude the current site from the in-page button.

New files:

• confirm-exclude.html - the confirmation page. Static markup, one inline stylesheet, no inline script. It loads confirm-exclude.js with a defer attribute.
• confirm-exclude.js - reads a token from location.hash, asks the background script which hostname that token is for, renders it with textContent (never innerHTML), and sends either confirmExcludeSite or cancelExcludeConfirmation when a button is pressed.

Changed files:

• extension/content/media-tab.js - the pill gains a "▾" toggle (aria-label "Cove pill options") opening a one-item menu, "Exclude <hostname>". Choosing it sends a requestExcludeConfirmation message and shows "Opening confirmation…". The content script never writes settings and cannot: see the authority note below.
• extension/background.js - requestExcludeConfirmation resolves the hostname itself from sender.tab.url, refuses unless the caller's expectHost matches it, mints a crypto.randomUUID token, stores a pending record in storage.session, and opens confirm-exclude.html#<token> with tabs.create. confirmExcludeSite consumes the token and writes the exclusion. cancelExcludeConfirmation consumes it and writes nothing.
• extension/options/options.js - the options page re-reads stored settings before saving, so an exclusion confirmed in another tab while the options page sat open is not overwritten by that page's stale copy, and the user's own unrelated edit on it is still saved.

Why a separate page, and what to check:

The security-relevant point is that a website cannot cause an exclusion to be saved, even by tricking the user into a genuine click. A page can only ever reach requestExcludeConfirmation, which opens a page at the add-on's own moz-extension:// origin. The three privileged messages - getExcludeConfirmation, confirmExcludeSite, cancelExcludeConfirmation - are each gated on the sender being that page, using the same fromExtensionPage(sender, ...) check the add-on already used for its options and popup pages. A content script's sender.url is the address of the page it was injected into and can never equal an extension URL, so the old direct route now always refuses.

The token is single-use and valid for two minutes. It is deleted before the privileged write, so a failed write cannot leave it replayable, and all reads and writes of the pending set are serialised so two overlapping confirmations cannot both commit. The pending record holds only the token, the hostname, the creation time, and the source tab id, in storage.session, which is in memory and not exposed to content scripts.

The hostname comes from sender.tab.url, which is the top-level page's address as the browser knows it. A cross-origin video player in an iframe, and the CDN the video is served from, therefore cannot name themselves in the exclusion; the site in the address bar is what gets excluded.

This is covered by tests/extension_confirm_exclude.test.js (new), tests/extension_background.test.js and tests/extension_media_tab.test.js, which run under node --test with no third-party dependencies.

The scope of the feature is the in-page button only. On an excluded site the add-on's content scripts still run, as they did before, the right-click entries still work, and the excluded list is not a substitute for removing a host permission.

Browser support: storage.session has been available since Firefox 115 and this add-on's strict_min_version is 140.0, so the confirmation path is inside the supported range. No minimum version is changed in this release.

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

No analytics, no telemetry, no remote endpoints. The add-on communicates only with the Cove desktop app on the same machine, over native messaging. Nothing is sent to the developer, and nothing is sold or shared for advertising.

Data collection declaration

1.4.10 corrects this. Earlier versions declared data_collection_permissions required: ["none"], which was wrong for an add-on whose purpose is handing a download to a native application. Mozilla's guidance states that data sent to native applications using NativeMessaging must be declared in the data collection consent model, so 1.4.10 declares:

  "data_collection_permissions": {
    "required": ["browsingActivity", "websiteContent", "websiteActivity"],
    "optional": ["technicalAndInteraction"]
  }

browsingActivity, because the handoff carries the address being downloaded and the address of the page it came from, and Mozilla defines that category as information about the websites users visit, such as specific URLs and domains. websiteContent, because the handoff also carries the media address and the cookies read for that download address - Mozilla's definition of that category explicitly includes cookies, page headers, and request and response information. websiteActivity, because the action being carried out is a download. These three are required because the download cannot be performed without them.

technicalAndInteraction is optional, and it covers exactly one field: the browser user-agent, which Cove passes so the download can use browser-compatible request headers. Mozilla requires this category to be optional and states that it cannot be required, so the extension reads the user's current grant through permissions.getAll() before every handoff and simply omits the user-agent when it is not granted. The download itself still proceeds. Revoking the permission in about:addons takes effect on the next handoff, with no restart. The extension never calls permissions.request() during a download; the install and about:addons consent flows are the only way it is granted.

authenticationInfo is not declared: Mozilla scopes it to credentials and account data such as passwords, usernames, PINs and registration information, and the cookie case is named inside the websiteContent definition instead. strict_min_version is unchanged at 140.0.

What this declaration does not mean: none of this data goes to the add-on's developer, there is no analytics or tracking, nothing is sold or shared for advertising, and no cookie is stored on any remote server. The recipient is the Cove Download Manager application on the user's own computer, which then contacts the site the user asked to download from. The add-on does not read, collect or transmit your browsing history: what it passes is the address of the file you chose to download and the page you were on when you chose it, at the moment you chose it.

The extension keeps a small local diagnostics ring in browser storage so a user can report a failed handoff while the desktop app is closed. It records event names and outcomes only - no page URLs, no media URLs, no tab titles, and no cookie values. This is asserted by tests/extension_diagnostics.test.js in the repository.

New in this version: while an exclusion is awaiting confirmation, the add-on holds one record in storage.session - a random token, the hostname being confirmed, the time it was created, and the id of the tab it came from. storage.session is held in memory and is never written to disk, and it is not exposed to content scripts. The record stops being valid two minutes after it is created, and it is removed when the user confirms or cancels, and otherwise the next time the add-on reads or sweeps the pending set after that point. It is not removed by a timer at the two-minute mark. A hostname the user does confirm is then stored under Excluded Domains in storage.local, which is the user's own settings and persists until they remove it.

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
| bundle is a copy of `extension/` with one composed HTML file | `scripts/build_extension.py` `_copy_shared` and `_compose_popup`; 22 of 23 files sha256-identical, `popup/popup.html` differing only by the two marker substitutions, asserted by `tests/test_extension_bundle.py` |
| the page can only request, never persist, an exclusion | `extension/background.js` `fromExtensionPage` gates `getExcludeConfirmation`, `confirmExcludeSite` and `cancelExcludeConfirmation`; `tests/extension_confirm_exclude.test.js` |
| the excluded host is the top-level page's | `extension/background.js` `excludableHost` reads `sender.tab.url`; `tests/extension_confirm_exclude.test.js` |
| no permission changes | `git diff -- extension/manifest.json` touches the version line and the `data_collection_permissions.required` array, and nothing else. The `permissions` array, `strict_min_version`, the gecko id and `gecko_android` are untouched |
| the declared categories match what is actually sent | `extension/background.js` `collectCookies` and the native payload (`url`, `referrer`, `filename`, `cookies`, `userAgent`, `fileSize`); asserted by `test_firefox_declares_the_data_it_sends_to_the_native_app` |

## Before submitting

- The feature bullets in the description carry over, with the pill bullet
  extended and one bullet added. The "What's new" paragraph, the release notes,
  and the reviewer notes are new copy for 1.4.10.
- **Screenshots: one decision outstanding.** Existing pill screenshots remain
  accurate, because the pill still looks the same until its menu is opened.
  Nothing currently on the listing shows the "▾" menu or the confirmation
  page, so nothing on the listing is *wrong*; whether to add a screenshot of
  the confirmation page is a judgement call, not a correction. Do not relabel
  an existing pill screenshot as the confirmation flow.
- **Corrected in 1.4.10: the data-collection declaration.** Up to and
  including the published 1.4.9, `manifest.json` declared
  `browser_specific_settings.gecko.data_collection_permissions.required:
  ["none"]`. That was inaccurate. Mozilla's guidance is explicit that "Data
  sent to native applications using NativeMessaging must be declared in the
  data collection consent and categorized in the appropriate consent model"
  (<https://extensionworkshop.com/documentation/develop/best-practices-for-collecting-user-data-consents/>,
  read 2026-09-12), and handing a download to the local Cove application is
  this add-on's entire purpose. 1.4.10 declares:

      "required": ["browsingActivity", "websiteContent", "websiteActivity"],
      "optional": ["technicalAndInteraction"]

  Against the category definitions at
  <https://extensionworkshop.com/documentation/develop/firefox-builtin-data-consent/>
  (same date): `browsingActivity` is "Information about the websites users
  visit, such as specific URLs, domains, or categories of pages users view",
  which is the download address and the referring page - the Chrome listing
  already discloses those same two as browsing activity, so omitting the
  category here would have contradicted our own disclosure.
  `websiteContent` "covers anything visible on a website - such as text,
  images, videos, and links - and anything embedded, such as cookies, audio,
  page headers, and request and response information", which is the media
  address and the cookies read for the download address. `websiteActivity`
  covers user interactions "such as saving and downloading", which is the
  handoff itself.

  `authenticationInfo` is deliberately **not** declared. Mozilla scopes that
  category to credentials and account data - passwords, usernames, PINs,
  security questions, registration information - and the cookie case is
  already named inside the `websiteContent` definition. `none` was removed
  rather than appended, because it is the standalone "collects nothing" value
  and cannot be combined with a category.

  `technicalAndInteraction` **is** declared, as the single optional category.
  Mozilla's examples for it are "Device and browser info, extension usage and
  settings data, crash and error reports", and the handoff carries the browser
  user-agent, which is browser info. This category cannot be handled like the
  other three: the same page states it "must be optional", and that personal
  data permissions "can be required or optional, except for
  `technicalAndInteraction` that cannot be required". Declaring it alone would
  therefore not have been enough, because an optional permission that is not
  granted must not be acted on. `extension/background.js` reads the user's
  current grant through `permissions.getAll()` before every user-agent-bearing
  handoff and omits the field when it is absent; the download still proceeds.
  Guarded by the U1-U6 tests in `tests/extension_background.test.js`.

  `strict_min_version` is
  unchanged at 140.0. A privacy policy is not itself mandatory under this
  model - Mozilla notes that "while a privacy policy is not required, it can
  help users better understand what data your extension uses" - so the
  binding item was the manifest declaration, and it is now correct.

  This is a **user-visible consent change**: existing users will be asked to
  consent to the newly declared categories on update. That is the intended
  consequence of declaring accurately, not a regression.
- **Outstanding: the AMO inline privacy text.** The dashboard's inline
  statement still says the extension "does not collect, store, or transmit
  any personal data", which contradicts the corrected declaration. Replacement
  copy is prepared in the release packet as `amo-inline-privacy-text.txt`. The
  privacy-policy field should also be pointed at the updated `PRIVACY.md`.
  Editing files in this repository does not change what AMO shows.
- `nativeMessaging`, `cookies`, `webRequest`, and `<all_urls>` are still
  requested and still need their justifications - they are reproduced in the
  reviewer notes above.
- **Done: the browser load check.** The 1.4.10 candidate was loaded as a
  temporary add-on in **Firefox 155.0.1** on an isolated profile, and the
  1.3.11 candidate in **Chrome 151.0.7922.173**, and the following were
  confirmed by execution rather than by hand-waving:
  1. the in-page pill appears on a page with a playing video, labelled
     "Download with Cove";
  2. its "▾" menu (aria-label "Cove pill options") offers
     "Exclude &lt;hostname&gt;";
  3. choosing it opens an extension-origin page titled "Exclude site from
     Cove" whose heading reads "Exclude &lt;hostname&gt; from Cove?";
  4. "Cancel" reports "Cancelled. No settings were changed." and Excluded
     Domains is unchanged; "Exclude site" reports "Site excluded from Cove.",
     adds the hostname, and the pill disappears from the already-open page
     without a reload;
  5. no pill, and no handoff, on a site under Excluded Domains;
  6. removing the entry restores the pill;
  7. a request made from a cross-origin embedded player names the top-level
     site, not the player's host or the media host.

  This also clears the load check that had been outstanding since `0be8c3e`
  changed the script load order: that layout has now been executed in a
  browser.

- **Still not done: the handoff against the real desktop app.** The primary
  download was exercised against a recording stub native host, which confirmed
  the handoff fires once with the correct address, referrer, and payload
  fields, and that an excluded page produces none. It does **not** confirm that
  Cove accepts the handoff, because the registered host launches the operator's
  real Cove and a second instance would share its data directory with a live
  one. Exercise this by hand once, or with an isolated data directory, before
  relying on it. See the note in `project-firefox-release-check`.
- 1.4.7 carried extension fixes from `c445cb1` that were written before the
  1.4.6 upload but never given a version bump, so the published 1.4.6 and the
  repository's 1.4.6 source were not the same code. That was corrected there.
- 1.4.7 was the version public on AMO when the 1.4.9 document was drafted
  (verified 2026-09-07 against the AMO API for add-on
  `cove-dm@cove-download-manager.net`) and 1.4.8 was subsequently uploaded from
  this repository.
- Verified again on **2026-09-12** against the same public AMO API: the add-on's
  status is `public`, its `current_version` is **1.4.9**, `last_updated` is
  **2026-09-11T05:31:17Z**, and the public version list holds 12 versions with
  1.4.9 the newest. 1.4.9 is therefore published, not merely uploaded, so
  **1.4.10** is the next patch. 1.4.9 is an immutable submission: it is not
  rebuilt or relabelled here. That check reads public versions only; it cannot
  see an upload awaiting review or an unlisted one, so confirm on the developer
  dashboard that 1.4.10 is unused before uploading.
- Consequence for issue #16: the A1 half of that issue, the in-page button
  honouring Excluded Domains, is already published as 1.4.9. What 1.4.10 adds
  is the A2 half, the exclude-site action and its confirmation page.
