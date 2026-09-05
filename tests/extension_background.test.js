const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");

function event() {
  const listeners = [];
  return {
    addListener(listener) { listeners.push(listener); },
    emit(...args) { return listeners.map((listener) => listener(...args)); },
  };
}

// Top-level const/let in a vm script land in the context's global lexical
// scope, which is shared between scripts but is not reachable as a property of
// the context object. Reading them back needs an evaluation in that scope.
function evalIn(context, source) {
  return vm.runInContext(source, context);
}

function loadBackground({ nativeResult = { status: "ok" }, settings,
                         breakStorage = false, slowStorage = false,
                         storedDiag = null, media = true, cookies = [],
                         tabs = [], downloadSearch = () => [],
                         storedIntercepted = null, eraseThrows = false,
                         slowInterceptedIds = false } = {}) {
  const calls = { native: [], cancel: [], erase: [], menus: [] };
  const events = {
    downloadCreated: event(),
    downloadChanged: event(),
    contextMenuClicked: event(),
    downloadErased: event(),
    message: event(),
  };
  const browserDownloads = [];
  const quietEvent = () => event();
  // A real key/value store, so the diagnostics ring can be inspected the way
  // the popup would read it back.
  const store = {
    data: {},
    async get(key) {
      if (key === "settings") return settings ? { settings } : {};
      if (slowInterceptedIds && key === "_interceptedIds") {
        for (let i = 0; i < 8; i += 1) await Promise.resolve();
      }
      if (slowStorage && key === "coveDiag") {
        // Hydration that lands well after the background has started
        // recording, which is the ordering the real storage API produces.
        for (let i = 0; i < 8; i += 1) await Promise.resolve();
      }
      return key in store.data ? { [key]: store.data[key] } : {};
    },
    async set(obj) {
      if (breakStorage) throw new Error("QuotaExceededError");
      Object.assign(store.data, obj);
    },
    async remove(key) { delete store.data[key]; },
  };
  const badge = { text: [], colors: [] };
  const browser = {
    action: {
      async setBadgeText({ text }) { badge.text.push(text); },
      async setBadgeBackgroundColor({ color }) { badge.colors.push(color); },
    },
    commands: { onCommand: quietEvent() },
    contextMenus: {
      create(props) { calls.menus.push(props); },
      onClicked: events.contextMenuClicked,
    },
    cookies: { async getAll() { return cookies; } },
    downloads: {
      onCreated: events.downloadCreated,
      onChanged: events.downloadChanged,
      onErased: events.downloadErased,
      async cancel(id) { calls.cancel.push(id); },
      async erase(query) {
        if (eraseThrows) throw new Error("erase failed");
        calls.erase.push(query);
      },
      async search(query) { return downloadSearch(query); },
      async download(options) { browserDownloads.push(options); },
    },
    notifications: { async create() {} },
    runtime: {
      lastError: null,
      getManifest: () => ({ version: "1.4.4" }),
      onInstalled: quietEvent(),
      onMessage: events.message,
      async sendNativeMessage(_host, message) {
        calls.native.push(message);
        return typeof nativeResult === "function" ? nativeResult(message) : nativeResult;
      },
    },
    storage: {
      local: store,
      session: store,
      onChanged: quietEvent(),
    },
    tabs: {
      async query() { return tabs; },
      async sendMessage() {},
      onRemoved: quietEvent(),
      onUpdated: quietEvent(),
      onActivated: quietEvent(),
    },
    webRequest: { onHeadersReceived: quietEvent() },
  };
  const context = vm.createContext({
    browser,
    console: { log() {}, error() {} },
    navigator: {
      userAgent: "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
    },
    URL,
    setTimeout,
    clearTimeout,
  });
  // The browser loads extension/diagnostics.js into the background context
  // before background.js runs (importScripts on Chrome, a script element on
  // the Firefox background page). Mirror that ordering here.
  vm.runInContext(
    fs.readFileSync("extension/diagnostics.js", "utf8"),
    context,
    { filename: "extension/diagnostics.js" },
  );
  if (storedDiag) store.data.coveDiag = storedDiag;
  if (storedIntercepted) store.data._interceptedIds = storedIntercepted;
  // The media runtime is split in two: media-core.js holds browser-neutral
  // mechanics and media-sites.js holds the Firefox-only site/extractor/stream
  // capability. The MV2 manifest lists both ahead of background.js, in that
  // order, so mirror it here. `media: false` reproduces the Chrome bundle,
  // whose MV3 manifest loads neither (scripts/build_extension.py).
  if (media) {
    for (const script of ["extension/media-core.js", "extension/media-sites.js"]) {
      vm.runInContext(fs.readFileSync(script, "utf8"), context, { filename: script });
    }
  }
  const source = fs.readFileSync("extension/background.js", "utf8");
  vm.runInContext(source, context, { filename: "extension/background.js" });
  return { calls, events, browserDownloads, store, context, badge };
}

async function settle() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

test("restored Chrome download history is never sent to Cove", async () => {
  const { calls, events } = loadBackground();
  await settle();
  calls.native.length = 0; // Ignore the startup ping.

  events.downloadCreated.emit({
    id: 1,
    url: "https://example.test/archive.zip",
    filename: "archive.zip",
    state: "complete",
    startTime: new Date().toISOString(),
    totalBytes: 2_000_000,
  });
  events.downloadCreated.emit({
    id: 2,
    url: "https://example.test/old.zip",
    filename: "old.zip",
    state: "in_progress",
    startTime: new Date(Date.now() - 60_000).toISOString(),
    totalBytes: 2_000_000,
  });
  await settle();

  assert.equal(calls.native.length, 0);
  assert.deepEqual(calls.cancel, []);
});

