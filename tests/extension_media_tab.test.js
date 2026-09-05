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

  setAttribute() {}

  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }

  removeEventListener() {}

  dispatch(type, event = {}) {
    for (const listener of this.listeners.get(type) || []) listener(event);
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
    return name in this ? this[name] : null;
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
function loadMediaTab({ href = "https://example.test/watch", videos = [],
                       sites = true,
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
    createElement: (tag) => new StubNode(tag),
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
  const browser = {
    runtime: {
      id: "cove-test",
      async sendMessage(message) {
        sent.push(message);
        if (typeof reply === "function") return reply(message);
        if (message && message.type === "getSettings") {
          return { mediaPillEnabled: true };
        }
        return reply;
      },
      onMessage: { addListener() {} },
    },
    storage: { onChanged: { addListener() {} } },
  };

  const context = vm.createContext({
    globalThis: undefined,
    browser,
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
  return { doc, win, body, pillHost, runTimers, timers, sent };
}

// Brings a video up as the active pill target through the hover path.
function hover(harness, video) {
  harness.doc.dispatch("mouseover", { target: video });
  const host = harness.pillHost();
  if (host) host.rect = { top: 0, left: 0, width: 160, height: 30, right: 160, bottom: 30 };
  harness.doc.dispatch("mouseover", { target: video });
  return harness.pillHost();
}

test("the pill is anchored above its video while the video is in view", () => {
  const video = stubVideo({ top: 200 });
  const harness = loadMediaTab({ videos: [video] });
  const host = hover(harness, video);

  assert.ok(host, "expected a pill host to be created");
  assert.equal(host.style.display, "block");
  // 200 (video top) - 30 (pill height) - 8 (gap)
  assert.equal(host.style.top, "162px");
});

test("the pill hides instead of pinning itself to the top of the viewport", () => {
  const video = stubVideo({ top: 200 });
  const harness = loadMediaTab({ videos: [video] });
  const host = hover(harness, video);
  assert.equal(host.style.display, "block");

  // Scroll the video off the top of the viewport.
  video.scrollTo(-400);
  harness.win.dispatch("scroll");

  assert.equal(host.style.display, "none");
  assert.notEqual(host.style.top, "4px");
});

test("the pill comes back when its video scrolls into view again", () => {
  const video = stubVideo({ top: 200 });
  const harness = loadMediaTab({ videos: [video] });
  const host = hover(harness, video);

  video.scrollTo(-400);
  harness.win.dispatch("scroll");
  assert.equal(host.style.display, "none");

  video.scrollTo(200);
  harness.win.dispatch("scroll");

  assert.equal(host.style.display, "block");
  assert.equal(host.style.top, "162px");
});

test("a video that starts playing off-screen gets its pill on scroll-in", () => {
  const video = stubVideo({ top: 900 }); // below a 720px viewport
  const harness = loadMediaTab({ videos: [video] });
  harness.doc.dispatch("mouseover", { target: video });
  assert.equal(harness.pillHost(), null, "no pill host is created off-screen");

  video.scrollTo(200);
  harness.win.dispatch("scroll");

  const host = harness.pillHost();
  assert.ok(host, "expected the pill to appear once the video scrolled in");
  assert.equal(host.style.display, "block");
});

test("a visible playing video wins the pill over a bigger off-screen one", () => {
  const visible = stubVideo({ top: 100, width: 640, height: 360 });
  const offscreen = stubVideo({ top: 900, width: 1280, height: 720 });
  for (const video of [visible, offscreen]) video.readyState = 4;
  // Registration order makes the off-screen video the last one to claim the
  // pill, so the bounded startup scans are what must hand it back.
  const harness = loadMediaTab({ videos: [visible, offscreen] });
  harness.runTimers();

  const host = harness.pillHost();
  assert.ok(host, "expected a pill for the visible video");
  assert.equal(host.style.display, "block");
  // 100 (visible video top) - 30 (pill height) - 8 (gap)
  assert.equal(host.style.top, "62px");
});

test("a stopped video hands the pill to a visible video, not an off-screen one", () => {
  const offscreen = stubVideo({ top: 900, width: 1280, height: 720 });
  const visible = stubVideo({ top: 300, width: 640, height: 360 });
  const active = stubVideo({ top: 100, width: 640, height: 360 });
  for (const video of [offscreen, visible, active]) video.readyState = 4;
  const harness = loadMediaTab({ videos: [offscreen, visible, active] });

  active.paused = true;
  active.dispatch("pause", { target: active });

  const host = harness.pillHost();
  assert.equal(host.style.display, "block");
  // 300 (visible video top) - 30 (pill height) - 8 (gap)
  assert.equal(host.style.top, "262px");
});

test("a small fully visible video outranks a large mostly off-screen one", () => {
  // 1920x1080 with only ~40% on screen still has more visible pixels than a
  // fully visible 640x360, so visible area alone would pick the wrong one.
  const big = stubVideo({ top: 260, width: 1920, height: 1080 });
  const small = stubVideo({ top: 100, width: 640, height: 360 });
  for (const video of [big, small]) video.readyState = 4;
  const harness = loadMediaTab({ videos: [small, big] });
  harness.runTimers();

  const host = harness.pillHost();
  assert.equal(host.style.display, "block");
  // 100 (small video top) - 30 (pill height) - 8 (gap)
  assert.equal(host.style.top, "62px");
});

test("an off-screen video starting playback does not steal the pill", () => {
  const visible = stubVideo({ top: 100, width: 640, height: 360 });
  const offscreen = stubVideo({ top: 900, width: 1280, height: 720 });
  visible.readyState = 4;
  offscreen.readyState = 4;
  offscreen.paused = true;
  const harness = loadMediaTab({ videos: [visible, offscreen] });

  offscreen.paused = false;
  offscreen.dispatch("playing", { target: offscreen });

  const host = harness.pillHost();
  assert.equal(host.style.display, "block");
  // 100 (visible video top) - 30 (pill height) - 8 (gap)
  assert.equal(host.style.top, "62px");
});

test("hovering a visible video takes the pill from an off-screen player", () => {
  const playing = stubVideo({ top: 100, width: 640, height: 360 });
  const hovered = stubVideo({ top: 300, width: 640, height: 360 });
  playing.readyState = 4;
  hovered.paused = true;
  const harness = loadMediaTab({ videos: [playing, hovered] });

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

test("a partly scrolled video keeps the pill inside its visible band", () => {
  // 260 of 360 px still on screen: eligible, but "above the video" is off
  // the top of the viewport.
  const video = stubVideo({ top: 200, height: 360 });
  const harness = loadMediaTab({ videos: [video] });
  hover(harness, video);

  video.scrollTo(-100);
  harness.win.dispatch("scroll");

  const host = harness.pillHost();
  assert.equal(host.style.display, "block");
  // Just inside the visible top edge of the video, not clamped to y=4.
  assert.equal(host.style.top, "8px");
});

test("scrolling the active video away hands the pill to a visible one", () => {
  const active = stubVideo({ top: 100, width: 640, height: 360 });
  const other = stubVideo({ top: 400, width: 640, height: 360 });
  active.readyState = 4;
  other.readyState = 4;
  const harness = loadMediaTab({ videos: [other, active] });

  active.scrollTo(-400);
  harness.win.dispatch("scroll");

  const host = harness.pillHost();
  assert.equal(host.style.display, "block");
  // 400 (other video top) - 30 (pill height) - 8 (gap)
  assert.equal(host.style.top, "362px");
});

test("a video scrolled just past the halfway mark hides the pill", () => {
  const video = stubVideo({ top: 0, height: 360 });
  const harness = loadMediaTab({ videos: [video] });
  const host = hover(harness, video);
  assert.equal(host.style.display, "block");

  video.scrollTo(-200); // 160 of 360 px visible
  harness.win.dispatch("scroll");

  assert.equal(host.style.display, "none");
});

test("a paused feed preview keeps the pill up long enough to click it", () => {
  const video = stubVideo({ top: 200 });
  const harness = loadMediaTab({ videos: [video] });
  const host = hover(harness, video);
  assert.equal(host.style.display, "block");

  // YouTube tears its inline preview down as soon as the pointer leaves the
  // thumbnail, which is exactly when the pointer is travelling to the pill.
  video.paused = true;
  video.dispatch("pause", { target: video });

  assert.equal(host.style.display, "block");
});

test("a torn-down feed preview hides the pill once the grace period expires", () => {
  const video = stubVideo({ top: 200 });
  const harness = loadMediaTab({ videos: [video] });
  const host = hover(harness, video);

  video.paused = true;
  video.dispatch("pause", { target: video });
  harness.runTimers();

  assert.equal(host.style.display, "none");
});

test("a detached preview that never paused still times out", () => {
  const video = stubVideo({ top: 200 });
  const harness = loadMediaTab({ videos: [video] });
  const host = hover(harness, video);

  // YouTube removes the inline preview element without pausing it first.
  video.isConnected = false;
  video.dispatch("emptied", { target: video });
  harness.runTimers();

  assert.equal(host.style.display, "none");
});

test("hovering the pill itself cancels the pending hide", () => {
  const video = stubVideo({ top: 200 });
  const harness = loadMediaTab({ videos: [video] });
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
  const pill = host.shadowRoot.children.find(
    (n) => n.className === "cove-pill"
  );
  assert.ok(pill, "expected the pill element inside the shadow root");
  pill.dispatch("click", {});
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  return host;
}

test("a pill download records a request with a generated request id", async () => {
  const video = stubVideo({ top: 200, src: "https://cdn.example.test/v/movie.mp4" });
  const harness = loadMediaTab({ videos: [video], reply: { ok: true } });
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
  const harness = loadMediaTab({ videos: [video], reply: { ok: true } });
  await clickPill(harness, video);

  const download = harness.sent.find((m) => m.type === "downloadMedia");
  const result = diagMessages(harness).find((m) => m.event === "video_pill_result");
  assert.equal(result.requestId, download.requestId);
  assert.equal(result.fields.result, "sent");
});

test("an unavailable Cove is recorded as such by the pill", async () => {
  const video = stubVideo({ top: 200, src: "https://cdn.example.test/v/movie.mp4" });
  const harness = loadMediaTab({
    videos: [video],
    reply: { ok: false, reason: "unavailable" },
  });
  await clickPill(harness, video);

  const result = diagMessages(harness).find((m) => m.event === "video_pill_result");
  assert.equal(result.fields.result, "unavailable");
});

test("a background script that never answers is recorded as unavailable", async () => {
  const video = stubVideo({ top: 200, src: "https://cdn.example.test/v/movie.mp4" });
  const harness = loadMediaTab({
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
  const harness = loadMediaTab({
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
  const harness = loadMediaTab({
    videos: [video],
    reply: { ok: false, reason: "unavailable" },
  });
  const host = await clickPill(harness, video);
  const pill = host.shadowRoot.children.find((n) => n.className === "cove-pill");
  const label = pill.children[0];
  assert.equal(label.textContent, "Cove is not running");
});

test("a diagnostics send failure never breaks a pill download", async () => {
  const video = stubVideo({ top: 200, src: "https://cdn.example.test/v/movie.mp4" });
  const harness = loadMediaTab({
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
  const harness = loadMediaTab({
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
  const harness = loadMediaTab({
    href: "https://www.reddit.com/",
    videos: [video],
    reply: { ok: true },
  });

  const host = await clickPill(harness, video);

  const pill = host.shadowRoot.children.find((n) => n.className === "cove-pill");
  const label = pill.children[0];
  assert.equal(label.textContent, "No video found");
});

test("an extractor-backed page still downloads from its page address", async () => {
  // The fallback's stated purpose. YouTube replaces the media element while
  // its controls are used, so the page address is the stable target - and it
  // is reached through the site adapter, not through the removed fallback.
  const video = blobVideo({ top: 200 });
  const harness = loadMediaTab({
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
  const harness = loadMediaTab({
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
  const harness = loadMediaTab({
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
  const harness = loadMediaTab({
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
  const harness = loadMediaTab({
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
  const pill = host.shadowRoot.children.find((n) => n.className === "cove-pill");
  assert.equal(pill.children[0].textContent, "No video found");
});

test("without a site adapter an embedded stream attribute is never read", async () => {
  const owner = new StubNode("DIV");
  owner["data-hls-url"] = "https://v.redd.it/first/HLSPlaylist.m3u8";
  const video = blobVideo({ top: 200 });

  const harness = loadMediaTab({
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

test("without a site adapter no detected-stream traffic is generated", () => {
  const video = stubVideo({ top: 200 });
  const harness = loadMediaTab({
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

test("with the site adapter the detected-stream fetch still happens", () => {
  // The counterpart of the assertion above: the Firefox path is unchanged.
  const video = stubVideo({ top: 200 });
  const harness = loadMediaTab({ href: "https://example.test/watch", videos: [video] });

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

  const harness = loadMediaTab({
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
  const pill = host.shadowRoot.children.find((n) => n.className === "cove-pill");
  assert.equal(pill.children[0].textContent, "No video found");
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

test("the stale-markup fixture really does expose a source element", () => {
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
  const harness = loadMediaTab({
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
  const harness = loadMediaTab({
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
    const harness = loadMediaTab({
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
  const harness = loadMediaTab({
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
  const harness = loadMediaTab({
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
  const harness = loadMediaTab({
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
  const harness = loadMediaTab({
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
  const harness = loadMediaTab({
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
  const harness = loadMediaTab({
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
  const harness = loadMediaTab({
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
  const harness = loadMediaTab({
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
  const harness = loadMediaTab({
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
    const harness = loadMediaTab({
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
    const harness = loadMediaTab({
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
  const harness = loadMediaTab({
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
  const harness = loadMediaTab({
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
  const pill = host.shadowRoot.children.find((n) => n.className === "cove-pill");
  assert.ok(pill, "expected the pill element inside the shadow root");
  pill.dispatch("click", {});
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

test("a cached direct URL is dropped when the resource becomes ineligible", async () => {
  const video = stubVideo({ top: 200 });
  const harness = loadMediaTab({
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
  const harness = loadMediaTab({
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
  const harness = loadMediaTab({
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
  const harness = loadMediaTab({
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
