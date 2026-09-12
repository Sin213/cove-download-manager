# Chrome Web Store listing - Cove Download Manager (draft)

Lives in `docs/` rather than `dist/`, which `scripts/build_extension.py`
deletes on every build.

**This is a release candidate, not a submission.** It describes the Chrome
bundle as it is built from the current source, at the candidate version
**1.3.11** (`dist/cove-chrome-1.3.11.zip`). That version is the next patch
above 1.3.10, which the public Store listing showed as the **published**
version on 2026-09-12 (see
[External evidence](#5-historical-rejection-context-and-unresolved-external-evidence)).
A published version number cannot be reused, so 1.3.11 is the lowest version
this bundle may be uploaded under. That still does not rule out a newer upload
sitting in review, which the public page never shows; confirm on the developer
dashboard before uploading.

Nothing here means Google has approved anything.

---

## 1. Current Chrome feature description

Plain text for the **Store listing -> Description** field. The store renders it
literally, so the bullets are real `•` characters and there is no other
formatting to strip.

```text
Cove Download Manager (browser extension) hands your downloads off to the Cove Download Manager desktop app, which downloads them with multiple connections and keeps them in a real queue.

Features:

• Cove takes over downloads the browser starts: installers, archives, documents, and other direct file links

• Right-click a link or an image and choose "Download with Cove"

• Right-click a video or audio player and choose "Download with Cove" when the player's own address is an ordinary file

• A Cove button appears over a playing video, so a video whose address is an ordinary file can be sent without using the menu

• That button is not shown on a site you have listed under Excluded Domains, and the list takes effect on pages that are already open

• Use the menu beside "Download with Cove" to exclude the current site. Cove opens its own confirmation page, which names the site and asks before the exclusion is saved

• Multi-connection downloads with up to 16 connections per file

• A real download queue with start, pause, and per-item controls, a daily schedule window, and a global speed cap

• Session cookies, the referring page, and your browser's user-agent are passed along, so a file behind a session you are already signed in to still downloads

• Interception can be toggled with a keyboard shortcut, restricted to chosen file types, switched off for domains you list, and held to a minimum file size whenever the browser gives a usable size at the start of a download. When the browser reports the size as unknown, the minimum cannot be applied and a smaller file may still be taken over

What this extension does not do:

It does not extract video from streaming sites, does not assemble video from playlists or fragments, and does not bypass any site's access restrictions. The Cove button can appear on a playing video whose address turns out not to be a plain file, and pressing it then reports that no video was found rather than downloading anything.

Support depends on how a site serves its media, so it will not work everywhere.

The free Cove Download Manager desktop app is required, because it is the download engine. Install Cove, launch it once, then use "Test Connection to Cove" in the extension to link them.

Cove is open source:
https://github.com/Sin213/cove-download-manager
```

---

## 2. Scope and limitations

What the Chrome bundle can do:

- Intercept a download the browser starts and hand it to Cove.
- Hand over a link or image target from the context menu.
- Hand over a `video` or `audio` element whose own address is an ordinary
  HTTP(S) file, from the context menu.
- Hand over a `video` element the same way from the in-page button. The button
  is video only: the content script looks for `<video>` elements and never
  scans for `<audio>`, so audio has the context menu and nothing else.
- Withhold the in-page button on a site the user listed under Excluded
  Domains, and follow a change to that list on an already-open page.
- Offer "Exclude <host>" in a menu beside the button, which *requests* an
  exclusion. The page never writes the setting. The request opens a Cove page
  at the extension's own origin (`confirm-exclude.html`), and only a separate
  action on that page persists the exclusion.

What the exclusion action does not do:

- It does not stop the extension running on the excluded site. Content scripts
  still load, the context-menu entries still work, and downloads the browser
  starts are still filtered by the same list they always were. What the
  exclusion removes is the in-page button.
- It does not infer a parent domain. The saved entry is the top-level page's
  own hostname, resolved by the background script from the tab's address, not
  from anything the page or an embedded player claims. The existing matcher
  then covers subdomains of an entry the user already had.

What it cannot do, by construction:

- It ships no page extractor. There is no site-specific handling of any kind in
  the Chrome bundle (`_CHROME_EXCLUDE` in `scripts/build_extension.py`).
- It ships no stream detection. `media-sites.js` and `content/media-sites.js`
  are Firefox-only, and Chrome does not request the `webRequest` permission.
- It ships no detected-stream popup section and no stream download button
  (`popup/streams.js` and `popup/streams.css` are Firefox-only).
- A media target whose path ends in `.m3u8`, `.m3u`, or `.mpd` is refused, and
  no link, page address, or fallback stands in for it.

  Both routes a media address can take now enforce it. The context menu
  consults the refusal in `background.js`, and the in-page button's
  `downloadMedia` handoff consults the same refusal in `handleMediaTabDownload`
  (`media-core.js`), before the address is marked, before cookies are read, and
  before anything reaches the native host. A refused request ends there and
  reports that the address is unsupported.

  What this does and does not establish. It is one pathname check on the
  address that is about to be sent. It does not verify that an accepted address
  is a direct media file, it does not recognise a playlist served without one of
  those three suffixes, and it does not inspect where an address redirects. A
  playable MP4 served under a `.m3u8` pathname is refused by it, which is the
  chosen policy behaving as written and not evidence that anything decoded a
  playlist. Downloads the browser itself starts, and ordinary link and image
  targets, do not pass through this refusal at all.

  Firefox publishes no such refusal, so nothing about its context menu or its
  button changes.

Limits to be honest about in any copy:

- This is **not** Firefox capability parity. Firefox additionally has stream
  detection, a detected-stream popup section, and page extractors. Chrome has
  the shared direct-media experience and nothing site-specific.
- The refusal above is narrow and matches on the address path. It is not a
  global block on any host, CDN, or site.
- An MSE player using a `blob:` source has no address to hand over, so nothing
  is downloaded on Chrome. The button can still appear over such a player while
  it is playing; pressing it reports that no video was found. That is most
  large video sites.

---

## 3. Permissions and local data use

Permission justifications for the dashboard. These are unchanged from the
currently requested set; this slice changes no permission.

| Permission | Justification |
| --- | --- |
| `nativeMessaging` | The only way to reach the Cove desktop app, which is the download engine. |
| `downloads` | See a download the browser started, cancel it once Cove has accepted it, and fall back to a browser download when Cove is unavailable. |
| `cookies` | Read cookies for the download address at handoff time so a file behind an existing signed-in session still downloads. |
| `contextMenus` | The "Download with Cove" entry. |
| `notifications` | Report that a handoff succeeded or failed. |
| `storage` | Settings and a bounded local diagnostics log. |
| `host_permissions: <all_urls>` | A download can start on any site, and the in-page button has to be able to appear on any site with a video. |

Local data use, in the terms the Store's user-data policy uses:

- The extension handles user data, including authentication cookies, website
  resources (media addresses), and web browsing activity (the addresses it is
  asked to download and the page they came from).
- Some of that handling is purely local: the content script inspects video
  elements and their geometry in the page and sends nothing as a result.
  Per the Store's User Data FAQ, local-only processing still has to be
  disclosed, and it is, in `PRIVACY.md`.
- A handoff passes the download address, a filename, the referrer, cookies for
  that address, and the user-agent to the local desktop app, which then
  contacts the download's origin to fetch the file.
- Settings and a 300-entry sanitised diagnostics ring are stored in
  `storage.local`. Addresses, titles, filenames, cookies, referrers, and
  user-agent strings are dropped by field name before an entry is written.
- A pending exclusion confirmation is held in `storage.session`, which Chrome
  keeps in memory only and never writes to disk. The record is a random
  token, the hostname being confirmed, the time it was created, and the id of
  the tab it came from. It stops counting as valid two minutes after creation,
  and is removed when it is confirmed, cancelled, or next read or swept after
  that point. It is not exposed to content scripts.
- Nothing is sent to the developer. There is no developer-controlled endpoint.

`PRIVACY.md` in this repository is the policy text these summarise.

---

## 4. Claim to evidence

Every claim in section 1 that is about behaviour, and where it is enforced.

| Claim | Code | Test |
| --- | --- | --- |
| Cove takes over browser-started downloads | `extension/background.js` `handleCreated`, `interceptDownload` | `tests/extension_background.test.js` |
| Bounded by toggle, type, domain, and by size when the browser gives a usable positive size at the start | `extension/background.js` `handleCreated`, `DEFAULT_SETTINGS`. The size test is `typeof size === "number" && size > 0 && size < minSizeBytes`, so a size the browser reports as unknown is deliberately not filtered rather than treated as zero. What the browser reports is the browser's decision: a declared `Content-Length` does not guarantee a usable size at that moment | `tests/extension_background.test.js` |
| Link and image context targets | `extension/background.js` `registerContextMenu` | `tests/extension_background.test.js` |
| Video and audio context targets exist in Chrome | `extension/media-core.js` `contexts`, `registerContextMenu` | `test_chrome_context_menu_derives_media_contexts_from_the_capability` |
| The element's own source wins over an enclosing link | `extension/background.js` `contextMenus.onClicked` | `test_chrome_media_action_selects_the_element_source_over_a_link` |
| In-page button on a direct video, in Chrome | `extension/content/media-tab.js` (shared), shipped by `_CHROME_EXCLUDE` not excluding it | `test_chrome_bundle_ships_the_shared_media_pill`, `tests/extension_media_tab.test.js` |
| No extraction, no site handling in Chrome | `scripts/build_extension.py` `_CHROME_EXCLUDE`; `extension/media-chrome.js` publishes no site hooks | `test_chrome_bundle_has_no_extractor_module`, `test_chrome_capability_supplies_no_site_hooks` |
| Playlist media targets refused on Chrome, on both media routes | `extension/media-chrome.js` `MANIFEST_SUFFIXES` and `rejectMediaTarget`, consulted by `extension/background.js` for the context menu and by `handleMediaTabDownload` in `extension/media-core.js` for the in-page button | `test_chrome_capability_refuses_playlist_media_targets`, "Chrome refuses a pill handoff for the manifest ...", "a refused pill handoff leaves nothing behind it" |
| No detected-stream popup section in Chrome | `scripts/build_extension.py` `_CHROME_EXCLUDE`, `_compose_popup` | `test_chrome_bundle_has_no_popup_stream_module`, `test_chrome_popup_markup_carries_no_stream_section`, `tests/extension_popup.test.js` |
| No stream download route in Chrome | `extension/background.js` message dispatch; the send lives in `extension/media-sites.js` | "Chrome answers the legacy stream download instead of forwarding it" |
| Chrome requests no `webRequest` | `extension/manifest.chrome.json` | `test_chrome_manifest_still_requests_no_webrequest` |
| 16 connections per file | `cove/config.py:32`, `cove/config.py:135` | - |
| Cookies, referrer, user-agent are passed on | `extension/background.js` `collectCookies`; `cove/single_instance.py`; `cove/aria2.py:515` | `tests/extension_background.test.js` |
| The in-page button is withheld on an excluded site, live | `extension/content/media-tab.js` pill admission; `extension/background.js` `resolvePillPermission` | `tests/extension_media_tab.test.js`, `tests/extension_background.test.js` |
| The page can only request an exclusion, never persist one | `extension/background.js` `requestExcludeConfirmation` creates a token and opens the extension page; `getExcludeConfirmation`, `confirmExcludeSite` and `cancelExcludeConfirmation` all refuse a sender that is not `confirm-exclude.html` via `fromExtensionPage` | `tests/extension_confirm_exclude.test.js`, `tests/extension_background.test.js` |
| The confirmed host is the top-level page's, not a frame's or a CDN's | `extension/background.js` `excludableHost` reads `sender.tab.url`; `requestExcludeConfirmation` refuses unless the caller's `expectHost` equals it | `tests/extension_confirm_exclude.test.js` |
| A token is one-time, two-minute, and cannot be replayed | `extension/background.js` `EXCLUDE_CONFIRMATION_TTL_MS`, `consumeExcludeConfirmation` deletes before the write, `chainExcludeConfirmations` serialises | `tests/extension_confirm_exclude.test.js` |
| Cancel changes no setting | `extension/background.js` `cancelExcludeConfirmation` consumes the token and returns without touching `storage.local` | `tests/extension_confirm_exclude.test.js` |
| A confirmed write preserves unrelated settings edited elsewhere | `extension/background.js` `excludeConfirmedHost` re-reads inside `chainSettings` and merges | `tests/extension_background.test.js`, `tests/extension_confirm_exclude.test.js` |

Wording that was deliberately avoided, and why:

- "protected downloads" - reads as bypassing an access restriction. The Store's
  Malicious and Prohibited policy forbids facilitating unauthorized access,
  naming paywalls and login restrictions. What actually happens is that cookies
  the browser already holds are reused, so the copy says that.
- "faster" as a bare promise, and "works everywhere" - neither is something the
  extension can guarantee.
- Any claim that manifest, YouTube, or CDN addresses are globally blocked. The
  refusal is a pathname suffix check on a media target and nothing more.
- Any claim of Firefox parity.

---

## 5. Historical rejection context and unresolved external evidence

**Historical, for context only. Do not treat as current instructions.**

1.3.5 was rejected under Fostering a Safe Ecosystem - Malicious and Prohibited
Products (reference "Blue Zinc", routing ID FZSL) for facilitating downloads of
copyrighted media, naming YouTube.

1.3.6 responded by removing video handling from the Chrome build entirely: no
in-page pill, no video/audio context menus, no HLS detection, no extractor.

The 1.3.6 listing copy was the previous content of this file and has been
replaced, because it described a build the source no longer produces: the
current Chrome bundle restored the shared direct-media handling while still
shipping no site handling at all. The superseded text remains in this
repository's history. `docs/chrome-store-listing-1.3.4-archived.md` holds the
older 1.3.4 copy and is left untouched.

### External evidence: what has been read, and what has not

Read on 2026-09-07, from the public Store listing for item
`liakghhamogjcmmgnmcpephlfecmilnf`:

- The published version was **1.3.6**, last updated 10 August 2026, and the
  item is live in the Tools category.

Read again on **2026-09-12**, from the same public listing:

- The published version is now **1.3.10**, updated **11 September 2026**.
  The listing's own embedded manifest reports `"version": "1.3.10"` and a
  package size of 85.75KiB.
- So 1.3.7 through 1.3.9 did become uploads at some point and 1.3.10 reached
  publication. Under
  <https://developer.chrome.com/docs/webstore/update> each new version must
  have a strictly larger version number than the previous one and a published
  number cannot be reused, which makes **1.3.11 the lowest version this
  candidate may use**.
- The live description still reads "What's new in version 1.3.6". The 1.3.10
  upload therefore shipped without its listing copy being updated from this
  repository's draft. Updating the repository document does not update the
  Store; the description field has to be edited in the dashboard.

That is the whole of what a public page can establish.

Still unread, and not inferable from the source tree or the public page:

- The exact 1.3.5 rejection text and its appeal state.
- Whether any version **above** 1.3.10 is already uploaded and sitting in
  review, draft, or rejected. A public listing shows the published version
  only, so it cannot rule that out for 1.3.11.
- The live screenshots and promotional images.
- The permission justifications currently recorded in the dashboard.
- The privacy-practices declarations currently recorded in the dashboard.
  In particular, whether the local-only page reading and the cookie read for
  the download address are declared. No permission changed in this candidate,
  which is not the same as the declarations being correct; "no new
  permissions" is not evidence that the existing disclosures were ever
  reviewed.

Firefox versioning is a separate question against AMO's own record. Checked on
**2026-09-12** through the public AMO API for
`cove-dm@cove-download-manager.net`: the add-on is `public`, its
`current_version` is **1.4.9**, and `last_updated` is
**2026-09-11T05:31:17Z**. The public version list holds 12 versions and 1.4.9
is the newest. 1.4.9 is therefore published and cannot be reused, which makes
**1.4.10** the Firefox candidate. The same limit applies in the other
direction: an upload awaiting review is not public, so an unlisted or pending
version above 1.4.9 is still not ruled out.

Consequence for issue #16, stated plainly: the A1 half of the issue - the
in-page button honouring Excluded Domains - is already **published** in both
stores, as Chrome 1.3.10 and Firefox 1.4.9 on 11 September 2026. What 1.3.11
and 1.4.10 add is the A2 half, the exclude-site action and its confirmation.

---

## 6. Submission checklist

- [x] Read the public Store record: published version 1.3.6, last updated
      10 August 2026. The dashboard-only facts below are still outstanding.
- [x] Tab 3: full validation of freshly built Chrome and Firefox artifacts.
- [x] Tab 4: version decided (1.3.9, provisional), bumped, and rebuilt. A Store
      upgrade is a new zip containing every file, changed or not, so the
      artifact that is validated must be the artifact that is uploaded. 1.3.9
      was subsequently uploaded to the dashboard.
- [x] Follow-up release: 1.3.10 bumped and rebuilt for the excluded-domains fix
      to the in-page button. 1.3.9 is an immutable submission and is not
      rebuilt or relabelled. 1.3.10 was subsequently published, 11 September
      2026.
- [x] This release: 1.3.11 bumped and rebuilt for the exclude-site action and
      its confirmation page. 1.3.10 is published and is not rebuilt or
      relabelled. Verified against the public listing on 2026-09-12 that
      1.3.11 is above the published version.
- [ ] Read the dashboard: rejection text and appeal state, uploaded-version
      history including drafts and rejections, permission justifications,
      privacy-practices declarations. Confirm 1.3.11 is unused before
      uploading; the public page only rules out 1.3.10 and below.
- [ ] Update the live description. It still reads "What's new in version
      1.3.6" and does not describe the in-page button, the excluded-domains
      behaviour, or the exclude-site action. Section 1 is the replacement text.
- [ ] Replace any screenshot that shows a feature this build does not have.
      Screenshots showing stream detection or extraction contradict the copy.
- [ ] Point the dashboard's privacy policy field at the current `PRIVACY.md`.
      The repository copy being updated does not update a Store URL.
- [ ] Declare the Privacy practices data categories. The Chrome bundle reads
      page content locally to place the in-page button, and reads cookies for
      the download address, and local-only processing still requires disclosure.
- [ ] Re-check the description against section 4 after any behaviour change.

## 7. Release notes for 1.3.11

The Store has no per-version release-notes field; this is the text to use if the
description's "What's new" area or a changelog entry is updated.

```text
Use the menu beside "Download with Cove" to exclude the current site.

Cove opens its own confirmation page before saving the exclusion. The page names the site it is about to exclude, and no setting changes until you choose "Exclude site" there. Choosing "Cancel", or closing the page, leaves your settings exactly as they were.

A confirmed exclusion takes effect on pages that are already open, without a reload: the in-page button disappears from that site. Remove the entry under Excluded Domains in the extension's options to get the button back.

The site that gets excluded is the one in the address bar. A video player embedded from another site, or the server the video itself is served from, cannot put its own name on the exclusion.

Saving the options page preserves an exclusion that was confirmed from another tab while the options page was open, and the unrelated change you made there is still saved.

No permission changes in this version.
```

The previous version, 1.3.10, is where the in-page button began respecting
Excluded Domains. That change is already published and is not re-announced
here.

## Official policy references

Read while drafting this document. The first five were accessed 2026-09-05; the
last two were accessed 2026-09-07 for this release candidate.

| Page | What was taken from it |
| --- | --- |
| <https://developer.chrome.com/docs/webstore/program-policies/privacy> | A product handling user data must post a policy that, with any in-product disclosures, comprehensively discloses how it collects, uses and shares user data, and all parties it is shared with. |
| <https://developer.chrome.com/docs/webstore/program-policies/user-data-faq> | Disclosure is required even when data is only processed or stored locally and never transmitted. User data explicitly includes authentication cookies, website content and resources, and web browsing activity such as the domains or URLs the browser interacts with. |
| <https://developer.chrome.com/docs/webstore/program-policies/unexpected-behavior> | Do not misrepresent functionality, and do not include non-obvious functionality that does not serve the product's primary purpose. |
| <https://developer.chrome.com/docs/webstore/program-policies/malicious-and-prohibited/> | Do not facilitate unauthorized access to site content such as circumventing paywalls or login restrictions. Do not encourage, facilitate, or enable unauthorized access, download, or streaming of copyrighted content or media. |
| <https://developer.chrome.com/docs/webstore/update> | An upgrade is a new zip containing all files, changed and unchanged, plus any changed listing metadata, resubmitted for review. Each new version must have a larger version number than the previous one, and the update is reviewed as a new item would be. |
| <https://developer.chrome.com/docs/extensions/reference/manifest/version> | One to four dot-separated integers, each 0-65535, no leading zeros on a non-zero integer, not all zero. Comparison is leftmost-first, integer by integer, with a missing integer equal to zero - so versions are not compared as strings. |

## Browser support for the confirmation path

Read 2026-09-12. The confirmation flow adds three platform requirements to
this bundle, and the manifest's declared floor is not raised for them.

| Requirement | Available from | Source |
| --- | --- | --- |
| `storage.session` | Chrome 102 | <https://developer.chrome.com/docs/extensions/reference/api/storage/> - "Chrome 102+", in-memory only, never persisted to disk, 10MB quota, not exposed to content scripts by default |
| `crypto.randomUUID` | Chrome 92 | Web Crypto, secure contexts only; an extension service worker and an extension page are both secure contexts |
| `tabs.create` | predates MV3 | - |

`manifest.chrome.json` declares no `minimum_chrome_version`, so the effective
floor is whatever the browser requires for `manifest_version: 3`. That floor is
below 102, which means a Chrome old enough to run MV3 but too old for
`storage.session` is reachable in principle. **The minimum is deliberately not
raised here**, because the behaviour on such a browser is a clean refusal
rather than a fault: `confirmationStorage()` in `extension/background.js`
returns `null` when `browser.storage.session` is absent,
`readExcludeConfirmations` throws, `requestExcludeConfirmation` returns
`{ ok: false, reason: "unavailable" }`, and the pill reports "Could not exclude
this site". No exclusion is written and nothing is left pending. Everything
else in the bundle is unaffected, because neither the excluded-domains
suppression nor the download handoff reads `storage.session`.

Raising `minimum_chrome_version` would be a manifest change beyond the version
line and is out of scope for this release; it is recorded here as a choice, not
an oversight.

Note for the record: none of these pages bans a video format, and none bans
HLS as a format. The prohibition is on facilitating unauthorized access to and
download of copyrighted media. Any future claim that a format is banned needs a
citation, not an assumption.