test("a fresh eligible download is sent once and then cancelled", async () => {
  const { calls, events } = loadBackground();
  await settle();
  calls.native.length = 0;
  const item = {
    id: 3,
    url: "https://example.test/fresh.zip",
    filename: "fresh.zip",
    state: "in_progress",
    startTime: new Date().toISOString(),
    totalBytes: 2_000_000,
  };

  events.downloadCreated.emit(item);
  events.downloadCreated.emit({ ...item, id: 4 });
  await settle();

  assert.equal(calls.native.filter((message) => message.action === "download").length, 1);
  assert.deepEqual(calls.cancel, [3]);
});

test("native rejection leaves the browser download running", async () => {
  const { calls, events } = loadBackground({ nativeResult: { status: "error", message: "offline" } });
  await settle();
  calls.native.length = 0;

  events.downloadCreated.emit({
    id: 5,
    url: "https://example.test/fallback.zip",
    filename: "fallback.zip",
    state: "in_progress",
    startTime: new Date().toISOString(),
    totalBytes: 2_000_000,
  });
  await settle();

  assert.equal(calls.native.filter((message) => message.action === "download").length, 1);
  assert.deepEqual(calls.cancel, []);
});

test("detected stream reports native-host failure instead of false success", async () => {
  const { events } = loadBackground({ nativeResult: { status: "error", message: "offline" } });
  await settle();
  let response;

  events.message.emit(
    { type: "downloadStream", url: "https://example.test/live.m3u8", filename: "live.mp4" },
    {},
    (value) => { response = value; },
  );
  await settle();

  assert.equal(response.ok, false);
  assert.equal(response.error, "offline");
});

// The repaired native host answers "error" whenever no running Cove accepted
// the download, and nothing is persisted for a later launch. These pin the
// browser side of that contract: only a positive acknowledgement may cost the
// user their browser download.

test("a native-host timeout leaves the browser download running", async () => {
  const { calls, events } = loadBackground({
    nativeResult: () => Promise.reject(new Error("Native host has exited.")),
  });
  await settle();
  calls.native.length = 0;

  events.downloadCreated.emit({
    id: 6,
    url: "https://example.test/timeout.zip",
    filename: "timeout.zip",
    state: "in_progress",
    startTime: new Date().toISOString(),
    totalBytes: 2_000_000,
  });
  await settle();

  assert.equal(calls.native.filter((m) => m.action === "download").length, 1);
  assert.deepEqual(calls.cancel, []);
  assert.deepEqual(calls.erase, []);
});

test("a malformed native reply leaves the browser download running", async () => {
  // `undefined` is omitted: it hits loadBackground's own default reply.
  for (const reply of [null, {}, { status: "queued" }, "ok", 42]) {
    const { calls, events } = loadBackground({ nativeResult: reply });
    await settle();
    calls.native.length = 0;

    events.downloadCreated.emit({
      id: 7,
      url: "https://example.test/malformed.zip",
      filename: "malformed.zip",
      state: "in_progress",
      startTime: new Date().toISOString(),
      totalBytes: 2_000_000,
    });
    await settle();

    assert.deepEqual(calls.cancel, [], `reply ${JSON.stringify(reply)} cancelled`);
    assert.deepEqual(calls.erase, [], `reply ${JSON.stringify(reply)} erased`);
  }
});

test("a failed send is retried rather than blocked by the dedup marker", async () => {
  // Fail-open depends on the dedup mark being cleared on failure: otherwise
  // the same URL is silently ignored for the rest of the dedup window.
  let downloads = 0;
  const { calls, events } = loadBackground({
    // Keyed on the action so the extension's startup ping doesn't consume
    // the first scripted answer.
    nativeResult: (message) => {
      if (message.action !== "download") return { status: "ok" };
      downloads += 1;
      return downloads === 1
        ? { status: "error", message: "offline" }
        : { status: "ok" };
    },
  });
  await settle();
  calls.native.length = 0;

  const item = {
    id: 8,
    url: "https://example.test/retry.zip",
    filename: "retry.zip",
    state: "in_progress",
    totalBytes: 2_000_000,
  };
  events.downloadCreated.emit({ ...item, startTime: new Date().toISOString() });
  await settle();
  await settle();
  events.downloadCreated.emit({ ...item, startTime: new Date().toISOString() });
  await settle();
  await settle();

  assert.equal(calls.native.filter((m) => m.action === "download").length, 2);
  assert.deepEqual(calls.cancel, [8]);
});

test("context menu falls back to a browser download when Cove is closed", async () => {
  const { calls, events, browserDownloads } = loadBackground({
    nativeResult: null, // malformed/absent reply, as when no Cove is running
  });
  await settle();

  await Promise.all(
    events.contextMenuClicked.emit(
      { menuItemId: "download-with-cove", linkUrl: "https://example.test/manual.zip" },
      {}
    )
  );
  await settle();

  // The browser downloads it instead; nothing is queued for a later launch.
  // Compared field-by-field: the options object is created inside the vm
  // realm, so deepStrictEqual would fail on its prototype alone.
  assert.equal(browserDownloads.length, 1);
  assert.equal(browserDownloads[0].url, "https://example.test/manual.zip");
  assert.equal(browserDownloads[0].filename, "manual.zip");
  assert.equal(browserDownloads[0].saveAs, false);
  assert.deepEqual(calls.cancel, []);
});

