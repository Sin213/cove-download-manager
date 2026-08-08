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
}

async function saveInterceptedIds() {
  await interceptedIdsReady;
  try {
    const store = browser.storage.session || browser.storage.local;
    store.set({ _interceptedIds: [...interceptedIds] }).catch(() => {});
  } catch {}
}

const interceptedIdsReady = loadInterceptedIds();

browser.downloads.onChanged.addListener(async (delta) => {
  await interceptedIdsReady;
  if (!interceptedIds.has(delta.id)) return;
  const state = delta.state && delta.state.current;
  if (state === "interrupted" || state === "complete") {
    browser.downloads.erase({ id: delta.id }).catch(() => {});
    interceptedIds.delete(delta.id);
    saveInterceptedIds();
  }
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
  browser.contextMenus.create(
    {
      id: "download-with-cove",
      title: "Download with Cove",
      // media.js adds the media targets when it is present. The Chrome
      // bundle omits that script, so the menu there is links and images.
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
}

registerContextMenu();

browser.contextMenus.onClicked.addListener(async (info, tab) => {
  console.log("Cove: context menu clicked", info.menuItemId, info.linkUrl || info.srcUrl);
  if (info.menuItemId !== "download-with-cove") return;

  // A link target is what the user pointed at, so it always wins. A media
  // srcUrl does not: MSE players expose a blob: URL that nothing downstream
  // can fetch, so fall back to the extractor page URL the pill already sends
  // for the same video. Without media.js there is no fallback and a
  // non-http target is simply ignored.
  const target = info.linkUrl || info.srcUrl || "";
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

function updateBadge() {
  if (!settings.enabled) {
    browserAction.setBadgeText({ text: "OFF" });
    browserAction.setBadgeBackgroundColor({ color: "#6b6b80" });
  } else {
    browserAction.setBadgeText({ text: "" });
  }
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
  // Video messages are media.js's. Without that script (Chrome) the popup's
  // stream list gets an empty answer and the pill does not exist to ask.
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

