// Video handling: the in-page pill's downloads, page-extractor URLs, and
// HLS stream detection.
//
// This file ships in the Firefox bundle only. The Chrome Web Store rejected
// 1.3.5 for facilitating downloads of copyrighted media, so the Chrome
// bundle omits this script entirely and background.js degrades to links and
// images (see scripts/build_extension.py). Everything video-related lives
// here so that exclusion is a whole-file decision rather than a set of
// scattered conditionals.
//
// Loaded before background.js so `CoveMedia` exists when background.js
// registers its context menu. Functions here call back into background.js
// globals (sendNativeMessage, markIntercepted, showNotification, diagRecord)
// at call time, by which point that script has been evaluated.

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
  // Correlates this handoff with the pill click that started it and, further
  // down, with the native host and Cove itself.
  const requestId = (typeof CoveDiag !== "undefined" &&
    CoveDiag.normalizeRequestId(msg.requestId)) || null;
  diagRecord("extension.background", "request_received", "INFO",
             { kind: "media" }, requestId);

  const url = extractorPageUrl(sender.tab && sender.tab.url) ||
    extractorPageUrl(msg.pageUrl) || msg.url || "";
  if (!/^https?:\/\//i.test(url)) {
    diagRecord("extension.native_bridge", "request_failed", "WARNING",
               { reason: "unsupported" }, requestId);
    return { ok: false, reason: "unsupported", error: "Unsupported URL" };
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

  const nativeMessage = {
    action: "download",
    url: url,
    filename: filename,
    referrer: referrer,
    cookies: cookieStr,
    fileSize: 0,
    userAgent: navigator.userAgent,
  };
  // Additive and optional: an older host ignores an unknown key.
  if (requestId) nativeMessage.requestId = requestId;
  const result = await sendNativeMessage(nativeMessage, requestId);

  if (result && result.status === "ok") {
    showNotification("Download sent to Cove", filename || url);
    return { ok: true };
  }
  // Clear the dedup mark so a manual retry is not blocked.
  recentIntercepted.delete(url);
  // "Cove is not available" is the native host's fixed sentence for a request
  // no running Cove accepted. Together with a transport failure that is the
  // one case the user can act on, so it is reported as such instead of being
  // folded into a generic failure that reads as a problem with the media.
  const unavailable = (result && result.transport === "error") ||
    (result && result.message === "Cove is not available");
  diagRecord("extension.native_bridge", "request_failed", "WARNING", {
    reason: result && result.transport === "error"
      ? "transport_error"
      : (unavailable ? "app_unavailable" : "gui_rejected"),
  }, requestId);
  return {
    ok: false,
    reason: unavailable ? "unavailable" : "failed",
    error: (result && result.message) || "Native host error",
  };
}

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

// ---- Surface consumed by background.js ----

const CoveMedia = {
  // Extra context-menu targets. Without this script the menu is links and
  // images only.
  contexts: ["video", "audio"],

  extractorPageUrl,
  mediaFilename,

  // Context-menu fallback for a target the browser cannot hand over
  // directly (an MSE player's blob: URL). Returns "" when this page has no
  // supported alternative. background.js calls only this, so it needs no
  // vocabulary for what media.js does behind it.
  pageFallbackUrl(tab, info) {
    return extractorPageUrl(tab && tab.url) || extractorPageUrl(info && info.pageUrl);
  },

  // The media half of background.js's runtime.onMessage listener. Returns
  // the same value that listener must return: true to keep sendResponse
  // alive, undefined when it already answered, and false for "not mine".
  handleMessage(msg, sender, sendResponse) {
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
    if (msg.type === "getMediaPageUrl") {
      sendResponse({ url: extractorPageUrl(sender.tab && sender.tab.url) });
      return;
    }
    if (msg.type === "downloadMedia") {
      handleMediaTabDownload(msg, sender).then(sendResponse);
      return true;
    }
    return false;
  },
};
