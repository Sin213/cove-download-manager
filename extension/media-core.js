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
//   rejectMediaTarget(url)      true for an address that must not be sent
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

    // A capability may refuse the address that was chosen. Chrome publishes
    // such a refusal because it ships no stream handling and a media element's
    // src is allowed to name a playlist; Firefox publishes none and this is
    // the neutral false for it, exactly as every other hook here defaults.
    //
    // Asked about the resolved address rather than msg.url, so it is the thing
    // actually about to be sent that was judged. Asked here, before anything
    // is marked, looked up or sent, so a refused address leaves no dedup mark,
    // no cookie read and no native request behind it. The refusal is not a
    // reason to look for a different address: it ends the request.
    const adapter = sites();
    const refused = !!(adapter && adapter.rejectMediaTarget &&
      adapter.rejectMediaTarget(url));

    if (refused || !/^https?:\/\//i.test(url)) {
      diagRecord("extension.native_bridge", "request_failed", "WARNING",
                 { reason: "unsupported" }, requestId);
      return { ok: false, reason: "unsupported", error: "Unsupported URL" };
    }

    // Claimed, not committed: while this handoff runs, a direct-file URL the
    // browser also starts downloading must not be intercepted as well. The
    // commit happens only if the handoff actually reaches Cove, below, so a
    // refusal or a failure leaves nothing behind - and takes nothing away from
    // another handoff for the same address.
    const dedupToken = claimIntercepted(url);

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
    };
    // Additive and optional: an older host ignores an unknown key.
    if (requestId) nativeMessage.requestId = requestId;

    // The admission check that let this request in ran before the cookie read
    // above, and that read is a suspension point: an exclusion the user commits
    // during it would otherwise be decided too late. Asked again here, the last
    // moment before anything leaves the browser, so the jar already in hand is
    // not sent either. Read through resolvePillPermission, which reads storage
    // rather than the cached settings, so the options page's committed change is
    // what answers. Same refusal the early check gives, for the same reason the
    // pill already renders it.
    // Read before the answer is asked for and compared after it arrives. The
    // answer is a snapshot, so "the snapshot says allowed" is not enough: a
    // write committed while it was in flight would otherwise be decided too
    // late again, one step further along. Any settings change inside that
    // window refuses - conservatively, since which setting changed cannot be
    // known to be irrelevant here.
    const generationBefore =
      typeof settingsGeneration === "number" ? settingsGeneration : null;
    if (typeof resolvePillPermission === "function" &&
        (!pillPermitted(await resolvePillPermission(sender)) ||
         (generationBefore !== null && settingsGeneration !== generationBefore))) {
      // This handoff is over and never reached Cove, so its claim goes. What
      // another handoff committed or claimed for the same address is untouched.
      releaseIntercepted(url, dedupToken);
      diagRecord("extension.native_bridge", "request_failed", "WARNING",
                 { reason: "unsupported" }, requestId);
      return { ok: false, reason: "unsupported",
               error: "Downloads are turned off for this site" };
    }

    // Asked last, for the same reason the exclusion above is asked twice.
    // Firefox's optional technicalAndInteraction consent governs the
    // user-agent and the user can revoke it at any moment, so resolving it
    // any earlier would leave a suspension point - the settings read above -
    // between the check and the send, and a revocation landing inside that
    // window would ship a user-agent the user had already withdrawn. Nothing
    // awaits between here and sendNativeMessage.
    Object.assign(nativeMessage, await userAgentField());

    const result = await sendNativeMessage(nativeMessage, requestId);

    if (result && result.status === "ok") {
      // It reached Cove: commit the address for the window, then drop the claim.
      // The commit is what protects it from here on, and it outlives this
      // request exactly as the old mark did.
      markIntercepted(url);
      releaseIntercepted(url, dedupToken);
      showNotification("Download sent to Cove", filename || url);
      return { ok: true };
    }
    // Nothing was committed, so releasing the claim leaves the address clear and
    // a manual retry is not blocked.
    releaseIntercepted(url, dedupToken);
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