test("context menu on a YouTube player sends the watch page, not the blob src", async () => {
  const { calls, events } = loadBackground();
  await settle();
  calls.native.length = 0;

  await Promise.all(
    events.contextMenuClicked.emit(
      {
        menuItemId: "download-with-cove",
        srcUrl: "blob:https://www.youtube.com/2b0f8c1e-0000-4000-8000-000000000000",
        pageUrl: "https://www.youtube.com/watch?v=abc123",
      },
      { url: "https://www.youtube.com/watch?v=abc123", title: "Clip - YouTube" }
    )
  );
  await settle();

  const sent = calls.native.filter((m) => m.action === "download");
  assert.equal(sent.length, 1);
  assert.equal(sent[0].url, "https://www.youtube.com/watch?v=abc123");
  assert.equal(sent[0].filename, "Clip.mp4");
});

test("context menu keeps a real link target on an extractor page", async () => {
  const { calls, events } = loadBackground();
  await settle();
  calls.native.length = 0;

  await Promise.all(
    events.contextMenuClicked.emit(
      {
        menuItemId: "download-with-cove",
        linkUrl: "https://example.test/manual.zip",
        pageUrl: "https://www.youtube.com/watch?v=abc123",
      },
      { url: "https://www.youtube.com/watch?v=abc123" }
    )
  );
  await settle();

  const sent = calls.native.filter((m) => m.action === "download");
  assert.equal(sent.length, 1);
  assert.equal(sent[0].url, "https://example.test/manual.zip");
});

test("an extractor page URL is never handed to the browser downloader", async () => {
  const { events, browserDownloads } = loadBackground({ nativeResult: null });
  await settle();

  await Promise.all(
    events.contextMenuClicked.emit(
      {
        menuItemId: "download-with-cove",
        srcUrl: "blob:https://www.youtube.com/2b0f8c1e-0000-4000-8000-000000000000",
        pageUrl: "https://www.youtube.com/watch?v=abc123",
      },
      { url: "https://www.youtube.com/watch?v=abc123" }
    )
  );
  await settle();

  // The browser would save the watch page's HTML, not the video.
  assert.equal(browserDownloads.length, 0);
});

test("context menu ignores an unusable blob src off an extractor page", async () => {
  const { calls, events, browserDownloads } = loadBackground();
  await settle();
  calls.native.length = 0;

  await Promise.all(
    events.contextMenuClicked.emit(
      {
        menuItemId: "download-with-cove",
        srcUrl: "blob:https://example.test/2b0f8c1e-0000-4000-8000-000000000000",
        pageUrl: "https://example.test/player",
      },
      { url: "https://example.test/player" }
    )
  );
  await settle();

  assert.equal(calls.native.filter((m) => m.action === "download").length, 0);
  assert.equal(browserDownloads.length, 0);
});

// ---- Shared media core, loaded on its own ----
//
// media-core.js must be browser-neutral: it is copied into the Chrome bundle
// and a later slice will load it there with no site adapter present. These
// exercise the default path - buildCoveMedia() with no argument and no
// CoveMediaCapability global - because that is the configuration Chrome will
// run, not a special explicit one.

function loadMediaCore({ capability } = {}) {
  const noop = () => {};
  const context = vm.createContext({
    globalThis: undefined,
    browser: {
      webRequest: null,
      tabs: {
        onRemoved: { addListener: noop },
        onUpdated: { addListener: noop },
        onActivated: { addListener: noop },
        query: () => Promise.resolve([]),
      },
    },
    console: { log: noop, error: noop, warn: noop },
    navigator: { userAgent: "test-agent" },
    URL, Date, Math, Promise, Map, Set,
    setTimeout, clearTimeout,
  });
  context.globalThis = context;
  if (capability) context.CoveMediaCapability = capability;
  vm.runInContext(
    fs.readFileSync("extension/media-core.js", "utf8"),
    context,
    { filename: "extension/media-core.js" },
  );
  // CoveMedia is a top-level const, so it lives in the context's lexical
  // scope rather than on the context object (see evalIn above).
  return { context, CoveMedia: evalIn(context, "CoveMedia") };
}

test("the shared core offers video and audio contexts without any site adapter", () => {
  const { CoveMedia } = loadMediaCore();
  // Array.from: the value comes from the script's realm, so it is structurally
  // but not referentially a host array.
  assert.deepEqual(Array.from(CoveMedia.contexts), ["video", "audio"]);
});

test("the shared core sanitises a title into a filename with no site adapter", () => {
  const { CoveMedia } = loadMediaCore();
  const tab = { title: "  spaced   out  title...  ", url: "https://example.test/page" };

  assert.equal(
    CoveMedia.mediaFilename(tab, "https://cdn.example.test/v/clip.mov"),
    "spaced out title.mov",
  );
});

test("the shared core replaces characters a filename cannot contain", () => {
  const { CoveMedia } = loadMediaCore();
  const tab = { title: 'a/b:c*d?e"f<g>h|i', url: "https://example.test/page" };

  assert.equal(
    CoveMedia.mediaFilename(tab, "https://cdn.example.test/v/clip.mkv"),
    "a b c d e f g h i.mkv",
  );
});

test("the shared core caps a filename at 180 characters plus its extension", () => {
  const { CoveMedia } = loadMediaCore();
  const tab = { title: "z".repeat(250), url: "https://example.test/page" };

  const name = CoveMedia.mediaFilename(tab, "https://cdn.example.test/v/clip.mp4");
  assert.equal(name, "z".repeat(180) + ".mp4");
});

