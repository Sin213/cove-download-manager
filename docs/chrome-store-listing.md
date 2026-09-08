# Chrome Web Store listing - Cove Download Manager (draft)

Lives in `docs/` rather than `dist/`, which `scripts/build_extension.py`
deletes on every build.

**This is a release candidate, not a submission.** It describes the Chrome
bundle as it is built from the current source, at the candidate version
**1.3.9** (`dist/cove-chrome-1.3.9.zip`). That version is provisional: it is the
next patch above both the repository source (1.3.8) and the highest version
verifiably published on the Store (1.3.6), but the public listing cannot show an
upload that was rejected, withdrawn, or is awaiting review. Confirm on the
developer dashboard before uploading (see
[Unresolved external evidence](#5-historical-rejection-context-and-unresolved-external-evidence)).

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

- The published version is **1.3.6**, last updated 10 August 2026, and the item
  is live in the Tools category.

That is the whole of what a public page can establish. The repository source was
at 1.3.8, so 1.3.7 and 1.3.8 were bumped in this repository (`22f4122`,
`444fabe`) and never became the published version. Whether either was uploaded
and rejected, uploaded and withdrawn, or simply never uploaded is not visible
publicly.

Still unread, and not inferable from the source tree or the public page:

- The exact current rejection text and its appeal state.
- Which Chrome versions have actually been **uploaded**, including any draft,
  in-review, or rejected upload. A public listing shows the published version
  only, so it cannot rule out an upload at or above 1.3.9.
- The live listing copy, screenshots, and promotional images.
- The permission justifications currently recorded in the dashboard.
- The privacy-practices declarations currently recorded in the dashboard.

Firefox versioning is a separate question against AMO's own record. Checked the
same day: the public version of `cove-dm@cove-download-manager.net` is 1.4.7,
matching the repository source, so 1.4.8 is the next patch. The same limit
applies - an upload awaiting review is not public.

---

## 6. Submission checklist

- [x] Read the public Store record: published version 1.3.6, last updated
      10 August 2026. The dashboard-only facts below are still outstanding.
- [x] Tab 3: full validation of freshly built Chrome and Firefox artifacts.
- [x] Tab 4: version decided (1.3.9, provisional), bumped, and rebuilt. A Store
      upgrade is a new zip containing every file, changed or not, so the
      artifact that is validated must be the artifact that is uploaded.
- [ ] Read the dashboard: rejection text and appeal state, uploaded-version
      history including drafts and rejections, permission justifications,
      privacy-practices declarations. Confirm 1.3.9 is unused before uploading.
- [ ] Replace any screenshot that shows a feature this build does not have.
      Screenshots showing stream detection or extraction contradict the copy.
- [ ] Point the dashboard's privacy policy field at the current `PRIVACY.md`.
      The repository copy being updated does not update a Store URL.
- [ ] Declare the Privacy practices data categories. The Chrome bundle reads
      page content locally to place the in-page button, and reads cookies for
      the download address, and local-only processing still requires disclosure.
- [ ] Re-check the description against section 4 after any behaviour change.

## 7. Release notes for 1.3.9

The Store has no per-version release-notes field; this is the text to use if the
description's "What's new" area or a changelog entry is updated.

```text
The refusal that already stopped a playlist address being sent from the right-click menu now also applies to the in-page Cove button, so both ways of handing a media address to Cove are held to it. A refused address is not marked, no cookies are read for it, nothing is sent to the desktop app, and no page address is substituted for it.

The minimum file size description has been corrected: the minimum can only be applied when the browser gives a usable size at the start of a download, so a smaller file may still be taken over.

No permission changes in this version.
```

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

Note for the record: none of these pages bans a video format, and none bans
HLS as a format. The prohibition is on facilitating unauthorized access to and
download of copyrighted media. Any future claim that a format is banned needs a
citation, not an assumption.
