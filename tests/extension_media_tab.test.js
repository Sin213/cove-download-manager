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
    const match = String(selector).match(/^a\[href\*="([^"]+)"\]$/);
    if (!match) return null;
    const needle = match[1];
    const walk = (node) => {
      for (const child of node.children) {
        const href = child.tagName === "A" ? child.getAttribute("href") : null;
        if (href && href.includes(needle)) return child;
        const found = walk(child);
        if (found) return found;
      }
      return null;
    };
    return walk(this);
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
function stubVideo({ top = 100, width = 640, height = 360 } = {}) {
  const video = new StubNode("VIDEO");
  video.paused = false;
  video.ended = false;
  video.currentSrc = "https://example.test/clip.mp4";
  video.src = "https://example.test/clip.mp4";
  video.scrollTo = (nextTop) => {
    video.rect = { ...video.rect, top: nextTop, bottom: nextTop + height };
  };
  video.rect = { top, left: 0, width, height, right: width, bottom: top + height };
  return video;
}

function loadMediaTab({ href = "https://example.test/watch", videos = [],
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

  const source = fs.readFileSync("extension/content/media-tab.js", "utf8");
  vm.runInContext(source, context, { filename: "extension/content/media-tab.js" });

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
  // is reached through extractorPageUrl, not through the removed fallback.
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
// Reddit feed
//
// The feed's player is MSE, so nothing on the page names the media. The post
// it belongs to does, and yt-dlp can resolve that - which also muxes Reddit's
// separate audio track. Lookup is ancestor-scoped: a feed holds many posts.
// ---------------------------------------------------------------------------

function redditCard({ tag = "SHREDDIT-POST", attrs = {}, link = "" } = {}) {
  const card = new StubNode(tag);
  for (const [name, value] of Object.entries(attrs)) card[name] = value;
  if (link) {
    const anchor = new StubNode("A");
    anchor.href = link;
    card.appendChild(anchor);
  }
  return card;
}

function feedHarness(cards, tops = null) {
  const videos = cards.map((_card, index) =>
    blobVideo({ top: tops ? tops[index] : 200 })
  );
  const harness = loadMediaTab({ href: "https://www.reddit.com/", videos });
  // loadMediaTab reparents every video onto the body, so the cards have to be
  // wired up afterwards or closest() never reaches them.
  cards.forEach((card, index) => {
    harness.body.appendChild(card);
    card.appendChild(videos[index]);
  });
  return { harness, videos };
}

test("a new-Reddit feed card resolves its own permalink", async () => {
  const card = redditCard({ attrs: { permalink: "/r/aww/comments/abc123/a_title/" } });
  const { harness, videos } = feedHarness([card]);

  await clickPill(harness, videos[0]);

  const download = harness.sent.find((m) => m.type === "downloadMedia");
  assert.equal(download.url, "https://www.reddit.com/r/aww/comments/abc123/a_title/");
});

test("an old-Reddit feed card resolves its own permalink", async () => {
  const card = redditCard({
    tag: "DIV",
    attrs: { "data-permalink": "/r/aww/comments/abc123/a_title/" },
  });
  const { harness, videos } = feedHarness([card]);

  await clickPill(harness, videos[0]);

  const download = harness.sent.find((m) => m.type === "downloadMedia");
  assert.equal(download.url, "https://www.reddit.com/r/aww/comments/abc123/a_title/");
});

test("a card with only a comments link still resolves", async () => {
  const card = redditCard({ tag: "ARTICLE", link: "/r/aww/comments/abc123/a_title/" });
  const { harness, videos } = feedHarness([card]);

  await clickPill(harness, videos[0]);

  const download = harness.sent.find((m) => m.type === "downloadMedia");
  assert.equal(download.url, "https://www.reddit.com/r/aww/comments/abc123/a_title/");
});

test("a feed of several posts resolves the clicked one", async () => {
  const first = redditCard({ attrs: { permalink: "/r/aww/comments/first/one/" } });
  const second = redditCard({ attrs: { permalink: "/r/aww/comments/second/two/" } });
  const third = redditCard({ attrs: { permalink: "/r/aww/comments/third/three/" } });
  // Scrolled so only the middle post is in view. Identical positions would
  // leave the pill's own target heuristic to break the tie, which is not what
  // this test is about - it is about the permalink following the pill.
  const { harness, videos } = feedHarness([first, second, third], [-600, 200, 2000]);

  await clickPill(harness, videos[1]);

  const download = harness.sent.find((m) => m.type === "downloadMedia");
  assert.equal(
    download.url, "https://www.reddit.com/r/aww/comments/second/two/",
    "the pill must download the post it was clicked on"
  );
});

test("a video in no recognisable card still reports no video", async () => {
  const video = blobVideo({ top: 200 });
  const harness = loadMediaTab({ href: "https://www.reddit.com/", videos: [video] });

  const host = await clickPill(harness, video);

  assert.equal(harness.sent.find((m) => m.type === "downloadMedia"), undefined);
  const pill = host.shadowRoot.children.find((n) => n.className === "cove-pill");
  assert.equal(pill.children[0].textContent, "No video found");
});

test("a thread page sends the playlist and never the permalink", async () => {
  // Inside a thread the player names its own stream, which is the path that
  // already works. The permalink must not displace it.
  const card = redditCard({ attrs: { permalink: "/r/aww/comments/abc123/a_title/" } });
  const video = blobVideo({ top: 200 });
  video["data-hls-url"] = "https://v.redd.it/abc123/HLSPlaylist.m3u8";
  card.appendChild(video);
  const harness = loadMediaTab({
    href: "https://www.reddit.com/r/aww/comments/abc123/a_title/",
    videos: [video],
  });
  harness.body.appendChild(card);

  await clickPill(harness, video);

  const download = harness.sent.find((m) => m.type === "downloadMedia");
  assert.equal(download.url, "https://v.redd.it/abc123/HLSPlaylist.m3u8");
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
// Reddit-hosted video, without the API
//
// Reddit's JSON API refuses whole networks, so resolving a post through it is
// unreliable even when logged in. A Reddit-hosted video does not need it: the
// post card names the v.redd.it id, and that id's HLS playlist is public,
// carries audio, and goes down the path Cove already uses for streams.
// ---------------------------------------------------------------------------

test("an old-Reddit card with a v.redd.it link yields its playlist", async () => {
  const card = redditCard({
    tag: "DIV",
    attrs: {
      "data-url": "https://v.redd.it/dzbdjfbrwuhh1",
      "data-permalink": "/r/aww/comments/abc123/a_title/",
    },
  });
  const { harness, videos } = feedHarness([card]);

  await clickPill(harness, videos[0]);

  const download = harness.sent.find((m) => m.type === "downloadMedia");
  assert.equal(
    download.url, "https://v.redd.it/dzbdjfbrwuhh1/HLSPlaylist.m3u8",
    "a Reddit-hosted video should not need the API at all"
  );
});

test("a trailing slash or query on the card link is tolerated", async () => {
  const card = redditCard({
    tag: "DIV",
    attrs: {
      "data-url": "https://v.redd.it/dzbdjfbrwuhh1/?utm_source=share",
      "data-permalink": "/r/aww/comments/abc123/a_title/",
    },
  });
  const { harness, videos } = feedHarness([card]);

  await clickPill(harness, videos[0]);

  const download = harness.sent.find((m) => m.type === "downloadMedia");
  assert.equal(download.url, "https://v.redd.it/dzbdjfbrwuhh1/HLSPlaylist.m3u8");
});

test("a card linking somewhere else still falls back to the permalink", async () => {
  // A YouTube or imgur post is not Reddit-hosted, so the extractor has to
  // resolve it from the post instead.
  const card = redditCard({
    tag: "DIV",
    attrs: {
      "data-url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
      "data-permalink": "/r/aww/comments/abc123/a_title/",
    },
  });
  const { harness, videos } = feedHarness([card]);

  await clickPill(harness, videos[0]);

  const download = harness.sent.find((m) => m.type === "downloadMedia");
  assert.equal(download.url, "https://www.reddit.com/r/aww/comments/abc123/a_title/");
});
