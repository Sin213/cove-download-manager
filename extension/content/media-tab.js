// extension/content/media-tab.js
//
// IDM-style floating "Download with Cove" pill anchored to the actively
// playing <video>. Appears automatically when a qualifying video starts
// playing: every discovered <video> gets direct play/playing/pause/ended
// listeners, and child-list observers attach them to videos inserted or
// replaced later, including videos in open shadow roots (e.g. a dynamic
// player that mounts below a custom element). Hover remains a secondary
// convenience. Inert on pages without qualifying video: no DOM is created
// until a video with a usable URL becomes the active target. Uses Shadow
// DOM for isolation; never injects page-context scripts and never
// auto-downloads.
//
// Site-neutral. Anything that depends on a particular site is supplied by an
// optional adapter (content/media-sites.js) through the capability global
// read below; with no adapter loaded the pill works from the media element's
// own address alone.

(() => {
  "use strict";

  const browser = globalThis.browser || globalThis.chrome;
  if (!browser || !browser.runtime || !browser.runtime.id) return;

  // The site adapter, when one was loaded ahead of this script.
  const sites = globalThis.__coveMediaSites || null;
  const usesDetectedStreams = !!(sites && sites.usesDetectedStreams);

  const HIDE_DELAY_MS = 500;
  // Must outlast the background's 5s dedup window so a manual retry after
  // the window still produces a fresh native request.
  const SENT_RESET_MS = 6000;
  const PILL_GAP_PX = 8;

  // Streams the background's site adapter observed for this tab. Stays empty
  // when no adapter is loaded, since nothing is detecting them.
  let adapterStreams = [];

  // Whether the in-page pill is allowed to show. Starts suppressed: a page the
  // user excluded must not flash a pill during the settings round-trip, and an
  // answer that never arrives is not permission. Only an explicit, current
  // allowing answer from the background turns it on.
  let pillEnabled = false;
  // Invalidates outstanding permission answers. Every settings change bumps
  // it, so an answer issued before that change cannot be applied after it.
  let permissionEpoch = 0;

  let host = null;
  let pill = null;
  let label = null;
  // The pill is two controls: the download action it has always been, and an
  // options control that opens a menu with one entry. Both are real buttons, so
  // the second is reachable without a pointer and cannot fall through into the
  // first.
  let primaryButton = null;
  let optionsButton = null;
  let menu = null;
  let excludeItem = null;
  let menuOpen = false;
  // The site the background named for the currently open menu. Only ever what
  // the background answered, and sent straight back to it so a tab that
  // navigated in between is caught there rather than excluded by mistake.
  let menuSiteHost = "";
  // Identifies the opening a site answer was asked for. `menuOpen` alone cannot:
  // it is true again after the menu is closed and reopened, so an earlier
  // opening's late answer would pass that check and label the menu on screen
  // with a site it was never asked about.
  let menuGeneration = 0;
  let excludePending = false;
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

  // The address a player exposes on its own container, when the site adapter
  // knows how to read one. "" without an adapter.
  function embeddedStreamUrl(video) {
    return (sites && sites.embeddedStreamUrl && sites.embeddedStreamUrl(video)) || "";
  }

  function candidateUrl(video) {
    if (isDrmProtected(video)) return "";
    // Direct DOM branch. Two things have to hold before an address written in
    // the markup describes something downloadable:
    //
    // - the browser holds data for it. Below HAVE_CURRENT_DATA there is no
    //   resource yet, only an address that may never resolve, and a bare
    //   play event does not establish otherwise.
    // - it is the resource the element is on. currentSrc is what is playing,
    //   so once it is set it decides: when it is a blob:/data:/MSE address
    //   the answer is that there is no direct candidate, not that some older
    //   src or <source> left in the markup can stand in for it. Those name a
    //   different file.
    //
    // Both restrictions stop here. Everything below is the site adapter's,
    // and a blob: or not-yet-buffered player is exactly what those fallbacks
    // are for, so neither may return early past them.
    if (video.readyState >= 2) {
      const current = video.currentSrc || "";
      if (current) {
        if (isHttpUrl(current)) return current;
      } else {
        const src = video.getAttribute("src") || "";
        if (isHttpUrl(src)) return src;
        const source = video.querySelector("source[src]");
        if (source && isHttpUrl(source.src)) return source.src;
      }
    }
    const embeddedUrl = embeddedStreamUrl(video);
    if (embeddedUrl) return embeddedUrl;
    // blob:/data:/MSE video: use a stream the adapter observed for this tab.
    // This must work in subframes too: a detached player can put the actual
    // playing video in an iframe while the network stream remains tab-scoped
    // in the background.
    if (adapterStreams.length > 0 && isHttpUrl(adapterStreams[0].url)) {
      return adapterStreams[0].url;
    }
    return "";
  }

  // The page address to download instead of the media element, when the site
  // adapter designates one. "" without an adapter.
  function sitePageUrl() {
    return (sites && sites.sitePageUrl && sites.sitePageUrl()) || "";
  }

  function videoUrl(video) {
    return sitePageUrl() || candidateUrl(video);
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
      // The container carries no padding of its own. Every pill-coloured pixel
      // belongs to one of the two buttons, so the surface that looks pressable
      // is the surface that is pressable - and the download action keeps the
      // whole hit area it had before it gained a neighbour.
      ".cove-pill {",
      "  display: flex; align-items: stretch; gap: 0;",
      "  padding: 0; border-radius: 999px; overflow: hidden;",
      "  background: #1b1b26; color: #50e6cf;",
      "  border: 1px solid #50e6cf;",
      "  font: 600 12px/1.2 system-ui, sans-serif;",
      "  user-select: none;",
      "  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.4);",
      "}",
      ".cove-pill.cove-sent {",
      "  color: #9a9ab0; border-color: #9a9ab0;",
      "}",
      ".cove-pill.cove-sent .cove-primary { cursor: default; }",
      ".cove-pill.cove-error {",
      "  color: #e66a6a; border-color: #e66a6a;",
      "}",
      ".cove-primary, .cove-options, .cove-menu-item {",
      "  all: unset; cursor: pointer; color: inherit;",
      "  font: inherit; box-sizing: border-box;",
      "  display: flex; align-items: center;",
      "}",
      ".cove-primary { padding: 5px 8px 5px 12px; }",
      ".cove-primary:hover { background: #24243a; }",
      ".cove-options {",
      "  padding: 5px 10px 5px 8px; border-left: 1px solid currentColor;",
      "  line-height: 1;",
      "}",
      ".cove-options:hover { background: #24243a; }",
      ".cove-menu {",
      "  margin-top: 4px; padding: 4px; border-radius: 8px;",
      "  background: #1b1b26; color: #50e6cf;",
      "  border: 1px solid #50e6cf;",
      "  font: 600 12px/1.2 system-ui, sans-serif;",
      "  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.4);",
      "}",
      ".cove-menu-item { display: block; padding: 5px 10px; border-radius: 6px; }",
      ".cove-menu-item:hover { background: #24243a; }",
    ].join("\n");
    shadow.appendChild(style);

    pill = document.createElement("div");
    pill.className = "cove-pill";

    primaryButton = document.createElement("button");
    primaryButton.className = "cove-primary";
    primaryButton.type = "button";
    primaryButton.title = "Download with Cove";
    label = document.createElement("span");
    label.textContent = "Download with Cove";
    primaryButton.appendChild(label);
    pill.appendChild(primaryButton);

    optionsButton = document.createElement("button");
    optionsButton.className = "cove-options";
    optionsButton.type = "button";
    optionsButton.textContent = "▾";
    optionsButton.setAttribute("aria-haspopup", "menu");
    optionsButton.setAttribute("aria-expanded", "false");
    optionsButton.setAttribute("aria-label", "Cove pill options");
    pill.appendChild(optionsButton);
    shadow.appendChild(pill);

    menu = document.createElement("div");
    menu.className = "cove-menu";
    menu.setAttribute("role", "menu");
    menu.style.display = "none";
    excludeItem = document.createElement("button");
    excludeItem.className = "cove-menu-item";
    excludeItem.type = "button";
    excludeItem.setAttribute("role", "menuitem");
    menu.appendChild(excludeItem);
    shadow.appendChild(menu);

    primaryButton.addEventListener("click", onPillClick);
    optionsButton.addEventListener("click", onOptionsClick);
    optionsButton.addEventListener("keydown", onOptionsKeydown);
    excludeItem.addEventListener("click", onExcludeClick);
    excludeItem.addEventListener("keydown", onMenuKeydown);
    host.addEventListener("mouseenter", cancelHide);
    host.addEventListener("mouseleave", scheduleHide);
    // Capture phase, on the document, because the menu has to close for a
    // gesture that lands anywhere else on the page. A click inside the pill
    // retargets to the host element, which is how this tells the two apart.
    document.addEventListener("click", onDocumentPointer, true);
    document.addEventListener("focusin", onDocumentPointer, true);

    (document.body || document.documentElement).appendChild(host);
  }

  // Error labels name the side that actually failed. The pill cannot tell
  // whether a video is playable, so it must never say the video is at fault
  // for what is almost always a closed Cove.
  const ERROR_LABELS = {
    unavailable: "Cove is not running",
    unsupported: "No video found",
    exclude: "Could not exclude this site",
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

  // Fraction of the anchor video that must be inside the viewport for the
  // pill to be shown, and for that video to be picked as the anchor at all.
  const MIN_VISIBLE_FRACTION = 0.5;

  function visibleFraction(rect) {
    const area = rect.width * rect.height;
    if (area <= 0) return 0;
    const visibleWidth = Math.max(0, Math.min(rect.right, window.innerWidth) - Math.max(rect.left, 0));
    const visibleHeight = Math.max(0, Math.min(rect.bottom, window.innerHeight) - Math.max(rect.top, 0));
    return (visibleWidth * visibleHeight) / area;
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

    // A video scrolled out of view still reports a full-size rect (with a
    // negative top), and the clamping below would then pin the pill to the
    // top of the viewport, attached to nothing. Hide it while the anchor is
    // out of view; a later reposition() brings it back. The anchor is still
    // valid, so this is not a deactivation.
    if (visibleFraction(rect) < MIN_VISIBLE_FRACTION) {
      if (host) host.style.display = "none";
      return true;
    }

    ensurePill();
    // Measure the pill's own footprint before placing it so the "would it
    // clip above the viewport" check is accurate. visibility:hidden keeps
    // this invisible to the user while still forcing a real layout.
    host.style.display = "block";
    host.style.visibility = "hidden";
    const pillRect = host.getBoundingClientRect();
    const pillHeight = pillRect.height || 30;
    const pillWidth = pillRect.width || 160;

    // The video's visible band, not the whole viewport: a partly scrolled
    // video is still eligible, and clamping to the viewport would park the
    // pill at y=4 with the video's visible part somewhere else entirely.
    const bandTop = Math.max(rect.top, 0);
    const bandBottom = Math.min(rect.bottom, window.innerHeight);

    let top = rect.top - pillHeight - PILL_GAP_PX;
    if (top < 4) {
      // Clipped above the viewport: sit just inside the video's own visible
      // top-right corner instead of jumping to an unrelated page corner.
      top = Math.min(bandTop + PILL_GAP_PX, Math.max(bandTop, bandBottom - pillHeight));
    }

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
    // A dynamic player may pause or replace its <video> while a click is
    // being handled. Keep the pill and target stable until the background
    // replies.
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
    // Neither a hidden nor a not-yet-created host is skipped: the pill is
    // hidden (not deactivated) while its anchor is scrolled out of view, and
    // a video that started playing off-screen has no host at all yet. This
    // is what creates or restores it once the anchor scrolls back in.
    if (!activeVideo) return;
    // The anchor scrolled mostly out of view: hand the pill to another
    // playing video that is visible rather than leaving it hidden until the
    // next hover or bounded scan.
    if (visibleFraction(activeVideo.getBoundingClientRect()) < MIN_VISIBLE_FRACTION) {
      const found = findAlreadyPlayingVideo();
      if (
        found &&
        found.video !== activeVideo &&
        visibleFraction(found.video.getBoundingClientRect()) >= MIN_VISIBLE_FRACTION
      ) {
        activateVideo(found.video, found.url);
        return;
      }
    }
    if (!positionPill(activeVideo)) deactivateVideo();
  }

  function hidePill() {
    // A menu cannot outlive the pill it hangs off, or it would be left open
    // over the page with no control to close it.
    closeMenu();
    if (host) host.style.display = "none";
    currentUrl = "";
  }

  function scheduleHide() {
    cancelHide();
    // A menu the user is reading holds the pill up, and not only from the
    // moment it opened: a preview that pauses, a player that is replaced or a
    // video that ends all schedule a hide of their own afterwards, and any of
    // them would take the action away mid-gesture. dismissMenu() is what
    // resumes the ordinary hide once the menu is gone.
    if (menuOpen) return;
    hideTimer = setTimeout(() => {
      // A pill anchored to a still-playing video is not hover-dismissed;
      // only a hover-only pill (nothing actively playing) times out. A
      // detached video never counts as playing, or a torn-down preview
      // (which is never paused before removal) would keep the pill forever.
      if (activeVideo && activeVideo.isConnected && isCurrentlyPlaying(activeVideo)) return;
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

  // Diagnostics. A content script cannot load extension/diagnostics.js (the
  // manifest lists exactly one content script), so events are reported to the
  // background, which owns the ring. Only event names and outcomes travel:
  // never the page address, the media address or the tab title.
  function coveDiag(event, level, fields, requestId) {
    try {
      Promise.resolve(
        browser.runtime.sendMessage({
          type: "coveDiag",
          component: "extension.content",
          event: event,
          level: level,
          fields: fields || {},
          requestId: requestId,
        })
      ).catch(() => {});
    } catch (e) { /* a diagnostic must never break a download */ }
  }

  function newRequestId() {
    let out = "";
    while (out.length < 8) out += Math.floor(Math.random() * 16).toString(16);
    return out.slice(0, 8);
  }

  // ---- Options menu: one action, "Exclude this site" ----

  function focusNode(node) {
    if (node && typeof node.focus === "function") node.focus();
  }

  function stopEvent(e) {
    if (!e) return;
    if (typeof e.stopPropagation === "function") e.stopPropagation();
    if (typeof e.preventDefault === "function") e.preventDefault();
  }

  // Pure: closing never schedules anything, because the pill is also torn down
  // through here and a hide scheduled from that path would run against a pill
  // that no longer exists. The gestures that close the menu deliberately ask
  // for the ordinary hide to resume.
  function closeMenu(refocus) {
    if (!menu) return;
    menuOpen = false;
    menu.style.display = "none";
    menuSiteHost = "";
    if (optionsButton) optionsButton.setAttribute("aria-expanded", "false");
    if (refocus) focusNode(optionsButton);
  }

  function dismissMenu(refocus) {
    if (!menuOpen) return;
    closeMenu(refocus);
    scheduleHide();
  }

  // Which site the exclusion would name. The page cannot answer this: only the
  // background can see the top-level address the browser recorded, and that is
  // the only thing the label is allowed to say.
  async function requestSiteHost() {
    try {
      const resp = await Promise.resolve(
        browser.runtime.sendMessage({ type: "getPillSiteHost" })
      );
      return resp && resp.ok === true && typeof resp.host === "string"
        ? resp.host
        : "";
    } catch {
      return "";
    }
  }

  async function openMenu() {
    if (!pillEnabled || !menu || menuOpen) return;
    menuOpen = true;
    menuGeneration += 1;
    const generation = menuGeneration;
    // A menu the user is reading must not be pulled out from under them by the
    // hover timer. The ordinary hide resumes when it closes.
    cancelHide();
    menu.style.display = "block";
    optionsButton.setAttribute("aria-expanded", "true");
    excludeItem.textContent = "Exclude this site";

    const siteHost = await requestSiteHost();
    // Closed while the answer was in flight, or reopened since: that opening
    // owns the menu now and this answer is not its.
    if (!menuOpen || menuGeneration !== generation) return;
    if (!siteHost) {
      // Nothing nameable to exclude. Say so on the pill rather than offering an
      // action that could only ever exclude the wrong thing.
      dismissMenu(true);
      setPillState("error", "exclude");
      return;
    }
    menuSiteHost = siteHost;
    excludeItem.textContent = "Exclude " + siteHost;
    focusNode(excludeItem);
  }

  function toggleMenu() {
    if (menuOpen) dismissMenu(true);
    else openMenu();
  }

  function onOptionsClick(e) {
    // The download action is the pill's primary gesture and this is not it.
    // Stopping here is what keeps a click on the chevron from reaching it.
    stopEvent(e);
    toggleMenu();
  }

  function onOptionsKeydown(e) {
    if (!e) return;
    if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") {
      // Consumed, or the browser synthesises a click for the same keystroke
      // and the menu opens and closes again immediately.
      stopEvent(e);
      toggleMenu();
      return;
    }
    if (e.key === "Escape") dismissMenu(true);
  }

  function onMenuKeydown(e) {
    if (!e) return;
    if (e.key === "Escape") {
      stopEvent(e);
      dismissMenu(true);
      return;
    }
    // Tab is not swallowed: focus leaves the menu the way it normally would,
    // and the menu closes behind it rather than trapping anyone inside it.
    if (e.key === "Tab") dismissMenu();
  }

  function onDocumentPointer(e) {
    if (!menuOpen || !host) return;
    const target = e && e.target;
    if (target === host || (host.contains && target && host.contains(target))) return;
    dismissMenu();
  }

  async function onExcludeClick(e) {
    stopEvent(e);
    // The pill lives in an open shadow root in the page's own DOM, so the page -
    // or an embedded player's frame, which gets its own pill and whose exclusion
    // would name the TOP-LEVEL site - can find this control and activate it.
    // isTrusted rejects ordinary scripted activation, but a page can redress a
    // real click. Therefore this gesture only asks the background to open a
    // packaged confirmation page; it has no settings authority. Absent counts
    // as untrusted, so the defense-in-depth guard does not depend on how
    // thorough a synthetic event is.
    // Opening the menu is deliberately not gated - it only asks which site this
    // is - and the download action is unchanged: it hands one address to Cove
    // and persists no setting.
    if (!e || e.isTrusted !== true) return;
    if (!menuSiteHost || excludePending) return;
    excludePending = true;
    // Captured before the round-trip: closing the menu clears it, and the
    // background has to be told which offer the user actually accepted.
    const expectHost = menuSiteHost;
    excludeItem.textContent = "Opening confirmation…";
    try {
      const resp = await Promise.resolve(
        browser.runtime.sendMessage({ type: "requestExcludeConfirmation", expectHost })
      );
      if (!resp || resp.ok !== true) {
        dismissMenu(true);
        setPillState("error", "exclude");
        return;
      }
      // Nothing else. This request wrote no setting. A later confirmation-page
      // mutation is what may take the pill away through storage.onChanged.
      dismissMenu();
    } catch {
      dismissMenu(true);
      setPillState("error", "exclude");
    } finally {
      excludePending = false;
    }
  }

  async function onPillClick() {
    if (!pillEnabled || downloadPending) return;

    // A page the site adapter designates is downloaded from its page URL. Do
    // not wait for a transient media/blob URL: a dynamic player frequently
    // replaces that media element while its controls are being used.
    //
    // Resolved before anything is claimed or displayed. Nothing is in flight
    // yet, so the unresolvable case below can simply return: taking the
    // in-flight flag first would mean every exit from here had to remember to
    // release it, and deactivateVideo() refuses to run while that flag is set,
    // so forgetting once pins the pill over the page until a reload.
    //
    // currentUrl is the address captured when the pill was put up. It stands
    // in only once there is nothing left to ask: while the element is still
    // on the page its present state is what the user is looking at, and a
    // cached address would otherwise outlive the resource it named. An
    // element that stops playing without being removed only schedules a
    // hide, so the pill stays clickable for that grace period, which is long
    // enough for a player to have switched to a blob: source or dropped
    // below HAVE_CURRENT_DATA underneath it.
    const pageUrl = sitePageUrl() || location.href;
    const url = sitePageUrl() ||
      (activeVideo && activeVideo.isConnected
        ? candidateUrl(activeVideo)
        : currentUrl);

    // Generated at the origin of the request so the same id can be followed
    // through the background, the native host and Cove itself.
    const requestId = newRequestId();
    coveDiag("video_download_requested", "INFO", { trigger: "pill" }, requestId);

    if (!url) {
      // Nothing resolvable: an MSE player whose src is a blob:, on a site that
      // is not extractor-backed, before any stream has been seen on the wire.
      // The page address used to stand in here, which could only ever fire for
      // a site the extractor does not handle - an extractor-backed page is
      // already the first term above. So the fallback never served its stated
      // purpose and instead handed an ordinary HTML page to the downloader,
      // which fetches a web page, or on a site that refuses unfamiliar clients
      // fails with a bare 403 nobody can act on. Say so on the pill instead.
      coveDiag("video_pill_result", "INFO", { result: "unsupported" }, requestId);
      setPillState("error", "unsupported");
      return;
    }

    downloadPending = true;
    cancelHide();
    setPillState("detecting");
    currentUrl = url;

    try {
      const last = sentUrls.get(url);
      if (last && Date.now() - last < SENT_RESET_MS) {
        setPillState("sent");
        coveDiag("video_pill_result", "INFO", { result: "already_sent" }, requestId);
        return;
      }

      const resp = await Promise.resolve(
        browser.runtime.sendMessage({
          type: "downloadMedia",
          url,
          pageUrl,
          requestId,
        })
      );
      if (!resp || resp.ok !== true) {
        sentUrls.delete(url);
        // No reply at all means the background script never answered, which
        // is the same practical outcome as an unreachable Cove.
        const reason = resp ? resp.reason : "unavailable";
        coveDiag("video_pill_result", "WARNING",
                 { result: reason || "failed", replied: !!resp }, requestId);
        setPillState("error", reason);
        return;
      }

      sentUrls.set(url, Date.now());
      setPillState("sent");
      coveDiag("video_pill_result", "INFO", { result: "sent" }, requestId);
      if (resetTimer) clearTimeout(resetTimer);
      resetTimer = setTimeout(() => {
        if (currentUrl === url) setPillState("ready");
      }, SENT_RESET_MS);
    } catch {
      sentUrls.delete(url);
      // sendMessage itself threw: the background script is gone, so Cove
      // could not have been reached either.
      coveDiag("video_pill_result", "WARNING",
               { result: "unavailable", replied: false }, requestId);
      setPillState("error", "unavailable");
    } finally {
      downloadPending = false;
    }
  }

  // ---- Playback detection (primary trigger, direct per-video listeners) ----
  //
  // Manual testing against a dynamic player showed capture-phase
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
    scheduleAdapterStreamRefreshes();
    // A mostly off-screen video does not take the pill from a visible video
    // that is still playing: its own pill would be hidden on arrival, so the
    // handover would just make the pill vanish.
    if (
      activeVideo &&
      activeVideo !== video &&
      isCurrentlyPlaying(activeVideo) &&
      visibleFraction(video.getBoundingClientRect()) < MIN_VISIBLE_FRACTION &&
      visibleFraction(activeVideo.getBoundingClientRect()) >= MIN_VISIBLE_FRACTION
    ) {
      return;
    }
    const url = videoUrl(video);
    activateVideo(video, url);
  }

  function onVideoStopped(e) {
    const video = e.target;
    if (video !== activeVideo) return;
    // Hand off to another currently-playing qualifying video if one exists
    // on the page (e.g. a feed where the next post auto-plays); otherwise
    // hide.
    // Same visibility-aware selection as the startup scan, so a mostly
    // off-screen video cannot take the pill ahead of a visible one.
    const found = findAlreadyPlayingVideo();
    if (found && found.video !== video) {
      activateVideo(found.video, found.url);
      return;
    }
    // Not an immediate hide: a feed preview stops the moment the pointer
    // leaves the thumbnail, which is exactly while the pointer is travelling
    // to the pill. The grace period is what makes the pill clickable there.
    scheduleHide();
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
    // a dynamic player can start playback in the same tick it's
    // created/replaced, before this listener exists to observe a
    // play/playing event for it. Treat "already playing at registration"
    // as equivalent to a play event.
    // Recording what is playing is an observation, not a decision, so it
    // happens whether or not the pill may show - exactly as onVideoPlaying
    // already does it. Registration runs before permission is established, and
    // gating the record too would lose the video for good: the later scan
    // would fall through to its readyState >= 2 search and never see a player
    // still below HAVE_CURRENT_DATA.
    if (isCurrentlyPlaying(video)) lastKnownPlayingVideo = video;
    if (pillEnabled && isCurrentlyPlaying(video)) {
      if (!isDrmProtected(video)) activateVideo(video, videoUrl(video));
    }
  }

  function observeVideoRoot(root) {
    if (!root || observedRoots.has(root)) return;
    observedRoots.add(root);
    videoObserver.observe(root, { childList: true, subtree: true });
    registerVideosWithin(root);
  }

  // Registers videos and open shadow roots in the added subtree. A player can
  // be mounted below a custom element, where document queries and retargeted
  // hover events cannot see the actual <video>.
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
        // Same grace period as a stopped preview: a feed detaches its
        // inline player element on pointer-out, and hiding right then is
        // what makes the pill unclickable on the feed.
        if (removedActive) scheduleHide();
      }
      if (activeVideo && !activeVideo.isConnected) scheduleHide();
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
      // A mostly off-screen last-known target is not usable: its pill would
      // be hidden on sight, suppressing a visible playing video's pill.
      if (rect.width >= 80 && rect.height >= 60 && visibleFraction(rect) >= MIN_VISIBLE_FRACTION) {
        if (!isDrmProtected(lastKnownPlayingVideo)) {
          return { video: lastKnownPlayingVideo, url: videoUrl(lastKnownPlayingVideo) };
        }
      }
    }
    // No usable last-known target: deterministically pick the largest
    // visible currently-playing qualifying video. Never a thumbnail/poster
    // (readyState gate) or an offscreen/zero-size element (rect gate).
    let best = null;
    let bestScore = -1;
    let bestQualifies = false;
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
      // Sufficiently visible candidates always outrank the rest, and only
      // then does size decide: a bigger but mostly off-screen video would
      // otherwise take the pill and immediately hide it, even though it can
      // still have more visible pixels than a smaller fully visible one.
      // A below-threshold candidate can still win when it is the only one;
      // its pill then appears once it scrolls into view.
      const fraction = visibleFraction(rect);
      const qualifies = fraction >= MIN_VISIBLE_FRACTION;
      const score = fraction * rect.width * rect.height;
      const better = qualifies === bestQualifies ? score > bestScore : qualifies;
      if (better) {
        bestQualifies = qualifies;
        bestScore = score;
        best = { video, url };
      }
    }
    return best;
  }

  function scanForActiveVideo() {
    if (!pillEnabled) return;
    // An active video whose pill is hidden for being off-screen does not
    // hold the pill against a visible playing video; fall through so the
    // search below can hand it over.
    if (
      activeVideo &&
      isCurrentlyPlaying(activeVideo) &&
      visibleFraction(activeVideo.getBoundingClientRect()) >= MIN_VISIBLE_FRACTION
    ) {
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

  function refreshAdapterStreams() {
    try {
      Promise.resolve(browser.runtime.sendMessage({ type: "getDetectedStreams" }))
        .then((streams) => {
          if (!Array.isArray(streams)) return;
          adapterStreams = streams;
          scanForActiveVideo();
        })
        .catch(() => {});
    } catch {
      // Background unavailable; direct-src videos still work.
    }
  }

  function scheduleAdapterStreamRefreshes() {
    // Nothing is observing streams without a site adapter, so the background
    // is not asked for a list that would always come back empty.
    if (!usesDetectedStreams) return;
    for (const timer of streamRefreshTimers) clearTimeout(timer);
    streamRefreshTimers = [];
    refreshAdapterStreams();
    for (const delay of [250, 750, 1500, 3000]) {
      streamRefreshTimers.push(setTimeout(refreshAdapterStreams, delay));
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
        // video that's actively playing - unless that video has scrolled
        // mostly out of view, in which case its pill is hidden anyway and
        // the hovered one is what the user can actually see.
        if (
          activeVideo &&
          activeVideo !== video &&
          isCurrentlyPlaying(activeVideo) &&
          visibleFraction(activeVideo.getBoundingClientRect()) >= MIN_VISIBLE_FRACTION
        ) {
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
    closeMenu();
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

  // Applies an answer only while it is still the current one. An allowing
  // answer issued before an exclusion landed arrives carrying a stale epoch
  // and is dropped, so it cannot put back a pill the change just took away.
  function applyPillPermission(epoch, resp) {
    if (epoch !== permissionEpoch) return;
    if (resp && resp.pillAllowed === true && resp.mediaPillEnabled !== false) {
      // enablePill() runs the bounded already-playing scan, which is what
      // covers a video that started during the round-trip.
      enablePill();
    } else {
      disablePill();
    }
  }

  // Asks the background whether the pill may show for this page and this
  // frame. Suppresses first: from the moment settings change the previous
  // decision is void, and the pill stays down until a new answer allows it.
  // A rejection, a synchronous transport failure or an answer without an
  // explicit allow all leave it suppressed - and silently, because a page the
  // user excluded is not a broken Cove and must not be reported as one.
  function requestPillPermission() {
    permissionEpoch += 1;
    const epoch = permissionEpoch;
    disablePill();
    try {
      Promise.resolve(
        browser.runtime.sendMessage({ type: "getSettings", forPill: true })
      )
        .then((resp) => applyPillPermission(epoch, resp))
        .catch(() => applyPillPermission(epoch, null));
    } catch {
      applyPillPermission(epoch, null);
    }
  }

  requestPillPermission();

  browser.storage.onChanged.addListener((changes, area) => {
    if (area !== "local" || !changes.settings) return;
    // Any settings write can change the exclusion list, and the pill toggle is
    // no longer the only thing that decides. Re-ask rather than reading the
    // new value here: what the answer depends on is this frame's own identity,
    // which only the background can see.
    requestPillPermission();
  });

  // ---- Adapter stream sync with background (every frame) ----

  if (usesDetectedStreams) {
    browser.runtime.onMessage.addListener((msg) => {
      if (msg && msg.type === "coveStreamsUpdated" && Array.isArray(msg.streams)) {
        adapterStreams = msg.streams;
        scheduleActiveVideoScans();
      }
    });

    try {
      Promise.resolve(browser.runtime.sendMessage({ type: "getDetectedStreams" }))
        .then((streams) => {
          if (Array.isArray(streams)) {
            adapterStreams = streams;
            scheduleActiveVideoScans();
          }
        })
        .catch(() => {});
    } catch {
      // Background unavailable; direct-src videos still work.
    }
  }
})();