test("the shared core infers the extension from the media path", () => {
  const { CoveMedia } = loadMediaCore();
  const tab = { title: "Holiday clip", url: "https://example.test/page" };

  assert.equal(
    CoveMedia.mediaFilename(tab, "https://cdn.example.test/v/clip.webm"),
    "Holiday clip.webm",
  );
  assert.equal(
    CoveMedia.mediaFilename(tab, "https://cdn.example.test/stream"),
    "Holiday clip.mp4",
  );
});

test("the shared core rewrites no title on any site of its own accord", () => {
  const { CoveMedia } = loadMediaCore();

  // The exact inputs the Firefox adapter does rewrite. With no adapter the
  // core must leave both alone rather than carrying site rules itself.
  assert.equal(
    CoveMedia.mediaFilename(
      { title: "Clip - YouTube", url: "https://www.youtube.com/watch?v=abc123" },
      "https://www.youtube.com/watch?v=abc123",
    ),
    "Clip - YouTube.mp4",
  );
  assert.equal(
    CoveMedia.mediaFilename(
      { title: "AI could never : funny", url: "https://old.reddit.com/r/funny/comments/a/b/" },
      "https://v.redd.it/abc/DASH_720.mp4",
    ),
    // The colon is not a legal filename character, so the core replaces it -
    // but it does not know the tail is a subreddit name to be dropped.
    "AI could never funny.mp4",
  );
});

test("the shared core returns no filename when the title is empty or the tab is gone", () => {
  const { CoveMedia } = loadMediaCore();
  const url = "https://cdn.example.test/v/clip.mp4";

  assert.equal(CoveMedia.mediaFilename({ title: "   ", url: "https://a.test/" }, url), null);
  assert.equal(CoveMedia.mediaFilename(null, url), null);
});

test("the shared core has no page fallback and no stream list without an adapter", () => {
  const { CoveMedia } = loadMediaCore();

  assert.equal(
    CoveMedia.pageFallbackUrl(
      { url: "https://www.youtube.com/watch?v=abc123" },
      { pageUrl: "https://www.youtube.com/watch?v=abc123" },
    ),
    "",
  );

  let streams;
  CoveMedia.handleMessage({ type: "getDetectedStreams" }, {}, (r) => { streams = r; });
  assert.deepEqual(Array.from(streams), []);

  let page;
  CoveMedia.handleMessage({ type: "getMediaPageUrl" }, {}, (r) => { page = r; });
  assert.equal(page.url, "");
});

test("the shared core leaves a message it does not own to the caller", () => {
  const { CoveMedia } = loadMediaCore();
  assert.equal(CoveMedia.handleMessage({ type: "somethingElse" }, {}, () => {}), false);
});

// ---- Firefox filename parity across the split ----
//
// Expected values were captured from the pre-split implementation. They pin
// the site adapter's contribution: the core sanitises, the adapter rewrites
// the title and rejects a playlist extension.

function firefoxMediaFilename(tab, url) {
  const { context } = loadBackground();
  return evalIn(context, "CoveMedia").mediaFilename(tab, url);
}

test("a subreddit suffix is still stripped from an old.reddit title", () => {
  assert.equal(
    firefoxMediaFilename(
      { title: "AI could never : funny", url: "https://old.reddit.com/r/funny/comments/abc/x/" },
      "https://v.redd.it/abc/DASH_720.mp4",
    ),
    "AI could never.mp4",
  );
  assert.equal(
    firefoxMediaFilename(
      { title: "Cool clip : r/videos", url: "https://old.reddit.com/r/videos/comments/abc/x/" },
      "https://v.redd.it/abc/DASH_720.mp4",
    ),
    "Cool clip.mp4",
  );
});

test("the site suffix is still stripped from a watch-page title", () => {
  assert.equal(
    firefoxMediaFilename(
      { title: "Clip - YouTube", url: "https://www.youtube.com/watch?v=abc123" },
      "https://www.youtube.com/watch?v=abc123",
    ),
    "Clip.mp4",
  );
});

test("a title is only rewritten on the site the rule belongs to", () => {
  assert.equal(
    firefoxMediaFilename(
      { title: "Clip - YouTube", url: "https://example.test/page" },
      "https://cdn.example.test/v/clip.mp4",
    ),
    "Clip - YouTube.mp4",
  );
});

test("a playlist extension is still never used as the download's extension", () => {
  assert.equal(
    firefoxMediaFilename(
      { title: "Live show", url: "https://example.test/live" },
      "https://cdn.example.test/master.m3u8",
    ),
    "Live show.mp4",
  );
});

test("a direct media extension still survives the site adapter", () => {
  assert.equal(
    firefoxMediaFilename(
      { title: "Holiday clip", url: "https://example.test/page" },
      "https://cdn.example.test/v/clip.webm",
    ),
    "Holiday clip.webm",
  );
});

test("the site adapter still recognises exactly the extractor pages it did", () => {
  const { context } = loadBackground();
  const resolve = (u) => context.CoveMediaCapability.sitePageUrl(u);

  assert.equal(resolve("https://www.youtube.com/watch?v=abc123"), "https://www.youtube.com/watch?v=abc123");
  assert.equal(resolve("https://youtu.be/abc123"), "https://youtu.be/abc123");
  assert.equal(resolve("https://m.youtube.com/shorts/xyz"), "https://m.youtube.com/shorts/xyz");
  assert.equal(resolve("https://music.youtube.com/watch?v=q"), "https://music.youtube.com/watch?v=q");
  assert.equal(resolve("https://www.youtube.com/"), "");
  assert.equal(resolve("https://example.test/watch?v=abc"), "");
  assert.equal(resolve(""), "");
  assert.equal(resolve(null), "");
});

