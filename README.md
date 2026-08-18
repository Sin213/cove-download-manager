# Cove Download Manager

A multi-connection download manager with a real queue, a daily schedule
window, and a global speed cap. Built on `aria2` for the protocol work and
PySide6 for the UI. Same look as the rest of the Cove suite.

![Python](https://img.shields.io/badge/python-3.10%2B-orange?style=flat-square&logo=python)
![Platforms](https://img.shields.io/badge/platforms-Windows%20%7C%20Linux-informational?style=flat-square)
![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)
![Version](https://img.shields.io/badge/release-v3.6.0-5eead4?style=flat-square)

![Cove Download Manager](docs/screenshot.png)

---

## Features

- **1-16 connections per file** - dropdown choices of 1, 2, 4, 8, or 16,
  default 16. Per-file segmenting handled by aria2.
- **Concurrent queue** - 1-16 parallel downloads. Default 1; bump to 2-4
  for small files.
- **Start / Pause queue** - pausing only stops what's actively downloading;
  individually-paused items stay paused after a queue resume.
- **Global speed cap** - KB/s limiter with an "Always on at startup" toggle.
  Hot-applied via aria2's `changeGlobalOption`, no restart required.
- **Daily schedule window** - restrict downloads to a time window per
  weekday, midnight-wrap supported. Outside the window the queue parks
  itself; inside it picks up where it left off.
- **Add from clipboard** - paste many URLs at once, pick which to queue.
- **Delete key + right-click menu** - remove selected, clear completed,
  clear all. Multi-select aware. File deletion is opt-in per row. The
  context menu also covers Open file, Show in folder, Start now
  (force-start, jumps the queue), View source, and Retry on errored tasks.
- **Duplicate detection** - adding something you already have is caught
  before it downloads. A match in the queue offers Focus Existing, a
  completed match offers Open Folder, and either can be overridden for that
  one add. Clipboard batches get a single summary instead of one dialog per
  link. Only short labels are shown, never a full signed link or a magnet
  carrying a tracker passkey.
- **View source** - right-click any task to see where it came from: the
  URL, the referring page, the save folder, and for torrents the name and
  info hash. Credentials, signed-link tokens, and tracker passkeys are
  masked on screen, cookies are never shown at all, and copying the
  unmasked URL is a separate deliberate button.
- **Resumable** - queue state persists in SQLite, partial downloads resume
  via aria2's control files. Closing the app does not lose work.
- **HLS / M3U8 stream downloads** - any URL ending in `.m3u8` is
  automatically routed through an `ffmpeg`-backed downloader instead of
  aria2, no configuration needed. Pause/resume aren't available for these
  tasks since ffmpeg streams straight through.
- **Light / dark theme** - toggle button in the titlebar next to the
  window controls swaps the whole UI live, no restart.
- **Category folders** - assign a destination folder per category in
  Settings, with an optional auto-sort toggle that files completed
  downloads into per-category subfolders automatically.
- **Auto-update** - checks GitHub Releases on launch. Always prompts
  before installing, never silent, and refuses to install assets that
  don't match a published `SHA256SUMS` digest.
- **Browser extension** - intercept downloads from Firefox, Chrome, and
  their derivatives (Zen, LibreWolf, Edge, Brave, and more). Cove offers the
  extension once on first run, keeps a permanent tray entry with both store
  listings, and shows in the window whether an extension has actually reached
  it. See [Browser Extension](#browser-extension).
- **Debrid accounts** - optional Real-Debrid, AllDebrid, and TorBox
  integration. Links on hosts your account supports are resolved
  automatically before downloading. See [Debrid services](#debrid-services).
- **Diagnostics** - a **Logs** button opens a filterable view of what Cove
  actually did, with **Copy diagnostics** for reporting a problem. Links,
  file paths, tokens and cookies are stripped before anything is stored or
  copied, and nothing is ever uploaded. See [Diagnostics](#diagnostics).
- **Magnet links from your browser** - once Cove is registered as a magnet
  handler on Linux or Windows, clicking a magnet link opens Cove and adds it
  to the existing torrent / debrid pipeline. An already-running Cove picks
  the link up in place, so there is no second window and no second aria2
  daemon. See [Opening magnet links from your browser](#opening-magnet-links-from-your-browser).
- **Magnet registration from Settings** - register Cove as your magnet handler
  from Settings -> Magnet links, with no terminal command. The row shows what
  the operating system currently reports rather than what Cove would prefer,
  so a default changed elsewhere is visible straight away. Cove also repairs
  its own registration after an update moves the executable, which the
  versioned portable `.exe` and AppImage filenames used to break.
- **Torrents and magnets** - Cove downloads torrents itself, no external
  client. Enabled debrid accounts are checked for a cached copy first and
  the files come down over HTTPS; anything uncached falls back to Cove's
  own aria2 BitTorrent engine, or is cancelled if you prefer. See
  [Torrents](#torrents).
- **Torrent search** - a built-in Search tab covering Movies, TV, Anime and
  Games across seven sources, plus any Torznab indexer you add yourself, with
  anything you pick going straight into the queue. See [Search](#search).
- **Network interface binding** - bind all of Cove's traffic to a chosen
  interface, such as a VPN adapter, in Settings -> BitTorrent. If that
  interface disappears, Cove stops rather than falling back to another one.
  See [Binding downloads to a network interface](#binding-downloads-to-a-network-interface).
- **Proxy support** - HTTP, HTTPS, or SOCKS5, with optional credentials.
  Local BitTorrent stays blocked while a proxy is set unless you enable the
  override, because a proxy cannot be trusted to cover peer traffic.
- **YouTube and video sites** - links yt-dlp supports are extracted and
  downloaded through it automatically, with the browser's referrer and user
  agent passed along. Browser cookies are deliberately not forwarded for
  YouTube pages, because sending a full cookie jar makes YouTube reject the
  request outright; public videos need no account cookies. Cookies are still
  forwarded for direct file and HLS downloads that depend on them.
- **Official local API** - authenticated loopback API plus an optional
  command-line client designed for AI agents and local automation.
- **In-page video pill** - a floating "Download with Cove" pill appears on
  video players while hovering or playing; one click sends the media to Cove.
  Supports direct MP4/WebM, detected HLS (M3U8), old Reddit posts, and YouTube.
- **Close to system tray** - optional, off by default, in Settings -> Window.
  With it on, the window X hides Cove to the tray instead of quitting, so the
  browser extension can still hand downloads to it; the tray icon's menu has
  Open Cove and Quit Cove, and a click on the icon restores the window. On a
  desktop with no tray the setting is disabled, because there would be no
  icon to bring Cove back from.
- **Frameless cove UI** - custom titlebar, mint accent, light and dark
  palettes. Dragging via `startSystemMove`, edge-resize via
  `startSystemResize`, both Wayland-safe. The controls column scrolls when
  the window is short rather than squeezing its sections together.
- **Its own app icon** - the skull keeps the Cove suite identity and gains a
  teal download arrow, so Cove Download Manager is distinguishable from the
  other Cove apps in the taskbar, tray, and launcher down to 16px.

---

## Install

### Linux - AppImage

Download the latest [`Cove-Download-Manager-<version>-x86_64.AppImage`](https://github.com/Sin213/cove-download-manager/releases/latest)
from the Releases page.

```bash
chmod +x Cove-Download-Manager-*.AppImage
./Cove-Download-Manager-*.AppImage
```

The AppImage requires `aria2` on `PATH` (`sudo pacman -S aria2`,
`sudo apt install aria2`, or your distro's equivalent).

### Linux - Debian / Ubuntu

```bash
sudo dpkg -i cove-download-manager_<version>_amd64.deb
sudo apt -f install   # if dependencies are missing
```

The `.deb` declares `Depends: aria2, yt-dlp`, so apt pulls both in for you.

### Windows

Two builds on the [Releases](https://github.com/Sin213/cove-download-manager/releases/latest) page:

- **`Cove-Download-Manager-<version>-Setup.exe`** - Inno Setup installer,
  per-user (no admin prompt), Start Menu + optional desktop shortcut.
- **`Cove-Download-Manager-<version>-Portable.exe`** - single-file build.
  No install, runs from anywhere. A normal portable launch writes nothing to
  the registry; the only thing that does is the opt-in magnet registration
  described in
  [Opening magnet links from your browser](#opening-magnet-links-from-your-browser).

Both Windows builds bundle `aria2c.exe` and `yt-dlp.exe`, so direct downloads
and YouTube extraction work without system installs of either tool.

> On first launch, Windows SmartScreen may show a warning because the
> `.exe` isn't code-signed. Click **More info** then **Run anyway**.

### Verifying downloads

Every artifact ships with a matching `.sha256` sidecar file. Verify with:

```bash
sha256sum -c Cove-Download-Manager-<version>-x86_64.AppImage.sha256
```

(or `Get-FileHash -Algorithm SHA256` on Windows). Cove's auto-update
verifies this digest before swapping any binary.

### Opening magnet links from your browser

Cove can be registered with the operating system as a magnet link handler.
Once it is, clicking a magnet link in your browser opens Cove and the link
goes into the normal torrent / debrid pipeline. If Cove is already running,
that same window takes the link, so you never get a second Cove window or a
second aria2 daemon.

Cove only ever advertises itself as *capable* of opening magnet links.
Choosing the default handler stays your decision on every platform, and
installing Cove never silently replaces a handler you already use.

Cove can register itself from **Settings -> Magnet links**. The row shows
what the operating system currently reports, not what Cove would prefer, so
a default changed elsewhere is visible immediately.

**Keeping it working after an update**

The portable executable and the AppImage carry their version in the file
name, so an update changes the path the registration points at. When
"Repair Cove's magnet registration after updates" is on, Cove repairs its
own registration the next time the updated build is launched.

The precise promise: after the updated build is launched once, Cove repairs
a stale registration it already owns. It does not silently reclaim a default
you changed to another application. If you delete the old file and click a
magnet link before ever launching the new one, Cove has not run yet and
cannot repair anything.

Cove only ever repairs a registration it owns. A portable copy and an
installed copy keep separate identities and never modify each other.

**Linux**

The AppImage installs its own desktop entry
(`cove-download-manager-appimage.desktop`) and sets the default directly.
The `.deb` ships its entry system-wide, so Cove only needs to set the
default. Cove verifies the result afterwards and reports honestly if your
desktop declined the change.

**Windows**

Since Windows 10, no application may make itself the default handler. Cove
registers itself as capable and opens Settings at its own entry; the final
choice is yours. Registration is per-user and needs no administrator rights.

The command-line flags still work for scripted setups:

```powershell
.\Cove-Download-Manager-<version>-Portable.exe --register-magnet-handler
.\Cove-Download-Manager-<version>-Portable.exe --unregister-magnet-handler
```

**Running from source**

A source checkout cannot register: the path it would record belongs to the
Python interpreter, not to Cove. Settings explains this rather than failing
silently.

---

## Diagnostics

Cove keeps a sanitized record of what it did, so a problem can be reported
with evidence instead of a description.

Press **Logs** in the action bar to open the Diagnostics window. Filter by
level, component, task or request id, or search the text. **Copy diagnostics**
puts a support-ready report on the clipboard, **Save diagnostics** writes it to
a text file, and **Open log folder** goes to the files themselves.

Logs live beside the rest of Cove's data:

| Install | Location |
|---------|----------|
| Linux | `~/.local/share/cove/logs/` |
| Windows (installed) | the Cove data folder plus `logs` |
| Windows (portable) | the portable data folder plus `logs` |

The active log is capped at 2 MB with three rotating backups, and the native
messaging host writes its own smaller `native-host.jsonl` beside it.

### What is not recorded

Redaction runs before anything is stored, displayed, copied or saved:

- Links keep only their scheme, a sanitized host and a route shape, so a
  Real-Debrid link is recorded as `https://real-debrid.com/d/<redacted>` and a
  magnet as `magnet:<redacted>`. Query strings and fragments are dropped.
- File paths keep only their shape. Your user folder becomes `%USERPROFILE%`
  or `~`, and the random work-directory suffix becomes `.cove-work-<work-id>`.
- API tokens, cookies, authorization headers and the aria2 RPC secret are
  never recorded. Debrid accounts appear only as configured or not.
- The browser extension never records a page address, a media address or a
  tab title.

There is no telemetry and no upload. Nothing leaves your machine unless you
press Copy or Save and send it yourself.

### Reporting a failed download

1. Reproduce the problem once.
2. Right-click the failed row and choose **View logs**, which opens the
   Diagnostics window already filtered to that download.
3. Press **Copy diagnostics** and paste the result into your report.

If the problem is that a browser download never reached Cove at all, the app
has nothing to log. Use **Copy diagnostics** in the extension popup instead,
which works with Cove closed.

---

## Browser Extension

**Firefox:** install the [Cove Download Manager extension](https://addons.mozilla.org/en-US/firefox/addon/cove-download-manager/)
from Firefox Add-ons. Works with Firefox, Zen, LibreWolf, Waterfox, Floorp,
and other Firefox-based browsers.

**Chrome / Chromium:** install the [Cove Download Manager extension](https://chromewebstore.google.com/detail/cove-download-manager/liakghhamogjcmmgnmcpephlfecmilnf)
from the Chrome Web Store. Works with Chrome, Edge, Brave, Vivaldi, Opera,
and Chromium.

### How it works

1. Install the extension for your browser.
2. Launch Cove at least once so the native messaging host is registered.
3. Open the extension's settings page and click **Test Connection to Cove**
   to confirm the link is active.

On first run Cove offers the extension once, with a banner above the download
list linking both store listings plus **Set up later**. It is answered exactly
once - dismissed, acted on, or made moot by an extension that connects - and
never asks again. The offer is also permanently available from the tray menu
under **Get the browser extension**.

### Connection status and setup problems

The **Browser extension** panel in the main window reports **Connected** or
**Not detected**, so you can tell an installed extension from a missing one
without waiting for a download to arrive. Any extension ping counts as proof,
as does an accepted browser download.

If registering the native messaging host fails - the usual reason an
extension cannot connect - the panel shows a **Setup problem** row with a
**Details** button explaining what went wrong, instead of leaving it in the
log file. Registration status is reported independently of the connection
indicator, because registration can fail for one browser while another talks
to Cove fine.

### Building the extension

`python scripts/build_extension.py` produces `dist/firefox/` and
`dist/chrome/` (plus zips). Firefox uses Manifest V2; Chrome uses Manifest V3
with a pinned key so the extension id is stable. Both the dev id and the
published Web Store id are whitelisted in the native host's
`allowed_origins` (`_CHROME_EXTENSION_IDS` in
`cove/native_host_install.py`).

Once connected, the extension intercepts downloads matching your configured
file types and minimum size, then sends them to Cove with cookies, referrer,
and user-agent headers so authenticated downloads work seamlessly.

### Settings

- **Interception** - toggle automatic download interception on/off, set a
  minimum file size threshold.
- **File types** - comma-separated list of extensions to intercept
  (`.zip`, `.exe`, `.mkv`, etc.). A sensible default list is included.
- **Excluded domains** - domains where interception is disabled
  (e.g. `drive.google.com`).

> **Tip:** You can also right-click any link and choose
> "Download with Cove" from the context menu, regardless of interception
> settings.

### In-page video pill

Hover or play a `<video>` on any page and a small floating "Download with
Cove" pill appears in its top-right corner. It remains available during
playback, including on direct old Reddit post pages. Clicking it sends the
media URL, page title, cookies, and referrer to Cove so downloads receive a
useful filename. Direct `http(s)` video sources work in both browsers;
Firefox also detects HLS (M3U8) streams for the tab. YouTube watch pages are
handed to Cove for extraction with yt-dlp. The pill can be disabled in the
extension settings. Nothing downloads automatically; it only acts on an
explicit click, and DRM-protected media remains unsupported.

---

## Debrid services

Cove can download through a **Real-Debrid**, **AllDebrid**, or **TorBox**
account. All three are optional and off by default; with none enabled,
nothing about downloading changes.

Configure them in **Settings -> Debrid services**:

- Tick **Enable AllDebrid** and paste an API key from
  <https://alldebrid.com/apikeys/>, tick **Enable Real-Debrid** and paste an
  API token from <https://real-debrid.com/apitoken>, and/or tick **Enable
  TorBox** and paste an API token generated from your TorBox account
  settings. Each provider works on its own; you do not need all three.
- **Test** verifies the credential and shows the account name and plan.
- **Try first** picks which provider handles a link multiple enabled
  accounts support. The default is AllDebrid first.
- TorBox hoster downloads and cached torrents are supported. TorBox Usenet
  is not.

When a download starts, Cove checks the URL's host against the provider's
published host list. If the host is supported, the original link is
resolved through your account and the resulting direct link is handed to
aria2 - you get the provider's filename, file size, and full-speed
multi-connection download. If no enabled provider supports the host, the
URL downloads directly exactly as before.

Credentials are stored locally in Cove's existing settings file
(`~/.config/cove/settings.json`, `0600` on POSIX) alongside the other
per-install secrets. They are never sent anywhere except the provider's
own API, and the local API never exposes them.

The generated direct link is transient and treated as a secret: it is used
for one aria2 download and never written to the queue database, a log, or
the UI. The task keeps the original hoster URL, so pausing, resuming, or
restarting Cove re-resolves a fresh link rather than reusing an expired one.

Torrents are handled separately - see [Torrents](#torrents).

Not included in this integration:

- Folder and container links, and streaming-quality selection.
- Account-bound debrid share/landing links. An AllDebrid `/f/` link
  (`alldebrid.com/f/...`) is tied to the browser session that created it
  and has no API unlock - Cove fails the download with a message telling
  you to add the original hoster link instead, rather than silently
  saving the provider's error page as a file. A Real-Debrid `/d/` link
  (`real-debrid.com/d/<token>`) is the standard link format Real-Debrid
  hands back from its `/torrents/info` and download-history endpoints;
  Cove passes it to Real-Debrid's `/unrestrict/link` API when an RD
  account is enabled, and only fails it with the same message when no RD
  account is configured. Direct node URLs that a provider already
  generated (`s1.debrid.it/...`, `*.download.real-debrid.com/...`) still
  download normally if you paste one by hand.
- Password-protected hoster links.

---

## Torrents

Cove handles torrents itself. No external torrent client is ever launched,
and every torrent appears in Cove's own list with the same progress, pause,
resume, retry and remove controls as any other download.

Turn it on in **Settings -> BitTorrent -> Enable torrent support**, then add
a torrent by pasting a magnet link or by choosing **Add torrent file...**
(you can also drop a `.torrent` onto the window).

### If you're used to the manual debrid workflow

If you already have a Real-Debrid, AllDebrid, or TorBox account, you do not
need the usual routine of pasting a magnet into the provider's own website,
checking whether it is cached, copying the generated link, and pasting that
link into a download manager. Once the account is enabled in **Settings ->
Debrid services**, Cove does that whole sequence itself:

1. Paste the magnet link, or add the `.torrent` file, into Cove exactly like
   any other torrent. There is nothing to check or copy by hand first.
2. Cove checks your enabled account for a cached copy and, if it finds one,
   requests the direct link and downloads it over HTTPS automatically.
3. If nothing is cached, Cove falls back to its own local BitTorrent engine
   (or the next enabled provider, if you have more than one configured)
   instead of creating a cloud download and waiting for it.

The same applies to a hoster link, for example a rapidgator.net URL: paste
the original hoster link into Cove, not a link generated on the provider's
own website. Cove resolves it through your account itself. See
[Debrid services](#debrid-services) for how to enable an account.

What happens to a torrent you add:

1. If an enabled AllDebrid, Real-Debrid, or TorBox account already has the
   torrent cached, Cove downloads the files over **HTTPS** through your
   account. No swarm is joined and your IP address is not shared with peers.
2. If no enabled provider has it cached - or you have no provider
   configured - Cove falls back to its own **built-in aria2 BitTorrent
   engine** and downloads it locally. Cove does not wait for TorBox (or any
   other provider) to cloud-download an uncached torrent first.

### Packed multi-file torrents

Real-Debrid sometimes compresses a cached multi-file torrent into a single
packed archive and returns one link for the whole thing rather than one link
per file. Cove recognises that answer and downloads it as **one honest file**,
named and sized from the provider's own metadata, instead of failing with
"the response could not be understood".

Cove does not extract the archive, and it does not pretend the individual
files arrived separately: you get one queue row and one file, which is
exactly what the provider delivered. Unpack it with whatever you normally
use. A response where the link count does not match the selected files in
this specific way is still treated as malformed and refused, rather than
guessing which link belongs to which file.

### What local BitTorrent means for your privacy

Downloading a torrent locally is a direct peer-to-peer connection:

- **Your IP address is visible to peers and trackers.** Before a local
  torrent starts, Cove shows a **Torrent is not cached** notice explaining
  this and waits: nothing is sent to any peer until you choose **Download
  locally**. The notice also offers **Open Settings** (change how uncached
  torrents are handled, then Cove re-checks the task) and **Cancel
  download**. Ticking *Don't show this notice again* only takes effect if
  you go on to download locally - cancelling or closing the dialog never
  records consent.
- **Cove does not seed after a download completes.** It may upload pieces
  to peers while the download is still running, and stops when it finishes.
- **Cove's proxy settings may not cover BitTorrent.** An HTTP proxy cannot
  be relied on for peer, DHT or UDP tracker traffic, so a configured proxy
  blocks local BitTorrent until you explicitly enable the override in
  **Settings -> BitTorrent**. Cached debrid downloads are unaffected.
- Cove cannot detect whether a VPN is active and makes no claim about
  anonymity.

Set **When a torrent is not cached** to *Cancel the download* if you want
Cove to use only the cached debrid route. Cancelled torrents stay in the
list as a failed task with the reason shown, and the notice above is skipped
entirely - you have already answered it.

### Binding downloads to a network interface

**Settings -> BitTorrent -> Network interface** lists the network interfaces
on your machine. Leave it on *Any interface* for normal behaviour, or pick
one - a VPN adapter such as `wg0-mullvad`, for example - and Cove passes it
to aria2 as `--interface`, so traffic leaves through that adapter.

Two things to know:

- **It binds every download Cove handles, not just torrents.** Cove runs a
  single shared aria2 daemon, so the selected interface applies to ordinary
  HTTP downloads and cached debrid downloads as well. Restart Cove to apply
  a change.
- **There is no silent fallback.** If the interface you selected is missing
  when Cove starts - a VPN that is down, an adapter that was renamed - Cove
  refuses to launch aria2 and shows an error naming the interface. It will
  not quietly send your downloads out over a different adapter. Reconnect
  the interface, or choose another one in Settings.

Cove still cannot verify that a VPN is actually up and working; binding to
its interface is a routing decision, not a guarantee.

### Search

Cove has its own torrent search, so finding something no longer means
leaving the window. Open **Search**, type a query and pick a category: every
built-in source that covers that category is queried at once, and the
results come back merged, de-duplicated and sorted together. Adding one
sends it to the same queue as any other download.

| Category | Sources |
|---|---|
| Movies | YTS, Torrents-CSV |
| TV | Torrents-CSV |
| Anime | Nyaa, SubsPlease, nekoBT |
| Games | FitGirl, GOG Games |

Search requests use the network interface selected in Settings, the same one
downloads are bound to. The built-in source list is fixed at build time:
nothing is loaded from disk or the network to decide what runs, and no
built-in source needs an account.

Results are ranked by how well the title matches what you typed, from an
exact match down to the same words scattered anywhere in the title, and ties
below that fall back to seeders and source priority. Matching works on whole
words, so a query for `Ring` finds "The Ring" and never "Daring".

#### Custom Torznab indexers

Beyond the built-ins you can add your own Torznab endpoints - a Jackett or
Prowlarr instance, or anything else that speaks Torznab. Open **Settings ->
Indexers**, add the endpoint URL and its API key if it needs one, and it is
queried alongside the built-in sources for every category. Each entry can be
enabled or disabled on its own, and reordered to set which source wins when
two of them return the same torrent.

Endpoint URLs are classified before any request is made. Only exact
localhost, literal loopback, RFC1918 and link-local addresses (and their IPv6
equivalents) are treated as local; everything else must be HTTPS and stays
bound to your selected interface. A hostname is never resolved to decide
this. Local routing is a transport decision only - responses from a custom
indexer are parsed as untrusted input either way.

Torrents-CSV carries no category of its own, so Movies and TV run the same
query against it; the release titles carry the season and episode markers that
tell them apart. It publishes no magnet either, so Cove builds one from the
info hash it validated, the same way it already does for GOG Games.

### Current limits

- Every file in a torrent is downloaded; there is no per-file selection UI
  yet.
- Remote `http(s)://.../file.torrent` URLs are not supported yet - download
  the `.torrent` first, then add it.
- No RSS or streaming.
- Delivery URLs generated by a debrid provider stay transient: they are
  never written to disk and are re-created on each launch.

---

## Where Cove keeps its files

| What | Where (Linux) | Where (Windows) |
|---|---|---|
| Settings | `~/.config/cove/settings.json` | `%USERPROFILE%\.config\cove\settings.json` |
| Queue DB | `~/.local/share/cove/cove.db` | `%USERPROFILE%\.local\share\cove\cove.db` |
| aria2 session / log | `~/.local/share/cove/aria2.{session,log}` | `%USERPROFILE%\.local\share\cove\aria2.{session,log}` |
| Debrid host cache | `~/.local/share/cove/debrid_hosts.json` | `%USERPROFILE%\.local\share\cove\debrid_hosts.json` |
| Torrent copies | `~/.local/share/cove/torrents/` | `%USERPROFILE%\.local\share\cove\torrents\` |
| Diagnostics logs | `~/.local/share/cove/logs/` | `%USERPROFILE%\.local\share\cove\logs\` |

Portable builds keep everything in a `cove-app-data` folder next to the
executable instead.

Settings include separate per-install random aria2 RPC and local API secrets,
plus any debrid API credentials you save;
on POSIX the file is written
with `0600` permissions so other local users can't read it (on Windows the
file inherits the user profile's ACL).

---

## Official local API

Cove starts a versioned HTTP API on `127.0.0.1:17681` by default. It is intended
for first-party local automation and never listens on a LAN interface. Apart
from the minimal `GET /api/v1/health` readiness check, endpoints require the
distinct `api_token` as an `Authorization: Bearer` credential. Browser-origin
requests and wildcard CORS are not accepted.

The v1 endpoints add, list, inspect, pause, resume, and safely cancel downloads.
Cancellation always maps to `QueueManager.remove(task_id, delete_file=False)`;
there is no file-deletion endpoint. All task reads and mutations are marshalled
onto the Qt main thread and use Cove's normal queue persistence, UI signals,
and status transitions.

The Windows-only companion
[`tools/cove-api/cove-api.cmd`](tools/cove-api/README.md) client can start Cove
when it is offline and emits one stable JSON object per command. Linux
integrations should use the direct local API method described below. Integer
Cove `task_id` values are the authoritative control identifiers; an aria2
`gid` may be null while a task is queued or launching.

### Give an AI access

Choose one integration method. On Windows, the command-line wrapper is
recommended for small local models because it handles startup, settings
discovery, authentication, validation, and predictable JSON. The wrapper is
not currently supported on Linux. Linux integrations should use direct HTTP
access through a trusted host that starts Cove and injects the API credential.

#### Option 1: Windows command-line wrapper (recommended on Windows)

1. On Windows, download `Cove-AI-Client-<version>.zip` from this repository's
   release and extract it locally.
2. Launch Cove once. If the client is not beside Cove, set `cove_executable`
   in `wrapper_config.json`; pass `--settings` when settings are not discovered
   automatically.
3. Give the AI the complete
   [`AI_WRAPPER_OPERATING_RULES.md`](tools/cove-api/AI_WRAPPER_OPERATING_RULES.md)
   file as operating instructions and allow it to run `cove-api.cmd`.
4. The AI runs `health`, then `add`, preserves the returned integer `task_id`,
   and uses that ID with `status`, `pause`, `resume`, or `cancel`.

```powershell
cove-api.cmd health
cove-api.cmd add "https://example.com/file.zip" --directory "D:\Downloads" --connections 8
cove-api.cmd status 123
```

The wrapper reads the API credential from Cove's settings itself. Do not copy
the credential into the prompt. See the
[`command-line client guide`](tools/cove-api/README.md) for settings discovery,
signed URLs, filenames, directories, and exit behavior.

#### Option 2: direct local API (Windows and Linux; recommended on Linux)

1. The trusted host integration starts Cove and checks `GET /api/v1/health`.
   On Linux, use this method instead of the Windows `.cmd` wrapper.
2. Outside the model, the host reads Cove's `api_token` and injects it as the
   `Authorization: Bearer <token>` header for authenticated requests.
3. Give the AI the complete
   [`AI_DIRECT_API_OPERATING_RULES.md`](tools/cove-api/AI_DIRECT_API_OPERATING_RULES.md)
   file and expose a local HTTP tool configured for Cove's base URL.
4. The AI calls `POST /api/v1/downloads`, preserves
   `download.task_id`, and polls `GET /api/v1/downloads/{task_id}`.

The bearer token must remain in the host's secret store: the AI should never
read, print, log, or request it. Both methods support URL, absolute destination,
safe filename, 1-16 connections, and per-download speed limit overrides.

---

## Usage

1. **Add download** - `Ctrl+N`. Paste one or many URLs (one per line),
   pick the destination folder.
2. **Add from clipboard** - `Ctrl+Shift+V`. Cove scans the clipboard for
   URLs and shows a checkable list.
3. **Pause / resume** - `Ctrl+P` toggles the whole queue. Right-click a
   row for per-item Pause / Resume / Remove.
4. **Delete key** - focus the downloads list, hit `Delete` to remove the
   selection (file on disk is kept; use right-click then "Remove and delete
   file" to wipe it too).
5. **Schedule** - toolbar, Edit schedule. Pick a daily window, weekdays,
   12-hour or 24-hour format.

---

## How it works

`QueueManager` (Qt main thread) holds the canonical state and persists
every transition to SQLite. It dispatches aria2 RPC calls to a
`QThreadPool` worker pool so the UI never blocks. A 500 ms `tellStatus`
poll feeds progress; a separate 30 fps redraw timer interpolates between
samples (`completed_bytes + speed * elapsed`) so progress bars glide
instead of stepping.

State machine per task:

```
queued -> active -> (paused -> active)* -> (completed | error)
```

Pause / remove issued before aria2's `addUri` returns a `gid` are deferred
via `_pending_launch` and dispatched once the gid lands, so the daemon
never ends up running a download Cove already forgot about.

Auto-update follows the same philosophy as Nexus's adoption flow: hit
`releases/latest` on launch, surface a prompt, and refuse to swap the
binary unless its SHA-256 matches the published manifest.

---

## Build from source

Running from source requires Python 3.10+. Windows artifacts are built natively
by GitHub Actions and can also be built locally with PowerShell, Python 3.12,
PyInstaller, Pillow, and an `aria2c.exe` path. The older Wine script remains
available for Linux cross-build environments.

```bash
git clone https://github.com/Sin213/cove-download-manager
cd cove-download-manager

# Run from source
pip install -r requirements.txt
./cove.sh

# Linux AppImage (python-appimage based)
./build.sh

# Linux .deb (PyInstaller based)
./scripts/build-deb.sh

# Windows portable (native PowerShell)
.\scripts\build-windows.ps1 -Python .\.buildenv\Scripts\python.exe -Aria2Exe C:\path\to\aria2c.exe

# Windows Setup.exe too, when Inno Setup 6 is installed
.\scripts\build-windows.ps1 -Python .\.buildenv\Scripts\python.exe -Aria2Exe C:\path\to\aria2c.exe -Setup

# Windows cross-build from Linux via Wine
./scripts/build-windows-wine.sh
```

Artifacts land in `release/` with matching `.sha256` sidecars. Windows builds
also stage `Cove-AI-Client-<version>.zip`. The native Windows builder downloads
the official `yt-dlp.exe` automatically when it is not already under
`build\yt-dlp-win`; use `-YtDlpExe PATH` or `COVE_YTDLP_EXE` to supply one.

---

## Project layout

```
cove-download-manager/
├── cove/                        # Python package
│   ├── app.py                   #   bootstrap: app + daemon + window wiring
│   ├── aria2.py                 #   aria2c lifecycle + JSON-RPC client
│   ├── clipboard.py             #   URL extractor for "Add from clipboard"
│   ├── config.py                #   JSON-backed Settings + ScheduleWindow
│   ├── db.py                    #   SQLite schema + connection helper
│   ├── debrid.py                #   Real-Debrid / AllDebrid link resolution
│   ├── dialogs.py               #   Add / Schedule / Settings / batch picker
│   ├── entry.py                 #   CLI entry point
│   ├── hls.py                   #   ffmpeg-backed HLS (M3U8) downloader
│   ├── main_window.py           #   QMainWindow + table + side panel
│   ├── native_host_install.py   #   auto-register native messaging hosts
│   ├── native_messaging.py      #   native messaging host for browser extension
│   ├── portable.py              #   portable-mode data directory detection
│   ├── queue.py                 #   QueueManager + DownloadTask
│   ├── scheduler.py             #   time-window allowed/not-allowed gate
│   ├── system_open.py           #   AppImage env scrubbing for xdg-open children
│   ├── theme.py                 #   cove design tokens + QSS, light/dark themes
│   ├── updater.py               #   GitHub releases + SHA-256 verifier
│   └── widgets.py               #   Titlebar, BrandBadge, StatsStrip, ...
├── extension/                   # Firefox WebExtension (native messaging)
├── packaging/                   # PyInstaller launcher + Inno Setup script
├── scripts/                     # build-deb.sh, build-windows-wine.sh
├── docs/screenshot.png
├── cove_icon.png                # shared cove skull badge
├── build.sh                     # AppImage build (python-appimage)
├── pyproject.toml
└── requirements.txt
```

---

## License

MIT.
