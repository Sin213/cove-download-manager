// extension/background.js

// Cross-browser shim: Firefox exposes `browser` (promise-based); Chromium
// exposes `chrome`. Chrome MV3 APIs used here are all promise-based, so the
// same code runs on both.
const browser = globalThis.browser || globalThis.chrome;

const HOST_NAME = "cove_download_manager";

function sendNativeMessage(msg) {
  return browser.runtime.sendNativeMessage(HOST_NAME, msg).catch((err) => {
    console.error("Cove native messaging error:", err);
    return { status: "error", message: err.message || String(err) };
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

async function interceptDownload(downloadItem) {
  // Mark synchronously to block concurrent same-URL events before any await.
  markIntercepted(downloadItem.url);

  const dlId = downloadItem.id;

  // Gather cookies while the browser download is still running.
  let cookieStr = "";
  try {
    const cookies = await browser.cookies.getAll({ url: downloadItem.url });
    cookieStr = cookies.map((c) => `${c.name}=${c.value}`).join("; ");
  } catch {
    // No cookies available.
  }

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
function registerContextMenu() {
  browser.contextMenus.create(
    {
      id: "download-with-cove",
      title: "Download with Cove",
      contexts: ["link", "image", "video", "audio"],
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

  const url = info.linkUrl || info.srcUrl;
  if (!url) return;

  let cookieStr = "";
  try {
    const cookies = await browser.cookies.getAll({ url });
    cookieStr = cookies.map((c) => `${c.name}=${c.value}`).join("; ");
  } catch {}

  let filename = null;
  try {
    const pathname = new URL(url).pathname;
    const parts = pathname.split("/");
    const last = parts[parts.length - 1];
    if (last && last.includes(".")) filename = decodeURIComponent(last);
  } catch {}

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
  if (msg.type === "getDetectedStreams") {
    // Content scripts have sender.tab; the popup does not and gets the
    // active tab's streams as before.
    if (sender.tab && typeof sender.tab.id === "number") {
      sendResponse(detectedStreams.get(sender.tab.id) || []);
      return;
    }
    browser.tabs.query({ active: true, currentWindow: true }).then((tabs) => {
      const tabId = tabs[0] ? tabs[0].id : -1;
      sendResponse(detectedStreams.get(tabId) || []);
    });
    return true;
  }
  if (msg.type === "getExtractorPageUrl") {
    sendResponse({ url: extractorPageUrl(sender.tab && sender.tab.url) });
    return;
  }
  if (msg.type === "downloadMedia") {
    handleMediaTabDownload(msg, sender).then(sendResponse);
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

// ---- Media-tab (in-page pill) download ----

function extractorPageUrl(value) {
  try {
    const url = new URL(value || "");
    const host = url.hostname.toLowerCase().replace(/^www\./, "");
    if (host === "youtu.be" && url.pathname.length > 1) return url.href;
    if (!["youtube.com", "m.youtube.com", "music.youtube.com"].includes(host)) return "";
    if (url.pathname === "/watch" && url.searchParams.get("v")) return url.href;
    if (/^\/(?:shorts|live|embed)\/[^/]+/.test(url.pathname)) return url.href;
  } catch {}
  return "";
}

function mediaFilename(tab, mediaUrl) {
  let title = (tab && tab.title ? tab.title : "").trim();

  // old.reddit titles end in the subreddit name (for example
  // "AI could never : funny"). Keep only the post title.
  try {
    const pageUrl = new URL(tab.url);
    const match = pageUrl.pathname.match(/^\/r\/([^/]+)\/comments\//i);
    if (match) {
      const subreddit = match[1].replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      title = title.replace(new RegExp(`\\s*:\\s*(?:r\\/)?${subreddit}\\s*$`, "i"), "");
    }
    if (/(^|\.)youtube\.com$/i.test(pageUrl.hostname)) {
      title = title.replace(/\s*-\s*YouTube\s*$/i, "");
    }
  } catch {}

  title = title
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, " ")
    .replace(/\s+/g, " ")
    .replace(/[. ]+$/g, "")
    .trim()
    .slice(0, 180);

  let extension = ".mp4";
  try {
    const match = new URL(mediaUrl).pathname.match(/\.([a-z0-9]{2,5})$/i);
    if (match && match[1].toLowerCase() !== "m3u8") extension = `.${match[1]}`;
  } catch {}

  return title ? `${title}${extension}` : null;
}

// Explicit user click on the in-page Cove pill. Routes through the same
// native "download" action as interception and the context menu.
async function handleMediaTabDownload(msg, sender) {
  const url = extractorPageUrl(sender.tab && sender.tab.url) ||
    extractorPageUrl(msg.pageUrl) || msg.url || "";
  if (!/^https?:\/\//i.test(url)) {
    return { ok: false, error: "Unsupported URL" };
  }

  // Same dedup pattern as interception: mark before sending so a direct-file
  // URL the browser also starts downloading is not intercepted twice.
  markIntercepted(url);

  let cookieStr = "";
  try {
    const cookies = await browser.cookies.getAll({ url });
    cookieStr = cookies.map((c) => `${c.name}=${c.value}`).join("; ");
  } catch {}

  let filename = mediaFilename(sender.tab, url);
  if (!filename) {
    try {
      const pathname = new URL(url).pathname;
      const last = pathname.split("/").pop();
      if (last && last.includes(".")) filename = decodeURIComponent(last);
    } catch {}
  }

  const referrer = msg.pageUrl || (sender.tab && sender.tab.url) || "";

  const result = await sendNativeMessage({
    action: "download",
    url: url,
    filename: filename,
    referrer: referrer,
    cookies: cookieStr,
    fileSize: 0,
    userAgent: navigator.userAgent,
  });

  if (result && result.status === "ok") {
    showNotification("Download sent to Cove", filename || url);
    return { ok: true };
  }
  // Clear the dedup mark so a manual retry is not blocked.
  recentIntercepted.delete(url);
  return { ok: false, error: result.message || "Native host error" };
}

// ---- Init ----

settingsReady.then(updateBadge);

// Startup connectivity test
sendNativeMessage({ action: "ping" }).then((r) => {
  console.log("Cove startup ping:", JSON.stringify(r));
}).catch((e) => {
  console.error("Cove startup ping FAILED:", e);
});

// ---- HLS/M3U8 stream detection ----

const HLS_CONTENT_TYPES = [
  "application/vnd.apple.mpegurl",
  "application/x-mpegurl",
  "audio/mpegurl",
  "audio/x-mpegurl",
];

const detectedStreams = new Map();

function isHlsResponse(details) {
  const url = details.url || "";
  if (url.split("?")[0].toLowerCase().endsWith(".m3u8")) return true;
  const headers = details.responseHeaders || [];
  for (const h of headers) {
    if (h.name.toLowerCase() === "content-type") {
      const ct = (h.value || "").toLowerCase().split(";")[0].trim();
      if (HLS_CONTENT_TYPES.includes(ct)) return true;
    }
  }
  return false;
}

if (browser.webRequest) {
  browser.webRequest.onHeadersReceived.addListener(
    (details) => {
      if (!isHlsResponse(details)) return;
      const tabId = details.tabId;
      if (tabId < 0) return;
      if (!detectedStreams.has(tabId)) {
        detectedStreams.set(tabId, []);
      }
      const streams = detectedStreams.get(tabId);
      if (streams.some((s) => s.url === details.url)) return;
      // Keep only the first M3U8 per hostname. HLS quality variants come
      // from the same CDN, so this filters them out while preserving
      // genuinely different streams from different sources.
      try {
        const host = new URL(details.url).hostname;
        if (streams.some((s) => new URL(s.url).hostname === host)) return;
      } catch {}
      streams.push({
        url: details.url,
        type: "m3u8",
        timestamp: Date.now(),
      });
      updateStreamBadge(tabId);
      // Push to the tab's content script (media-tab pill). Fails harmlessly
      // when no content script is listening.
      try {
        browser.tabs
          .sendMessage(tabId, { type: "coveStreamsUpdated", streams })
          .catch(() => {});
      } catch {}
    },
    { urls: ["<all_urls>"] },
    ["responseHeaders"]
  );
}

function updateStreamBadge(tabId) {
  browser.tabs.query({ active: true, currentWindow: true }).then((tabs) => {
    if (tabs[0] && tabs[0].id === tabId) {
      const streams = detectedStreams.get(tabId) || [];
      const count = streams.length;
      const api = browser.browserAction || browser.action;
      api.setBadgeText({ text: count > 0 ? String(count) : "" });
      api.setBadgeBackgroundColor({ color: "#50e6cf" });
    }
  });
}

browser.tabs.onRemoved.addListener((tabId) => {
  detectedStreams.delete(tabId);
});

browser.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.url) {
    detectedStreams.delete(tabId);
    updateStreamBadge(tabId);
  }
});

browser.tabs.onActivated.addListener(({ tabId }) => {
  updateStreamBadge(tabId);
});