// ---- Chrome bundle: background.js without the media runtime ----
//
// The Chrome Web Store rejected 1.3.5 for facilitating downloads of
// copyrighted media, so that bundle's manifest loads neither media script and
// omits the pill content script. background.js must degrade rather than throw
// on the references it keeps. tests/test_extension_bundle.py asserts the
// exclusion itself; these assert the behaviour that is left.

test("without media.js the context menu offers links and images only", async () => {
  const { calls } = loadBackground({ media: false });
  await settle();

  const menu = calls.menus.find((m) => m.id === "download-with-cove");
  assert.deepEqual(Array.from(menu.contexts), ["link", "image"]);
});

test("with media.js the context menu still offers video and audio", async () => {
  const { calls } = loadBackground();
  await settle();

  const menu = calls.menus.find((m) => m.id === "download-with-cove");
  assert.deepEqual(Array.from(menu.contexts), ["link", "image", "video", "audio"]);
});

test("without media.js a blob player src is ignored, not guessed at", async () => {
  const { calls, events } = loadBackground({ media: false });
  await settle();
  calls.native.length = 0;

  await Promise.all(
    events.contextMenuClicked.emit(
      {
        menuItemId: "download-with-cove",
        srcUrl: "blob:https://example.com/2b0f8c1e-0000-4000-8000-000000000000",
        pageUrl: "https://example.com/watch?v=abc123",
      },
      { url: "https://example.com/watch?v=abc123", title: "Clip" }
    )
  );
  await settle();

  assert.deepEqual(calls.native.filter((m) => m.action === "download"), []);
});

test("without media.js a plain file link still downloads", async () => {
  const { calls, events } = loadBackground({ media: false });
  await settle();
  calls.native.length = 0;

  await Promise.all(
    events.contextMenuClicked.emit(
      {
        menuItemId: "download-with-cove",
        linkUrl: "https://example.com/files/setup.zip",
        pageUrl: "https://example.com/downloads",
      },
      { url: "https://example.com/downloads", title: "Downloads" }
    )
  );
  await settle();

  const sent = calls.native.filter((m) => m.action === "download");
  assert.equal(sent.length, 1);
  assert.equal(sent[0].url, "https://example.com/files/setup.zip");
  assert.equal(sent[0].filename, "setup.zip");
});

test("without media.js the popup's stream request is answered, not dropped", async () => {
  const { events } = loadBackground({ media: false });
  await settle();

  let reply = "never called";
  events.message.emit({ type: "getDetectedStreams" }, {}, (r) => { reply = r; });
  await settle();

  assert.deepEqual(Array.from(reply), []);
});

test("without media.js a media download request is refused cleanly", async () => {
  const { calls, events } = loadBackground({ media: false });
  await settle();
  calls.native.length = 0;

  const reply = await pillDownload(events, { url: "https://example.com/clip.mp4" });

  assert.equal(reply.ok, false);
  assert.equal(reply.reason, "unsupported");
  assert.deepEqual(calls.native.filter((m) => m.action === "download"), []);
});

// ---- Media pill failure reporting ----

// Drives the onMessage listener the in-page pill talks to and returns the
// single reply it sends back.
async function pillDownload(events, msg) {
  let reply;
  events.message.emit(
    { type: "downloadMedia", url: msg.url, pageUrl: msg.pageUrl || msg.url },
    { tab: { url: msg.pageUrl || msg.url } },
    (response) => { reply = response; }
  );
  await settle();
  await settle();
  return reply;
}

test("a closed Cove is reported as unavailable, not as a bad video", async () => {
  const { events } = loadBackground({
    nativeResult: { status: "error", message: "Cove is not available" },
  });
  await settle();

  const reply = await pillDownload(events, {
    url: "https://www.youtube.com/watch?v=SCD2tB1qILc",
  });

  assert.equal(reply.ok, false);
  assert.equal(reply.reason, "unavailable");
});

test("an unreachable native host is reported as unavailable", async () => {
  const { events } = loadBackground({
    nativeResult: () => { throw new Error("No such native application"); },
  });
  await settle();

  const reply = await pillDownload(events, {
    url: "https://www.youtube.com/watch?v=SCD2tB1qILc",
  });

  assert.equal(reply.ok, false);
  assert.equal(reply.reason, "unavailable");
});

test("a genuine Cove-side refusal is not blamed on a closed Cove", async () => {
  const { events } = loadBackground({
    nativeResult: { status: "error", message: "Invalid or blocked URL" },
  });
  await settle();

  const reply = await pillDownload(events, {
    url: "https://www.youtube.com/watch?v=SCD2tB1qILc",
  });

  assert.equal(reply.ok, false);
  assert.equal(reply.reason, "failed");
});

test("an unsupported URL is reported as unsupported", async () => {
  const { events } = loadBackground();
  await settle();

  const reply = await pillDownload(events, { url: "blob:https://example.test/x" });

  assert.equal(reply.ok, false);
  assert.equal(reply.reason, "unsupported");
});

test("an accepted media download still reports success", async () => {
  const { events } = loadBackground({ nativeResult: { status: "ok" } });
  await settle();

  const reply = await pillDownload(events, {
    url: "https://www.youtube.com/watch?v=SCD2tB1qILc",
  });

  assert.equal(reply.ok, true);
});

