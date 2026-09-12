// Drives extension/content/media-tab.js against a hand-rolled DOM stub.
//
// The content script only touches a small, well-known slice of the DOM
// (createElement/attachShadow/appendChild, getBoundingClientRect, event
// listeners, MutationObserver, ResizeObserver), so a stub is enough and
// keeps the extension tests dependency-free like the background ones.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");

class StubNode {
  constructor(tagName = "DIV") {
    this.tagName = tagName.toUpperCase();
    this.nodeType = 1;
    this.children = [];
    this.parentNode = null;
    this.shadowRoot = null;
    this.style = {};
    this.listeners = new Map();
    this.attributes = new Map();
    this.isConnected = true;
    this.rect = { top: 0, left: 0, width: 0, height: 0, right: 0, bottom: 0 };
    this.classList = {
      _set: new Set(),
      add: (...names) => names.forEach((n) => this.classList._set.add(n)),
      remove: (...names) => names.forEach((n) => this.classList._set.delete(n)),
      contains: (name) => this.classList._set.has(name),
    };
  }

  appendChild(child) {
    this.children.push(child);
    child.parentNode = this;
    return child;
  }

  // Recorded rather than discarded: the pill's options control is only
  // operable at all because of its aria state, so a test has to be able to
  // read back what the script set.
  setAttribute(name, value) {
    this.attributes.set(String(name), String(value));
  }

  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }

  removeEventListener() {}

  dispatch(type, event = {}) {
    for (const listener of this.listeners.get(type) || []) listener(event);
  }

  // HTMLElement.click(). The browser marks what it produces isTrusted: false,
  // which is the whole reason a page can reach an open shadow root and still
  // not be the user. Modelled exactly, so a test that calls this is calling
  // what hostile page script calls.
  click() {
    this.dispatch("click", {
      isTrusted: false,
      preventDefault() {},
      stopPropagation() {},
    });
  }

  attachShadow() {
    this.shadowRoot = new StubNode("SHADOW");
    this.shadowRoot.nodeType = 11;
    return this.shadowRoot;
  }

  getBoundingClientRect() {
    return this.rect;
  }

  closest(selector) {
    const parts = String(selector).split(",").map((s) => s.trim());
    let node = this;
    while (node) {
      for (const part of parts) {
        const attr = part.match(/^\[([^\]=]+)\]$/);
        if (attr) {
          if (node.getAttribute(attr[1]) != null) return node;
        } else if (/^[a-z][a-z0-9-]*$/i.test(part)) {
          if (node.tagName === part.toUpperCase()) return node;
        }
      }
      node = node.parentNode;
    }
    return null;
  }

  querySelector(selector) {
    const sel = String(selector).trim();
    const walk = (node, match) => {
      for (const child of node.children) {
        if (match(child)) return child;
        const found = walk(child, match);
        if (found) return found;
      }
      return null;
    };

    const link = sel.match(/^a\[href\*="([^"]+)"\]$/);
    if (link) {
      const needle = link[1];
      return walk(this, (child) => {
        const href = child.tagName === "A" ? child.getAttribute("href") : null;
        return !!href && href.includes(needle);
      });
    }

    // `tag[attr]`: first descendant of that tag carrying the attribute. The
    // pill resolves a <source src> child through exactly this shape, so the
    // stub has to answer it or a stale-source fixture is never seen at all.
    const tagAttr = sel.match(/^([a-z][a-z0-9-]*)\[([a-z-]+)\]$/i);
    if (tagAttr) {
      const [, tag, attr] = tagAttr;
      return walk(this, (child) =>
        child.tagName === tag.toUpperCase() && child.getAttribute(attr) != null
      );
    }

    return null;
  }

  getAttribute(name) {
    if (name in this) return this[name];
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  contains(node) {
    if (node === this) return true;
    return this.children.some((child) => child.contains && child.contains(node));
  }

  querySelectorAll() {
    return [];
  }
}

// A <video> placed at `top` with the given size, in viewport coordinates.
//
// `readyState` defaults to HAVE_ENOUGH_DATA because the rest of the fixture
// already describes a playing element with a resolved currentSrc, and a real
// browser never reports that combination below HAVE_CURRENT_DATA. Tests that
// mean "not ready yet" set it to 0 or 1 explicitly.
function stubVideo({ top = 100, width = 640, height = 360, readyState = 4 } = {}) {
  const video = new StubNode("VIDEO");
  video.paused = false;
  video.ended = false;
  video.readyState = readyState;
  video.currentSrc = "https://example.test/clip.mp4";
  video.src = "https://example.test/clip.mp4";
  video.scrollTo = (nextTop) => {
    video.rect = { ...video.rect, top: nextTop, bottom: nextTop + height };
  };
  video.rect = { top, left: 0, width, height, right: width, bottom: top + height };
  return video;
}

// `sites` mirrors the manifest: Firefox loads content/media-sites.js ahead of
// the shared pill so its capability global is published before the pill reads
// it. `sites: false` is the no-adapter configuration the shared pill must also
// survive, which is what a bundle without a site adapter would run.
// `chromeOnly` drops the `browser` global. Chrome exposes `chrome` alone, and
// the shared pill has to run there off the same file Firefox loads.
// Async because the pill now starts suppressed and is only enabled by the
// background's answer, which is a message round-trip. Returning before that
// settles would hand every test a page whose permission is still unknown -
// which is a real state, but not the one most of these tests are about.
async function loadMediaTab({ href = "https://example.test/watch", videos = [],
                       sites = true, chromeOnly = false,
                       settingsReply = { mediaPillEnabled: true, pillAllowed: true },
                       siteHostReply = { ok: true, host: "example.test" },
                       excludeReply = { ok: true },
                       reply = { mediaPillEnabled: true } } = {}) {
  const timers = [];
  const documentElement = new StubNode("HTML");
  const body = new StubNode("BODY");
  documentElement.appendChild(body);
  for (const video of videos) body.appendChild(video);
  // The script's startup scan is what attaches the direct play/pause
  // listeners, so the videos have to be discoverable from the root.
  documentElement.querySelectorAll = (selector) => (selector === "video" ? videos : []);

  const doc = {
    documentElement,
    body,
    listeners: new Map(),
    activeElement: null,
    // Real controls take focus, and the menu is only escapable because focus
    // goes back to the control that opened it. Elements the script creates
    // therefore record where focus landed.
    createElement: (tag) => {
      const node = new StubNode(tag);
      node.focus = () => { doc.activeElement = node; };
      return node;
    },
    querySelector: () => null,
    addEventListener(type, listener) {
      if (!doc.listeners.has(type)) doc.listeners.set(type, []);
      doc.listeners.get(type).push(listener);
    },
    dispatch(type, event) {
      for (const listener of doc.listeners.get(type) || []) listener(event);
    },
  };

  const win = {
    innerWidth: 1280,
    innerHeight: 720,
    listeners: new Map(),
    addEventListener(type, listener) {
      if (!win.listeners.has(type)) win.listeners.set(type, []);
      win.listeners.get(type).push(listener);
    },
    dispatch(type, event = {}) {
      for (const listener of win.listeners.get(type) || []) listener(event);
    },
  };

  // Every message the content script sends, so the diagnostics it reports
  // can be inspected exactly as the background would receive them.
  const sent = [];
  // Set by a test to make the very next sendMessage fail the way the real API
  // can: a synchronous throw when the background is gone, or a rejection.
  let sendThrows = false;
  const storageListeners = [];
  const runtimeListeners = [];
  const browser = {
    runtime: {
      id: "cove-test",
      // Not an async function: a synchronous throw and a rejected promise are
      // different failures, and the pill has to survive both. An async
      // function can only ever produce the second.
      sendMessage(message) {
        sent.push(message);
        if (sendThrows === "sync") throw new Error("Could not establish connection");
        if (sendThrows === "reject") {
          return Promise.reject(new Error("Could not establish connection"));
        }
        // The permission answer is its own fake. A test about the download
        // handoff should not have to restate the pill's permission, and a test
        // about permission should not have to restate the handoff.
        if (message && message.type === "getSettings") {
          return typeof settingsReply === "function"
            ? Promise.resolve(settingsReply(message))
            : Promise.resolve(settingsReply);
        }
        // The two background-owned intents behind the options menu answer from
        // their own fakes for the same reason: a test about the download
        // handoff must not have to restate them, and a test about excluding a
        // site must not have to restate the handoff.
        if (message && message.type === "getPillSiteHost") {
          return typeof siteHostReply === "function"
            ? Promise.resolve(siteHostReply(message))
            : Promise.resolve(siteHostReply);
        }
        if (message && message.type === "requestExcludeConfirmation") {
          return typeof excludeReply === "function"
            ? Promise.resolve(excludeReply(message))
            : Promise.resolve(excludeReply);
        }
        if (typeof reply === "function") return Promise.resolve(reply(message));
        return Promise.resolve(reply);
      },
      onMessage: { addListener(listener) { runtimeListeners.push(listener); } },
    },
    storage: {
      onChanged: { addListener(listener) { storageListeners.push(listener); } },
    },
  };

  const context = vm.createContext({
    globalThis: undefined,
    browser: chromeOnly ? undefined : browser,
    chrome: browser,
    document: doc,
    window: win,
    location: { href },
    console: { log() {}, error() {}, warn() {} },
    // The playback listeners type-check their target; the stub videos are
    // not real elements, so match on the tag name instead.
    HTMLVideoElement: class {
      static [Symbol.hasInstance](value) {
        return !!value && value.tagName === "VIDEO";
      }
    },
    URL,
    Date,
    Math,
    Promise,
    Set,
    Map,
    WeakSet,
    MutationObserver: class {
      observe() {}
      disconnect() {}
    },
    ResizeObserver: class {
      observe() {}
      disconnect() {}
    },
    setTimeout: (fn, ms) => {
      const handle = { fn, ms };
      timers.push(handle);
      return handle;
    },
    clearTimeout: (handle) => {
      const index = timers.indexOf(handle);
      if (index >= 0) timers.splice(index, 1);
    },
  });
  context.globalThis = context;

  const scripts = sites
    ? ["extension/content/media-sites.js", "extension/content/media-tab.js"]
    : ["extension/content/media-tab.js"];
  for (const script of scripts) {
    vm.runInContext(fs.readFileSync(script, "utf8"), context, { filename: script });
  }

  // The pill host is the only node the script itself appends to the body.
  const pillHost = () =>
    body.children.find((node) => node.className === "cove-media-tab-host") || null;
  const runTimers = () => {
    const pending = timers.splice(0, timers.length);
    for (const handle of pending) handle.fn();
  };
  // Fires a storage.onChanged the way the options page's save does, then lets
  // the resulting permission requery settle. `area` and `changes` are open so
  // a test can also deliver an event the pill must ignore entirely.
  const changeSettings = async (changes = { settings: { newValue: {} } },
                                area = "local") => {
    for (const listener of storageListeners) listener(changes, area);
    await settle();
  };

  // The permission round-trip is a message, so nothing about the pill is
  // decided until the microtask queue has run.
  await settle();

  return {
    doc, win, body, pillHost, runTimers, timers, sent,
    changeSettings,
    storageListeners,
    // Delivers a background push, e.g. the adapter's coveStreamsUpdated, which
    // is one of the paths that can schedule a scan after a pill was taken away.
    pushMessage: (message) => {
      for (const listener of runtimeListeners) listener(message);
    },
    settle,
    setSendFailure: (mode) => { sendThrows = mode; },
    // How many permission requests the content script has made so far. The
    // pill must not ask again per play, hover, scan or resize.
    permissionRequests: () =>
      sent.filter((m) => m && m.type === "getSettings").length,
    downloadMessages: () =>
      sent.filter((m) => m && m.type === "downloadMedia"),
    excludeMessages: () =>
      sent.filter((m) => m && m.type === "requestExcludeConfirmation"),
    siteHostMessages: () =>
      sent.filter((m) => m && m.type === "getPillSiteHost"),
  };
}

