// extension/background.js

// Cross-browser shim: Firefox exposes `browser` (promise-based); Chromium
// exposes `chrome`. Chrome MV3 APIs used here are all promise-based, so the
// same code runs on both.
const browser = globalThis.browser || globalThis.chrome;

const HOST_NAME = "cove_download_manager";

// ---- Diagnostics ----
//
// The background context owns the extension's diagnostic ring. It is the only
// context that outlives a popup closing or a tab navigating, and it is still
// running when Cove is not - which is exactly the failure the user needs to be
// able to report. Content scripts and the popup send their events here rather
// than keeping rings of their own.
//
// extension/diagnostics.js is loaded by the browser, not imported: the
// manifest lists one background script, so this picks whichever loader the
// current browser offers. Anything recorded before it lands is buffered.

let coveDiag = null;
let diagPendingAppVersion = null;
// Resolves once diagnostics exist and their stored ring has been hydrated.
// A report served before that would be missing the previous session.
let resolveDiagReady = null;
const diagReady = new Promise((resolve) => { resolveDiagReady = resolve; });
const diagPending = [];
const DIAG_PENDING_MAX = 100;
let diagFlushTimer = null;

function diagFlushSoon() {
  if (diagFlushTimer || !coveDiag) return;
  diagFlushTimer = setTimeout(() => {
    diagFlushTimer = null;
    try {
      coveDiag.flush();
    } catch (e) { /* diagnostics never break a download */ }
  }, 1000);
}

function diagRecord(component, event, level, fields, requestId) {
  try {
    if (!coveDiag) {
      diagPending.push([component, event, level, fields, requestId]);
      if (diagPending.length > DIAG_PENDING_MAX) diagPending.shift();
      return;
    }
    coveDiag.record(component, event, level, fields, requestId);
    diagFlushSoon();
  } catch (e) { /* diagnostics never break a download */ }
}

function diagSetAppVersion(version) {
  try {
    if (coveDiag) coveDiag.setEnvironment({ appVersion: version });
    else diagPendingAppVersion = version;
  } catch (e) { /* diagnostics never break a download */ }
}

function diagInit() {
  if (coveDiag || typeof CoveDiag === "undefined") return;
  try {
    coveDiag = CoveDiag.createDiagnostics({
      storage: browser.storage.local,
      context: "background",
      version: (browser.runtime.getManifest && browser.runtime.getManifest().version) ||
        "unknown",
      browser: CoveDiag.browserLabel(navigator && navigator.userAgent),
    });
    if (diagPendingAppVersion) {
      coveDiag.setEnvironment({ appVersion: diagPendingAppVersion });
      diagPendingAppVersion = null;
    }
    Promise.resolve(coveDiag.load()).then(() => {
      const pending = diagPending.splice(0, diagPending.length);
      for (const entry of pending) coveDiag.record(...entry);
      diagFlushSoon();
    }).catch(() => {}).then(() => resolveDiagReady());
  } catch (e) {
    coveDiag = null;
    resolveDiagReady();
  }
}

function diagLoadScript() {
  try {
    if (typeof CoveDiag !== "undefined") {
      diagInit();
      return;
    }
    if (typeof importScripts === "function") {
      importScripts("diagnostics.js");
      diagInit();
      return;
    }
    if (typeof document !== "undefined" && document.head) {
      const element = document.createElement("script");
      element.src = browser.runtime.getURL("diagnostics.js");
      element.onload = diagInit;
      element.onerror = () => resolveDiagReady();
      document.head.appendChild(element);
      return;
    }
    // No loader at all: nothing will ever hydrate, so unblock the waiters.
    resolveDiagReady();
  } catch (e) {
    // Diagnostics are optional, downloads are not.
    resolveDiagReady();
  }
}

diagLoadScript();

// ---- Media ----
//
// The media runtime is two scripts: media-core.js, which publishes CoveMedia,
// and a per-browser capability it resolves - media-sites.js on Firefox,
// media-chrome.js on Chrome. Firefox's MV2 manifest lists both ahead of this
// script, so they are already loaded by the time this runs. Chrome's MV3
// manifest can name a single service worker file, so this script is the
// loader there.
//
// Synchronously, during ordinary top-level evaluation. An MV3 worker is woken
// by the event it has to serve and gets no install or startup event on that
// path, so anything deferred to onInstalled, onStartup, a later message or an
// asynchronous fetch would leave a woken worker answering a pill click, and
// registering a context menu, as if the build shipped no media at all.