test("the pill never labels a failure as a problem with the video", () => {
  const source = fs.readFileSync("extension/content/media-tab.js", "utf8");
  // The old catch-all label blamed YouTube for a closed Cove, which sent a
  // real debugging session chasing a video that was fine all along.
  assert.equal(source.includes("Video unavailable"), false);
  assert.equal(source.includes("Cove is not running"), true);
});

// ---------------------------------------------------------------------------
// Diagnostics
//
// The background context owns the extension's diagnostic ring: it is the one
// context that survives a popup closing and a tab navigating, and it is still
// alive when Cove is not. Content scripts and the popup report into it by
// message, because the manifest cannot load a second script into them.
//
// Assertions run against the report the popup would copy, which is the actual
// support surface and does not depend on the storage flush debounce.
// ---------------------------------------------------------------------------

// The listeners answer through sendResponse, not through their return value
// (which is the "reply asynchronously" flag), so capture the callback.
async function sendToBackground(events, msg, sender = {}) {
  let captured;
  events.message.emit(msg, sender, (reply) => { captured = reply; });
  await settle();
  return captured;
}

async function diagReport(events) {
  const reply = await sendToBackground(events, { type: "coveDiagReport" });
  return reply && reply.text ? reply.text : "";
}

async function requestMedia(events, msg = {}) {
  return sendToBackground(
    events,
    { type: "downloadMedia", url: "https://cdn.example.test/v/movie.mp4", ...msg },
    { tab: { id: 4, url: "https://news.example.test/x" } },
  );
}

test("the background records a media download request and its result", async () => {
  const { events, calls } = loadBackground({ nativeResult: { status: "ok" } });
  await settle();
  calls.native.length = 0;

  await requestMedia(events, { requestId: "51c2a711" });
  const report = await diagReport(events);

  assert.ok(report.includes("request_received"));
  assert.ok(report.includes("native_message_sent"));
  assert.ok(report.includes("native_message_result"));
  assert.ok(report.includes("request=51c2a711"));
});

test("no page url, media url or filename reaches the extension log", async () => {
  const { events } = loadBackground({ nativeResult: { status: "ok" } });
  await settle();

  await requestMedia(events, {
    url: "https://cdn.example.test/v/secret-movie.mp4",
    pageUrl: "https://news.example.test/private-article",
  });
  const report = await diagReport(events);

  assert.ok(!report.includes("secret-movie"));
  assert.ok(!report.includes("private-article"));
  assert.ok(!report.includes("news.example.test"));
});

test("a request id from the content script reaches the native message", async () => {
  const { calls, events } = loadBackground({ nativeResult: { status: "ok" } });
  await settle();
  calls.native.length = 0;

  await requestMedia(events, { requestId: "51c2a711" });

  const download = calls.native.find((m) => m.action === "download");
  assert.equal(download.requestId, "51c2a711");
});

test("a media download without a request id still works", async () => {
  const { calls, events } = loadBackground({ nativeResult: { status: "ok" } });
  await settle();
  calls.native.length = 0;

  const result = await requestMedia(events);

  assert.equal(result.ok, true);
  const download = calls.native.find((m) => m.action === "download");
  assert.equal(download.requestId, undefined);
});

test("an unreachable Cove is recorded with a reason the user can act on", async () => {
  const { events } = loadBackground({
    nativeResult: { status: "error", message: "Cove is not available" },
  });
  await settle();

  await requestMedia(events, { requestId: "51c2a711" });
  const report = await diagReport(events);

  assert.ok(report.includes("request_failed"));
  assert.ok(report.includes("reason=app_unavailable"));
  assert.ok(report.includes("request=51c2a711"));
});

test("a transport failure is distinguished from a rejection", async () => {
  const { events } = loadBackground({
    nativeResult: () => { throw new Error("no host"); },
  });
  await settle();

  await requestMedia(events);
  const report = await diagReport(events);

  assert.ok(report.includes("reason=transport_error"));
  assert.ok(!report.includes("reason=app_unavailable"));
});

test("a rejection by a running Cove is not reported as unavailable", async () => {
  const { events } = loadBackground({
    nativeResult: { status: "error", message: "Invalid or blocked URL" },
  });
  await settle();

  await requestMedia(events);
  const report = await diagReport(events);

  assert.ok(report.includes("reason=gui_rejected"));
});

test("the startup ping result is recorded instead of logged raw", async () => {
  const { events } = loadBackground({
    nativeResult: { status: "ok", version: "3.4.0" },
  });
  await settle();

  const report = await diagReport(events);
  assert.ok(report.includes("native_ping_result"));
  assert.ok(report.includes("appVersion=3.4.0"));
});

test("a content script can record through the background", async () => {
  const { events } = loadBackground();
  await settle();

  await sendToBackground(
    events,
    { type: "coveDiag", component: "extension.content",
      event: "video_download_requested", level: "INFO", requestId: "51c2a711",
      fields: { trigger: "pill" } },
    { tab: { id: 4, url: "https://news.example.test/x" } },
  );

  const report = await diagReport(events);
  assert.ok(report.includes("extension.content/video_download_requested"));
  assert.ok(report.includes("request=51c2a711"));
  assert.ok(report.includes("trigger=pill"));
});

test("a forbidden field sent by a content script is still dropped", async () => {
  const { events } = loadBackground();
  await settle();

  await sendToBackground(
    events,
    { type: "coveDiag", component: "extension.content", event: "video_pill_result",
      fields: { pageUrl: "https://news.example.test/private", result: "ok" } },
    { tab: { id: 4 } },
  );

  const report = await diagReport(events);
  assert.ok(!report.includes("private"));
  assert.ok(report.includes("result=ok"));
});