// The pill is two controls in one shape: the download action the user has
// always had, and an options control beside it. Every test that used to reach
// for "the pill" wants one or the other, so name them in one place.
function pillParts(host) {
  const pill = host && host.shadowRoot.children.find((n) => n.className === "cove-pill");
  const find = (cls) => (pill ? pill.children.find((n) => n.className === cls) : null);
  const primary = find("cove-primary");
  return {
    pill,
    primary,
    options: find("cove-options"),
    label: primary ? primary.children[0] : null,
    menu: host
      ? host.shadowRoot.children.find((n) => n.className === "cove-menu")
      : null,
  };
}

// The download gesture: a click on the primary control, not on the container.
function clickPrimary(host) {
  const { primary } = pillParts(host);
  assert.ok(primary, "expected the primary download control inside the shadow root");
  primary.dispatch("click", {});
}

// Brings a video up as the active pill target through the hover path.
function hover(harness, video) {
  harness.doc.dispatch("mouseover", { target: video });
  const host = harness.pillHost();
  if (host) host.rect = { top: 0, left: 0, width: 160, height: 30, right: 160, bottom: 30 };
  harness.doc.dispatch("mouseover", { target: video });
  return harness.pillHost();
}

test("the pill is anchored above its video while the video is in view", async () => {
  const video = stubVideo({ top: 200 });
  const harness = await loadMediaTab({ videos: [video] });
  const host = hover(harness, video);

  assert.ok(host, "expected a pill host to be created");
  assert.equal(host.style.display, "block");
  // 200 (video top) - 30 (pill height) - 8 (gap)
  assert.equal(host.style.top, "162px");
});

test("the pill hides instead of pinning itself to the top of the viewport", async () => {
  const video = stubVideo({ top: 200 });
  const harness = await loadMediaTab({ videos: [video] });
  const host = hover(harness, video);
  assert.equal(host.style.display, "block");

  // Scroll the video off the top of the viewport.
  video.scrollTo(-400);
  harness.win.dispatch("scroll");

  assert.equal(host.style.display, "none");
  assert.notEqual(host.style.top, "4px");
});

test("the pill comes back when its video scrolls into view again", async () => {
  const video = stubVideo({ top: 200 });
  const harness = await loadMediaTab({ videos: [video] });
  const host = hover(harness, video);

  video.scrollTo(-400);
  harness.win.dispatch("scroll");
  assert.equal(host.style.display, "none");

  video.scrollTo(200);
  harness.win.dispatch("scroll");

  assert.equal(host.style.display, "block");
  assert.equal(host.style.top, "162px");
});

test("a video that starts playing off-screen gets its pill on scroll-in", async () => {
  const video = stubVideo({ top: 900 }); // below a 720px viewport
  const harness = await loadMediaTab({ videos: [video] });
  harness.doc.dispatch("mouseover", { target: video });
  assert.equal(harness.pillHost(), null, "no pill host is created off-screen");

  video.scrollTo(200);
  harness.win.dispatch("scroll");

  const host = harness.pillHost();
  assert.ok(host, "expected the pill to appear once the video scrolled in");
  assert.equal(host.style.display, "block");
});

test("a visible playing video wins the pill over a bigger off-screen one", async () => {
  const visible = stubVideo({ top: 100, width: 640, height: 360 });
  const offscreen = stubVideo({ top: 900, width: 1280, height: 720 });
  for (const video of [visible, offscreen]) video.readyState = 4;
  // Registration order makes the off-screen video the last one to claim the
  // pill, so the bounded startup scans are what must hand it back.
  const harness = await loadMediaTab({ videos: [visible, offscreen] });
  harness.runTimers();

  const host = harness.pillHost();
  assert.ok(host, "expected a pill for the visible video");
  assert.equal(host.style.display, "block");
  // 100 (visible video top) - 30 (pill height) - 8 (gap)
  assert.equal(host.style.top, "62px");
});

test("a stopped video hands the pill to a visible video, not an off-screen one", async () => {
  const offscreen = stubVideo({ top: 900, width: 1280, height: 720 });
  const visible = stubVideo({ top: 300, width: 640, height: 360 });
  const active = stubVideo({ top: 100, width: 640, height: 360 });
  for (const video of [offscreen, visible, active]) video.readyState = 4;
  const harness = await loadMediaTab({ videos: [offscreen, visible, active] });

  active.paused = true;
  active.dispatch("pause", { target: active });

  const host = harness.pillHost();
  assert.equal(host.style.display, "block");
  // 300 (visible video top) - 30 (pill height) - 8 (gap)
  assert.equal(host.style.top, "262px");
});

test("a small fully visible video outranks a large mostly off-screen one", async () => {
  // 1920x1080 with only ~40% on screen still has more visible pixels than a
  // fully visible 640x360, so visible area alone would pick the wrong one.
  const big = stubVideo({ top: 260, width: 1920, height: 1080 });
  const small = stubVideo({ top: 100, width: 640, height: 360 });
  for (const video of [big, small]) video.readyState = 4;
  const harness = await loadMediaTab({ videos: [small, big] });
  harness.runTimers();

  const host = harness.pillHost();
  assert.equal(host.style.display, "block");
  // 100 (small video top) - 30 (pill height) - 8 (gap)
  assert.equal(host.style.top, "62px");
});

test("an off-screen video starting playback does not steal the pill", async () => {
  const visible = stubVideo({ top: 100, width: 640, height: 360 });
  const offscreen = stubVideo({ top: 900, width: 1280, height: 720 });
  visible.readyState = 4;
  offscreen.readyState = 4;
  offscreen.paused = true;
  const harness = await loadMediaTab({ videos: [visible, offscreen] });

  offscreen.paused = false;
  offscreen.dispatch("playing", { target: offscreen });

  const host = harness.pillHost();
  assert.equal(host.style.display, "block");
  // 100 (visible video top) - 30 (pill height) - 8 (gap)
  assert.equal(host.style.top, "62px");
});

test("hovering a visible video takes the pill from an off-screen player", async () => {
  const playing = stubVideo({ top: 100, width: 640, height: 360 });
  const hovered = stubVideo({ top: 300, width: 640, height: 360 });
  playing.readyState = 4;
  hovered.paused = true;
  const harness = await loadMediaTab({ videos: [playing, hovered] });

  // The playing video scrolls away but keeps playing, so it stays active.
  playing.scrollTo(-400);
  harness.win.dispatch("scroll");
  assert.equal(harness.pillHost().style.display, "none");

  harness.doc.dispatch("mouseover", { target: hovered });

  const host = harness.pillHost();
  assert.equal(host.style.display, "block");
  // 300 (hovered video top) - 30 (pill height) - 8 (gap)
  assert.equal(host.style.top, "262px");
});

test("a partly scrolled video keeps the pill inside its visible band", async () => {
  // 260 of 360 px still on screen: eligible, but "above the video" is off
  // the top of the viewport.
  const video = stubVideo({ top: 200, height: 360 });
  const harness = await loadMediaTab({ videos: [video] });
  hover(harness, video);

  video.scrollTo(-100);
  harness.win.dispatch("scroll");

  const host = harness.pillHost();
  assert.equal(host.style.display, "block");
  // Just inside the visible top edge of the video, not clamped to y=4.
  assert.equal(host.style.top, "8px");
});

test("scrolling the active video away hands the pill to a visible one", async () => {
  const active = stubVideo({ top: 100, width: 640, height: 360 });
  const other = stubVideo({ top: 400, width: 640, height: 360 });
  active.readyState = 4;
  other.readyState = 4;
  const harness = await loadMediaTab({ videos: [other, active] });

  active.scrollTo(-400);
  harness.win.dispatch("scroll");

  const host = harness.pillHost();
  assert.equal(host.style.display, "block");
  // 400 (other video top) - 30 (pill height) - 8 (gap)
  assert.equal(host.style.top, "362px");
});

test("a video scrolled just past the halfway mark hides the pill", async () => {
  const video = stubVideo({ top: 0, height: 360 });
  const harness = await loadMediaTab({ videos: [video] });
  const host = hover(harness, video);
  assert.equal(host.style.display, "block");

  video.scrollTo(-200); // 160 of 360 px visible
  harness.win.dispatch("scroll");

  assert.equal(host.style.display, "none");
});

