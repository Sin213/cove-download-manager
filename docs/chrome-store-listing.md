# Chrome Web Store listing - Cove Download Manager (draft)

Lives in `docs/` rather than `dist/`, which `scripts/build_extension.py`
deletes on every build.

**This is a draft, not a submission.** It describes the Chrome bundle as it is
built from the current source. It does not name an upload artifact, because the
version to upload and the artifact to upload have not been decided or built
(Tab 4), and the live Store record has not been read (see
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

• Interception can be toggled with a keyboard shortcut, restricted to chosen file types, switched off for domains you list, and held to a minimum file size whenever the browser reports one

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
- A media target whose path ends in `.m3u8`, `.m3u`, or `.mpd` is refused when
  it is chosen from the context menu, and no link, page address, or fallback
  stands in for it.

  That refusal is enforced on the context-menu path only. The in-page button
  goes through `handleMediaTabDownload` in `media-core.js`, which gates on the
  HTTP(S) scheme and does not consult the refusal policy. In practice Chrome
  cannot play an HLS playlist from a `video` element, so such an element never
  becomes eligible for the button, which is what the browser check observed.
  That is platform behaviour rather than an enforced invariant, so this
  document does not claim the suffix refusal covers every path.

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
| Bounded by toggle, type, domain, and by size when the browser reports one | `extension/background.js` `handleCreated`, `DEFAULT_SETTINGS`. The size test is `typeof size === "number" && size > 0 && size < minSizeBytes`, so an unreported size is deliberately not filtered rather than treated as zero | `tests/extension_background.test.js` |
| Link and image context targets | `extension/background.js` `registerContextMenu` | `tests/extension_background.test.js` |
| Video and audio context targets exist in Chrome | `extension/media-core.js` `contexts`, `registerContextMenu` | `test_chrome_context_menu_derives_media_contexts_from_the_capability` |
| The element's own source wins over an enclosing link | `extension/background.js` `contextMenus.onClicked` | `test_chrome_media_action_selects_the_element_source_over_a_link` |
| In-page button on a direct video, in Chrome | `extension/content/media-tab.js` (shared), shipped by `_CHROME_EXCLUDE` not excluding it | `test_chrome_bundle_ships_the_shared_media_pill`, `tests/extension_media_tab.test.js` |
| No extraction, no site handling in Chrome | `scripts/build_extension.py` `_CHROME_EXCLUDE`; `extension/media-chrome.js` publishes no site hooks | `test_chrome_bundle_has_no_extractor_module`, `test_chrome_capability_supplies_no_site_hooks` |
| Playlist media targets refused on Chrome | `extension/media-chrome.js` `MANIFEST_SUFFIXES` | `test_chrome_capability_refuses_playlist_media_targets` |
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

### Unresolved external evidence, required before any submission

None of the following has been read in this work. They cannot be inferred from
the source tree and must not be guessed at:

- The exact current rejection text and its appeal state.
- The live listing copy, screenshots, and promotional images.
- Which Chrome versions have actually been uploaded and which are published.
  The manifest version in this repository is what would be built next; it is
  not evidence of the highest version ever uploaded.
- The permission justifications currently recorded in the dashboard.
- The privacy-practices declarations currently recorded in the dashboard.

Firefox versioning is a separate question against AMO's own published record.

---

## 6. Submission checklist

Nothing here is done, and none of it is in scope for this slice.

- [ ] Read the live Store record: rejection text, published versions, listing
      copy, screenshots, permission justifications, privacy declarations.
- [ ] Tab 3: full validation of freshly built Chrome and Firefox artifacts.
- [ ] Tab 4: decide the version, bump it, and rebuild. A Store upgrade is a new
      zip containing every file, changed or not, so the artifact that is
      validated must be the artifact that is uploaded.
- [ ] Replace any screenshot that shows a feature this build does not have.
      Screenshots showing stream detection or extraction contradict the copy.
- [ ] Point the dashboard's privacy policy field at the current `PRIVACY.md`.
- [ ] Re-check the description against section 4 after any behaviour change.

## Official policy references

Read while drafting this document. Accessed 2026-09-05.

| Page | What was taken from it |
| --- | --- |
| <https://developer.chrome.com/docs/webstore/program-policies/privacy> | A product handling user data must post a policy that, with any in-product disclosures, comprehensively discloses how it collects, uses and shares user data, and all parties it is shared with. |
| <https://developer.chrome.com/docs/webstore/program-policies/user-data-faq> | Disclosure is required even when data is only processed or stored locally and never transmitted. User data explicitly includes authentication cookies, website content and resources, and web browsing activity such as the domains or URLs the browser interacts with. |
| <https://developer.chrome.com/docs/webstore/program-policies/unexpected-behavior> | Do not misrepresent functionality, and do not include non-obvious functionality that does not serve the product's primary purpose. |
| <https://developer.chrome.com/docs/webstore/program-policies/malicious-and-prohibited/> | Do not facilitate unauthorized access to site content such as circumventing paywalls or login restrictions. Do not encourage, facilitate, or enable unauthorized access, download, or streaming of copyrighted content or media. |
| <https://developer.chrome.com/docs/webstore/update> | An upgrade is a new zip containing all files, changed and unchanged, plus any changed listing metadata, resubmitted for review. |

Note for the record: none of these pages bans a video format, and none bans
HLS as a format. The prohibition is on facilitating unauthorized access to and
download of copyrighted media. Any future claim that a format is banned needs a
citation, not an assumption.