function mediaLoadScripts() {
  // Firefox: the manifest already did it. Also the guard that keeps a worker
  // restart from importing twice on a context that somehow kept the binding.
  if (typeof CoveMedia !== "undefined") return;
  // No importScripts and nothing preloaded: a build with no media runtime.
  // Everything downstream already degrades to links and images.
  if (typeof importScripts !== "function") return;
  try {
    // One call on purpose: a worker fetches every argument before evaluating
    // any of them, so a missing file leaves neither half loaded rather than a
    // core with no capability behind it.
    importScripts("media-core.js", "media-chrome.js");
  } catch (e) {
    // The bundle is wrong, not the page. Degrade the way a media-less build
    // does - CoveMedia stays undefined and the menu below reads it - but say
    // so, because from the outside those two are indistinguishable and only
    // one of them is a bug.
    diagRecord("extension.background", "media_load_failed", "ERROR", {
      reason: "import_failed",
    });
  }
}

mediaLoadScripts();

function sendNativeMessage(msg, requestId) {
  const action = msg && typeof msg.action === "string" ? msg.action : "unknown";
  diagRecord("extension.background", "native_message_sent", "INFO",
             { action }, requestId);
  return browser.runtime.sendNativeMessage(HOST_NAME, msg).then((result) => {
    diagRecord("extension.background", "native_message_result", "INFO", {
      action,
      status: (result && result.status) || "none",
    }, requestId);
    return result;
  }).catch((err) => {
    // The error text can carry a path or a host name, so only its shape is
    // kept. transport: the host could not be reached at all (not installed,
    // or the browser refused to launch it), as opposed to a reply that says no.
    diagRecord("extension.background", "native_message_result", "WARNING", {
      action,
      status: "transport_error",
    }, requestId);
    return { status: "error", message: err.message || String(err), transport: "error" };
  });
}

// ---- Default settings ----

const DEFAULT_SETTINGS = {
  enabled: true,
  minSizeBytes: 1024 * 1024, // 1 MB
  interceptExtensions: [
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".exe", ".msi", ".dmg", ".iso", ".img",
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".mp3", ".flac", ".aac", ".ogg", ".wav",
    ".pdf", ".torrent",
    ".deb", ".rpm", ".appimage",
  ],
  excludedDomains: [],
  mediaPillEnabled: true,
};

let settings = { ...DEFAULT_SETTINGS };

async function loadSettings() {
  const stored = await browser.storage.local.get("settings");
  if (stored.settings) {
    settings = { ...DEFAULT_SETTINGS, ...stored.settings };
  }
  return settings;
}

// On MV3 the service worker is torn down and this script re-runs on wake,
// resetting `settings` to defaults. Event handlers must await this before
// reading `settings`, or they'd act on defaults (ignoring excluded domains,
// re-enabling a disabled extension, etc.).
let settingsReady = loadSettings().catch(() => {
  settings = { ...DEFAULT_SETTINGS };
  return settings;
});

function ensureSettings() {
  return settingsReady;
}

// Keep the in-memory copy fresh if another context (the options page) writes.
browser.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.settings) {
    settings = { ...DEFAULT_SETTINGS, ...(changes.settings.newValue || {}) };
    updateBadge();
  }
});

async function saveSettings(newSettings) {
  settings = { ...DEFAULT_SETTINGS, ...newSettings };
  await browser.storage.local.set({ settings });
}

// ---- Download interception ----

function isDomainExcluded(url) {
  try {
    const hostname = new URL(url).hostname;
    return settings.excludedDomains.some(
      (d) => hostname === d || hostname.endsWith("." + d)
    );
  } catch {
    return false;
  }
}