test("a paused feed preview keeps the pill up long enough to click it", async () => {
  const video = stubVideo({ top: 200 });
  const harness = await loadMediaTab({ videos: [video] });
  const host = hover(harness, video);
  assert.equal(host.style.display, "block");

  // YouTube tears its inline preview down as soon as the pointer leaves the
  // thumbnail, which is exactly when the pointer is travelling to the pill.
  video.paused = true;
  video.dispatch("pause", { target: video });

  assert.equal(host.style.display, "block");
});

test("a torn-down feed preview hides the pill once the grace period expires", async () => {
  const video = stubVideo({ top: 200 });
  const harness = await loadMediaTab({ videos: [video] });
  const host = hover(harness, video);

  video.paused = true;
  video.dispatch("pause", { target: video });
  harness.runTimers();

  assert.equal(host.style.display, "none");
});

test("a detached preview that never paused still times out", async () => {
  const video = stubVideo({ top: 200 });
  const harness = await loadMediaTab({ videos: [video] });
  const host = hover(harness, video);

  // YouTube removes the inline preview element without pausing it first.
  video.isConnected = false;
  video.dispatch("emptied", { target: video });
  harness.runTimers();

  assert.equal(host.style.display, "none");
});

test("hovering the pill itself cancels the pending hide", async () => {
  const video = stubVideo({ top: 200 });
  const harness = await loadMediaTab({ videos: [video] });
  const host = hover(harness, video);

  video.paused = true;
  video.dispatch("pause", { target: video });
  host.dispatch("mouseenter", {});
  harness.runTimers();

  assert.equal(host.style.display, "block");
});


// ---------------------------------------------------------------------------
// Diagnostics
//
// The content script cannot load extension/diagnostics.js (the manifest lists
// one content script), so it reports events to the background instead. It must
// never send a page address, a media address or a title along with them.
// ---------------------------------------------------------------------------

function diagMessages(harness) {
  return harness.sent.filter((m) => m && m.type === "coveDiag");
}

async function clickPill(harness, video) {
  const host = hover(harness, video);
  clickPrimary(host);
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  return host;
}

test("a pill download records a request with a generated request id", async () => {
  const video = stubVideo({ top: 200, src: "https://cdn.example.test/v/movie.mp4" });
  const harness = await loadMediaTab({ videos: [video], reply: { ok: true } });
  await clickPill(harness, video);

  const download = harness.sent.find((m) => m.type === "downloadMedia");
  assert.match(download.requestId, /^[0-9a-f]{8}$/);

  const requested = diagMessages(harness).find(
    (m) => m.event === "video_download_requested"
  );
  assert.ok(requested, "the pill request must be recorded");
  assert.equal(requested.component, "extension.content");
  assert.equal(requested.requestId, download.requestId);
});

test("a pill result is recorded with the same request id", async () => {
  const video = stubVideo({ top: 200, src: "https://cdn.example.test/v/movie.mp4" });
  const harness = await loadMediaTab({ videos: [video], reply: { ok: true } });
  await clickPill(harness, video);

  const download = harness.sent.find((m) => m.type === "downloadMedia");
  const result = diagMessages(harness).find((m) => m.event === "video_pill_result");
  assert.equal(result.requestId, download.requestId);
  assert.equal(result.fields.result, "sent");
});

test("an unavailable Cove is recorded as such by the pill", async () => {
  const video = stubVideo({ top: 200, src: "https://cdn.example.test/v/movie.mp4" });
  const harness = await loadMediaTab({
    videos: [video],
    reply: { ok: false, reason: "unavailable" },
  });
  await clickPill(harness, video);

  const result = diagMessages(harness).find((m) => m.event === "video_pill_result");
  assert.equal(result.fields.result, "unavailable");
});

test("a background script that never answers is recorded as unavailable", async () => {
  const video = stubVideo({ top: 200, src: "https://cdn.example.test/v/movie.mp4" });
  const harness = await loadMediaTab({
    videos: [video],
    reply: (message) => {
      if (message.type === "downloadMedia") throw new Error("no background");
      if (message.type === "getSettings") return { mediaPillEnabled: true };
      return {};
    },
  });
  await clickPill(harness, video);

  const result = diagMessages(harness).find((m) => m.event === "video_pill_result");
  assert.equal(result.fields.result, "unavailable");
});

test("no page url, media url or title is sent with a pill diagnostic", async () => {
  const video = stubVideo({
    top: 200,
    src: "https://cdn.example.test/v/secret-movie.mp4",
  });
  const harness = await loadMediaTab({
    videos: [video],
    href: "https://news.example.test/private-article",
    reply: { ok: true },
  });
  await clickPill(harness, video);

  const dumped = JSON.stringify(diagMessages(harness));
  assert.ok(!dumped.includes("secret-movie"));
  assert.ok(!dumped.includes("private-article"));
  assert.ok(!dumped.includes("news.example.test"));
  assert.ok(!dumped.includes("cdn.example.test"));
});

test("the pill wording is unchanged by diagnostics", async () => {
  const video = stubVideo({ top: 200, src: "https://cdn.example.test/v/movie.mp4" });
  const harness = await loadMediaTab({
    videos: [video],
    reply: { ok: false, reason: "unavailable" },
  });
  const host = await clickPill(harness, video);
  assert.equal(pillParts(host).label.textContent, "Cove is not running");
});

test("a diagnostics send failure never breaks a pill download", async () => {
  const video = stubVideo({ top: 200, src: "https://cdn.example.test/v/movie.mp4" });
  const harness = await loadMediaTab({
    videos: [video],
    reply: (message) => {
      if (message.type === "coveDiag") throw new Error("diagnostics exploded");
      if (message.type === "getSettings") return { mediaPillEnabled: true };
      return { ok: true };
    },
  });
  await clickPill(harness, video);

  const download = harness.sent.find((m) => m.type === "downloadMedia");
  assert.ok(download, "the download must still be sent");
});

// ---------------------------------------------------------------------------
// Unresolvable media
//
// A page can show a video whose address the content script cannot work out:
// an MSE player whose src is a blob:, on a site that is not extractor-backed,
// before any stream has been seen on the wire. The Reddit front page is the
// case that surfaced this. There is nothing to download, and the page address
// is not a substitute - handing an HTML page to aria2 downloads a web page or,
// on a site that refuses unfamiliar clients, fails with a bare 403.
// ---------------------------------------------------------------------------

function blobVideo(options = {}) {
  const video = stubVideo(options);
  video.currentSrc = "blob:https://www.reddit.com/9c3f2f1e-0d4a-4c1e-8d2b";
  video.src = "";
  return video;
}

test("a video with no resolvable address does not fall back to the page", async () => {
  const video = blobVideo({ top: 200 });
  const harness = await loadMediaTab({
    href: "https://www.reddit.com/",
    videos: [video],
    reply: { ok: true },
  });

  await clickPill(harness, video);

  const download = harness.sent.find((m) => m.type === "downloadMedia");
  assert.equal(
    download, undefined,
    "the page address must never be sent as if it were the media"
  );
});

test("an unresolvable video reports why instead of failing at the backend", async () => {
  const video = blobVideo({ top: 200 });
  const harness = await loadMediaTab({
    href: "https://www.reddit.com/",
    videos: [video],
    reply: { ok: true },
  });

  const host = await clickPill(harness, video);

  assert.equal(pillParts(host).label.textContent, "No video found");
});

test("an extractor-backed page still downloads from its page address", async () => {
  // The fallback's stated purpose. YouTube replaces the media element while
  // its controls are used, so the page address is the stable target - and it
  // is reached through the site adapter, not through the removed fallback.
  const video = blobVideo({ top: 200 });
  const harness = await loadMediaTab({
    href: "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    videos: [video],
    reply: { ok: true },
  });

  await clickPill(harness, video);

  const download = harness.sent.find((m) => m.type === "downloadMedia");
  assert.ok(download, "a YouTube page must still be handed over");
  assert.equal(download.url, "https://www.youtube.com/watch?v=dQw4w9WgXcQ");
});

test("an unresolvable video leaves the pill able to hide again", async () => {
  // The early return that reports "No video found" must still clear the
  // in-flight flag. deactivateVideo() refuses to run while a download is
  // pending, so a flag left set pins the pill over the feed until the page is
  // reloaded - and blocks every later click on it too.
  const video = blobVideo({ top: 200 });
  const harness = await loadMediaTab({
    href: "https://www.reddit.com/",
    videos: [video],
    reply: { ok: true },
  });

  const host = await clickPill(harness, video);

  video.paused = true;
  video.dispatch("pause", { target: video });
  harness.runTimers();

  assert.equal(host.style.display, "none", "the pill must be able to go away");
});

test("an unresolvable video does not wedge the pill against later clicks", async () => {
  const video = blobVideo({ top: 200 });
  const harness = await loadMediaTab({
    href: "https://www.reddit.com/",
    videos: [video],
    reply: { ok: true },
  });

  await clickPill(harness, video);

  // The stream arrives on the wire after the first click, as it does when the
  // player starts fetching. A second click must be able to act on it.
  video.currentSrc = "https://v.redd.it/abc123/DASH_720.mp4";
  await clickPill(harness, video);

  const download = harness.sent.find((m) => m.type === "downloadMedia");
  assert.ok(download, "the second click must be allowed through");
  assert.equal(download.url, "https://v.redd.it/abc123/DASH_720.mp4");
});

// ---------------------------------------------------------------------------
// The shared pill without a site adapter
//
// content/media-tab.js is destined for a bundle that ships no site adapter.
// It must load and drive a direct media element there, while contributing
// nothing that only the adapter knows: no extractor page address, no embedded
// stream, and no detected-stream traffic.
// ---------------------------------------------------------------------------

test("the shared pill loads and downloads a direct video with no site adapter", async () => {
  const video = stubVideo({ top: 200 });
  const harness = await loadMediaTab({
    sites: false,
    href: "https://example.test/watch",
    videos: [video],
    reply: { ok: true },
  });

  await clickPill(harness, video);

  const download = harness.sent.find((m) => m.type === "downloadMedia");
  assert.ok(download, "a direct media element must still be downloadable");
  assert.equal(download.url, "https://example.test/clip.mp4");
});

