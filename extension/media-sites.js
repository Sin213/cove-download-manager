// Firefox-only site capability: page-extractor addresses, site title rules,
// and HLS stream detection.
//
// This file ships in the Firefox bundle only. The Chrome Web Store rejected
// 1.3.5 for facilitating downloads of copyrighted media, so the Chrome bundle
// omits this script entirely (see scripts/build_extension.py) and the
// browser-neutral mechanics in media-core.js fall back to their site-agnostic
// defaults. Everything site-specific lives here so that exclusion is a
// whole-file decision rather than a set of scattered conditionals.
//
// Loaded after media-core.js and before background.js. media-core.js resolves
// the capability below at call time, so this ordering is enough; nothing here
// runs before CoveMedia exists.

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

// Site title rules, applied before the shared core sanitises the result.
function mediaTitleCleanup(title, tabUrl) {
  // old.reddit titles end in the subreddit name (for example
  // "AI could never : funny"). Keep only the post title.
  try {
    const pageUrl = new URL(tabUrl);
    const match = pageUrl.pathname.match(/^\/r\/([^/]+)\/comments\//i);
    if (match) {
      const subreddit = match[1].replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      title = title.replace(new RegExp(`\\s*:\\s*(?:r\\/)?${subreddit}\\s*$`, "i"), "");
    }
    if (/(^|\.)youtube\.com$/i.test(pageUrl.hostname)) {
      title = title.replace(/\s*-\s*YouTube\s*$/i, "");
    }
  } catch {}
  return title;
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
      // Never paint the badge directly. Disabled state has priority over a
      // media count, and only background.js knows whether interception is on;
      // writing here unconditionally replaced its "OFF" and made a disabled
      // extension look active. renderBadge is defined by background.js, which
      // always loads after this script.
      renderBadge({ mediaCount: streams.length });
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

// ---- Capability consumed by media-core.js ----

// Assigned onto globalThis rather than declared: a top-level `const` in a
// background script is not a global property, and media-core.js looks the
// capability up there.
globalThis.CoveMediaCapability = {
  sitePageUrl: extractorPageUrl,

  titleCleanup: mediaTitleCleanup,

  // A playlist is not the download's container format: the file Cove
  // produces from an M3U8 is an MP4, so the core's default extension is
  // kept instead.
  rejectExtension(ext) {
    return String(ext || "").toLowerCase() === "m3u8";
  },

  // Context-menu fallback for a target the browser cannot hand over
  // directly (an MSE player's blob: URL). Returns "" when this page has no
  // supported alternative.
  pageFallbackUrl(tab, info) {
    return extractorPageUrl(tab && tab.url) || extractorPageUrl(info && info.pageUrl);
  },

  // The site-specific half of the media message surface. Returns false for
  // anything this adapter does not own, which hands the message back to the
  // shared core.
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
    return false;
  },
};
