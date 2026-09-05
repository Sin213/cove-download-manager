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

// `worker` reproduces Chrome's MV3 entry point: the manifest names
// background.js and nothing else, so background.js is the only script the
// browser evaluates and every other module arrives through its own
// importScripts call. Nothing is pre-loaded in that mode - pre-loading is
// what would hide a wrong load order. `installedMenus` is the browser's
// surviving context-menu state, which is what makes a second load an upgrade
// or a worker restart rather than a fresh install.
function loadBackground({ nativeResult = { status: "ok" }, settings,
                         breakStorage = false, slowStorage = false,
                         storedDiag = null, media = true, cookies = [],
                         tabs = [], downloadSearch = () => [],
                         storedIntercepted = null, eraseThrows = false,
                         slowInterceptedIds = false,
                         worker = false, missingScripts = [],
                         installedMenus = new Map(), menuApi = "lenient" } = {}) {
  const calls = { native: [], cancel: [], erase: [], menus: [], menuOps: [], imported: [] };
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
      // Chrome keeps created items across service worker restarts and across
      // an extension update, and answers a second create() for the same id
      // with a duplicate-id lastError while leaving the installed item - and
      // its contexts - exactly as they were. `installedMenus` is that
      // surviving state; a shared Map across two loads is an upgrade.
      create(props, callback) {
        calls.menuOps.push("create");
        calls.menus.push(props);
        if (installedMenus.has(props.id)) {
          browser.runtime.lastError = {
            message: `Cannot create item with duplicate id ${props.id}`,
          };
        } else {
          installedMenus.set(props.id, props);
        }
        if (callback) callback();
        browser.runtime.lastError = null;
        return props.id;
      },
      // removeAll is not the same API on every browser this ships to, and the
      // difference is exactly what a create() racing an unfinished removal
      // would hide. Removal is deferred in every mode, so an item created
      // before it completes is wiped by it and the assertions see an empty
      // menu rather than a passing one.
      //
      //   "callback"  Chrome: completion callback, no promise. Promise
      //               support only arrived in Chrome 123, and the manifest
      //               names no minimum version.
      //   "strict"    Firefox: promise only, and it rejects extra arguments
      //               the way a schema-validated API does.
      //   "lenient"   promise, extra arguments ignored.
      removeAll(callback) {
        // A call rejected for its arguments removed nothing, so it is not
        // recorded as a removal having happened.
        if (menuApi === "strict" && arguments.length > 0) {
          throw new TypeError("Incorrect argument types for menus.removeAll.");
        }
        calls.menuOps.push("removeAll");
        const finish = () => {
          installedMenus.clear();
          calls.menuOps.push("removed");
        };
        if (menuApi === "callback") {
          queueMicrotask(() => { finish(); if (callback) callback(); });
          return undefined;
        }
        return new Promise((resolve) => {
          queueMicrotask(() => {
            finish();
            if (menuApi === "lenient" && callback) callback();
            resolve();
          });
        });
      },
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
  if (worker) {
    // A worker fetches every argument before evaluating any of them, so a
    // missing file leaves nothing half-loaded. Modelling that is the point:
    // it is what lets background.js import the two media scripts in one call
    // and still know that neither ran.
    context.importScripts = (...names) => {
      calls.imported.push(names);
      const sources = names.map((name) => {
        if (missingScripts.includes(name)) {
          const error = new Error(`Failed to load '${name}'`);
          error.name = "NetworkError";
          throw error;
        }
        return [name, fs.readFileSync(`extension/${name}`, "utf8")];
      });
      for (const [name, source] of sources) {
        vm.runInContext(source, context, { filename: `extension/${name}` });
      }
    };
  } else {
    // The browser loads extension/diagnostics.js into the background context
    // before background.js runs (a script element on the Firefox background
    // page). Mirror that ordering here.
    vm.runInContext(
      fs.readFileSync("extension/diagnostics.js", "utf8"),
      context,
      { filename: "extension/diagnostics.js" },
    );
  }
  if (storedDiag) store.data.coveDiag = storedDiag;
  if (storedIntercepted) store.data._interceptedIds = storedIntercepted;
  // The media runtime is split in three: media-core.js holds browser-neutral
  // mechanics, media-sites.js holds the Firefox-only site/extractor/stream
  // capability, and media-chrome.js holds Chrome's, which is the deliberate
  // absence of one. The MV2 manifest lists core then sites ahead of
  // background.js, so mirror it here. Chrome's MV3 manifest lists neither and
  // background.js imports them itself, which is what `worker: true` exercises
  // instead. `media: false` is a bundle with no media scripts at all.
  if (media && !worker) {
    for (const script of ["extension/media-core.js", "extension/media-sites.js"]) {
      vm.runInContext(fs.readFileSync(script, "utf8"), context, { filename: script });
    }
  }
  const source = fs.readFileSync("extension/background.js", "utf8");
  vm.runInContext(source, context, { filename: "extension/background.js" });
  return { calls, events, browserDownloads, store, context, badge, installedMenus };
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

// ---------------------------------------------------------------------------
// Chrome MV3: the shared media runtime, activated through background.js
// ---------------------------------------------------------------------------
//
// Firefox's manifest lists the media scripts ahead of background.js. Chrome's
// MV3 manifest can name one service worker file, so background.js loads them
// itself. Everything below starts from background.js alone, with importScripts
// as the only way anything else gets in, because pre-loading the modules is
// precisely what would hide a wrong order.

const chromeWorker = (options = {}) => loadBackground({ worker: true, ...options });

// Values built inside the vm have that context's prototypes, so a strict deep
// comparison against a literal here fails on identity alone. Round-tripping
// them is the same trick Array.from() plays elsewhere in this file.
const plain = (value) => JSON.parse(JSON.stringify(value));

// ---- Load order and cold start ----

test("the Chrome worker imports the shared core and then its own capability",
     async () => {
  const { calls } = chromeWorker();
  await settle();

  assert.deepEqual(calls.imported, [
    ["diagnostics.js"],
    ["media-core.js", "media-chrome.js"],
  ]);
});

test("media is available on a cold worker evaluation, before any event fires",
     async () => {
  const { calls, context } = chromeWorker();

  // Read before settle() and before a single listener has been called: the
  // capability has to exist from top-level evaluation, not from onInstalled,
  // onStartup or a later message. A woken worker gets no install event.
  assert.equal(evalIn(context, "typeof CoveMedia"), "object");
  assert.equal(evalIn(context, "typeof CoveMediaCapability"), "object");

  await settle();
  const menu = calls.menus.find((m) => m.id === "download-with-cove");
  assert.deepEqual(Array.from(menu.contexts), ["link", "image", "video", "audio"]);
});

test("the Chrome capability contributes no site hooks at all", async () => {
  const { context } = chromeWorker();
  await settle();

  const capability = evalIn(context, "CoveMediaCapability");
  // One key, and it is a refusal. Every hook media-core.js knows how to call
  // is absent, so every site-dependent decision stays at its neutral default.
  assert.deepEqual(Object.keys(capability), ["rejectMediaTarget"]);
  for (const hook of ["sitePageUrl", "titleCleanup", "rejectExtension",
                      "pageFallbackUrl", "handleMessage"]) {
    assert.equal(capability[hook], undefined, `${hook} must not be supplied`);
  }
  assert.equal(evalIn(context, "CoveMedia.pageFallbackUrl({url:'https://example.test/watch'}, {pageUrl:'https://example.test/watch'})"), "");
});

test("a missing media module leaves a build that says it has no media",
     async () => {
  const { calls, events, context } = chromeWorker({
    missingScripts: ["media-core.js"],
  });
  await settle();
  calls.native.length = 0;

  assert.equal(evalIn(context, "typeof CoveMedia"), "undefined");
  const menu = calls.menus.find((m) => m.id === "download-with-cove");
  assert.deepEqual(Array.from(menu.contexts), ["link", "image"]);

  // And the message surface refuses rather than throwing or hanging.
  let reply = null;
  const kept = events.message.emit(
    { type: "downloadMedia", url: "https://cdn.example.test/v/clip.mp4" },
    { tab: { id: 3, url: "https://example.test/watch" } },
    (r) => { reply = r; },
  );
  await settle();
  assert.deepEqual(kept, [undefined]);
  assert.equal(reply.ok, false);
  assert.equal(reply.reason, "unsupported");
  assert.equal(calls.native.length, 0);
});

test("a failed media import is recorded rather than passed off as a plain build",
     async () => {
  const { store } = chromeWorker({ missingScripts: ["media-chrome.js"] });
  await settle();
  await new Promise((resolve) => setTimeout(resolve, 1100));

  const events = (store.data.coveDiag || []).map((e) => e.event);
  assert.ok(events.includes("media_load_failed"),
            `no media_load_failed in ${JSON.stringify(events)}`);
});

test("Firefox loads its media from the manifest and imports nothing",
     async () => {
  const { calls, context } = loadBackground();
  await settle();

  assert.equal(evalIn(context, "typeof importScripts"), "undefined");
  assert.deepEqual(calls.imported, []);
  // The site capability, not Chrome's: Firefox's own adapter is still the one
  // media-core.js resolves.
  assert.equal(typeof evalIn(context, "CoveMediaCapability").sitePageUrl, "function");
});

// ---- Context menu installation and upgrade ----

test("a fresh Chrome install gets link, image, video and audio contexts",
     async () => {
  const { installedMenus, calls } = chromeWorker();
  await settle();

  assert.deepEqual(calls.menuOps, ["removeAll", "removed", "create"]);
  assert.deepEqual(
    Array.from(installedMenus.get("download-with-cove").contexts),
    ["link", "image", "video", "audio"],
  );
});

test("an installed link/image-only menu gains the media contexts on upgrade",
     async () => {
  // The shipped Chrome build: no media scripts, so link and image only. Its
  // menu item survives the update, which is the whole difficulty.
  const installedMenus = new Map();
  chromeWorker({ installedMenus, missingScripts: ["media-core.js", "media-chrome.js"] });
  await settle();
  assert.deepEqual(
    Array.from(installedMenus.get("download-with-cove").contexts),
    ["link", "image"],
  );

  // The update. Swallowing the duplicate-id error would leave the item above
  // in place with the contexts it was first registered with.
  chromeWorker({ installedMenus });
  await settle();

  assert.deepEqual(
    Array.from(installedMenus.get("download-with-cove").contexts),
    ["link", "image", "video", "audio"],
  );
});

test("a worker restart re-registers exactly one menu item", async () => {
  const installedMenus = new Map();
  chromeWorker({ installedMenus });
  await settle();
  const second = chromeWorker({ installedMenus });
  await settle();

  assert.equal(installedMenus.size, 1);
  assert.deepEqual(second.calls.menuOps, ["removeAll", "removed", "create"]);
  assert.deepEqual(
    Array.from(installedMenus.get("download-with-cove").contexts),
    ["link", "image", "video", "audio"],
  );
});

test("Firefox still registers its menu once, unchanged", async () => {
  const { calls, installedMenus } = loadBackground();
  await settle();

  assert.equal(installedMenus.size, 1);
  assert.deepEqual(
    Array.from(installedMenus.get("download-with-cove").contexts),
    ["link", "image", "video", "audio"],
  );
  assert.equal(calls.menus.length, 1);
});

// ---- Eligibility, by entry point ----
//
// The pill, the context menu and the runtime message are three different
// inputs. A capability whose sitePageUrl is absent proves only that no page
// address is substituted; what each entry point accepts has to be driven
// through its own dispatch.

const CHROME_TAB = { id: 7, url: "https://example.test/watch", title: "Clip" };

async function chromeMediaMessage(msg, options = {}) {
  const loaded = chromeWorker(options);
  await settle();
  loaded.calls.native.length = 0;

  let reply;
  let resolved = null;
  const done = new Promise((resolve) => { resolved = resolve; });
  const kept = loaded.events.message.emit(msg, { tab: CHROME_TAB }, (r) => {
    reply = r;
    resolved();
  });
  await settle();
  return { ...loaded, kept, done, reply: () => reply };
}

for (const url of [
  "blob:https://example.test/2b0f8c1e-0000-4000-8000-000000000000",
  "data:video/mp4;base64,AAAA",
  "file:///home/user/clip.mp4",
  "javascript:void(0)",
  "ftp://files.example.test/clip.mp4",
  "",
]) {
  test(`Chrome refuses a downloadMedia request for ${url || "an empty url"}`,
       async () => {
    const m = await chromeMediaMessage({ type: "downloadMedia", url });
    await m.done;

    assert.equal(m.calls.native.length, 0, "nothing may reach the native host");
    assert.deepEqual(plain(m.reply()), {
      ok: false, reason: "unsupported", error: "Unsupported URL",
    });
  });
}

test("a downloadMedia request never falls back to the page it came from",
     async () => {
  const m = await chromeMediaMessage({
    type: "downloadMedia",
    url: "blob:https://example.test/abcd",
    pageUrl: "https://example.test/watch",
  });
  await m.done;

  assert.equal(m.calls.native.length, 0);
  // The exact wording separates "the media path looked at this URL and would
  // not take it" from "this build has no media path"; only the first is what
  // is being asserted here.
  assert.deepEqual(plain(m.reply()), {
    ok: false, reason: "unsupported", error: "Unsupported URL",
  });
});

test("a downloadMedia request the message calls eligible is still checked",
     async () => {
  // A message is not evidence about itself. Its own flags are ignored.
  const m = await chromeMediaMessage({
    type: "downloadMedia",
    url: "blob:https://example.test/abcd",
    eligible: true,
    readyState: 4,
  });
  await m.done;

  assert.equal(m.calls.native.length, 0);
  assert.deepEqual(plain(m.reply()), {
    ok: false, reason: "unsupported", error: "Unsupported URL",
  });
});

test("Chrome answers the stream list empty rather than leaving it hanging",
     async () => {
  const { events, calls } = chromeWorker();
  await settle();
  calls.native.length = 0;

  let streams;
  let page;
  events.message.emit({ type: "getDetectedStreams" }, { tab: CHROME_TAB },
                      (r) => { streams = r; });
  events.message.emit({ type: "getMediaPageUrl" }, { tab: CHROME_TAB },
                      (r) => { page = r; });
  await settle();

  assert.deepEqual(plain(streams), []);
  assert.deepEqual(plain(page), { url: "" });
  assert.equal(calls.native.length, 0);
});

test("a Chrome context-menu click on a blob player is ignored, not guessed at",
     async () => {
  const { calls, events } = chromeWorker();
  await settle();
  calls.native.length = 0;

  await Promise.all(events.contextMenuClicked.emit(
    {
      menuItemId: "download-with-cove",
      srcUrl: "blob:https://example.test/2b0f8c1e-0000-4000-8000-000000000000",
      pageUrl: "https://example.test/watch",
    },
    CHROME_TAB,
  ));

  assert.equal(calls.native.length, 0,
               "the page must not stand in for a media target");
});

test("a Chrome context-menu click on a direct video hands over that video",
     async () => {
  const { calls, events } = chromeWorker();
  await settle();
  calls.native.length = 0;

  await Promise.all(events.contextMenuClicked.emit(
    {
      menuItemId: "download-with-cove",
      srcUrl: "https://cdn.example.test/v/clip.mp4",
      pageUrl: "https://example.test/watch",
    },
    CHROME_TAB,
  ));

  assert.equal(calls.native.length, 1);
  assert.equal(calls.native[0].url, "https://cdn.example.test/v/clip.mp4");
  assert.equal(calls.native[0].filename, "clip.mp4");
});

// ---- Shared handoff parity ----

test("Chrome and Firefox send the same native message for the same direct media",
     async () => {
  const msg = {
    type: "downloadMedia",
    url: "https://cdn.example.test/v/clip.mp4",
    pageUrl: "https://neutral.example.test/watch",
    requestId: "abcd1234",
  };
  const sender = { tab: { id: 7, url: "https://neutral.example.test/watch",
                          title: "Neutral clip" } };

  const messages = [];
  for (const loaded of [chromeWorker(), loadBackground()]) {
    await settle();
    loaded.calls.native.length = 0;
    let done = null;
    const replied = new Promise((resolve) => { done = resolve; });
    loaded.events.message.emit(msg, sender, done);
    await replied;
    messages.push(loaded.calls.native);
  }

  assert.equal(messages[0].length, 1);
  assert.equal(messages[1].length, 1);
  assert.deepEqual(plain(messages[0][0]), plain(messages[1][0]));
  assert.deepEqual(plain(messages[0][0]), {
    action: "download",
    url: "https://cdn.example.test/v/clip.mp4",
    filename: "Neutral clip.mp4",
    referrer: "https://neutral.example.test/watch",
    cookies: "",
    fileSize: 0,
    userAgent: messages[0][0].userAgent,
    requestId: "abcd1234",
  });
});

test("a Chrome pill handoff reports success the way the pill expects",
     async () => {
  const m = await chromeMediaMessage({
    type: "downloadMedia",
    url: "https://cdn.example.test/v/clip.mp4",
    pageUrl: "https://example.test/watch",
    requestId: "12345678",
  });
  await m.done;

  assert.deepEqual(plain(m.reply()), { ok: true });
  assert.equal(m.calls.native.length, 1);
});

test("a Chrome pill handoff distinguishes an unavailable Cove from a failure",
     async () => {
  for (const [nativeResult, reason] of [
    [{ status: "error", message: "Cove is not available" }, "unavailable"],
    [{ status: "error", message: "Disk full" }, "failed"],
  ]) {
    const m = await chromeMediaMessage(
      { type: "downloadMedia", url: "https://cdn.example.test/v/clip.mp4" },
      { nativeResult },
    );
    await m.done;
    assert.equal(m.reply().reason, reason);
  }
});

test("a Chrome pill handoff records no cookie, token or page title", async () => {
  const m = await chromeMediaMessage(
    {
      type: "downloadMedia",
      url: "https://cdn.example.test/v/clip.mp4?token=s3cr3t-token-value",
      pageUrl: "https://example.test/watch",
      requestId: "deadbeef",
    },
    { cookies: [{ name: "session", value: "s3cr3t-cookie-value" }] },
  );
  await m.done;
  await new Promise((resolve) => setTimeout(resolve, 1100));

  const serialised = JSON.stringify(m.store.data.coveDiag || []);
  for (const secret of ["s3cr3t-cookie-value", "s3cr3t-token-value", "Clip"]) {
    assert.ok(!serialised.includes(secret),
              `${secret} must not reach the diagnostic ring`);
  }
  assert.ok(serialised.includes("deadbeef"), "the request id must correlate");
});

// ---- Deduplication ----

async function chromePillSend(loaded, url, requestId = "aaaaaaaa") {
  let done = null;
  const replied = new Promise((resolve) => { done = resolve; });
  loaded.events.message.emit(
    { type: "downloadMedia", url, pageUrl: "https://example.test/watch", requestId },
    { tab: CHROME_TAB },
    done,
  );
  return replied;
}

test("two pill activations for the same media produce one native download",
     async () => {
  // The background's guard, not the pill's: the content script has its own
  // sentUrls, but this is the worker's recentIntercepted doing the work.
  const loaded = chromeWorker();
  await settle();
  loaded.calls.native.length = 0;

  await chromePillSend(loaded, "https://cdn.example.test/v/clip.mp4");
  loaded.events.downloadCreated.emit({
    id: 1,
    url: "https://cdn.example.test/v/clip.mp4",
    filename: "clip.mp4",
    state: "in_progress",
    startTime: new Date().toISOString(),
    totalBytes: 20_000_000,
  });
  await settle();

  assert.equal(loaded.calls.native.length, 1,
               "the browser's own download event must not send a second time");
  // requestId is only ever set by the pill's handoff, so this is the pill's
  // message and not an interception that happened to be the only one.
  assert.equal(loaded.calls.native[0].requestId, "aaaaaaaa");
});

test("a failed pill handoff leaves a retry able to reach Cove", async () => {
  let attempt = 0;
  const loaded = chromeWorker({
    nativeResult: () => {
      attempt += 1;
      return attempt === 1
        ? { status: "error", message: "Cove is not available" }
        : { status: "ok" };
    },
  });
  await settle();
  loaded.calls.native.length = 0;

  await chromePillSend(loaded, "https://cdn.example.test/v/clip.mp4");
  await chromePillSend(loaded, "https://cdn.example.test/v/clip.mp4");

  assert.equal(loaded.calls.native.length, 2,
               "the dedup mark must be rolled back when the send failed");
  assert.equal(evalIn(loaded.context, "recentIntercepted.size"), 1);
});

test("two different media are never deduplicated against each other",
     async () => {
  const loaded = chromeWorker();
  await settle();
  loaded.calls.native.length = 0;

  await chromePillSend(loaded, "https://cdn.example.test/v/one.mp4");
  await chromePillSend(loaded, "https://cdn.example.test/v/two.mp4");

  assert.deepEqual(loaded.calls.native.map((m) => m.url), [
    "https://cdn.example.test/v/one.mp4",
    "https://cdn.example.test/v/two.mp4",
  ]);
});

// ---- Worker recreation ----

test("a recreated worker initialises media and hands over a fresh click",
     async () => {
  const installedMenus = new Map();
  chromeWorker({ installedMenus });
  await settle();

  // A genuinely new background context. Nothing of the first one's state
  // carries over, which is the point: recentIntercepted and interceptedIds
  // are the worker's, and a restart is how MV3 loses them.
  const restarted = chromeWorker({ installedMenus });
  await settle();
  restarted.calls.native.length = 0;

  assert.equal(evalIn(restarted.context, "recentIntercepted.size"), 0);
  await chromePillSend(restarted, "https://cdn.example.test/v/clip.mp4");

  assert.equal(restarted.calls.native.length, 1);
  assert.equal(restarted.calls.native[0].url, "https://cdn.example.test/v/clip.mp4");
});

test("cross-event dedup is the worker's, so a restart between the two loses it",
     async () => {
  // Measured, not claimed. The content script's sentUrls lives in the page and
  // survives the restart, but it cannot suppress a browser download event -
  // that is handled by the worker, whose recentIntercepted is gone. Both
  // events reaching one worker is the covered case (asserted above); this
  // records the boundary of it rather than pretending it extends further.
  const first = chromeWorker();
  await settle();
  first.calls.native.length = 0;
  await chromePillSend(first, "https://cdn.example.test/v/clip.mp4");
  assert.equal(first.calls.native.length, 1);

  const restarted = chromeWorker({ settings: { enabled: true } });
  await settle();
  restarted.calls.native.length = 0;
  restarted.events.downloadCreated.emit({
    id: 1,
    url: "https://cdn.example.test/v/clip.mp4",
    filename: "clip.mp4",
    state: "in_progress",
    startTime: new Date().toISOString(),
    totalBytes: 20_000_000,
  });
  await settle();

  assert.equal(restarted.calls.native.length, 1,
               "a new worker has no record of the old one's handoff");
});

// ---- Settings ----

test("Chrome keeps interception and the pill as two separate settings",
     async () => {
  // mediaPillEnabled is the content script's; `enabled` gates interception.
  // Turning interception off must not stop a deliberate pill click, exactly
  // as on Firefox.
  const loaded = chromeWorker({
    settings: { enabled: false, minSizeBytes: 0, excludedDomains: [],
                interceptExtensions: [], mediaPillEnabled: true },
  });
  await settle();
  loaded.calls.native.length = 0;

  await chromePillSend(loaded, "https://cdn.example.test/v/clip.mp4");
  assert.equal(loaded.calls.native.length, 1);

  loaded.events.downloadCreated.emit({
    id: 2,
    url: "https://cdn.example.test/other.zip",
    filename: "other.zip",
    state: "in_progress",
    startTime: new Date().toISOString(),
    totalBytes: 20_000_000,
  });
  await settle();
  assert.equal(loaded.calls.native.length, 1, "interception stays off");
});

test("Chrome serves the pill's settings request", async () => {
  const { events } = chromeWorker({
    settings: { enabled: true, minSizeBytes: 0, excludedDomains: [],
                interceptExtensions: [], mediaPillEnabled: false },
  });
  await settle();

  let reply;
  events.message.emit({ type: "getSettings" }, {}, (r) => { reply = r; });
  await settle();

  assert.equal(reply.mediaPillEnabled, false);
  assert.equal(reply.enabled, true);
});

// ---------------------------------------------------------------------------
// Chrome: the new media context action refuses a playlist target
// ---------------------------------------------------------------------------
//
// Enabling video and audio contexts put a new kind of address within reach of
// the menu: a media element's src can name a playlist describing a stream
// rather than a file. Chrome ships no stream handling, so forwarding one would
// present that description as if it were the media. The refusal is Chrome's
// alone - it lives on the Chrome capability - and it is a negative check on
// identifiable manifest targets, not a claim that every other address is a
// direct file.
//
// The pill cannot reach this: Chrome will not decode a playlist, so the
// element never leaves readyState 0 and 2A.1's readiness gate already refuses
// it. This is the separate context-menu entry point.

function mediaMenuClick(loaded, info) {
  return Promise.all(loaded.events.contextMenuClicked.emit(
    { menuItemId: "download-with-cove", ...info }, CHROME_TAB,
  ));
}

for (const srcUrl of [
  "https://cdn.example.test/live/master.m3u8",
  "https://cdn.example.test/live/manifest.mpd",
  "https://cdn.example.test/live/playlist.m3u",
  "https://cdn.example.test/live/MASTER.M3U8",
  "https://cdn.example.test/live/master.m3u8?token=abc123",
  "https://cdn.example.test/live/master.m3u8#t=10",
  "https://cdn.example.test/live/manifest.MPD?cdn=edge&x=1",
]) {
  test(`a Chrome media context action refuses ${srcUrl}`, async () => {
    const loaded = chromeWorker();
    await settle();
    loaded.calls.native.length = 0;

    await mediaMenuClick(loaded, {
      srcUrl, mediaType: "video", pageUrl: "https://example.test/watch",
    });

    assert.equal(loaded.calls.native.length, 0,
                 "a playlist address must not reach the native host");
    assert.equal(evalIn(loaded.context, "recentIntercepted.size"), 0,
                 "a refusal must not leave a mark that blocks a later retry");
  });
}

test("an audio context action refuses a playlist just as a video one does",
     async () => {
  const loaded = chromeWorker();
  await settle();
  loaded.calls.native.length = 0;

  await mediaMenuClick(loaded, {
    srcUrl: "https://cdn.example.test/live/audio.m3u8", mediaType: "audio",
  });

  assert.equal(loaded.calls.native.length, 0);
});

test("a link on the same element cannot smuggle a refused media source past",
     async () => {
  const loaded = chromeWorker();
  await settle();
  loaded.calls.native.length = 0;

  // The link is a perfectly ordinary address, so checking only the address the
  // handler settles on would let it carry the refused media source through.
  await mediaMenuClick(loaded, {
    srcUrl: "https://cdn.example.test/live/master.m3u8",
    linkUrl: "https://cdn.example.test/v/decoy.mp4",
    mediaType: "video",
    pageUrl: "https://example.test/watch",
  });

  assert.equal(loaded.calls.native.length, 0,
               "the media source is what was selected, link or no link");
});

test("a refused media source is not replaced by the page it sits on",
     async () => {
  const loaded = chromeWorker();
  await settle();
  loaded.calls.native.length = 0;

  await mediaMenuClick(loaded, {
    srcUrl: "https://cdn.example.test/live/master.m3u8",
    mediaType: "video",
    pageUrl: "https://example.test/watch",
  });

  assert.deepEqual(loaded.calls.native.map((m) => m.url), []);
});

test("the guard leaves a direct media context action exactly as it was",
     async () => {
  const loaded = chromeWorker();
  await settle();
  loaded.calls.native.length = 0;

  await mediaMenuClick(loaded, {
    srcUrl: "https://cdn.example.test/v/clip.mp4", mediaType: "video",
    pageUrl: "https://example.test/watch",
  });

  assert.equal(loaded.calls.native.length, 1);
  assert.equal(loaded.calls.native[0].url, "https://cdn.example.test/v/clip.mp4");
});

test("the guard leaves extensionless direct media reachable", async () => {
  // The check is on identifiable manifest suffixes. An address with no suffix
  // at all is not one, and must not be swept up by a rule that only knows how
  // to recognise names.
  const loaded = chromeWorker();
  await settle();
  loaded.calls.native.length = 0;

  await mediaMenuClick(loaded, {
    srcUrl: "https://cdn.example.test/v/9f3ab21c", mediaType: "video",
    pageUrl: "https://example.test/watch",
  });

  assert.equal(loaded.calls.native.length, 1);
  assert.equal(loaded.calls.native[0].url, "https://cdn.example.test/v/9f3ab21c");
});

test("an ordinary link to a playlist is untouched by the media guard",
     async () => {
  // Narrow on purpose: this is about the media action the slice added, not an
  // application-wide restriction on what a user may download.
  const loaded = chromeWorker();
  await settle();
  loaded.calls.native.length = 0;

  await mediaMenuClick(loaded, {
    linkUrl: "https://cdn.example.test/live/master.m3u8",
    pageUrl: "https://example.test/watch",
  });

  assert.equal(loaded.calls.native.length, 1);
  assert.equal(loaded.calls.native[0].url,
               "https://cdn.example.test/live/master.m3u8");
});

test("an image context action is untouched by the media guard", async () => {
  const loaded = chromeWorker();
  await settle();
  loaded.calls.native.length = 0;

  await mediaMenuClick(loaded, {
    srcUrl: "https://cdn.example.test/i/poster.png", mediaType: "image",
    pageUrl: "https://example.test/watch",
  });

  assert.equal(loaded.calls.native.length, 1);
  assert.equal(loaded.calls.native[0].url, "https://cdn.example.test/i/poster.png");
});

test("browser-download interception is untouched by the media guard",
     async () => {
  const loaded = chromeWorker({
    settings: { enabled: true, minSizeBytes: 0, excludedDomains: [],
                interceptExtensions: [], mediaPillEnabled: true },
  });
  await settle();
  loaded.calls.native.length = 0;

  loaded.events.downloadCreated.emit({
    id: 9,
    url: "https://cdn.example.test/live/master.m3u8",
    filename: "master.m3u8",
    state: "in_progress",
    startTime: new Date().toISOString(),
    totalBytes: 4096,
  });
  await settle();

  assert.equal(loaded.calls.native.length, 1,
               "the user's own browser download is not this guard's business");
});

test("Firefox keeps the media context behaviour it shipped with", async () => {
  // The refusal is on Chrome's capability. Firefox publishes its own, which
  // does not carry it, so nothing here changes for Firefox.
  const loaded = loadBackground();
  await settle();
  loaded.calls.native.length = 0;

  await mediaMenuClick(loaded, {
    srcUrl: "https://cdn.example.test/live/master.m3u8", mediaType: "video",
    pageUrl: "https://example.test/watch",
  });

  assert.equal(loaded.calls.native.length, 1);
  assert.equal(loaded.calls.native[0].url,
               "https://cdn.example.test/live/master.m3u8");
  assert.equal(evalIn(loaded.context,
                      "typeof CoveMediaCapability.rejectMediaTarget"),
               "undefined");
});

// ---------------------------------------------------------------------------
// Codex review round 1 - two findings, both on code this slice added
// ---------------------------------------------------------------------------

// Finding 1: a media action inside a hyperlink handed over the link.
//
// The handler has always preferred info.linkUrl, which is right for the link
// and image targets it shipped with. The video and audio targets are new, and
// for those the media element's own source is what was selected: a player
// wrapped in a hyperlink would otherwise hand over the link's destination, a
// page, in place of the media. Firefox publishes no media-target policy and
// keeps link-first, so its behaviour is unchanged.

test("a linked video hands over the video, not the link's destination",
     async () => {
  const loaded = chromeWorker();
  await settle();
  loaded.calls.native.length = 0;

  await mediaMenuClick(loaded, {
    srcUrl: "https://cdn.example.test/v/clip.mp4",
    linkUrl: "https://example.test/watch-page",
    mediaType: "video",
    pageUrl: "https://example.test/watch-page",
  });

  assert.equal(loaded.calls.native.length, 1);
  assert.equal(loaded.calls.native[0].url, "https://cdn.example.test/v/clip.mp4",
               "a direct media action must never become a page request");
});

test("a linked audio element hands over the audio, not the link", async () => {
  const loaded = chromeWorker();
  await settle();
  loaded.calls.native.length = 0;

  await mediaMenuClick(loaded, {
    srcUrl: "https://cdn.example.test/a/track.m4a",
    linkUrl: "https://example.test/album",
    mediaType: "audio",
  });

  assert.equal(loaded.calls.native.length, 1);
  assert.equal(loaded.calls.native[0].url, "https://cdn.example.test/a/track.m4a");
});

test("a linked image keeps the link-first behaviour it shipped with",
     async () => {
  const loaded = chromeWorker();
  await settle();
  loaded.calls.native.length = 0;

  await mediaMenuClick(loaded, {
    srcUrl: "https://cdn.example.test/i/thumb.png",
    linkUrl: "https://cdn.example.test/i/full.png",
    mediaType: "image",
  });

  assert.equal(loaded.calls.native.length, 1);
  assert.equal(loaded.calls.native[0].url, "https://cdn.example.test/i/full.png");
});

test("a media element with no source of its own still follows its link",
     async () => {
  const loaded = chromeWorker();
  await settle();
  loaded.calls.native.length = 0;

  await mediaMenuClick(loaded, {
    linkUrl: "https://cdn.example.test/v/clip.mp4",
    mediaType: "video",
  });

  assert.equal(loaded.calls.native.length, 1);
  assert.equal(loaded.calls.native[0].url, "https://cdn.example.test/v/clip.mp4");
});

test("Firefox keeps link-first on a linked video", async () => {
  const loaded = loadBackground();
  await settle();
  loaded.calls.native.length = 0;

  await mediaMenuClick(loaded, {
    srcUrl: "https://cdn.example.test/v/clip.mp4",
    linkUrl: "https://example.test/watch-page",
    mediaType: "video",
    pageUrl: "https://example.test/watch-page",
  });

  assert.equal(loaded.calls.native.length, 1);
  assert.equal(loaded.calls.native[0].url, "https://example.test/watch-page",
               "the media-target policy is Chrome's; Firefox is untouched");
});

// Finding 2: creation raced an unfinished removal.
//
// removeAll answers with a completion callback on Chrome and gained promise
// support only in Chrome 123; the manifest names no minimum version, and
// Firefox's API is promise-only and validates its arguments. Creating before
// removal completes lets the outstanding removal take the new item with it,
// leaving no menu at all. The fake defers removal in every mode, so a create
// that jumped the queue is wiped and shows up as an empty menu.

for (const menuApi of ["callback", "strict", "lenient"]) {
  test(`the menu survives removal sequencing on a ${menuApi} removeAll`,
       async () => {
    const installedMenus = new Map();
    const { calls } = chromeWorker({ installedMenus, menuApi });
    await settle();

    assert.equal(installedMenus.size, 1,
                 "creation must wait for removal to finish");
    assert.deepEqual(
      Array.from(installedMenus.get("download-with-cove").contexts),
      ["link", "image", "video", "audio"],
    );
    assert.deepEqual(calls.menuOps, ["removeAll", "removed", "create"]);
    assert.equal(calls.menus.length, 1, "exactly one create, never two");
  });
}

test("an upgrade still gains the media contexts on a callback-only removeAll",
     async () => {
  const installedMenus = new Map();
  chromeWorker({ installedMenus, menuApi: "callback",
                 missingScripts: ["media-core.js", "media-chrome.js"] });
  await settle();
  assert.deepEqual(
    Array.from(installedMenus.get("download-with-cove").contexts),
    ["link", "image"],
  );

  chromeWorker({ installedMenus, menuApi: "callback" });
  await settle();

  assert.deepEqual(
    Array.from(installedMenus.get("download-with-cove").contexts),
    ["link", "image", "video", "audio"],
  );
});

test("Firefox still registers exactly one menu on its promise-only API",
     async () => {
  const { calls, installedMenus } = loadBackground({ menuApi: "strict" });
  await settle();

  assert.equal(calls.menus.length, 1);
  assert.equal(installedMenus.size, 1);
  assert.deepEqual(
    Array.from(installedMenus.get("download-with-cove").contexts),
    ["link", "image", "video", "audio"],
  );
});
