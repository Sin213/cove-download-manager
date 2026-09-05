// extension/content/media-sites.js
//
// Firefox-only site capability for the in-page pill. Publishes the hooks that
// content/media-tab.js calls when they are present: the page address to
// download instead of the media element, and the stream address a player
// exposes on its container.
//
// This file ships in the Firefox bundle only (see scripts/build_extension.py).
// Without it the shared pill contributes neither of those and works from the
// media element's own address alone.
//
// Listed before content/media-tab.js in the manifest, so the capability below
// exists by the time the pill reads it. Both scripts share the same extension
// isolated world.

(() => {
  "use strict";

  function isHttpUrl(u) {
    return typeof u === "string" && /^https?:\/\//i.test(u);
  }

  function extractorPageUrl() {
    try {
      const url = new URL(location.href);
      const host = url.hostname.toLowerCase().replace(/^www\./, "");
      if (host === "youtu.be" && url.pathname.length > 1) return url.href;
      if (!["youtube.com", "m.youtube.com", "music.youtube.com"].includes(host)) return "";
      if (url.pathname === "/watch" && url.searchParams.get("v")) return url.href;
      if (/^\/(?:shorts|live|embed)\/[^/]+/.test(url.pathname)) return url.href;
    } catch {}
    return "";
  }

  function embeddedStreamUrl(video) {
    // Reddit's embed exposes both DASH and HLS URLs in the player container,
    // but normally requests only DASH. Prefer HLS because Cove can download
    // and merge that stream directly.
    //
    // Ancestor-scoped on purpose. A document-wide lookup returns the first
    // player on the page, so on a feed every video resolved to the first
    // post's stream and downloaded the wrong video with no sign of error.
    const player = video.closest("[data-hls-url]");
    const hlsUrl = player ? player.getAttribute("data-hls-url") : "";
    return isHttpUrl(hlsUrl) ? hlsUrl : "";
  }

  globalThis.__coveMediaSites = {
    sitePageUrl: extractorPageUrl,
    embeddedStreamUrl,
    // The background's stream detection is part of this capability, so the
    // shared pill only exchanges stream messages when this adapter is loaded.
    usesDetectedStreams: true,
  };
})();
