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

function loadBackground({ nativeResult = { status: "ok" }, settings } = {}) {
  const calls = { native: [], cancel: [], erase: [] };
  const events = {
    downloadCreated: event(),
    downloadChanged: event(),
    contextMenuClicked: event(),
    message: event(),
  };
  const browserDownloads = [];
  const quietEvent = () => event();
  const store = {
    async get(key) {
      if (key === "settings") return settings ? { settings } : {};
      return {};
    },
    async set() {},
  };
  const browser = {
    action: {
      async setBadgeText() {},
      async setBadgeBackgroundColor() {},
    },
    commands: { onCommand: quietEvent() },
    contextMenus: { create() {}, onClicked: events.contextMenuClicked },
    cookies: { async getAll() { return []; } },
    downloads: {
      onCreated: events.downloadCreated,
      onChanged: events.downloadChanged,
      async cancel(id) { calls.cancel.push(id); },
      async erase(query) { calls.erase.push(query); },
      async download(options) { browserDownloads.push(options); },
    },
    notifications: { async create() {} },
    runtime: {
      lastError: null,
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
      async query() { return []; },
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
    navigator: { userAgent: "test" },
    URL,
    setTimeout,
    clearTimeout,
  });
  const source = fs.readFileSync("extension/background.js", "utf8");
  vm.runInContext(source, context, { filename: "extension/background.js" });
  return { calls, events, browserDownloads };
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