test("the popup can clear the ring", async () => {
  const { events } = loadBackground();
  await settle();

  await sendToBackground(events, {
    type: "coveDiag", component: "extension.popup",
    event: "connection_status_rendered", fields: { state: "connected" },
  });
  assert.ok((await diagReport(events)).includes("connection_status_rendered"));

  await sendToBackground(events, { type: "coveDiagClear" });
  assert.ok(!(await diagReport(events)).includes("connection_status_rendered"));
});

test("the ring is persisted for a report after a background restart", async () => {
  const { events, store } = loadBackground();
  await settle();
  await sendToBackground(events, {
    type: "coveDiag", component: "extension.popup",
    event: "connection_status_rendered", fields: { state: "connected" },
  });
  // The flush is debounced, so drive it the way the timer would.
  await new Promise((resolve) => setTimeout(resolve, 1100));
  assert.ok(store.data.coveDiag && store.data.coveDiag.length > 0);
});

test("diagnostics failure never breaks a media download", async () => {
  const { events } = loadBackground({
    nativeResult: { status: "ok" },
    breakStorage: true,
  });
  await settle();

  const result = await requestMedia(events);
  assert.equal(result.ok, true);
});

test("the report header names the extension, the browser and Cove", async () => {
  const { events } = loadBackground({
    nativeResult: { status: "ok", version: "3.4.0" },
  });
  await settle();

  const report = await diagReport(events);
  assert.ok(report.includes("extension version: 1.4.4"));
  assert.ok(report.includes("last seen Cove version: 3.4.0"));
  assert.ok(report.includes("browser: Firefox 140"));
});

test("an unreachable Cove leaves the version unknown rather than wrong", async () => {
  const { events } = loadBackground({
    nativeResult: () => { throw new Error("no host"); },
  });
  await settle();

  const report = await diagReport(events);
  assert.ok(report.includes("last seen Cove version: unknown"));
});

test("startup events survive a slow storage hydration", async () => {
  const { events } = loadBackground({
    nativeResult: { status: "ok", version: "3.4.0" },
    slowStorage: true,
    storedDiag: [{
      ts: "2026-08-01T00:00:00.000Z", level: "INFO",
      component: "extension.background", event: "event_from_last_run",
      session: "aaaabbbb", context: "background", fields: {},
    }],
  });
  await settle();
  await settle();

  const report = await diagReport(events);
  // The event recorded while hydration was in flight must not be discarded,
  // and the persisted history must not be lost either.
  assert.ok(report.includes("native_ping_result"), "startup event was dropped");
  assert.ok(report.includes("event_from_last_run"), "persisted history was lost");
});

test("a report requested during hydration still includes stored history", async () => {
  const { events } = loadBackground({
    slowStorage: true,
    storedDiag: [{
      ts: "2026-08-01T00:00:00.000Z", level: "INFO",
      component: "extension.background", event: "event_from_last_run",
      session: "aaaabbbb", context: "background", fields: {},
    }],
  });

  // No settle first: ask while the storage read is still outstanding.
  const report = await diagReport(events);
  assert.ok(report.includes("event_from_last_run"));
});

test("a clear that storage refuses is reported as a failure", async () => {
  const { events } = loadBackground({ breakStorage: true });
  await settle();
  const reply = await sendToBackground(events, { type: "coveDiagClear" });
  assert.equal(reply.ok, false);
});

// ---- Oversized cookie jars -------------------------------------------------
//
// The whole cookie jar for one origin can exceed Cove's native handoff bound,
// and Cove refused the entire request for it. Sending the download without
// cookies is strictly better than not sending it at all.

const COOKIE_LIMIT = 32 * 1024;

function jarOfSize(total) {
  return [{ name: "sid", value: "c".repeat(Math.max(0, total - 4)) }];
}

function freshItem(overrides = {}) {
  return {
    id: 90,
    url: "https://example.test/big.zip",
    filename: "big.zip",
    state: "in_progress",
    startTime: new Date().toISOString(),
    totalBytes: 2_000_000,
    ...overrides,
  };
}

test("a cookie jar within the handoff limit is sent unchanged", async () => {
  const jar = jarOfSize(100);
  const { calls, events } = loadBackground({ cookies: jar });
  await settle();
  calls.native.length = 0;

  events.downloadCreated.emit(freshItem());
  await settle();

  const sent = calls.native.find((m) => m.action === "download");
  assert.equal(sent.cookies, `${jar[0].name}=${jar[0].value}`);
  assert.equal(sent.cookies.length, 100);
});

test("an oversized cookie jar is dropped rather than sent or truncated", async () => {
  const { calls, events } = loadBackground({ cookies: jarOfSize(COOKIE_LIMIT + 1) });
  await settle();
  calls.native.length = 0;

  events.downloadCreated.emit(freshItem());
  await settle();

  const sent = calls.native.find((m) => m.action === "download");
  assert.ok(sent, "the download must still be handed to Cove");
  assert.equal(sent.cookies, "");
  assert.equal(sent.url, "https://example.test/big.zip");
});

test("an oversized cookie jar still lets Cove take the download", async () => {
  const { calls, events } = loadBackground({
    cookies: jarOfSize(COOKIE_LIMIT + 1),
    nativeResult: { status: "ok" },
  });
  await settle();
  calls.native.length = 0;

  events.downloadCreated.emit(freshItem({ id: 91 }));
  await settle();

  assert.deepEqual(calls.cancel, [91]);
});