test("without a site adapter the extractor page address is never contributed", async () => {
  // The same page that resolves to its watch address with the adapter loaded.
  const video = blobVideo({ top: 200 });
  const harness = await loadMediaTab({
    sites: false,
    href: "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    videos: [video],
    reply: { ok: true },
  });

  const host = await clickPill(harness, video);

  assert.equal(
    harness.sent.find((m) => m.type === "downloadMedia"), undefined,
    "the page address must not be handed over without the adapter",
  );
  assert.equal(pillParts(host).label.textContent, "No video found");
});

test("without a site adapter an embedded stream attribute is never read", async () => {
  const owner = new StubNode("DIV");
  owner["data-hls-url"] = "https://v.redd.it/first/HLSPlaylist.m3u8";
  const video = blobVideo({ top: 200 });

  const harness = await loadMediaTab({
    sites: false,
    href: "https://example.test/feed",
    videos: [video],
    reply: { ok: true },
  });
  harness.body.appendChild(owner);
  owner.appendChild(video);

  await clickPill(harness, video);

  assert.equal(
    harness.sent.find((m) => m.type === "downloadMedia"), undefined,
    "the embedded stream attribute belongs to the site adapter",
  );
});

test("without a site adapter no detected-stream traffic is generated", async () => {
  const video = stubVideo({ top: 200 });
  const harness = await loadMediaTab({
    sites: false,
    href: "https://example.test/watch",
    videos: [video],
  });
  harness.runTimers();

  assert.deepEqual(
    harness.sent.filter((m) => m.type === "getDetectedStreams"), [],
    "the stream list is the adapter's, so it must not be asked for",
  );
});

test("with the site adapter the detected-stream fetch still happens", async () => {
  // The counterpart of the assertion above: the Firefox path is unchanged.
  const video = stubVideo({ top: 200 });
  const harness = await loadMediaTab({ href: "https://example.test/watch", videos: [video] });

  assert.ok(
    harness.sent.some((m) => m.type === "getDetectedStreams"),
    "Firefox must still fetch the tab's streams on startup",
  );
});

test("a player does not borrow another player's stream", async () => {
  // The fallback used to be document-wide, so on a page with several players
  // every one of them resolved to the first player's stream - a download that
  // looks like it worked and fetches the wrong video.
  const owner = new StubNode("DIV");
  owner["data-hls-url"] = "https://v.redd.it/first/HLSPlaylist.m3u8";
  const ownerVideo = blobVideo({ top: -600 });
  const bare = new StubNode("DIV");
  const bareVideo = blobVideo({ top: 200 });

  const harness = await loadMediaTab({
    href: "https://example.test/feed",
    videos: [ownerVideo, bareVideo],
  });
  harness.body.appendChild(owner);
  owner.appendChild(ownerVideo);
  harness.body.appendChild(bare);
  bare.appendChild(bareVideo);
  // A real document finds the first matching element anywhere on the page,
  // which is the whole hazard. The stub returns null by default, so without
  // this the document-wide branch is never exercised at all.
  harness.doc.querySelector = (selector) =>
    selector === "[data-hls-url]" ? owner : null;

  const host = await clickPill(harness, bareVideo);

  assert.equal(
    harness.sent.find((m) => m.type === "downloadMedia"), undefined,
    "a player with no stream of its own must not claim one"
  );
  assert.equal(pillParts(host).label.textContent, "No video found");
});


// ---------------------------------------------------------------------------
// Direct candidate eligibility
//
// A direct DOM candidate has to be the resource the element is actually on,
// and the browser has to hold data for it. Neither held before: a blob:
// currentSrc fell through to whatever stale src/source the markup still
// carried, and a video with no buffered data at all was offered and handed
// over. Both are proven below through the real click path, and both are
// gated without disturbing the site adapter's own fallbacks further down.
// ---------------------------------------------------------------------------

// A <source src> child, the shape the pill looks up with querySelector.
function sourceChild(url) {
  const source = new StubNode("SOURCE");
  source.src = url;
  return source;
}

const STALE_SOURCE = "https://cdn.test/stale-source.mp4";
const STALE_ATTR = "https://cdn.test/stale-attribute.mp4";

// A player on a blob: (MSE) resource whose markup still carries both older
// direct URLs. Neither describes what is playing.
function staleMarkupVideo(options = {}) {
  const video = blobVideo(options);
  video.src = STALE_ATTR;
  video.appendChild(sourceChild(STALE_SOURCE));
  return video;
}

function downloads(harness) {
  return harness.sent.filter((m) => m.type === "downloadMedia");
}

test("the stale-markup fixture really does expose a source element", async () => {
  // Guard for the guards: the stub answered every source[src] lookup with
  // null before, so a stale-source regression could pass without the branch
  // it names ever being reached.
  const video = staleMarkupVideo({ top: 200 });
  const source = video.querySelector("source[src]");

  assert.ok(source, "the fixture must expose a <source src> child");
  assert.equal(source.src, STALE_SOURCE);
  assert.equal(video.getAttribute("src"), STALE_ATTR);
  assert.match(video.currentSrc, /^blob:/);
});

test("an ineligible active resource is not swapped for a stale source element", async () => {
  const video = staleMarkupVideo({ top: 200 });
  const harness = await loadMediaTab({
    sites: false,
    href: "https://example.test/player",
    videos: [video],
    reply: { ok: true },
  });

  await clickPill(harness, video);

  assert.deepEqual(
    downloads(harness).map((m) => m.url), [],
    "the blob: resource is what is playing, so no older DOM URL substitutes for it",
  );
});

test("an ineligible active resource is not swapped for a stale src attribute", async () => {
  // The same rejection must not simply move from one stale DOM fallback to
  // the other: with no <source> child at all the src attribute is equally
  // not the active resource.
  const video = blobVideo({ top: 200 });
  video.src = STALE_ATTR;
  const harness = await loadMediaTab({
    sites: false,
    href: "https://example.test/player",
    videos: [video],
    reply: { ok: true },
  });

  await clickPill(harness, video);

  assert.deepEqual(downloads(harness).map((m) => m.url), []);
});

for (const readyState of [0, 1]) {
  test(`a video with readyState ${readyState} is not handed over`, async () => {
    const video = stubVideo({ top: 200, readyState });
    const harness = await loadMediaTab({
      sites: false,
      href: "https://example.test/watch",
      videos: [video],
      reply: { ok: true },
    });

    await clickPill(harness, video);

    assert.deepEqual(
      downloads(harness).map((m) => m.url), [],
      "below HAVE_CURRENT_DATA the browser has nothing to hand over",
    );
  });
}

test("readyState 2 is enough for a direct resource", async () => {
  // The boundary itself: HAVE_CURRENT_DATA is the point the gate admits.
  const video = stubVideo({ top: 200, readyState: 2 });
  const harness = await loadMediaTab({
    sites: false,
    href: "https://example.test/watch",
    videos: [video],
    reply: { ok: true },
  });

  await clickPill(harness, video);

  assert.deepEqual(
    downloads(harness).map((m) => m.url), ["https://example.test/clip.mp4"],
  );
});

test("an eligible active resource outranks every stale DOM alternative", async () => {
  const video = stubVideo({ top: 200 });
  video.currentSrc = "https://example.test/active.mp4";
  video.src = STALE_ATTR;
  video.appendChild(sourceChild(STALE_SOURCE));
  const harness = await loadMediaTab({
    sites: false,
    href: "https://example.test/watch",
    videos: [video],
    reply: { ok: true },
  });

  await clickPill(harness, video);

  assert.deepEqual(
    downloads(harness).map((m) => m.url), ["https://example.test/active.mp4"],
    "one click, one handoff, and it is the resource being played",
  );
});

test("an element with no currentSrc yet still uses its src attribute", async () => {
  const video = stubVideo({ top: 200 });
  video.currentSrc = "";
  video.src = "https://example.test/from-attribute.mp4";
  const harness = await loadMediaTab({
    sites: false,
    href: "https://example.test/watch",
    videos: [video],
    reply: { ok: true },
  });

  await clickPill(harness, video);

  assert.deepEqual(
    downloads(harness).map((m) => m.url), ["https://example.test/from-attribute.mp4"],
  );
});

test("an element with no currentSrc or src falls back to its source child", async () => {
  const video = stubVideo({ top: 200 });
  video.currentSrc = "";
  video.src = "";
  video.appendChild(sourceChild("https://example.test/from-source.mp4"));
  const harness = await loadMediaTab({
    sites: false,
    href: "https://example.test/watch",
    videos: [video],
    reply: { ok: true },
  });

  await clickPill(harness, video);

  assert.deepEqual(
    downloads(harness).map((m) => m.url), ["https://example.test/from-source.mp4"],
  );
});

test("an extensionless direct resource is still eligible", async () => {
  // Eligibility is structural, not an extension allowlist.
  const video = stubVideo({ top: 200 });
  video.currentSrc = "https://example.test/media/stream";
  video.src = "https://example.test/media/stream";
  const harness = await loadMediaTab({
    sites: false,
    href: "https://example.test/watch",
    videos: [video],
    reply: { ok: true },
  });

  await clickPill(harness, video);

  assert.deepEqual(
    downloads(harness).map((m) => m.url), ["https://example.test/media/stream"],
  );
});

test("a replaced resource is re-resolved at click time", async () => {
  const video = stubVideo({ top: 200 });
  const harness = await loadMediaTab({
    sites: false,
    href: "https://example.test/watch",
    videos: [video],
    reply: { ok: true },
  });
  hover(harness, video);

  video.currentSrc = "https://example.test/second.mp4";
  video.src = "https://example.test/second.mp4";
  await clickPill(harness, video);

  assert.deepEqual(
    downloads(harness).map((m) => m.url), ["https://example.test/second.mp4"],
  );
});