// Dedup guard: URLs intercepted recently (prevents re-intercept after
// cancel). Timestamp-based + pruned on read, so it survives without a
// setTimeout (unreliable in an MV3 service worker that may sleep).
const DEDUP_WINDOW_MS = 5000;
const recentIntercepted = new Map(); // url -> timestamp
function markIntercepted(url) {
  const now = Date.now();
  // Sweep expired entries so the Map can't grow unbounded over a long-lived
  // (Firefox MV2) background page.
  for (const [u, ts] of recentIntercepted) {
    if (now - ts > DEDUP_WINDOW_MS) recentIntercepted.delete(u);
  }
  recentIntercepted.set(url, now);
}
function wasRecentlyIntercepted(url) {
  const ts = recentIntercepted.get(url);
  if (ts === undefined) return false;
  if (Date.now() - ts > DEDUP_WINDOW_MS) {
    recentIntercepted.delete(url);
    return false;
  }
  return true;
}

// Extension of the file being downloaded, preferring the suggested filename
// and falling back to the URL path. Returns "" when none can be determined.
function downloadExtension(item) {
  const name = (item.filename || item.url || "").split(/[?#]/)[0];
  const slash = Math.max(name.lastIndexOf("/"), name.lastIndexOf("\\"));
  const dot = name.lastIndexOf(".");
  if (dot === -1 || dot < slash) return "";
  return name.substring(dot).toLowerCase();
}

browser.downloads.onCreated.addListener((downloadItem) => {
  // Don't await here; the handler kicks off async work itself.
  handleCreated(downloadItem);
});

// Max age of a download item we treat as new. Chromium replays restored
// history items through onCreated when the download manager initializes
// (crbug 41142658); on MV3 that can happen on every service worker wake.
// Replayed items carry their original startTime, fresh ones start "now".
const MAX_NEW_DOWNLOAD_AGE_MS = 10000;

async function handleCreated(downloadItem) {
  // Restored/replayed history items are never "in_progress". Without this
  // guard, Chrome re-sends the user's entire download history (e.g. items
  // another download manager cancelled) to Cove on every worker wake.
  if (downloadItem.state && downloadItem.state !== "in_progress") return;
  if (downloadItem.startTime) {
    const started = Date.parse(downloadItem.startTime);
    if (!Number.isNaN(started) && Date.now() - started > MAX_NEW_DOWNLOAD_AGE_MS) return;
  }

  await ensureSettings();
  const url = downloadItem.url || "";
  if (!settings.enabled) return;
  if (url.startsWith("blob:") || url.startsWith("data:")) return;
  if (isDomainExcluded(url)) return;
  if (wasRecentlyIntercepted(url)) return;

  // Size filter: only when the size is known. Small files are left to the
  // browser per the user's minimum-size setting.
  const size = downloadItem.totalBytes;
  if (typeof size === "number" && size > 0 && size < settings.minSizeBytes) return;

  // Extension allowlist: only grab configured file types. An empty list
  // means "intercept everything".
  const exts = settings.interceptExtensions || [];
  if (exts.length && !exts.includes(downloadExtension(downloadItem))) return;

  interceptDownload(downloadItem);
}

// Download ids we cancelled and still want erased from the browser's list.
// Persisted to session storage so IDs survive MV3 service worker restarts.
let interceptedIds = new Set();

async function loadInterceptedIds() {
  try {
    const store = browser.storage.session || browser.storage.local;
    const data = await store.get("_interceptedIds");
    if (Array.isArray(data._interceptedIds)) {
      for (const id of data._interceptedIds) interceptedIds.add(id);
    }
  } catch {}
  await pruneInterceptedIds();
}

// Drop ids the browser no longer has an in-progress download for. Cleanup used
// to depend entirely on catching a terminal onChanged, which is missed when it
// races the insertion below or fires while the extension is suspended - so an
// id could persist for the whole session and suppress or misclassify later
// events for a reused id.
async function pruneInterceptedIds() {
  if (interceptedIds.size === 0) return;
  for (const id of [...interceptedIds]) {
    let items;
    try {
      items = await browser.downloads.search({ id });
    } catch {
      continue;  // Cannot tell; leave it alone rather than guess.
    }
    const item = items && items[0];
    if (!item) {
      interceptedIds.delete(id);   // Already gone from the browser's list.
      continue;
    }
    if (item.state === "in_progress") continue;
    // Terminal, and still listed: this is exactly the missed cleanup the
    // persisted set exists to recover. Dropping the id without erasing would
    // strand the cancelled download in the browser's history permanently.
    try {
      await browser.downloads.erase({ id });
      interceptedIds.delete(id);
    } catch {
      // Keep it for the next startup rather than lose track of it.
    }
  }
  persistInterceptedIds();
}

// Writes are chained rather than fired off independently: two overlapping
// store.set() calls can complete out of order, which would let an older
// snapshot overwrite a newer one and resurrect ids that had just been cleared.
// The snapshot is taken synchronously at call time, so the last caller's state
// is what the last write in the chain carries.
let persistChain = Promise.resolve();

function persistInterceptedIds() {
  const snapshot = [...interceptedIds];
  persistChain = persistChain.then(async () => {
    try {
      const store = browser.storage.session || browser.storage.local;
      await store.set({ _interceptedIds: snapshot });
    } catch {}
  });
  return persistChain;
}

// The pruning that runs *during* hydration must not wait on itself, so only
// the event-driven mutators below await readiness.
async function saveInterceptedIds() {
  await interceptedIdsReady;
  return persistInterceptedIds();
}

async function forgetIntercepted(id) {
  // Without this, an erase arriving while the stored set is still hydrating
  // operates on an empty Set and is lost - and the id comes back on the next
  // startup as though it were never cleaned up.
  await interceptedIdsReady;
  if (!interceptedIds.delete(id)) return;
  return persistInterceptedIds();
}

const interceptedIdsReady = loadInterceptedIds();

browser.downloads.onChanged.addListener(async (delta) => {
  await interceptedIdsReady;
  if (!interceptedIds.has(delta.id)) return;
  const state = delta.state && delta.state.current;
  if (state === "interrupted" || state === "complete") {
    browser.downloads.erase({ id: delta.id }).catch(() => {});
    await forgetIntercepted(delta.id);
  }
});

// Terminal in every ordering, including ones onChanged never reports: the item
// is gone from the browser's list, so there is nothing left to erase.
browser.downloads.onErased.addListener(async (id) => {
  await forgetIntercepted(id);
});

// Cove's native handoff bounds the cookie header (single_instance.py,
// MAX_BROWSER_COOKIES_LENGTH). A large origin's whole jar can exceed it, and
// Cove then refused the entire download - so an oversized jar is dropped
// rather than sent. Never truncated: half a cookie header authenticates
// nothing, and a request Cove accepts without cookies is still better than a
// request Cove refuses outright.
const MAX_HANDOFF_COOKIES_LENGTH = 32 * 1024;

async function collectCookies(url) {
  let cookieStr = "";
  try {
    const cookies = await browser.cookies.getAll({ url });
    cookieStr = cookies.map((c) => `${c.name}=${c.value}`).join("; ");
  } catch {
    // No cookies available.
    return "";
  }
  if (cookieStr.length > MAX_HANDOFF_COOKIES_LENGTH) {
    // Fixed sentence: the jar itself must never reach the browser console.
    console.log(
      "Cove: browser cookie header exceeded native handoff limit; sending without cookies"
    );
    return "";
  }
  return cookieStr;
}

async function interceptDownload(downloadItem) {
  // Mark synchronously to block concurrent same-URL events before any await.
  markIntercepted(downloadItem.url);

  const dlId = downloadItem.id;

  // Gather cookies while the browser download is still running.
  const cookieStr = await collectCookies(downloadItem.url);

  // Extract filename from the download item.
  let filename = null;
  if (downloadItem.filename) {
    const parts = downloadItem.filename.replace(/\\/g, "/").split("/");
    filename = parts[parts.length - 1] || null;
  }

  console.log("Cove: sending download to native host", downloadItem.url);

  // Send to native host BEFORE cancelling. The browser download continues
  // until we have confirmed acceptance of this specific request.
  const result = await sendNativeMessage({
    action: "download",
    url: downloadItem.url,
    filename: filename,
    referrer: downloadItem.referrer || "",
    cookies: cookieStr,
    fileSize: downloadItem.totalBytes || 0,
    userAgent: navigator.userAgent,
  });

  console.log("Cove: native host response", JSON.stringify(result));

  if (result && result.status === "ok") {
    // Confirmed: native host accepted this download. Now cancel the browser copy.
    interceptedIds.add(dlId);
    await saveInterceptedIds();
    try {
      await browser.downloads.cancel(dlId);
    } catch {
      // The browser download completed before cancel() ran. Both the browser
      // and Cove will have the file. This is an inherent limitation of the
      // WebExtension API: there is no pause/reservation primitive that would
      // let us hold the browser transfer while confirming with the native host.
      // The alternative (cancel-first) silently loses downloads when Cove is
      // unavailable, which is the worse failure mode.
    }
    browser.downloads.erase({ id: dlId }).catch(() => {});
    showNotification("Download sent to Cove", filename || downloadItem.url);
  } else {
    // Native host failed or is unavailable. Clear the dedup mark so the
    // browser's original download proceeds unimpeded and future intercepts
    // of the same URL are not blocked.
    recentIntercepted.delete(downloadItem.url);
    // `result` may be null/undefined if the host replied with malformed JSON,
    // so don't dereference it: an exception here would abort the handler.
    console.error(
      "Cove: native host failed, browser download continues",
      (result && result.message) || "no response"
    );
  }
}

// ---- Context menu ----

// Registered unconditionally at top level rather than inside onInstalled:
// onInstalled only fires on an actual install/update, not on a normal
// browser restart, and Firefox's persistent background page does not
// persist previously-created menu items across a restart -- so an
// onInstalled-only registration disappears the first time Firefox restarts
// after install. Running this at top level re-registers the menu every
// time the background page loads (every Firefox startup; every Chrome MV3
// service-worker wake), which is exactly what's needed on Firefox and a
// harmless duplicate-id no-op on Chrome.
// Message types media.js owns. Listed here rather than in media.js so the
// Chrome build, which has no media.js, still recognises and answers them.
const MEDIA_MESSAGE_TYPES = new Set([
  "getDetectedStreams",
  "getMediaPageUrl",
  "downloadMedia",
]);

function registerContextMenu() {
  const create = () => browser.contextMenus.create(
    {
      id: "download-with-cove",
      title: "Download with Cove",
      // The media runtime adds the media targets when it loaded. A build
      // without it - or one whose import failed - offers links and images.
      contexts: ["link", "image"].concat(
        typeof CoveMedia !== "undefined" ? CoveMedia.contexts : []
      ),
    },
    () => {
      if (browser.runtime.lastError) {
        const message = browser.runtime.lastError.message || "";
        if (!/duplicate id/i.test(message)) {
          console.error("Cove: context menu create error:", browser.runtime.lastError);
        }
      } else {
        console.log("Cove: context menu registered");
      }
    }
  );

  // Chrome keeps menu items across service worker restarts and across an
  // extension update, and answers a second create() for the same id with a
  // duplicate-id error while leaving the installed item exactly as it was -
  // contexts included. Swallowing that error is therefore not the same as
  // registering: an install upgrading from the links-and-images build would
  // keep those two contexts for good and never acquire video and audio.
  // Clearing first is what makes the registration describe this build rather
  // than whichever one happened to install the item.
  //
  // Creation has to wait for the removal to finish, or the outstanding
  // removal takes the new item with it and the extension is left with no menu
  // at all. Which completion signal is available depends on the browser:
  // Chrome answers with a callback and only gained promise support for this
  // call in 123, which this manifest does not require; Firefox is
  // promise-only and validates its arguments. Take whichever arrives, and
  // create exactly once either way.
  let created = false;
  const createOnce = () => {
    if (created) return;
    created = true;
    create();
  };

  let pending;
  try {
    pending = browser.contextMenus.removeAll(createOnce);
  } catch (e) {
    // The callback was rejected, so nothing was removed and nothing was
    // scheduled. Ask again the way this browser wants to be asked.
    try {
      pending = browser.contextMenus.removeAll();
    } catch (e2) {
      // No usable removeAll at all: create as before. An existing item then
      // stays as it is, which the duplicate-id branch above already reports.
      createOnce();
      return;
    }
  }
  if (pending && typeof pending.then === "function") {
    pending.then(createOnce, createOnce);
  }
}

registerContextMenu();

browser.contextMenus.onClicked.addListener(async (info, tab) => {
  console.log("Cove: context menu clicked", info.menuItemId, info.linkUrl || info.srcUrl);
  if (info.menuItemId !== "download-with-cove") return;

  // The video and audio targets are new here, and a browser that publishes a
  // media-target policy opts into both halves of it. Firefox publishes none
  // and keeps exactly the behaviour it shipped with.
  const mediaAction = info.mediaType === "video" || info.mediaType === "audio";
  const mediaPolicy = (typeof CoveMediaCapability !== "undefined" &&
    CoveMediaCapability.rejectMediaTarget) || null;

  // First half: for a media action the element's own source is what was
  // selected. A link target otherwise wins - which is right for the link and
  // image targets this menu shipped with, and wrong for media, because a
  // player wrapped in a hyperlink would hand over the link's destination, a
  // page, in place of the media the user pointed at. An element with no
  // source of its own still follows its link.
  const target = (mediaPolicy && mediaAction)
    ? (info.srcUrl || info.linkUrl || "")
    : (info.linkUrl || info.srcUrl || "");

  // Second half: a media element's src is allowed to name a playlist
  // describing a stream rather than a file, and this build ships no stream
  // handling. Refusing here, before the address below is chosen, is what
  // stops a link on the same element, the page address, or a page fallback
  // from standing in for a media source that was just refused. Ordinary link
  // and image targets never reach this, and neither does a download the
  // browser itself started.
  if (mediaAction && mediaPolicy &&
      (mediaPolicy(info.srcUrl || "") || mediaPolicy(target))) {
    // Same silent outcome as any other target the menu cannot hand over, and
    // recorded so a support report can tell a refusal from nothing happening.
    diagRecord("extension.background", "request_failed", "WARNING",
               { reason: "unsupported", trigger: "context_menu" });
    return;
  }

  // A media srcUrl the browser cannot hand over directly - an MSE player's
  // blob: URL - is where the capability may name an alternative. Only
  // Firefox's does; on Chrome, and on any build with no media runtime, there
  // is no fallback and a non-http target is simply ignored rather than being
  // replaced by the page it sits on.

  const fallbackUrl = typeof CoveMedia !== "undefined"
    ? CoveMedia.pageFallbackUrl(tab, info)
    : "";
  const url = /^https?:\/\//i.test(target) ? target : fallbackUrl;
  if (!url) return;

  const cookieStr = await collectCookies(url);

  let filename = (fallbackUrl && url === fallbackUrl)
    ? CoveMedia.mediaFilename(tab, url)
    : null;
  if (!filename) {
    try {
      const pathname = new URL(url).pathname;
      const parts = pathname.split("/");
      const last = parts[parts.length - 1];
      if (last && last.includes(".")) filename = decodeURIComponent(last);
    } catch {}
  }

  const result = await sendNativeMessage({
    action: "download",
    url: url,
    filename: filename,
    referrer: info.pageUrl || "",
    cookies: cookieStr,
    userAgent: navigator.userAgent,
  });

  if (result && result.status === "ok") {
    showNotification("Download sent to Cove", filename || url);
  } else {
    // Same as above: a malformed reply can be null, and throwing here would
    // skip the browser fallback entirely - the one thing that must not fail.
    const reason = (result && result.message) || "no response";
    if (url === fallbackUrl) {
      // The browser would save the watch page's HTML, not the video.
      showNotification("Cove error", reason);
      return;
    }
    console.error("Cove: context menu send failed, falling back to browser", reason);
    try {
      markIntercepted(url);
      await browser.downloads.download({ url, filename: filename || undefined, saveAs: false });
      showNotification("Cove unavailable", "Downloading in browser instead");
    } catch (fallbackErr) {
      showNotification("Cove error", reason);
    }
  }
});

// ---- Keyboard shortcut ----

browser.commands.onCommand.addListener(async (command) => {
  if (command === "toggle-intercept") {
    await ensureSettings();  // toggle from the real value, not defaults
    await saveSettings({ ...settings, enabled: !settings.enabled });
    updateBadge();
    showNotification(
      "Cove Interception",
      settings.enabled ? "Download interception enabled" : "Download interception disabled"
    );
  }
});

// ---- Badge ----

// MV3 renamed browserAction -> action; fall back for MV2 Firefox.
const browserAction = browser.action || browser.browserAction;

// The single place the badge is painted. Everything that wants to change it
// goes through here, so precedence is decided once: a disabled extension says
// OFF no matter what else is happening. media.js used to write the badge
// itself, which erased that.
let badgeMediaCount = 0;

function renderBadge({ mediaCount } = {}) {
  if (mediaCount !== undefined) badgeMediaCount = mediaCount;
  if (!settings.enabled) {
    browserAction.setBadgeText({ text: "OFF" });
    browserAction.setBadgeBackgroundColor({ color: "#6b6b80" });
    return;
  }
  if (badgeMediaCount > 0) {
    browserAction.setBadgeText({ text: String(badgeMediaCount) });
    browserAction.setBadgeBackgroundColor({ color: "#50e6cf" });
    return;
  }
  browserAction.setBadgeText({ text: "" });
}

function updateBadge() {
  renderBadge();
}

// ---- Notifications ----

function showNotification(title, message) {
  browser.notifications.create({
    type: "basic",
    iconUrl: "icons/icon-96.png",
    title: title,
    message: message,
  });
}

// ---- Message handler for popup/options ----

browser.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "getSettings") {
    ensureSettings().then(() => sendResponse(settings));
    return true; // async: wait for settings to load before responding
  }
  if (msg.type === "saveSettings") {
    saveSettings(msg.settings).then(() => {
      updateBadge();
      sendResponse({ ok: true });
    });
    return true; // async
  }
  if (msg.type === "getStatus") {
    sendNativeMessage({ action: "status" }).then(sendResponse);
    return true; // async
  }
  if (msg.type === "ping") {
    sendNativeMessage({ action: "ping" }).then(sendResponse);
    return true;
  }
  // Media messages belong to the media runtime. Without it the popup's stream
  // list gets an empty answer and the pill does not exist to ask. Chrome loads
  // the runtime but no stream detector, so it answers that list empty too.
  if (MEDIA_MESSAGE_TYPES.has(msg.type)) {
    if (typeof CoveMedia === "undefined") {
      if (msg.type === "getDetectedStreams") sendResponse([]);
      else if (msg.type === "getMediaPageUrl") sendResponse({ url: "" });
      else sendResponse({
        ok: false,
        reason: "unsupported",
        error: "Video downloads are not available in this build",
      });
      return;
    }
    return CoveMedia.handleMessage(msg, sender, sendResponse);
  }
  // Content scripts and the popup cannot load diagnostics.js themselves (the
  // manifest lists one script per context), so they report through here.
  if (msg.type === "coveDiag") {
    diagRecord(msg.component, msg.event, msg.level, msg.fields, msg.requestId);
    sendResponse({ ok: true });
    return;
  }
  if (msg.type === "coveDiagReport") {
    // Wait for hydration: a report is only useful if it has the whole ring.
    diagReady.then(() => {
      sendResponse({ ok: true, text: coveDiag ? coveDiag.report() : "" });
    });
    return true;
  }
  if (msg.type === "coveDiagClear") {
    // Also gated, so a clear cannot be undone by a hydration still in flight.
    diagReady.then(() => {
      if (!coveDiag) {
        sendResponse({ ok: false });
        return;
      }
      Promise.resolve(coveDiag.clear()).then((ok) => sendResponse({ ok: !!ok }));
    });
    return true;
  }
  if (msg.type === "downloadStream") {
    if (typeof msg.url !== "string" || !/^https?:\/\//i.test(msg.url)) {
      sendResponse({ ok: false, error: "Unsupported stream URL" });
      return;
    }
    sendNativeMessage({
      action: "download",
      url: msg.url,
      filename: msg.filename || "",
      referrer: "",
      cookies: "",
      fileSize: 0,
      userAgent: navigator.userAgent,
    }).then((result) => {
      if (result && result.status === "ok") {
        sendResponse({ ok: true });
      } else {
        sendResponse({ ok: false, error: (result && result.message) || "Cove is unavailable" });
      }
    }).catch((e) => {
      sendResponse({ ok: false, error: (e && e.message) || "Cove is unavailable" });
    });
    return true;
  }
});


// ---- Init ----

settingsReady.then(updateBadge);

// Startup connectivity test. The reply body is never logged raw: it is a
// native-message payload, and those are exactly what must not be retained.
sendNativeMessage({ action: "ping" }).then((r) => {
  diagRecord("extension.background", "native_ping_result", "INFO", {
    status: (r && r.status) || "none",
    appVersion: (r && r.version) || "unknown",
  });
  // Carry the version into the report header too: an event buried in a
  // 300-line ring is not what a supporter reads first.
  if (r && r.status === "ok" && r.version) diagSetAppVersion(r.version);
}).catch(() => {
  diagRecord("extension.background", "native_ping_result", "WARNING", {
    status: "transport_error",
  });
});

