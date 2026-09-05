// Browser-neutral media mechanics: filename derivation, the pill's download
// handoff, and the message surface background.js consumes.
//
// Nothing here knows about any particular site. Everything that does - page
// extraction, playlist handling, stream observation - lives in media-sites.js
// and reaches this file only through the optional capability object described
// below. That split is what lets a bundle ship these mechanics without
// shipping site handling: this file is browser-agnostic, media-sites.js is
// not (see scripts/build_extension.py).
//
// Loaded before background.js so `CoveMedia` exists when background.js
// registers its context menu. Functions here call back into background.js
// globals (sendNativeMessage, markIntercepted, showNotification, diagRecord)
// at call time, by which point that script has been evaluated.

// Builds the CoveMedia surface. `capability` is optional; when omitted the
// global published by a site adapter is used if one loaded, and the neutral
// defaults apply when none did. Resolution is deferred to call time because
// the adapter is a separate script that may be evaluated after this one.
//
// A capability may provide:
//   sitePageUrl(value)          page address to download instead of the media
//   titleCleanup(title, url)    site-specific title rewrite, pre-sanitation
//   rejectExtension(ext)        true for an extension that must not be used
//   pageFallbackUrl(tab, info)  context-menu fallback for an unusable target
//   handleMessage(...)          extra message types, false when not its own
function buildCoveMedia(capability) {
  const sites = () => capability || globalThis.CoveMediaCapability || null;

  // The page address the site adapter designates for this address, when it
  // designates one. "" without an adapter.
  function sitePageUrl(value) {
    const adapter = sites();
    return (adapter && adapter.sitePageUrl && adapter.sitePageUrl(value)) || "";
  }

  function mediaFilename(tab, mediaUrl) {
    let title = (tab && tab.title ? tab.title : "").trim();

    const adapter = sites();
    if (adapter && adapter.titleCleanup) {
      title = adapter.titleCleanup(title, tab && tab.url);
    }

    title = title
      .replace(/[<>:"/\\|?*\0-\x1f]/g, " ")
      .replace(/\s+/g, " ")
      .replace(/[. ]+$/g, "")
      .trim()
      .slice(0, 180);

    let extension = ".mp4";
    try {
      const match = new URL(mediaUrl).pathname.match(/\.([a-z0-9]{2,5})$/i);
      const rejected = !!(adapter && adapter.rejectExtension &&
        adapter.rejectExtension(match && match[1]));
      if (match && !rejected) extension = `.${match[1]}`;
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

    const url = sitePageUrl(sender.tab && sender.tab.url) ||
      sitePageUrl(msg.pageUrl) || msg.url || "";
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

  return {
    // Extra context-menu targets. Without this script the menu is links and
    // images only.
    contexts: ["video", "audio"],

    mediaFilename,

    // Context-menu fallback for a target the browser cannot hand over
    // directly (an MSE player's blob: URL). Returns "" when nothing on this
    // page is a supported alternative, which is always the case without a
    // site adapter. background.js calls only this, so it needs no vocabulary
    // for what the adapter does behind it.
    pageFallbackUrl(tab, info) {
      const adapter = sites();
      return (adapter && adapter.pageFallbackUrl && adapter.pageFallbackUrl(tab, info)) || "";
    },

    // The media half of background.js's runtime.onMessage listener. Returns
    // the same value that listener must return: true to keep sendResponse
    // alive, undefined when it already answered, and false for "not mine".
    handleMessage(msg, sender, sendResponse) {
      const adapter = sites();
      if (adapter && adapter.handleMessage) {
        const handled = adapter.handleMessage(msg, sender, sendResponse);
        if (handled !== false) return handled;
      }
      if (msg.type === "downloadMedia") {
        handleMediaTabDownload(msg, sender).then(sendResponse);
        return true;
      }
      // The adapter owns the stream list and the page address. With none
      // loaded these are answered empty rather than left unanswered, which
      // would hang the caller waiting for a reply that never comes.
      if (msg.type === "getDetectedStreams") {
        sendResponse([]);
        return;
      }
      if (msg.type === "getMediaPageUrl") {
        sendResponse({ url: "" });
        return;
      }
      return false;
    },
  };
}

// ---- Surface consumed by background.js ----

const CoveMedia = buildCoveMedia();