// ---------------------------------------------------------------------------
// The site adapter's own fallbacks survive the gate above
//
// Both restrictions apply to the direct DOM branch only. Firefox reaches its
// embedded-stream and detected-stream candidates through the same function,
// below that branch, and a blob: or unready element is exactly the case those
// fallbacks exist for - so gating the direct branch must not short-circuit
// past them.
// ---------------------------------------------------------------------------

const EMBEDDED_STREAM = "https://v.redd.it/embedded/HLSPlaylist.m3u8";
const DETECTED_STREAM = "https://v.redd.it/detected/HLSPlaylist.m3u8";

function withEmbeddedOwner(harness, video, url = EMBEDDED_STREAM) {
  const owner = new StubNode("DIV");
  owner["data-hls-url"] = url;
  harness.body.appendChild(owner);
  owner.appendChild(video);
  return owner;
}

function streamReply(streams) {
  return (message) => {
    if (message.type === "getDetectedStreams") return streams;
    if (message.type === "getSettings") return { mediaPillEnabled: true };
    return { ok: true };
  };
}

async function settle() {
  for (let i = 0; i < 4; i++) await new Promise((resolve) => setImmediate(resolve));
}

test("a stale source element never displaces the adapter's embedded stream", async () => {
  const video = staleMarkupVideo({ top: 200 });
  const harness = await loadMediaTab({
    href: "https://www.reddit.com/feed",
    videos: [video],
    reply: streamReply([]),
  });
  withEmbeddedOwner(harness, video);
  await settle();

  await clickPill(harness, video);

  assert.deepEqual(
    downloads(harness).map((m) => m.url), [EMBEDDED_STREAM],
    "the adapter's stream is the answer here, and the stale markup is not",
  );
});

test("a stale source element never displaces the adapter's detected stream", async () => {
  const video = staleMarkupVideo({ top: 200 });
  const harness = await loadMediaTab({
    href: "https://www.reddit.com/feed",
    videos: [video],
    reply: streamReply([{ url: DETECTED_STREAM }]),
  });
  await settle();

  await clickPill(harness, video);

  assert.deepEqual(downloads(harness).map((m) => m.url), [DETECTED_STREAM]);
});

test("the embedded stream still outranks the detected stream", async () => {
  const video = staleMarkupVideo({ top: 200 });
  const harness = await loadMediaTab({
    href: "https://www.reddit.com/feed",
    videos: [video],
    reply: streamReply([{ url: DETECTED_STREAM }]),
  });
  withEmbeddedOwner(harness, video);
  await settle();

  await clickPill(harness, video);

  assert.deepEqual(
    downloads(harness).map((m) => m.url), [EMBEDDED_STREAM],
    "adapter precedence is unchanged by the direct-branch gate",
  );
});

for (const readyState of [0, 1]) {
  test(`the adapter's embedded stream survives readyState ${readyState}`, async () => {
    // Baseline behaviour, pinned: an MSE player has no buffered data of its
    // own at this point, which is precisely when the adapter's stream is the
    // only usable answer. A readiness gate placed ahead of the adapter block
    // would take it away.
    const video = staleMarkupVideo({ top: 200, readyState });
    const harness = await loadMediaTab({
      href: "https://www.reddit.com/feed",
      videos: [video],
      reply: streamReply([]),
    });
    withEmbeddedOwner(harness, video);
    await settle();

    await clickPill(harness, video);

    assert.deepEqual(downloads(harness).map((m) => m.url), [EMBEDDED_STREAM]);
  });

  test(`the adapter's detected stream survives readyState ${readyState}`, async () => {
    const video = staleMarkupVideo({ top: 200, readyState });
    const harness = await loadMediaTab({
      href: "https://www.reddit.com/feed",
      videos: [video],
      reply: streamReply([{ url: DETECTED_STREAM }]),
    });
    await settle();

    await clickPill(harness, video);

    assert.deepEqual(downloads(harness).map((m) => m.url), [DETECTED_STREAM]);
  });
}

test("the adapter's page address still wins over everything", async () => {
  // sitePageUrl is resolved before candidateUrl is ever consulted, so the
  // direct-branch gate must be invisible to it.
  const video = staleMarkupVideo({ top: 200, readyState: 0 });
  const harness = await loadMediaTab({
    href: "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    videos: [video],
    reply: streamReply([{ url: DETECTED_STREAM }]),
  });
  await settle();

  await clickPill(harness, video);

  assert.deepEqual(
    downloads(harness).map((m) => m.url),
    ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"],
  );
});

test("with the adapter loaded and no stream of any kind nothing is handed over", async () => {
  const video = staleMarkupVideo({ top: 200 });
  const harness = await loadMediaTab({
    href: "https://www.reddit.com/feed",
    videos: [video],
    reply: streamReply([]),
  });
  await settle();

  await clickPill(harness, video);

  assert.deepEqual(downloads(harness).map((m) => m.url), []);
});

// ---------------------------------------------------------------------------
// The cached candidate cannot outlive its resource
//
// onPillClick prefers the URL captured at activation over re-resolving the
// element. That cache exists for a player that swaps its <video> out from
// under a click in flight - a case where the old element is detached and
// there is nothing left to re-resolve. It must not survive the element
// simply changing what it is playing: `emptied` on a still-present, no
// longer playing element only schedules a hide, so the pill stays clickable
// for the grace period with a URL that no longer describes anything.
// ---------------------------------------------------------------------------