test("a host rejection still leaves the browser download alone", async () => {
  const { calls, events } = loadBackground({
    cookies: jarOfSize(COOKIE_LIMIT + 1),
    nativeResult: { status: "error", message: "Cove refused this download." },
  });
  await settle();
  calls.native.length = 0;

  events.downloadCreated.emit(freshItem({ id: 92 }));
  await settle();

  assert.deepEqual(calls.cancel, []);
});

test("no cookie value ever reaches the extension diagnostics ring", async () => {
  const jar = [{ name: "sid", value: "dummysecretcookie".repeat(4000) }];
  const { events, store } = loadBackground({ cookies: jar });
  await settle();

  events.downloadCreated.emit(freshItem({ id: 93 }));
  await settle();

  assert.ok(!JSON.stringify(store.data).includes("dummysecretcookie"));
});

// --- badge precedence and interception bookkeeping -------------------------

test("a media count never overwrites the disabled OFF badge", async () => {
  // Interception is off, so the toolbar must keep saying so. Media detection
  // published its own count unconditionally, which made a disabled extension
  // look active.
  const { context, badge } = loadBackground({
    settings: { enabled: false },
    tabs: [{ id: 7 }],
  });
  await settle();

  assert.equal(badge.text.at(-1), "OFF");

  evalIn(context, 'detectedStreams.set(7, [{ url: "https://x/a.m3u8" }]); updateStreamBadge(7)');
  await settle();

  assert.equal(badge.text.at(-1), "OFF");
});

test("a media count still shows while interception is enabled", async () => {
  const { context, badge } = loadBackground({
    settings: { enabled: true },
    tabs: [{ id: 7 }],
  });
  await settle();

  evalIn(context, 'detectedStreams.set(7, [{ url: "https://x/a.m3u8" }, { url: "https://x/b.m3u8" }]); updateStreamBadge(7)');
  await settle();

  assert.equal(badge.text.at(-1), "2");
});

test("an erased download stops being tracked as intercepted", async () => {
  const { events, context } = loadBackground({ settings: { enabled: true } });
  await settle();
  evalIn(context, 'interceptedIds.add(42)');

  events.downloadErased.emit(42);
  await settle();

  assert.equal(evalIn(context, 'interceptedIds.has(42)'), false);
});

test("stored intercepted ids are pruned when they are no longer downloading", async () => {
  // The set is persisted, so ids left behind by a missed terminal event or an
  // extension suspension came back on every wake and could suppress events for
  // a reused id.
  const { context, store } = loadBackground({
    settings: { enabled: true },
    storedIntercepted: [1, 2, 3],
    downloadSearch: ({ id }) => (id === 2 ? [{ id: 2, state: "in_progress" }] : []),
  });
  await settle();

  // Arrays built inside the vm realm are not reference-equal to host ones.
  assert.deepEqual(Array.from(evalIn(context, '[...interceptedIds]')), [2]);
  assert.deepEqual(Array.from(store.data._interceptedIds), [2]);
});

test("pruning erases a terminal download instead of just forgetting it", async () => {
  // The persisted set exists to recover cleanup that a missed terminal event
  // or a suspended worker skipped. Dropping the id without erasing would
  // strand the cancelled download in the browser's history for good.
  const { context, calls, store } = loadBackground({
    settings: { enabled: true },
    storedIntercepted: [5],
    downloadSearch: ({ id }) => [{ id, state: "interrupted" }],
  });
  await settle();

  // Objects built inside the vm realm are not reference-equal to host ones.
  assert.deepEqual(JSON.parse(JSON.stringify(calls.erase)), [{ id: 5 }]);
  assert.deepEqual(Array.from(evalIn(context, "[...interceptedIds]")), []);
  assert.deepEqual(Array.from(store.data._interceptedIds), []);
});

test("an id the browser no longer knows about is dropped without erasing", async () => {
  const { context, calls } = loadBackground({
    settings: { enabled: true },
    storedIntercepted: [6],
    downloadSearch: () => [],
  });
  await settle();

  assert.deepEqual(calls.erase, []);
  assert.deepEqual(Array.from(evalIn(context, "[...interceptedIds]")), []);
});

test("an id is kept when its erase fails, for the next startup to retry", async () => {
  const { context } = loadBackground({
    settings: { enabled: true },
    storedIntercepted: [7],
    downloadSearch: ({ id }) => [{ id, state: "complete" }],
    eraseThrows: true,
  });
  await settle();

  assert.deepEqual(Array.from(evalIn(context, "[...interceptedIds]")), [7]);
});

test("an erase during hydration is not lost", async () => {
  // The stored set arrives asynchronously. An erase handled before it lands
  // would operate on an empty Set, and the id would come back on the next
  // startup as though it had never been cleaned up.
  const { events, context, store } = loadBackground({
    settings: { enabled: true },
    storedIntercepted: [11, 12],
    downloadSearch: ({ id }) => [{ id, state: "in_progress" }],
    slowInterceptedIds: true,
  });

  events.downloadErased.emit(11);   // fires before hydration completes
  await settle();

  assert.deepEqual(Array.from(evalIn(context, "[...interceptedIds]")), [12]);
  assert.deepEqual(Array.from(store.data._interceptedIds), [12]);
});

test("overlapping persists leave the newest state stored", async () => {
  const { context, store } = loadBackground({ settings: { enabled: true } });
  await settle();

  // Two mutations back to back: the stored value must reflect the second.
  evalIn(context, "interceptedIds.add(1); persistInterceptedIds();");
  evalIn(context, "interceptedIds.add(2); persistInterceptedIds();");
  await settle();

  assert.deepEqual(Array.from(store.data._interceptedIds), [1, 2]);
});
