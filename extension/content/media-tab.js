// extension/content/media-tab.js
//
// IDM-style floating "Download with Cove" pill anchored to the actively
// playing <video>. Appears automatically when a qualifying video starts
// playing: every discovered <video> gets direct play/playing/pause/ended
// listeners, and child-list observers attach them to videos inserted or
// replaced later, including videos in open shadow roots (e.g. Reddit's
// dynamic player). Hover remains a secondary
// convenience. Inert on pages without qualifying video: no DOM is created
// until a video with a usable URL becomes the active target. Uses Shadow
// DOM for isolation; never injects page-context scripts and never
// auto-downloads.

(() => {
  "use strict";

  const browser = globalThis.browser || globalThis.chrome;
  if (!browser || !browser.runtime || !browser.runtime.id) return;

  const HIDE_DELAY_MS = 500;
  // Must outlast the background's 5s dedup window so a manual retry after
  // the window still produces a fresh native request.
  const SENT_RESET_MS = 6000;
  const PILL_GAP_PX = 8;

  // HLS/M3U8 streams the background detected for this tab (Firefox only;
  // Chrome has no webRequest detection and this stays empty).
  let detectedStreams = [];

  // Whether the in-page pill is allowed to show. Defaults to true so the
  // pill behaves as before until settings are read, and stays true if the
  // read fails (fail open, matching current shipped behavior).
  let pillEnabled = true;

  let host = null;
  let pill = null;
  let label = null;
  let hideTimer = null;
  let resetTimer = null;
  let currentUrl = "";
  let downloadPending = false;
  // The <video> the pill is currently anchored to. Playback is the
  // authoritative trigger; hover only fills in when nothing is playing.
  let activeVideo = null;
  let resizeObserver = null;
  // Most recently observed video to fire a genuine play/playing event, kept
  // even if activation didn't happen (e.g. pill was disabled at the time).
  // Used to break ties when a bounded scan later finds it still playing.
  let lastKnownPlayingVideo = null;
  let scanTimers = [];
  let streamRefreshTimers = [];
  const sentUrls = new Map(); // url -> timestamp of last send

  // ---- URL selection ----

  function isHttpUrl(u) {
    return typeof u === "string" && /^https?:\/\//i.test(u);
  }

  function isDrmProtected(video) {
    // EME-protected media fails closed: no pill.
    return !!video.mediaKeys;
  }

  function embeddedStreamUrl(video) {
    // Reddit's embed exposes both DASH and HLS URLs in the player container,
    // but normally requests only DASH. Prefer HLS because Cove can download
    // and merge that stream directly.
    const player =
      video.closest("[data-hls-url]") || document.querySelector("[data-hls-url]");
    const hlsUrl = player ? player.getAttribute("data-hls-url") : "";
    return isHttpUrl(hlsUrl) ? hlsUrl : "";
  }

  function candidateUrl(video) {
    if (isDrmProtected(video)) return "";
    const src = video.currentSrc || video.getAttribute("src") || "";
    if (isHttpUrl(src)) return src;
    const source = video.querySelector("source[src]");
    if (source && isHttpUrl(source.src)) return source.src;
    const embeddedUrl = embeddedStreamUrl(video);
    if (embeddedUrl) return embeddedUrl;
    // blob:/data:/MSE video: use an HLS stream detected for this tab. This
    // must work in subframes too: Reddit's direct-post player can put the
    // actual playing video in an iframe while the network stream remains
    // tab-scoped in the background.
    if (detectedStreams.length > 0 && isHttpUrl(detectedStreams[0].url)) {
      return detectedStreams[0].url;
    }
    return "";
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

  function videoUrl(video) {
    return extractorPageUrl() || candidateUrl(video);
  }

  function isCurrentlyPlaying(video) {
    return !!video && !video.paused && !video.ended;
  }

  // ---- Pill UI (created lazily, Shadow DOM) ----

  function ensurePill() {
    if (host) return;
    host = document.createElement("div");
    host.className = "cove-media-tab-host";
    const shadow = host.attachShadow({ mode: "open" });

    const style = document.createElement("style");
    style.textContent = [
      ":host { all: initial; }",
      ".cove-pill {",
      "  display: flex; align-items: center; gap: 6px;",
      "  padding: 5px 12px; border-radius: 999px;",
      "  background: #1b1b26; color: #50e6cf;",
      "  border: 1px solid #50e6cf;",
      "  font: 600 12px/1.2 system-ui, sans-serif;",
      "  cursor: pointer; user-select: none;",
      "  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.4);",
      "}",
      ".cove-pill:hover { background: #24243a; }",
      ".cove-pill.cove-sent {",
      "  color: #9a9ab0; border-color: #9a9ab0; cursor: default;",
      "}",
      ".cove-pill.cove-error {",
      "  color: #e66a6a; border-color: #e66a6a;",
      "}",
    ].join("\n");
    shadow.appendChild(style);

    pill = document.createElement("div");
    pill.className = "cove-pill";
    pill.setAttribute("role", "button");
    pill.title = "Download with Cove";

    label = document.createElement("span");
    label.textContent = "Download with Cove";
    pill.appendChild(label);
    shadow.appendChild(pill);

    pill.addEventListener("click", onPillClick);
    host.addEventListener("mouseenter", cancelHide);
    host.addEventListener("mouseleave", scheduleHide);

    (document.body || document.documentElement).appendChild(host);
  }

  // Error labels name the side that actually failed. The pill cannot tell
  // whether a video is playable, so it must never say the video is at fault
  // for what is almost always a closed Cove.
  const ERROR_LABELS = {
    unavailable: "Cove is not running",
    unsupported: "No video found",
  };

  function setPillState(state, reason) {
    if (!pill) return;
    pill.classList.remove("cove-sent", "cove-error");
    if (state === "sent") {
      pill.classList.add("cove-sent");
      label.textContent = "Sent to Cove";
    } else if (state === "error") {
      pill.classList.add("cove-error");
      label.textContent = ERROR_LABELS[reason] || "Download failed";
    } else if (state === "detecting") {
      label.textContent = "Finding video…";
    } else {
      label.textContent = "Download with Cove";
    }
  }

  function refreshPillState(url) {
    const last = sentUrls.get(url);
    if (last && Date.now() - last < SENT_RESET_MS) {
      setPillState("sent");
    } else {
      setPillState("ready");
    }
  }

  // Positions the host directly above the video, right-aligned to its
  // top-right edge. Falls back to just inside the video's own top-right
  // corner when "above" would clip outside the viewport. An invalid,
  // zero-size, or detached rect returns false so the caller hides the pill
  // instead of guessing a fixed viewport-corner position.
  function positionPill(video) {
    if (!video || !video.isConnected) return false;
    const rect = video.getBoundingClientRect();
    if (rect.width < 80 || rect.height < 60) return false;

    ensurePill();
    // Measure the pill's own footprint before placing it so the "would it
    // clip above the viewport" check is accurate. visibility:hidden keeps
    // this invisible to the user while still forcing a real layout.
    host.style.display = "block";
    host.style.visibility = "hidden";
    const pillRect = host.getBoundingClientRect();
    const pillHeight = pillRect.height || 30;
    const pillWidth = pillRect.width || 160;

    let top = rect.top - pillHeight - PILL_GAP_PX;
    if (top < 4) {
      // Clipped above the viewport: sit just inside the video's own
      // top-right corner instead of jumping to an unrelated page corner.
      top = rect.top + PILL_GAP_PX;
    }
    top = Math.max(4, Math.min(top, window.innerHeight - pillHeight - 4));

    let right = Math.max(4, window.innerWidth - rect.right);
    const maxRight = window.innerWidth - pillWidth - 4;
    if (right > maxRight) right = Math.max(4, maxRight);

    host.style.top = top + "px";
    host.style.right = right + "px";
    host.style.visibility = "visible";
    return true;
  }

  // Makes `video` the active target and shows the pill positioned above it.
  // Playback can activate the pill before a blob/MSE stream URL is detected.
  function activateVideo(video, url) {
    if (!pillEnabled) return;
    activeVideo = video;
    currentUrl = url;
    refreshPillState(url);
    if (!positionPill(video)) {
      deactivateVideo();
      return;
    }
    watchActiveVideo(video);
    cancelHide();
  }

  function deactivateVideo(force = false) {
    // YouTube may pause or replace its <video> while a click is being
    // handled. Keep the pill and target stable until the background replies.
    if (downloadPending && !force) return;
    activeVideo = null;
    watchActiveVideo(null);
    hidePill();
  }

  // Reposition the pill when the active video's own layout changes (player
  // chrome resizing, fullscreen toggles, etc.), scoped to just that element
  // rather than a whole-document observer.
  function watchActiveVideo(video) {
    if (resizeObserver) {
      resizeObserver.disconnect();
      resizeObserver = null;
    }
    if (!video || typeof ResizeObserver === "undefined") return;
    resizeObserver = new ResizeObserver(() => reposition());
    resizeObserver.observe(video);
  }

  function reposition() {
    if (!activeVideo || !host || host.style.display === "none") return;
    if (!positionPill(activeVideo)) deactivateVideo();
  }

  function hidePill() {
    if (host) host.style.display = "none";
    currentUrl = "";
  }

  function scheduleHide() {
    cancelHide();
    hideTimer = setTimeout(() => {
      // A pill anchored to a still-playing video is not hover-dismissed;
      // only a hover-only pill (nothing actively playing) times out.
      if (activeVideo && isCurrentlyPlaying(activeVideo)) return;
      deactivateVideo();
    }, HIDE_DELAY_MS);
  }

  function cancelHide() {
    if (hideTimer) {
      clearTimeout(hideTimer);
      hideTimer = null;
    }
  }

  // ---- Click -> one explicit download request ----

  async function resolveCurrentMediaUrl() {
    const extractorUrl = extractorPageUrl();
    if (extractorUrl) return extractorUrl;
    try {
      const response = await Promise.resolve(
        browser.runtime.sendMessage({ type: "getExtractorPageUrl" })
      );
      if (response && isHttpUrl(response.url)) return response.url;
    } catch {}
    for (const delay of [0, 250, 750]) {
      if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
      const directUrl = activeVideo ? candidateUrl(activeVideo) : "";
      if (directUrl) return directUrl;
      try {
        const streams = await Promise.resolve(
          browser.runtime.sendMessage({ type: "getDetectedStreams" })
        );
        if (Array.isArray(streams)) detectedStreams = streams;
      } catch {
        // The final empty result is handled by the caller.
      }
      const detectedUrl = activeVideo ? candidateUrl(activeVideo) : "";
      if (detectedUrl) return detectedUrl;
    }
    return "";
  }

  async function onPillClick() {
    if (!pillEnabled || downloadPending) return;
    downloadPending = true;
    cancelHide();
    setPillState("detecting");

    // Extractor-backed sites are downloaded from their page URL. Do not wait
    // for a transient media/blob URL: YouTube frequently replaces that media
    // element while its controls are being used.
    const pageUrl = extractorPageUrl() || location.href;
    const url = extractorPageUrl() || currentUrl || candidateUrl(activeVideo) || pageUrl;
    currentUrl = url;

    try {
      const last = sentUrls.get(url);
      if (last && Date.now() - last < SENT_RESET_MS) {
        setPillState("sent");
        return;
      }

      const resp = await Promise.resolve(
        browser.runtime.sendMessage({
          type: "downloadMedia",
          url,
          pageUrl,
        })
      );
      if (!resp || resp.ok !== true) {
        sentUrls.delete(url);
        // No reply at all means the background script never answered, which
        // is the same practical outcome as an unreachable Cove.
        setPillState("error", resp ? resp.reason : "unavailable");
        return;
      }

      sentUrls.set(url, Date.now());
      setPillState("sent");
      if (resetTimer) clearTimeout(resetTimer);
      resetTimer = setTimeout(() => {
        if (currentUrl === url) setPillState("ready");
      }, SENT_RESET_MS);
    } catch {
      sentUrls.delete(url);
      // sendMessage itself threw: the background script is gone, so Cove
      // could not have been reached either.
      setPillState("error", "unavailable");
    } finally {
      downloadPending = false;
    }
  }

  // ---- Playback detection (primary trigger, direct per-video listeners) ----
  //
  // Manual testing on Reddit's dynamic player showed capture-phase
  // document-level play/playing listeners alone are not reliably reaching
  // this script for every video (player re-creation/replacement timing).
  // Direct listeners attached to each discovered <video> element are used
  // instead: strictly more reliable since they don't depend on event timing
  // relative to a document-level listener, and each is attached at most
  // once (WeakSet-guarded). A narrow MutationObserver (video-only,
  // childList+subtree, no attribute observation) discovers newly
  // inserted/replaced videos so they get listeners too, without polling or
  // scanning the whole document on every mutation.

  function onVideoPlaying(e) {
    const video = e.target;
    if (!(video instanceof HTMLVideoElement)) return;
    if (isCurrentlyPlaying(video)) lastKnownPlayingVideo = video;
    if (!pillEnabled) return;
    if (!isCurrentlyPlaying(video)) return;
    scheduleDetectedStreamRefreshes();
    const url = videoUrl(video);
    activateVideo(video, url);
  }

  function onVideoStopped(e) {
    const video = e.target;
    if (video !== activeVideo) return;
    // Hand off to another currently-playing qualifying video if one exists
    // on the page (e.g. a feed where the next post auto-plays); otherwise
    // hide.
    for (const v of knownVideos) {
      if (v === video || !isCurrentlyPlaying(v)) continue;
      if (isDrmProtected(v)) continue;
      activateVideo(v, videoUrl(v));
      return;
    }
    deactivateVideo();
  }

  // Videos that already have direct listeners attached. Prevents duplicate
  // registration (and duplicate event handling) for a video discovered
  // multiple times (initial scan, then again via a MutationObserver
  // mutation record covering an ancestor of an already-registered video).
  const registeredVideos = new WeakSet();
  const knownVideos = new Set();
  const observedRoots = new WeakSet();

  function attachVideoListeners(video) {
    if (registeredVideos.has(video)) return;
    registeredVideos.add(video);
    knownVideos.add(video);
    video.addEventListener("play", onVideoPlaying);
    video.addEventListener("playing", onVideoPlaying);
    video.addEventListener("pause", onVideoStopped);
    video.addEventListener("ended", onVideoStopped);
    // A replaced/reset player (src cleared or swapped) without a pause/end
    // in between should still relinquish the pill if it was the active
    // target.
    video.addEventListener("emptied", onVideoStopped);
    // The video may already be playing at the moment we discover it:
    // Reddit's player can start playback in the same tick it's
    // created/replaced, before this listener exists to observe a
    // play/playing event for it. Treat "already playing at registration"
    // as equivalent to a play event.
    if (pillEnabled && isCurrentlyPlaying(video)) {
      lastKnownPlayingVideo = video;
      if (!isDrmProtected(video)) activateVideo(video, videoUrl(video));
    }
  }

  function observeVideoRoot(root) {
    if (!root || observedRoots.has(root)) return;
    observedRoots.add(root);
    videoObserver.observe(root, { childList: true, subtree: true });
    registerVideosWithin(root);
  }

  // Registers videos and open shadow roots in the added subtree. Reddit can
  // mount its player below a custom element, where document queries and
  // retargeted hover events cannot see the actual <video>.
  function registerVideosWithin(node) {
    if (!node || (node.nodeType !== 1 && node.nodeType !== 11)) return;
    if (node.tagName === "VIDEO") attachVideoListeners(node);
    if (node.shadowRoot) observeVideoRoot(node.shadowRoot);
    if (typeof node.querySelectorAll === "function") {
      for (const v of node.querySelectorAll("video")) attachVideoListeners(v);
      for (const element of node.querySelectorAll("*")) {
        if (element.shadowRoot) observeVideoRoot(element.shadowRoot);
      }
    }
  }

  // Observe only child-list changes. Each mutation is scoped to its added or
  // removed subtree, including any open shadow roots found there.
  const videoObserver = new MutationObserver((mutations) => {
    for (const m of mutations) {
      for (const node of m.addedNodes) registerVideosWithin(node);
      for (const node of m.removedNodes) {
        if (node.nodeType !== 1) continue;
        const removedActive =
          node === activeVideo ||
          (activeVideo && typeof node.contains === "function" && node.contains(activeVideo));
        if (removedActive) deactivateVideo();
      }
      if (activeVideo && !activeVideo.isConnected) deactivateVideo();
    }
  });
  observeVideoRoot(document.documentElement);

  // ---- Already-playing detection (extra safety net) ----
  //
  // Bounded retry scans layered on top of the direct-listener/observer
  // mechanism above, for any video the observer's childList-only scope
  // might miss (e.g. a video swapped in place without a childList mutation
  // reaching document - attribute-only src changes are still caught by the
  // direct 'play'/'playing' listeners already attached to that element, so
  // this is a defense-in-depth fallback, not the primary mechanism).

  function findAlreadyPlayingVideo() {
    if (
      lastKnownPlayingVideo &&
      lastKnownPlayingVideo.isConnected &&
      isCurrentlyPlaying(lastKnownPlayingVideo)
    ) {
      const rect = lastKnownPlayingVideo.getBoundingClientRect();
      if (rect.width >= 80 && rect.height >= 60) {
        if (!isDrmProtected(lastKnownPlayingVideo)) {
          return { video: lastKnownPlayingVideo, url: videoUrl(lastKnownPlayingVideo) };
        }
      }
    }
    // No usable last-known target: deterministically pick the largest
    // visible currently-playing qualifying video. Never a thumbnail/poster
    // (readyState gate) or an offscreen/zero-size element (rect gate).
    let best = null;
    let bestArea = 0;
    for (const video of knownVideos) {
      if (!video.isConnected) {
        knownVideos.delete(video);
        continue;
      }
      if (!isCurrentlyPlaying(video)) continue;
      if (isDrmProtected(video)) continue;
      if (video.readyState < 2) continue; // below HAVE_CURRENT_DATA
      const rect = video.getBoundingClientRect();
      if (rect.width < 80 || rect.height < 60) continue;
      const url = videoUrl(video);
      const area = rect.width * rect.height;
      if (area > bestArea) {
        bestArea = area;
        best = { video, url };
      }
    }
    return best;
  }

  function scanForActiveVideo() {
    if (!pillEnabled) return;
    if (activeVideo && isCurrentlyPlaying(activeVideo)) {
      const url = videoUrl(activeVideo);
      if (url && url !== currentUrl) {
        currentUrl = url;
        refreshPillState(url);
      }
      return;
    }
    const found = findAlreadyPlayingVideo();
    if (found) activateVideo(found.video, found.url);
  }

  function clearActiveVideoScans() {
    for (const t of scanTimers) clearTimeout(t);
    scanTimers = [];
  }

  // A short bounded retry sequence (not a permanent poll) to catch a video
  // that starts playing, or gets replaced/re-mounted, in the brief window
  // around content-script/settings initialization.
  function scheduleActiveVideoScans() {
    clearActiveVideoScans();
    scanForActiveVideo();
    for (const delay of [300, 700, 1500]) {
      scanTimers.push(setTimeout(scanForActiveVideo, delay));
    }
  }

  function refreshDetectedStreams() {
    try {
      Promise.resolve(browser.runtime.sendMessage({ type: "getDetectedStreams" }))
        .then((streams) => {
          if (!Array.isArray(streams)) return;
          detectedStreams = streams;
          scanForActiveVideo();
        })
        .catch(() => {});
    } catch {
      // Background unavailable; direct-src videos still work.
    }
  }

  function scheduleDetectedStreamRefreshes() {
    for (const timer of streamRefreshTimers) clearTimeout(timer);
    streamRefreshTimers = [];
    refreshDetectedStreams();
    for (const delay of [250, 750, 1500, 3000]) {
      streamRefreshTimers.push(setTimeout(refreshDetectedStreams, delay));
    }
  }

  window.addEventListener("scroll", reposition, { capture: true, passive: true });
  window.addEventListener("resize", reposition);

  // ---- Hover wiring (secondary convenience, no MutationObserver) ----

  function videoFromEvent(e) {
    if (typeof e.composedPath === "function") {
      for (const item of e.composedPath()) {
        if (item instanceof HTMLVideoElement) return item;
      }
    }
    const target = e.target;
    return target && typeof target.closest === "function" ? target.closest("video") : null;
  }

  document.addEventListener(
    "mouseover",
    (e) => {
      if (!pillEnabled) return;
      const t = e.target;
      if (!t || typeof t.closest !== "function") return;
      if (host && (t === host || host.contains(t))) {
        cancelHide();
        return;
      }
      const video = videoFromEvent(e);
      if (video) {
        // Playback is authoritative: don't steal the pill from a different
        // video that's actively playing.
        if (activeVideo && activeVideo !== video && isCurrentlyPlaying(activeVideo)) {
          return;
        }
        const url = videoUrl(video);
        if (url) {
          activateVideo(video, url);
        } else if (activeVideo === video && !isCurrentlyPlaying(video)) {
          deactivateVideo();
        }
        return;
      }
      if (!activeVideo || !isCurrentlyPlaying(activeVideo)) scheduleHide();
    },
    true
  );

  // ---- Pill enable/disable via settings ----

  function disablePill() {
    pillEnabled = false;
    cancelHide();
    clearActiveVideoScans();
    for (const timer of streamRefreshTimers) clearTimeout(timer);
    streamRefreshTimers = [];
    if (resetTimer) {
      clearTimeout(resetTimer);
      resetTimer = null;
    }
    deactivateVideo(true);
  }

  function enablePill() {
    pillEnabled = true;
    // If a qualifying video is already playing, surface the pill for it
    // immediately without requiring a reload. Bounded scan (not a single
    // pass) in case the video hasn't finished mounting yet.
    scheduleActiveVideoScans();
  }

  // Initial already-playing scan: covers a video that started (and
  // finished dispatching play/playing) before this script's listeners
  // attached. Runs immediately at pillEnabled's default (true) so it isn't
  // blocked on the settings round-trip below.
  scheduleActiveVideoScans();

  Promise.resolve(browser.runtime.sendMessage({ type: "getSettings" }))
    .then((s) => {
      if (s && s.mediaPillEnabled === false) {
        disablePill();
      } else {
        // Re-scan now that settings are confirmed, in case a video started
        // playing during the async round-trip.
        scheduleActiveVideoScans();
      }
    })
    .catch(() => {
      // Settings unreachable; stay enabled (fail open).
      scheduleActiveVideoScans();
    });

  browser.storage.onChanged.addListener((changes, area) => {
    if (area !== "local" || !changes.settings) return;
    const newValue = changes.settings.newValue || {};
    if (newValue.mediaPillEnabled === false) {
      disablePill();
    } else {
      enablePill();
    }
  });

  // ---- Detected-stream sync with background (every frame) ----

  browser.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === "coveStreamsUpdated" && Array.isArray(msg.streams)) {
      detectedStreams = msg.streams;
      scheduleActiveVideoScans();
    }
  });

  try {
    Promise.resolve(browser.runtime.sendMessage({ type: "getDetectedStreams" }))
      .then((streams) => {
        if (Array.isArray(streams)) {
          detectedStreams = streams;
          scheduleActiveVideoScans();
        }
      })
      .catch(() => {});
  } catch {
    // Background unavailable; direct-src videos still work.
  }
})();
