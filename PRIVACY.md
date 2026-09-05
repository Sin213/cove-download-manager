# Privacy Policy - Cove Download Manager (browser extension)

_Last updated: 2026-09-05_

Cove Download Manager is a browser extension that hands downloads off to the
Cove Download Manager desktop application running on the same computer. This
policy describes what the extension looks at, what it keeps, what it passes to
the desktop app, and what leaves your computer as a result.

It covers both the Chrome and the Firefox builds. They are built from one
source tree and differ in which modules each one ships; where that changes what
happens, it is called out.

## Summary

- The extension has no server of its own, sends nothing to the developer, and
  contains no analytics or tracking code.
- It does inspect pages you visit, locally, to find video and audio you could
  download. That is local processing, and it is disclosed here because it is
  still handling of your data.
- When you start a download, the extension passes the download address, a
  filename, the referring page address, cookies for that address, and your
  browser's user-agent string to the local Cove desktop app.
- Cove then contacts the site you chose in order to download the file, using
  those cookies and headers. The download itself is a normal network request to
  that site. "Local handoff" describes how Cove is reached, not the download.
- Settings and a bounded diagnostics log are stored in your browser's local
  storage on your device.

## What the extension looks at, and when

### Pages you visit

On every ordinary web page (`http` and `https`, including frames), a content
script looks for `<video>` elements. Finding them means walking the page's
elements, including into open shadow roots, and watching for elements added
later. It reads:

- the element's own media address (`currentSrc`, `src`, or a child `<source>`),
  and whether it has buffered enough to be playable,
- the element's position and size on screen, and the page's scroll position, so
  the Cove button can be drawn over the player,
- the page address.

On Firefox only, it additionally looks at the video's ancestor elements for the
nearest one carrying a `data-hls-url` attribute, and reads that attribute's
value. Some players publish a stream address there for a video that has no
plain file address of its own, and that is the address Cove would be given.
The Chrome bundle does not ship this and never reads it.

No other attribute, no page text, and no form field is read.

This runs locally in the page. Nothing is sent anywhere because of it, and
nothing is stored. It exists so the Cove button can appear over a video rather
than on every page.

The button appears while a video is playing, before its address has
necessarily been resolved. If pressing it turns out to find no downloadable
address, it says so and sends nothing.

`<audio>` elements are not looked for and are not scanned. Audio is reachable
only by right-clicking a player yourself, which reads nothing from the page
until you do.

If you then press that button, the tab's title is used to suggest a filename,
and the media address and page address are handed off as described below.

It is not a scan of page text, form fields, or credentials, and the extension
does not build a history of the pages you visit.

On Firefox only, the extension additionally watches response headers for HLS
playlists so that streamed video can be listed in the popup. Chrome does not
ship that code and does not request the `webRequest` permission at all.

### Downloads you start

When the browser begins a download, the extension reads the browser's own
record of it: the address, the suggested filename, the referring page address,
and the size. It acts on it only when interception is enabled, the address is
not on a domain you excluded, and the file type is one you allowed.

Your minimum size applies whenever the browser reports a size for the
download. When the browser reports no size, which happens on responses that do
not declare a length, the minimum cannot be applied and the download is not
filtered by it.

### Right-click downloads

Choosing "Download with Cove" gives the extension the address of the link,
image, or media element you clicked, the address of the page it is on, and the
page title, which is used to suggest a filename.

On Chrome, a media address whose path ends in `.m3u8`, `.m3u`, or `.mpd` is
refused at this point and nothing is handed off. That refusal is narrow and
based on the address path; it is not a general block on any category of site.

### Cookies

At the moment a download is handed off, the extension reads the cookies your
browser holds **for that download address only**, using the browser's cookie
API. They are combined into one request header and passed to Cove so that a
file behind a session you are already logged into can still be fetched. If that
header would exceed 32 KB it is dropped entirely rather than truncated.

The extension does not read cookies for any other site, and does not store
cookies anywhere.

## What is passed to the Cove desktop app

Handoffs travel over the browser's native messaging channel to Cove on the same
computer. A handoff carries:

- the download address,
- a filename, derived from the browser's suggestion, the address, or the page
  title,
- the referring page address,
- cookies for the download address,
- your browser's user-agent string,
- the file size where the browser knew it,
- an opaque request id used to match up log entries.

Not every route sends every field. A right-click handoff sends no size. The
Firefox stream handoff sends no referrer and no cookies. Nothing else about the
page, and no page content, is included.

The popup additionally asks Cove for its version and for the list of downloads
currently running, so it can show connection status and progress.

## What leaves your computer

Reaching Cove is local. The download is not.

To perform the download, Cove passes the cookies, referrer, and user-agent it
received to its download engine, which then requests the file from the site the
address points at. That request carries your session cookies for that site, in
the same way your browser's own download would have.

If Cove cannot be reached on a right-click handoff, the extension falls back to
letting the browser download the file, which likewise contacts that site.

No other network activity originates from this extension. There is no
developer-controlled endpoint for it to contact.

## What is stored on your device

- **Settings** in local storage: interception on/off, minimum file size,
  intercepted file types, excluded domains, and whether the in-page button is
  shown. These never leave your device.
- **Diagnostics** in local storage: a rolling log of at most 300 recent events,
  kept so a support report can be produced after something goes wrong.
- **Short-lived working state**: the ids of downloads the extension cancelled
  after handing them to Cove, so they can be cleared from the browser's list,
  and a five-second record of recently handed-off addresses so the same
  download is not sent twice. On Firefox, playlist addresses seen in a tab are
  held while that tab is open, and dropped when it closes or when it navigates
  to a different address. Reloading the same address does not clear them, so
  addresses seen before a reload can still be listed after it.

### What the diagnostics log contains

Each entry holds a timestamp, a severity, the component that recorded it, an
event name, a random per-session id, and a small set of fields.

Addresses, page titles, filenames, cookies, referrers, user-agent strings, and
message payloads are removed by field name before an entry is written; they are
dropped, not shortened. Any address that still appears inside a remaining text
value is reduced to its scheme, host, and first path segment, with a deep
subdomain replaced by a placeholder. Long identifiers and UUIDs are replaced.
Your browser is recorded as a family and major version, such as "Firefox 140",
never the full user-agent string.

"Copy diagnostics" places that report on your clipboard when you press it, and
nowhere else. "Clear diagnostics" deletes the stored log. Nothing is sent
anywhere automatically.

If you send a diagnostics report to a maintainer, you are sharing it
deliberately, and what it contains is what is described above.

## Permissions and why they are requested

- `nativeMessaging` - to reach the Cove desktop app.
- `downloads` - to see downloads the browser starts, cancel one that Cove has
  accepted, and fall back to a browser download when Cove is unavailable.
- `cookies` - to read cookies for a download address at handoff time.
- `contextMenus` - for the "Download with Cove" entry.
- `notifications` - to report that a handoff succeeded or failed.
- `storage` - for the settings and the diagnostics log described above.
- Access to all sites - because a download can start on any site, and the
  in-page button has to be able to appear on any site with a video.
- `webRequest` (Firefox only) - to notice HLS playlists in response headers.

## Data sharing

Data is not sold, and is not transferred to any third party. It is not used for
anything unrelated to performing a download you asked for, and it is not used
for creditworthiness or lending purposes.

## Contact

Questions: open an issue at
https://github.com/Sin213/cove-download-manager/issues