// Clicks the pill that is already up, without hovering again. hover() would
// reactivate the video and refresh the cached URL, which is the whole thing
// under test here.
async function clickWithoutReactivating(host) {
  clickPrimary(host);
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

test("a cached direct URL is dropped when the resource becomes ineligible", async () => {
  const video = stubVideo({ top: 200 });
  const harness = await loadMediaTab({
    sites: false,
    href: "https://example.test/watch",
    videos: [video],
    reply: { ok: true },
  });
  const host = hover(harness, video);

  // The player switches to an MSE source. Nothing reactivates the pill.
  video.currentSrc = "blob:https://example.test/deadbeef";
  video.src = "";
  video.paused = true;
  video.dispatch("emptied", { target: video });

  await clickWithoutReactivating(host);

  assert.deepEqual(
    downloads(harness).map((m) => m.url), [],
    "the previous file is not what this element is on any more",
  );
});

test("a cached direct URL is dropped when the resource stops being ready", async () => {
  const video = stubVideo({ top: 200 });
  const harness = await loadMediaTab({
    sites: false,
    href: "https://example.test/watch",
    videos: [video],
    reply: { ok: true },
  });
  const host = hover(harness, video);

  video.readyState = 0;
  video.paused = true;
  video.dispatch("emptied", { target: video });

  await clickWithoutReactivating(host);

  assert.deepEqual(downloads(harness).map((m) => m.url), []);
});

test("a detached element still falls back to the URL it was activated on", async () => {
  // The cache's stated purpose, preserved: a dynamic player that tears the
  // element down mid-click leaves nothing to re-resolve, and the address
  // captured at activation is still the right answer.
  //
  // The element is torn down as well as removed. A detached element that
  // still holds its own resolvable source would be answered identically with
  // or without the cache, so the cache would not be under test at all.
  const video = stubVideo({ top: 200 });
  const harness = await loadMediaTab({
    sites: false,
    href: "https://example.test/watch",
    videos: [video],
    reply: { ok: true },
  });
  const host = hover(harness, video);

  video.isConnected = false;
  video.currentSrc = "";
  video.src = "";

  await clickWithoutReactivating(host);

  assert.deepEqual(
    downloads(harness).map((m) => m.url), ["https://example.test/clip.mp4"],
  );
});

test("a cached adapter stream survives an element that is still a blob", async () => {
  // Firefox: the cached URL came from the adapter, and the element being a
  // blob: is the normal steady state there, not a transition. Re-resolving
  // must return the same stream rather than throwing the candidate away.
  const video = staleMarkupVideo({ top: 200 });
  const harness = await loadMediaTab({
    href: "https://www.reddit.com/feed",
    videos: [video],
    reply: streamReply([{ url: DETECTED_STREAM }]),
  });
  await settle();
  const host = hover(harness, video);

  video.paused = true;
  video.dispatch("emptied", { target: video });

  await clickWithoutReactivating(host);

  assert.deepEqual(downloads(harness).map((m) => m.url), [DETECTED_STREAM]);
});

// ---------------------------------------------------------------------------
// The shared pill on Chrome
// ---------------------------------------------------------------------------
//
// Chrome runs this file with no site adapter, which the block above already
// covers in full and which stays the regression for every candidate rule. The
// one thing Chrome changes is the extension API global: Chromium exposes
// `chrome` and no `browser`.

test("the shared pill runs and hands over a direct video with chrome alone",
     async () => {
  const video = stubVideo({ top: 200 });
  video.currentSrc = "https://cdn.example.test/v/clip.mp4";
  video.src = "https://cdn.example.test/v/clip.mp4";
  const harness = await loadMediaTab({
    videos: [video],
    sites: false,
    chromeOnly: true,
    reply: (message) => (message.type === "getSettings"
      ? { mediaPillEnabled: true }
      : { ok: true }),
  });

  const host = await clickPill(harness, video);
  assert.ok(host, "the pill must exist without a `browser` global");

  const sent = harness.sent.find((m) => m.type === "downloadMedia");
  assert.equal(sent.url, "https://cdn.example.test/v/clip.mp4");
});

test("the shared pill stays inert with chrome alone when nothing is eligible",
     async () => {
  const video = stubVideo({ top: 200 });
  video.currentSrc = "blob:https://example.test/abcd";
  video.src = "blob:https://example.test/abcd";
  const harness = await loadMediaTab({
    videos: [video],
    sites: false,
    chromeOnly: true,
  });

  await clickPill(harness, video);

  assert.deepEqual(
    downloads(harness).map((m) => m.url), [],
    "a blob player has no direct candidate and no adapter behind it",
  );
});

// ---- Excluded domains and the in-page pill (issue #16 A1) ----
//
// The pill starts suppressed and is only turned on by an explicit, current
// allowing answer. These assert what the page shows, not an internal flag:
// a suppressed pill has no visible host at all.

const DENIED = { mediaPillEnabled: true, pillAllowed: false };
const ALLOWED = { mediaPillEnabled: true, pillAllowed: true };

function visiblePill(harness) {
  const host = harness.pillHost();
  return !!host && host.style.display === "block";
}

test("an excluded page shows no pill for a video already playing", async () => {
  const video = stubVideo({ top: 200 });
  const harness = await loadMediaTab({ videos: [video], settingsReply: DENIED });

  assert.equal(harness.pillHost(), null, "no pill host is created at all");
  harness.runTimers();
  assert.equal(visiblePill(harness), false);
  assert.deepEqual(harness.downloadMessages(), []);
});

test("an excluded page shows no pill for a video that starts later", async () => {
  const video = stubVideo({ top: 200 });
  const harness = await loadMediaTab({ videos: [video], settingsReply: DENIED });

  video.dispatch("playing", { target: video });
  harness.runTimers();
  assert.equal(visiblePill(harness), false);
  assert.equal(hover(harness, video), null, "hover cannot force it either");
  assert.deepEqual(harness.downloadMessages(), []);
});

test("no pill appears while the permission answer is still outstanding", async () => {
  const video = stubVideo({ top: 200 });
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  const harness = await loadMediaTab({
    videos: [video],
    settingsReply: () => held,
  });

  // The answer has not arrived. Permission is unknown, which is not permission.
  harness.runTimers();
  assert.equal(visiblePill(harness), false,
               "an excluded page must not flash a pill during the round-trip");

  release(DENIED);
  await harness.settle();
  harness.runTimers();
  assert.equal(visiblePill(harness), false);
});

test("a permission answer that never arrives leaves the pill suppressed", async () => {
  // Four separate transport failures. None of them is permission, and none of
  // them may be reported to the user as a broken Cove: an excluded page is a
  // setting, not a fault.
  const cases = [
    ["synchronous throw", null, "sync"],
    ["rejected promise", null, "reject"],
    ["a reply that is not an object", "not an object", false],
    ["a reply with no decision in it", { mediaPillEnabled: true }, false],
  ];
  for (const [name, reply, failure] of cases) {
    const video = stubVideo({ top: 200 });
    const harness = await loadMediaTab({
      videos: [video],
      settingsReply: reply === null ? ALLOWED : reply,
    });
    if (failure) {
      harness.setSendFailure(failure);
      await harness.changeSettings();
    }
    if (failure) {
      harness.runTimers();
      assert.equal(visiblePill(harness), false, name);
    } else {
      // The malformed reply was the answer to the initial request.
      harness.runTimers();
      assert.equal(visiblePill(harness), false, name);
    }
  }
});

test("an allowed page still gets exactly one pill", async () => {
  const video = stubVideo({ top: 200 });
  const harness = await loadMediaTab({ videos: [video], settingsReply: ALLOWED });
  harness.runTimers();

  assert.equal(visiblePill(harness), true);
  const hosts = harness.body.children.filter(
    (node) => node.className === "cove-media-tab-host");
  assert.equal(hosts.length, 1);
});

test("excluding a page hides its pill without a reload", async () => {
  const video = stubVideo({ top: 200 });
  let answer = ALLOWED;
  const harness = await loadMediaTab({
    videos: [video],
    settingsReply: () => answer,
  });
  harness.runTimers();
  assert.equal(visiblePill(harness), true, "precondition: the pill is up");

  answer = DENIED;
  await harness.changeSettings();
  harness.runTimers();
  assert.equal(visiblePill(harness), false);
});

test("removing an exclusion brings back exactly one pill", async () => {
  const video = stubVideo({ top: 200 });
  let answer = DENIED;
  const harness = await loadMediaTab({
    videos: [video],
    settingsReply: () => answer,
  });
  harness.runTimers();
  assert.equal(visiblePill(harness), false);

  answer = ALLOWED;
  await harness.changeSettings();
  harness.runTimers();
  assert.equal(visiblePill(harness), true);
  const hosts = harness.body.children.filter(
    (node) => node.className === "cove-media-tab-host");
  assert.equal(hosts.length, 1, "one pill, not one per settings change");
});

test("an allowing answer from before an exclusion cannot resurrect the pill", async () => {
  const video = stubVideo({ top: 200 });
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  let call = 0;
  const harness = await loadMediaTab({
    videos: [video],
    settingsReply: () => {
      call += 1;
      return call === 1 ? held : DENIED;
    },
  });

  // The exclusion lands while the first answer is still in flight, and the
  // requery it triggers is answered first.
  await harness.changeSettings();
  harness.runTimers();
  assert.equal(visiblePill(harness), false);

  // Only now does the older, allowing answer arrive.
  release(ALLOWED);
  await harness.settle();
  harness.runTimers();
  assert.equal(visiblePill(harness), false,
               "an answer to a superseded question is not permission");
});

test("an unrelated settings write cannot bypass an active exclusion", async () => {
  const video = stubVideo({ top: 200 });
  const harness = await loadMediaTab({ videos: [video], settingsReply: DENIED });

  // The user changes the minimum size. Nothing about the exclusion changed,
  // and re-asking is what keeps that true.
  await harness.changeSettings({
    settings: { newValue: { minSizeBytes: 4096, excludedDomains: ["example.test"] } },
  });
  harness.runTimers();
  assert.equal(visiblePill(harness), false);
});

test("a storage event the pill does not own costs nothing", async () => {
  const video = stubVideo({ top: 200 });
  const harness = await loadMediaTab({ videos: [video], settingsReply: ALLOWED });
  const before = harness.permissionRequests();

  await harness.changeSettings({ coveDiag: { newValue: [] } });
  await harness.changeSettings({ settings: { newValue: {} } }, "sync");
  assert.equal(harness.permissionRequests(), before,
               "only a local settings change revalidates");
});

test("late activity cannot revive a pill an exclusion took away", async () => {
  const video = stubVideo({ top: 200 });
  let answer = ALLOWED;
  const harness = await loadMediaTab({
    videos: [video],
    settingsReply: () => answer,
  });
  harness.runTimers();
  assert.equal(visiblePill(harness), true);

  answer = DENIED;
  await harness.changeSettings();

  // Every path that can put the pill up, delivered after the exclusion.
  harness.runTimers();
  video.dispatch("play", { target: video });
  video.dispatch("playing", { target: video });
  harness.pushMessage({ type: "coveStreamsUpdated", streams: [] });
  harness.win.dispatch("resize");
  harness.win.dispatch("scroll");
  hover(harness, video);
  harness.runTimers();

  assert.equal(visiblePill(harness), false);
  assert.deepEqual(harness.downloadMessages(), []);
});

test("a click on a pill an exclusion invalidated sends nothing", async () => {
  const video = stubVideo({ top: 200 });
  let answer = ALLOWED;
  const harness = await loadMediaTab({
    videos: [video],
    settingsReply: () => answer,
  });
  harness.runTimers();
  const host = harness.pillHost();
  assert.ok(host, "precondition: the pill is up");
  assert.ok(pillParts(host).primary, "precondition: the pill element exists");

  answer = DENIED;
  await harness.changeSettings();

  // The pill element is still in the shadow root; the user clicks it.
  clickPrimary(host);
  await harness.settle();

  assert.deepEqual(harness.downloadMessages(), [],
                   "an invalidated pill does not hand anything over");
});

test("a click on an allowed pill still hands over", async () => {
  // The control for the case above: the same gesture, on the same element, on
  // a page nothing excluded. Without this, "sent nothing" could just mean the
  // click never reached the handler.
  const video = stubVideo({ top: 200 });
  const harness = await loadMediaTab({
    videos: [video],
    settingsReply: ALLOWED,
  });
  harness.runTimers();
  const host = harness.pillHost();
  clickPrimary(host);
  await harness.settle();

  assert.equal(harness.downloadMessages().length, 1);
});

test("the pill toggle being off suppresses an otherwise allowed page", async () => {
  const video = stubVideo({ top: 200 });
  const harness = await loadMediaTab({
    videos: [video],
    settingsReply: { mediaPillEnabled: false, pillAllowed: true },
  });
  harness.runTimers();
  assert.equal(visiblePill(harness), false);
});

test("ordinary interception being off does not suppress the pill", async () => {
  const video = stubVideo({ top: 200 });
  const harness = await loadMediaTab({
    videos: [video],
    settingsReply: { enabled: false, mediaPillEnabled: true, pillAllowed: true },
  });
  harness.runTimers();
  assert.equal(visiblePill(harness), true,
               "the interception switch is not a pill master switch");
});

test("the pill asks the background a pill question", async () => {
  const harness = await loadMediaTab({ videos: [stubVideo({ top: 200 })] });
  const asked = harness.sent.filter((m) => m && m.type === "getSettings");
  assert.equal(asked.length, 1);
  assert.equal(asked[0].forPill, true,
               "the popup's answer is a different thing and must stay so");
});

test("permission is asked once, not per play, hover, scan or resize", async () => {
  const video = stubVideo({ top: 200 });
  const harness = await loadMediaTab({ videos: [video], settingsReply: ALLOWED });
  const after = harness.permissionRequests();
  assert.equal(after, 1);

  harness.runTimers();
  video.dispatch("play", { target: video });
  video.dispatch("playing", { target: video });
  hover(harness, video);
  harness.win.dispatch("resize");
  harness.win.dispatch("scroll");
  harness.runTimers();
  await harness.settle();

  assert.equal(harness.permissionRequests(), 1,
               "nothing about ordinary playback re-asks");
});

test("repeated identical settings notifications do not stack up pills", async () => {
  const video = stubVideo({ top: 200 });
  const harness = await loadMediaTab({ videos: [video], settingsReply: ALLOWED });
  for (let i = 0; i < 3; i += 1) await harness.changeSettings();
  harness.runTimers();

  const hosts = harness.body.children.filter(
    (node) => node.className === "cove-media-tab-host");
  assert.equal(hosts.length, 1);
  assert.equal(harness.permissionRequests(), 4);
});

test("the pill goes down the moment settings change, not when the answer arrives", async () => {
  // The gap between a settings write and the background's answer is the whole
  // problem: a pill left up across it is a pill the user already excluded.
  const video = stubVideo({ top: 200 });
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  let call = 0;
  const harness = await loadMediaTab({
    videos: [video],
    settingsReply: () => {
      call += 1;
      return call === 1 ? ALLOWED : held;
    },
  });
  harness.runTimers();
  assert.equal(visiblePill(harness), true, "precondition: the pill is up");

  await harness.changeSettings();
  harness.runTimers();
  assert.equal(visiblePill(harness), false,
               "suppressed while the new decision is still outstanding");

  release(ALLOWED);
  await harness.settle();
  harness.runTimers();
  assert.equal(visiblePill(harness), true, "and back once it is allowed again");
});

// ---------------------------------------------------------------------------
// "Exclude this site" on the pill (issue #16 A2).
//
// One discoverable action beside the download the pill already offers. The
// pill does not decide anything here: it asks the background which site it is
// on, then requests a separate extension-origin confirmation. Only that page
// may ask the background to persist the setting that eventually takes the pill
// away through the ordinary storage-change path.
// ---------------------------------------------------------------------------

const DENIED_PERMISSION = { mediaPillEnabled: true, pillAllowed: false };

function menuItem(host) {
  const { menu } = pillParts(host);
  return menu ? menu.children[0] : null;
}

function menuIsOpen(host) {
  const { menu, options } = pillParts(host);
  if (!menu) return false;
  return menu.style.display !== "none" &&
         options.getAttribute("aria-expanded") === "true";
}

// A genuine user activation. In a browser the trust flag is set by the browser
// itself and cannot be forged from script; this harness cannot manufacture one,
// so it stands in for a browser-generated event. It is the positive control for
// the logic only - the real trusted-activation proof is the Chrome and Firefox
// input-automation cases, not this stub.
function userActivate(node, type = "click", event = {}) {
  node.dispatch(type, {
    isTrusted: true,
    preventDefault() {},
    stopPropagation() {},
    ...event,
  });
}

// Opens the options menu the way a pointer user does, and lets the background's
// answer about which site this is settle.
async function openOptions(harness, host) {
  const { options } = pillParts(host);
  assert.ok(options, "expected the pill's options control");
  options.dispatch("click", {});
  await harness.settle();
  return options;
}

// A pill that is up and anchored, ready for either of its two controls.
async function pillUp(overrides = {}) {
  const video = stubVideo({ top: 200 });
  const harness = await loadMediaTab({ videos: [video], ...overrides });
  const host = hover(harness, video);
  assert.ok(host, "precondition: the pill is up");
  return { harness, host, video };
}

test("an eligible pill offers an options control beside the download", async () => {
  const { host } = await pillUp();
  const { primary, options } = pillParts(host);
  assert.ok(primary, "the download action is a control of its own");
  assert.ok(options, "and the options control sits beside it");
  assert.equal(options.getAttribute("aria-haspopup"), "menu");
  assert.equal(options.getAttribute("aria-expanded"), "false");
  assert.equal(options.getAttribute("aria-label"), "Cove pill options");
});

test("an excluded page offers no options control, because it offers no pill", async () => {
  const video = stubVideo({ top: 200 });
  const harness = await loadMediaTab({
    videos: [video], settingsReply: DENIED_PERMISSION,
  });
  hover(harness, video);
  const host = harness.pillHost();
  const { options } = pillParts(host);
  assert.equal(options, null, "nothing to exclude a site from");
  assert.deepEqual(harness.siteHostMessages(), [],
                   "and nothing asked the background which site this is");
});

test("the primary control still hands the video over, unchanged", async () => {
  const { harness, host } = await pillUp();
  clickPrimary(host);
  await harness.settle();
  assert.equal(harness.downloadMessages().length, 1);
});

test("opening the options menu hands nothing over", async () => {
  const { harness, host } = await pillUp();
  await openOptions(harness, host);
  assert.deepEqual(harness.downloadMessages(), [],
                   "the options control is not a second download button");
  assert.deepEqual(harness.excludeMessages(), [],
                   "and opening a menu excludes nothing on its own");
  assert.equal(harness.siteHostMessages().length, 1);
  assert.equal(menuIsOpen(host), true);
});

test("the menu names the top-level site the background reported", async () => {
  const { harness, host } = await pillUp({
    siteHostReply: { ok: true, host: "news.example.test" },
  });
  await openOptions(harness, host);
  const item = menuItem(host);
  assert.equal(item.getAttribute("role"), "menuitem");
  assert.equal(item.textContent, "Exclude news.example.test");
});

test("opening the menu again does not stack a second one", async () => {
  const { harness, host } = await pillUp();
  const options = await openOptions(harness, host);
  options.dispatch("click", {});
  await harness.settle();
  options.dispatch("click", {});
  await harness.settle();
  const menus = host.shadowRoot.children.filter((n) => n.className === "cove-menu");
  assert.equal(menus.length, 1);
  assert.equal(menuIsOpen(host), true, "the third click left it open");
  assert.equal(harness.siteHostMessages().length, 2,
               "one site question per opening, not per click");
});

// Each opening asks the background which site it is offering. Those answers can
// arrive in any order, and the menu the user is looking at is the one that asked
// last: an earlier opening's answer naming a different site must never relabel
// it, or the action reads as excluding a site it would not exclude.
async function reopenWithHeldHostAnswers() {
  const pending = [];
  const { harness, host } = await pillUp({
    siteHostReply: () => new Promise((resolve) => { pending.push(resolve); }),
  });
  const { options } = pillParts(host);
  options.dispatch("click", {});          // opening 1 - answer held
  await harness.settle();
  options.dispatch("click", {});          // closed again
  await harness.settle();
  options.dispatch("click", {});          // opening 2 - the one on screen
  await harness.settle();
  assert.equal(pending.length, 2, "one site question per opening");
  return { harness, host, pending };
}

test("a stale site answer arriving first cannot label a reopened menu", async () => {
  const { harness, host, pending } = await reopenWithHeldHostAnswers();
  pending[0]({ ok: true, host: "first.example.test" });
  await harness.settle();
  assert.equal(menuItem(host).textContent, "Exclude this site",
               "the first opening's answer is not this opening's");
  pending[1]({ ok: true, host: "second.example.test" });
  await harness.settle();
  assert.equal(menuItem(host).textContent, "Exclude second.example.test");
});

test("a stale site answer arriving last cannot relabel a reopened menu", async () => {
  const { harness, host, pending } = await reopenWithHeldHostAnswers();
  pending[1]({ ok: true, host: "second.example.test" });
  await harness.settle();
  assert.equal(menuItem(host).textContent, "Exclude second.example.test");
  pending[0]({ ok: true, host: "first.example.test" });
  await harness.settle();
  assert.equal(menuItem(host).textContent, "Exclude second.example.test",
               "the late answer belongs to an opening that is gone");
});

test("a second click on the options control closes the menu", async () => {
  const { harness, host } = await pillUp();
  const options = await openOptions(harness, host);
  options.dispatch("click", {});
  await harness.settle();
  assert.equal(menuIsOpen(host), false);
});

test("a click elsewhere on the page closes the menu", async () => {
  const { harness, host } = await pillUp();
  await openOptions(harness, host);
  harness.doc.dispatch("click", { target: harness.body });
  await harness.settle();
  assert.equal(menuIsOpen(host), false);
});

test("Escape closes the menu and gives focus back to the control", async () => {
  const { harness, host } = await pillUp();
  const options = await openOptions(harness, host);
  assert.equal(harness.doc.activeElement, menuItem(host),
               "opening moved focus into the menu");
  menuItem(host).dispatch("keydown", { key: "Escape", preventDefault() {} });
  await harness.settle();
  assert.equal(menuIsOpen(host), false);
  assert.equal(harness.doc.activeElement, options, "and focus is not lost");
});

for (const key of ["Enter", " "]) {
  test("the options control opens from the keyboard with " + JSON.stringify(key),
       async () => {
    const { harness, host } = await pillUp();
    const { options } = pillParts(host);
    let prevented = false;
    options.dispatch("keydown", { key, preventDefault() { prevented = true; } });
    await harness.settle();
    assert.equal(menuIsOpen(host), true);
    assert.equal(prevented, true,
                 "the key is consumed, so the browser does not also click it");
    assert.equal(harness.doc.activeElement, menuItem(host));
  });
}

test("tabbing out of the menu closes it", async () => {
  const { harness, host } = await pillUp();
  await openOptions(harness, host);
  harness.doc.dispatch("focusin", { target: harness.body });
  await harness.settle();
  assert.equal(menuIsOpen(host), false);
});

test("the pill does not time out from under an open menu", async () => {
  const { harness, host, video } = await pillUp();
  video.paused = true;
  await openOptions(harness, host);
  harness.runTimers();
  assert.notEqual(host.style.display, "none",
                  "a pill whose menu is open is not hover-dismissed");
});

test("closing the menu lets the pill time out again", async () => {
  const { harness, host, video } = await pillUp();
  const options = await openOptions(harness, host);
  video.paused = true;
  options.dispatch("click", {});
  await harness.settle();
  harness.runTimers();
  assert.equal(host.style.display, "none");
});

test("choosing the action asks the background to confirm the host it named", async () => {
  const { harness, host } = await pillUp({
    siteHostReply: { ok: true, host: "news.example.test" },
  });
  await openOptions(harness, host);
  userActivate(menuItem(host));
  await harness.settle();
  const asked = harness.excludeMessages();
  assert.equal(asked.length, 1);
  assert.equal(asked[0].expectHost, "news.example.test");
  assert.deepEqual(harness.downloadMessages(), [],
                   "and still nothing was handed to Cove");
  assert.equal(menuIsOpen(host), false);
});

test("a successful confirmation request leaves storage to take the pill away", async () => {
  // The content script must not hide the pill itself. If it did, a write that
  // never committed would still look like it worked, and the pill would come
  // back on the next navigation with the user believing the site was excluded.
  let answer = { mediaPillEnabled: true, pillAllowed: true };
  const { harness, host } = await pillUp({
    settingsReply: () => answer,
    siteHostReply: { ok: true, host: "news.example.test" },
  });
  await openOptions(harness, host);
  userActivate(menuItem(host));
  await harness.settle();

  assert.notEqual(host.style.display, "none",
                  "opening confirmation must not hide the pill");

  clickPrimary(host);
  await harness.settle();
  assert.equal(harness.downloadMessages().length, 1,
               "nothing local was switched off by the request itself");

  answer = DENIED_PERMISSION;
  await harness.changeSettings();
  assert.equal(host.style.display, "none",
               "the stored setting is what the pill actually obeys");
});

test("an exclusion the background refused is not reported as done", async () => {
  const { harness, host } = await pillUp({
    excludeReply: { ok: false, reason: "unsupported" },
  });
  await openOptions(harness, host);
  userActivate(menuItem(host));
  await harness.settle();
  assert.equal(pillParts(host).label.textContent, "Could not exclude this site");
  assert.notEqual(host.style.display, "none",
                  "a failed exclusion does not hide the pill as if it worked");
});

test("a background that never answers the exclusion is not a success either", async () => {
  const { harness, host } = await pillUp();
  await openOptions(harness, host);
  harness.setSendFailure("reject");
  userActivate(menuItem(host));
  await harness.settle();
  assert.equal(pillParts(host).label.textContent, "Could not exclude this site");
  assert.notEqual(host.style.display, "none");
});

test("a site the background will not name offers no exclusion to choose", async () => {
  const { harness, host } = await pillUp({
    siteHostReply: { ok: false, reason: "unsupported" },
  });
  await openOptions(harness, host);
  assert.equal(menuIsOpen(host), false);
  assert.deepEqual(harness.excludeMessages(), [],
                   "no host was named, so no host is excluded");
  assert.equal(pillParts(host).label.textContent, "Could not exclude this site");
});

test("a pill an exclusion took away does not leave its menu behind", async () => {
  let answer = { mediaPillEnabled: true, pillAllowed: true };
  const { harness, host } = await pillUp({ settingsReply: () => answer });
  await openOptions(harness, host);
  answer = DENIED_PERMISSION;
  await harness.changeSettings();
  assert.equal(menuIsOpen(host), false);
});

test("Chrome gets the same options control off the same file", async () => {
  const { harness, host } = await pillUp({
    chromeOnly: true,
    siteHostReply: { ok: true, host: "news.example.test" },
  });
  await openOptions(harness, host);
  assert.equal(menuItem(host).textContent, "Exclude news.example.test");
  userActivate(menuItem(host));
  await harness.settle();
  assert.equal(harness.excludeMessages().length, 1);
  assert.deepEqual(harness.downloadMessages(), []);
});

// The pill's visible surface and its clickable surface have to be the same
// surface. The two controls are real buttons now, so any padding left on the
// container around them is pill-coloured, pill-shaped space that looks pressable
// and does nothing - and it is exactly the space the download action used to
// own. There is no layout engine here, so this reads the rule the pill ships.
function pillStyleText(host) {
  const style = host.shadowRoot.children.find((n) => n.tagName === "STYLE");
  assert.ok(style, "expected the pill's own stylesheet in the shadow root");
  return style.textContent;
}

// The rule whose selector IS this one, anchored at the start of its line, so a
// descendant rule that merely mentions it is not mistaken for it.
function ruleBody(css, selector) {
  const start = ("\n" + css).indexOf("\n" + selector + " {");
  assert.notEqual(start, -1, "expected a " + selector + " rule");
  return css.slice(start, css.indexOf("}", start));
}

test("the pill container keeps no padding the controls cannot receive", async () => {
  const { host } = await pillUp();
  const css = pillStyleText(host);
  assert.match(ruleBody(css, ".cove-pill"), /padding:\s*0\b/,
               "container padding would be dead pill-shaped space");
});

test("each pill control covers its own visible segment", async () => {
  const { host } = await pillUp();
  const css = pillStyleText(host);
  for (const selector of [".cove-primary", ".cove-options"]) {
    const body = ruleBody(css, selector);
    const padding = body.match(/padding:\s*([^;]+);/);
    assert.ok(padding, selector + " must carry the padding the container gave up");
    assert.ok(/[1-9]/.test(padding[1]),
              selector + " padding must be a real hit area, got " + padding[1]);
  }
});

test("the hover affordance follows the controls, not the dead container", async () => {
  // A container that lights up on hover promises a hit target the container no
  // longer has.
  const { host } = await pillUp();
  const css = pillStyleText(host);
  assert.ok(!css.includes(".cove-pill:hover"),
            "the container must not advertise itself as pressable");
  assert.ok(css.includes(".cove-primary:hover") && css.includes(".cove-options:hover"),
            "each control advertises its own hit area");
});

test("a media event under an open menu does not take the action away", async () => {
  // Opening cancels the hide timer, but an autoplay preview that pauses (or a
  // player that is replaced) schedules a hide of its own afterwards. That must
  // not close a menu the user is reading.
  const { harness, host, video } = await pillUp();
  await openOptions(harness, host);
  video.paused = true;
  video.dispatch("pause", { target: video });
  harness.runTimers();
  assert.notEqual(host.style.display, "none", "the pill is still up");
  assert.equal(menuIsOpen(host), true, "and its menu is still open");
});

// ---- Trusted activation: the page is not the user ----

// The pill host is an open shadow root in the page's own DOM, so page script -
// including script in an embedded frame that has its own pill - can find these
// controls and activate them. Opening the menu that way costs nothing: it asks
// the background which site this is and nothing else. Recording an exclusion is
// different. It is a persistent settings write, and the only thing that
// separates the user from the page is whether the browser itself vouched for
// the event, which is knowable here and nowhere else.
async function menuOpenFor(overrides = {}) {
  const { harness, host } = await pillUp(overrides);
  await openOptions(harness, host);
  assert.equal(menuItem(host).textContent, "Exclude example.test",
               "precondition: the menu is open and offering the site");
  harness.excludeMessages().length = 0;
  return { harness, host };
}

function assertNothingExcluded(harness, host, why) {
  assert.deepEqual(harness.excludeMessages(), [], why);
  assert.ok(host.isConnected !== false, "and the pill was not taken away");
}

// T1 - the DOM API a page reaches for.
test("a page calling click() on the exclude item excludes nothing", async () => {
  const { harness, host } = await menuOpenFor();
  menuItem(host).click();
  await harness.settle();
  assertNothingExcluded(harness, host,
                        "element.click() is the page acting, not the user");
});

// T2 - a hand-built event, which is what a page does when click() is not enough.
test("a synthetic click event on the exclude item excludes nothing", async () => {
  const { harness, host } = await menuOpenFor();
  menuItem(host).dispatch("click", {
    isTrusted: false, preventDefault() {}, stopPropagation() {},
  });
  await harness.settle();
  assertNothingExcluded(harness, host, "a dispatched MouseEvent is not a user");
});

// An event with no trust flag at all must not be treated as trusted either:
// failing open here would make the guard depend on the attacker's thoroughness.
test("a click event with no trust flag excludes nothing", async () => {
  const { harness, host } = await menuOpenFor();
  menuItem(host).dispatch("click", { preventDefault() {}, stopPropagation() {} });
  await harness.settle();
  assertNothingExcluded(harness, host, "absent is not trusted");
});

// T3 - the keyboard route, synthetically. A real Enter on a focused native
// button makes the browser emit a trusted click; a dispatched keydown emits
// nothing, and must not be turned into an activation here either.
for (const key of ["Enter", " "]) {
  test("a synthetic " + JSON.stringify(key) + " keydown excludes nothing", async () => {
    const { harness, host } = await menuOpenFor();
    menuItem(host).dispatch("keydown", {
      key, isTrusted: false, preventDefault() {}, stopPropagation() {},
    });
    await harness.settle();
    assertNothingExcluded(harness, host, "a dispatched keydown is not a user");
  });
}

// T6 - the case that makes this more than a site opting itself out. An embedded
// player's frame runs its own copy of the content script and gets its own pill,
// but the exclusion the background records is the TOP-LEVEL host. So a hostile
// frame activating its own pill from script would disable Cove for the site
// that embedded it. The guard is what stops that, and it has to hold in a frame
// exactly as it does at the top. The cross-origin half of this is proven in the
// browser cases; what is pinned here is that the frame's own script gets no
// further than the top-level page's would.
test("a hostile embedded frame cannot exclude the site that embeds it", async () => {
  const { harness, host } = await menuOpenFor({
    href: "https://player.example.test/embed",
    siteHostReply: { ok: true, host: "example.test" },
  });
  menuItem(host).click();
  menuItem(host).dispatch("click", {
    isTrusted: false, preventDefault() {}, stopPropagation() {},
  });
  await harness.settle();
  assert.deepEqual(harness.excludeMessages(), [],
                   "script in the frame never reached the settings write");

  // Positive control in the same context: the guard rejects the page, not the
  // frame. A real user inside that frame can still exclude.
  userActivate(menuItem(host));
  await harness.settle();
  assert.equal(harness.excludeMessages().length, 1,
               "and a genuine activation in that same frame still works");
});

// T4 - the positive control, and that one activation is one request.
test("a trusted activation sends exactly one exclusion request", async () => {
  const { harness, host } = await menuOpenFor();
  userActivate(menuItem(host));
  await harness.settle();
  const asked = harness.excludeMessages();
  assert.equal(asked.length, 1, "one user action, one request");
  assert.equal(asked[0].expectHost, "example.test");
});

// T5 - the keyboard positive. A focused native button turns a real Enter into a
// browser-generated click, so this is the event the accessible path delivers.
// That the browser generates it at all is proven in Chrome and Firefox, not here.
test("a trusted keyboard activation sends exactly one exclusion request", async () => {
  const { harness, host } = await menuOpenFor();
  const item = menuItem(host);
  assert.equal(harness.doc.activeElement, item, "opening focused the item");
  item.dispatch("keydown", {
    key: "Enter", isTrusted: true, preventDefault() {}, stopPropagation() {},
  });
  userActivate(item); // the click the browser emits for that keystroke
  await harness.settle();
  assert.equal(harness.excludeMessages().length, 1,
               "the keydown must not activate separately from the click");
});
