// Tests for extension/diagnostics.js - the extension-local sanitized ring.
//
// The extension has to be able to explain a failure while Cove is closed, so
// this ring is the only diagnostics surface available in that case. Nothing
// page-identifying may ever enter it: no page URL, no media URL, no title,
// no native-message body.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");

const RD_TOKEN = "A7QK3ZP9WVN2XLMDR4TJ6YB8C5FGH1SE";
const MEDIA_URL = `https://cdn.example.com/v/${RD_TOKEN}/video.mp4?token=${RD_TOKEN}`;
const PAGE_URL = "https://news.example.com/articles/private-thing";
const COOKIE = "session=eyJhbGciOiJIUzI1NiJ9.QWxhZGRpbjpvcGVuIHNlc2FtZQ";

const SECRETS = [RD_TOKEN, "eyJhbGciOiJIUzI1NiJ9", "private-thing", "video.mp4"];

function assertClean(text) {
  for (const secret of SECRETS) {
    assert.ok(!text.includes(secret), `leaked ${secret} in: ${text}`);
  }
}

function loadDiagnostics() {
  const context = vm.createContext({ console: { log() {}, error() {} }, URL });
  const source = fs.readFileSync("extension/diagnostics.js", "utf8");
  vm.runInContext(source, context, { filename: "extension/diagnostics.js" });
  return context.CoveDiag;
}

function fakeStorage({ failSet = false, quotaOnce = false, slowGet = 0,
                      failClear = false } = {}) {
  const data = {};
  let failures = 0;
  return {
    data,
    get failures() { return failures; },
    async get(key) {
      for (let i = 0; i < slowGet; i += 1) await Promise.resolve();
      return key in data ? { [key]: data[key] } : {};
    },
    async set(obj) {
      if (failClear && Array.isArray(obj.coveDiag) && obj.coveDiag.length === 0) {
        throw new Error("storage is read-only");
      }
      if (failSet || (quotaOnce && failures === 0)) {
        failures += 1;
        throw new Error("QuotaExceededError");
      }
      Object.assign(data, obj);
    },
    async remove(key) { delete data[key]; },
  };
}

function makeDiag(options = {}) {
  const CoveDiag = loadDiagnostics();
  const storage = options.storage || fakeStorage();
  const diag = CoveDiag.createDiagnostics({
    storage,
    context: options.context || "background",
    version: "1.4.4",
  });
  return { CoveDiag, diag, storage };
}

async function settle() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

// ---------------------------------------------------------------------------
// Sanitization
// ---------------------------------------------------------------------------

test("a media url never survives into a record", async () => {
  const { diag } = makeDiag();
  diag.record("extension.content", "video_download_requested", "INFO", {
    url: MEDIA_URL,
    pageUrl: PAGE_URL,
  });
  assertClean(JSON.stringify(diag.records()));
});

test("known page-identifying field names are dropped outright", async () => {
  const { diag } = makeDiag();
  diag.record("extension.content", "video_download_requested", "INFO", {
    url: MEDIA_URL,
    pageUrl: PAGE_URL,
    pageTitle: "My private tab",
    title: "My private tab",
    filename: "video.mp4",
    cookies: COOKIE,
    payload: { action: "download", url: MEDIA_URL },
  });
  const dumped = JSON.stringify(diag.records());
  assertClean(dumped);
  assert.ok(!dumped.includes("My private tab"));
  assert.ok(!dumped.includes("action"));
});

test("only allowlisted primitive values are kept", async () => {
  const { diag } = makeDiag();
  diag.record("extension.background", "native_message_sent", "INFO", {
    action: "download",
    ok: true,
    count: 3,
    weird: { deep: { deeper: 1 } },
    fn: () => {},
  });
  const fields = diag.records()[0].fields;
  assert.equal(fields.action, "download");
  assert.equal(fields.ok, true);
  assert.equal(fields.count, 3);
  assert.ok(!("fn" in fields));
});

test("sanitizeUrl keeps only scheme, safe host and route", () => {
  const { CoveDiag } = makeDiag();
  assert.equal(
    CoveDiag.sanitizeUrl(`https://real-debrid.com/d/${RD_TOKEN}`),
    "https://real-debrid.com/d/<redacted>"
  );
  assert.equal(CoveDiag.sanitizeUrl("magnet:?xt=urn:btih:abc"), "magnet:<redacted>");
  assert.equal(CoveDiag.sanitizeUrl(null), "<redacted>");
});

test("sanitizeText removes long opaque tokens but keeps event vocabulary", () => {
  const { CoveDiag } = makeDiag();
  assert.ok(!CoveDiag.sanitizeText(`token ${RD_TOKEN}`).includes(RD_TOKEN));
  assert.equal(CoveDiag.sanitizeText("app_unavailable"), "app_unavailable");
});

// ---------------------------------------------------------------------------
// Ring and retention
// ---------------------------------------------------------------------------

test("the ring holds at most 300 records and drops the oldest first", async () => {
  const { CoveDiag, diag } = makeDiag();
  assert.equal(CoveDiag.MAX_RECORDS, 300);
  for (let i = 0; i < CoveDiag.MAX_RECORDS + 50; i += 1) {
    diag.record("extension.background", "tick", "INFO", { i });
  }
  const records = diag.records();
  assert.equal(records.length, CoveDiag.MAX_RECORDS);
  assert.equal(records[records.length - 1].fields.i, CoveDiag.MAX_RECORDS + 49);
  assert.equal(records[0].fields.i, 50);
});

test("records carry a timestamp, level, component, event and session", async () => {
  const { diag } = makeDiag();
  diag.record("extension.popup", "connection_status_rendered", "INFO", {});
  const record = diag.records()[0];
  assert.match(record.ts, /^\d{4}-\d{2}-\d{2}T[\d:.]+Z$/);
  assert.equal(record.level, "INFO");
  assert.equal(record.component, "extension.popup");
  assert.equal(record.event, "connection_status_rendered");
  assert.match(record.session, /^[0-9a-f]{8}$/);
});

test("a request id is recorded when it is well formed and dropped otherwise", async () => {
  const { diag } = makeDiag();
  diag.record("extension.content", "video_download_requested", "INFO", {}, "51c2a711");
  diag.record("extension.content", "video_download_requested", "INFO", {}, "bad id!");
  const records = diag.records();
  assert.equal(records[0].request, "51c2a711");
  assert.equal(records[1].request, undefined);
});

test("newRequestId produces an eight hex character id", () => {
  const { CoveDiag } = makeDiag();
  assert.match(CoveDiag.newRequestId(), /^[0-9a-f]{8}$/);
});

// ---------------------------------------------------------------------------
// Persistence
// ---------------------------------------------------------------------------

test("records are persisted to storage.local under coveDiag", async () => {
  const { CoveDiag, diag, storage } = makeDiag();
  diag.record("extension.background", "request_received", "INFO", {});
  await diag.flush();
  assert.equal(CoveDiag.STORAGE_KEY, "coveDiag");
  assert.equal(storage.data.coveDiag.length, 1);
});

test("persisted records are sanitized on disk too", async () => {
  const { diag, storage } = makeDiag();
  diag.record("extension.content", "video_download_requested", "INFO", {
    url: MEDIA_URL,
    pageUrl: PAGE_URL,
  });
  await diag.flush();
  assertClean(JSON.stringify(storage.data.coveDiag));
});

test("a background restart recovers the persisted ring", async () => {
  const { CoveDiag, diag, storage } = makeDiag();
  diag.record("extension.background", "native_message_sent", "INFO", {});
  await diag.flush();

  const restarted = CoveDiag.createDiagnostics({ storage, context: "background" });
  await restarted.load();
  assert.equal(restarted.records().length, 1);
  assert.equal(restarted.records()[0].event, "native_message_sent");
});

test("a quota failure halves retention and retries once", async () => {
  const { CoveDiag, diag, storage } = makeDiag({
    storage: fakeStorage({ quotaOnce: true }),
  });
  for (let i = 0; i < CoveDiag.MAX_RECORDS; i += 1) {
    diag.record("extension.background", "tick", "INFO", { i });
  }
  await diag.flush();
  assert.equal(storage.failures, 1);
  assert.ok(storage.data.coveDiag.length <= CoveDiag.MAX_RECORDS / 2);
});

test("a storage that always fails falls back to memory only", async () => {
  const { diag, storage } = makeDiag({ storage: fakeStorage({ failSet: true }) });
  diag.record("extension.background", "tick", "INFO", {});
  await diag.flush();
  assert.equal(storage.data.coveDiag, undefined);
  assert.equal(diag.memoryOnly, true);
  assert.equal(diag.records().length, 1);
});

test("recording never throws even without any storage at all", async () => {
  const { CoveDiag } = makeDiag();
  const diag = CoveDiag.createDiagnostics({ storage: null });
  diag.record("extension.background", "tick", "INFO", { a: 1 });
  await diag.flush();
  assert.equal(diag.records().length, 1);
});

test("clear empties both memory and storage", async () => {
  const { diag, storage } = makeDiag();
  diag.record("extension.background", "tick", "INFO", {});
  await diag.flush();
  await diag.clear();
  assert.equal(diag.records().length, 0);
  assert.ok(!storage.data.coveDiag || storage.data.coveDiag.length === 0);
});

// ---------------------------------------------------------------------------
// Report
// ---------------------------------------------------------------------------

test("the copied report carries context, events and the sanitization notice", async () => {
  const { CoveDiag, diag } = makeDiag();
  diag.setEnvironment({ extensionVersion: "1.4.4", browser: "Firefox 140",
                        appVersion: "3.4.0" });
  diag.record("extension.popup", "connection_status_rendered", "INFO",
              { state: "connected" });
  diag.record("extension.content", "video_pill_result", "WARNING",
              { result: "app_unavailable", url: MEDIA_URL });

  const report = diag.report();
  assert.ok(report.includes("Cove extension diagnostics"));
  assert.ok(report.includes("1.4.4"));
  assert.ok(report.includes("Firefox 140"));
  assert.ok(report.includes("connection_status_rendered"));
  assert.ok(report.includes("video_pill_result"));
  assert.ok(report.includes(CoveDiag.SANITIZATION_NOTICE));
  assertClean(report);
});

test("a report can still be produced when Cove was never reachable", async () => {
  const { diag } = makeDiag();
  diag.record("extension.popup", "connection_status_rendered", "INFO",
              { state: "not_connected" });
  diag.record("extension.native_bridge", "request_failed", "WARNING",
              { reason: "transport_error" });
  const report = diag.report();
  assert.ok(report.includes("transport_error"));
  assert.ok(report.includes("not_connected"));
});

test("the report is bounded even with a full ring", async () => {
  const { CoveDiag, diag } = makeDiag();
  for (let i = 0; i < CoveDiag.MAX_RECORDS; i += 1) {
    diag.record("extension.background", "tick", "INFO", { i });
  }
  assert.ok(diag.report().split("\n").length <= CoveDiag.MAX_RECORDS + 20);
});

// ---------------------------------------------------------------------------
// Popup integration
//
// The popup is the only place a user can copy diagnostics while Cove is
// unreachable, so its two buttons have to work with no connection at all.
// ---------------------------------------------------------------------------

function stubElement(id) {
  return {
    id,
    textContent: "",
    className: "",
    style: {},
    dataset: {},
    disabled: false,
    listeners: {},
    children: [],
    addEventListener(type, fn) { this.listeners[type] = fn; },
    async click() {
      if (this.listeners.click) await this.listeners.click();
    },
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren() { this.children = []; },
  };
}

function loadPopup({ pingReply = { status: "ok", version: "3.4.0" },
                     reportReply = { ok: true, text: "REPORT BODY" },
                     clearReply = { ok: true },
                     failReport = false, storedRecords = null } = {}) {
  const elements = {};
  const sent = [];
  const clipboard = [];
  const getElement = (id) => {
    if (!elements[id]) elements[id] = stubElement(id);
    return elements[id];
  };

  const storage = fakeStorage();
  if (storedRecords) storage.data.coveDiag = storedRecords;

  const browser = {
    runtime: {
      getManifest: () => ({ version: "1.4.4" }),
      async sendMessage(message) {
        sent.push(message);
        if (message.type === "ping") return pingReply;
        if (message.type === "getSettings") return { enabled: true };
        if (message.type === "coveDiagReport") {
          if (failReport) throw new Error("background is gone");
          return reportReply;
        }
        if (message.type === "coveDiagClear") return clearReply;
        return {};
      },
      openOptionsPage() {},
    },
    storage: { local: storage },
  };

  const document = {
    getElementById: getElement,
    createElement: (tag) => stubElement(tag),
  };

  const context = vm.createContext({
    document,
    navigator: { clipboard: { async writeText(text) { clipboard.push(text); } } },
    console: { log() {}, error() {} },
    setInterval() {},
    setTimeout(fn) { return fn; },
    URL,
    globalThis: undefined,
  });
  context.globalThis = context;
  context.browser = browser;
  context.chrome = browser;

  vm.runInContext(fs.readFileSync("extension/diagnostics.js", "utf8"), context,
                  { filename: "extension/diagnostics.js" });
  vm.runInContext(fs.readFileSync("extension/popup/popup.js", "utf8"), context,
                  { filename: "extension/popup/popup.js" });
  return { elements, sent, clipboard, getElement, storage };
}

test("popup.html loads the diagnostics module and the two buttons", () => {
  const html = fs.readFileSync("extension/popup/popup.html", "utf8");
  assert.ok(html.includes("diagnostics.js"));
  assert.ok(html.includes("copy-diagnostics"));
  assert.ok(html.includes("clear-diagnostics"));
});

test("the popup records what it rendered as the connection status", async () => {
  const { sent } = loadPopup({ pingReply: { status: "ok", version: "3.4.0" } });
  await settle();
  const record = sent.find(
    (m) => m.type === "coveDiag" && m.event === "connection_status_rendered"
  );
  assert.ok(record, "the rendered status must be recorded");
  assert.equal(record.component, "extension.popup");
  assert.equal(record.fields.state, "connected");
  assert.equal(record.fields.appVersion, "3.4.0");
});

test("a disconnected popup records the state it actually showed", async () => {
  const { sent, elements } = loadPopup({ pingReply: { status: "error" } });
  await settle();
  const record = sent.find(
    (m) => m.type === "coveDiag" && m.event === "connection_status_rendered"
  );
  assert.equal(record.fields.state, "not_connected");
  // Wording is a product decision and must be untouched.
  assert.equal(elements["connection-status"].textContent, "Not connected to Cove");
});

test("the connected wording is unchanged", async () => {
  const { elements } = loadPopup({ pingReply: { status: "ok", version: "3.4.0" } });
  await settle();
  assert.equal(elements["connection-status"].textContent, "Connected - Cove v3.4.0");
});

test("copy diagnostics puts the background report on the clipboard", async () => {
  const { getElement, clipboard } = loadPopup();
  await settle();
  await getElement("copy-diagnostics").click();
  await settle();
  assert.equal(clipboard[0], "REPORT BODY");
});

test("copy diagnostics still works when the background cannot answer", async () => {
  const stored = [{
    ts: "2026-08-06T12:00:00.000Z", level: "WARNING", component: "extension.content",
    event: "video_pill_result", session: "aaaabbbb", context: "content",
    fields: { result: "app_unavailable" },
  }];
  const { getElement, clipboard } = loadPopup({ failReport: true, storedRecords: stored });
  await settle();
  await getElement("copy-diagnostics").click();
  await settle();
  assert.ok(clipboard[0].includes("video_pill_result"));
  assert.ok(clipboard[0].includes("app_unavailable"));
  assert.ok(clipboard[0].includes(loadDiagnostics().SANITIZATION_NOTICE));
});

test("clear diagnostics asks the background to empty the ring", async () => {
  const { getElement, sent } = loadPopup();
  await settle();
  await getElement("clear-diagnostics").click();
  await settle();
  assert.ok(sent.some((m) => m.type === "coveDiagClear"));
});

test("a clipboard failure never throws out of the popup", async () => {
  const { getElement } = loadPopup();
  await settle();
  const context = getElement("copy-diagnostics");
  await context.click();
  await settle();
});

// ---------------------------------------------------------------------------
// Version metadata in the report header
//
// A support report is only actionable if it says which extension, which
// browser and which Cove it came from. The family and major version are the
// most that may be taken from the user agent - the full string is a
// fingerprint, not a diagnostic.
// ---------------------------------------------------------------------------

test("browserLabel reports family and major version only", () => {
  const { CoveDiag } = makeDiag();
  assert.equal(
    CoveDiag.browserLabel(
      "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
    ),
    "Firefox 140"
  );
  assert.equal(
    CoveDiag.browserLabel(
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Chrome 130"
  );
});

test("browserLabel never returns the raw user agent", () => {
  const { CoveDiag } = makeDiag();
  const ua = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0";
  const label = CoveDiag.browserLabel(ua);
  assert.ok(!label.includes("X11"));
  assert.ok(!label.includes("Linux x86_64"));
  assert.equal(CoveDiag.browserLabel("something unrecognisable"), "unknown");
  assert.equal(CoveDiag.browserLabel(null), "unknown");
});

test("the report header carries the extension version it was built with", () => {
  const { diag } = makeDiag();
  assert.ok(diag.report().includes("extension version: 1.4.4"));
});

test("a later ping updates the recorded Cove version", () => {
  const { diag } = makeDiag();
  assert.ok(diag.report().includes("last seen Cove version: unknown"));
  diag.setEnvironment({ appVersion: "3.4.0" });
  assert.ok(diag.report().includes("last seen Cove version: 3.4.0"));
});

test("the popup fallback report still names the extension version", async () => {
  const stored = [{
    ts: "2026-08-06T12:00:00.000Z", level: "WARNING", component: "extension.content",
    event: "video_pill_result", session: "aaaabbbb", context: "content",
    fields: { result: "app_unavailable" },
  }];
  const { getElement, clipboard } = loadPopup({ failReport: true, storedRecords: stored });
  await settle();
  await getElement("copy-diagnostics").click();
  await settle();
  assert.ok(clipboard[0].includes("extension version: 1.4.4"));
});

// ---------------------------------------------------------------------------
// Hydration race
//
// createDiagnostics returns synchronously but load() is async, so events can
// be recorded while the stored ring is still being read. Those events must
// survive hydration, and a report must not be served from a half-loaded ring.
// ---------------------------------------------------------------------------

test("load merges stored history with events recorded meanwhile", async () => {
  const { CoveDiag, diag, storage } = makeDiag();
  diag.record("extension.background", "old_event", "INFO", {});
  await diag.flush();

  const restarted = CoveDiag.createDiagnostics({ storage, context: "background" });
  // Recorded before hydration finishes, exactly as the startup ping is.
  restarted.record("extension.background", "native_ping_result", "INFO", {});
  await restarted.load();

  // Joined rather than deep-compared: the module runs in its own vm realm, so
  // its arrays do not share Array.prototype with this one.
  const events = restarted.records().map((r) => r.event).join(",");
  assert.equal(events, "old_event,native_ping_result");
});

test("hydration keeps the ring bounded", async () => {
  const { CoveDiag, diag, storage } = makeDiag();
  for (let i = 0; i < CoveDiag.MAX_RECORDS; i += 1) {
    diag.record("extension.background", "old", "INFO", { i });
  }
  await diag.flush();

  const restarted = CoveDiag.createDiagnostics({ storage, context: "background" });
  for (let i = 0; i < 20; i += 1) {
    restarted.record("extension.background", "fresh", "INFO", { i });
  }
  await restarted.load();

  const records = restarted.records();
  assert.equal(records.length, CoveDiag.MAX_RECORDS);
  // The newest events are the ones that must survive the trim.
  assert.equal(records[records.length - 1].event, "fresh");
});

test("a diagnostics instance exposes a ready promise for its hydration", async () => {
  const { CoveDiag, storage } = makeDiag();
  const diag = CoveDiag.createDiagnostics({ storage, context: "background" });
  assert.ok(diag.ready && typeof diag.ready.then === "function");
  await diag.ready;
});

// ---------------------------------------------------------------------------
// Serialized persistence
//
// Hydration, flush and clear all write the same key. Left unordered, a flush
// that started during hydration overwrites the stored history with a partial
// ring, and a flush queued around a clear resurrects what was just deleted.
// ---------------------------------------------------------------------------

test("a flush during hydration does not clobber stored history", async () => {
  const { CoveDiag, diag, storage } = makeDiag({ storage: fakeStorage({ slowGet: 8 }) });
  diag.record("extension.background", "old_event", "INFO", {});
  await diag.flush();

  const restarted = CoveDiag.createDiagnostics({ storage, context: "background" });
  restarted.record("extension.background", "native_ping_result", "INFO", {});
  // Flush immediately, while the hydrating read is still outstanding.
  await restarted.flush();
  await restarted.ready;
  await restarted.flush();

  const stored = storage.data.coveDiag.map((r) => r.event).join(",");
  assert.equal(stored, "old_event,native_ping_result");
});

test("a flush racing a clear never resurrects the cleared records", async () => {
  const { diag, storage } = makeDiag();
  diag.record("extension.background", "tick", "INFO", {});
  await diag.ready;

  const flushing = diag.flush();
  const clearing = diag.clear();
  await Promise.all([flushing, clearing]);

  assert.equal(storage.data.coveDiag.length, 0);
  assert.equal(diag.records().length, 0);
});

test("stored records are a copy, not the live ring", async () => {
  const { diag, storage } = makeDiag();
  diag.record("extension.background", "first", "INFO", {});
  await diag.flush();
  diag.record("extension.background", "second", "INFO", {});
  assert.equal(storage.data.coveDiag.length, 1);
});

test("clear reports failure when storage refuses the write", async () => {
  const { diag } = makeDiag({ storage: fakeStorage({ failClear: true }) });
  diag.record("extension.background", "tick", "INFO", {});
  await diag.ready;
  assert.equal(await diag.clear(), false);
});

test("the popup says so when a clear did not happen", async () => {
  const { getElement } = loadPopup({ clearReply: { ok: false } });
  await settle();
  const button = getElement("clear-diagnostics");
  await button.click();
  await settle();
  assert.equal(button.textContent, "Clear failed");
});

test("the popup confirms a clear that did happen", async () => {
  const { getElement } = loadPopup({ clearReply: { ok: true } });
  await settle();
  const button = getElement("clear-diagnostics");
  await button.click();
  await settle();
  assert.equal(button.textContent, "Cleared");
});

// ---------------------------------------------------------------------------
// Clear during hydration
//
// Clear is synchronous from the user's point of view: the moment they ask,
// the ring is empty. Hydration is not - it may already be reading the stored
// ring when Clear arrives. The read must not be allowed to land afterwards,
// or records the user explicitly deleted reappear in Copy diagnostics.
// ---------------------------------------------------------------------------

test("a clear issued during hydration is not undone by the read", async () => {
  const { CoveDiag, diag, storage } = makeDiag({ storage: fakeStorage({ slowGet: 8 }) });
  diag.record("extension.background", "event_from_last_run", "INFO", {});
  await diag.flush();

  // A fresh session: hydration starts at construction and is still in flight.
  const restarted = CoveDiag.createDiagnostics({ storage, context: "background" });
  const cleared = restarted.clear();
  await cleared;
  await restarted.ready;

  assert.equal(storage.data.coveDiag.length, 0, "storage must stay cleared");
  assert.equal(restarted.records().length, 0, "the ring must stay cleared");
  assert.ok(
    !JSON.stringify(restarted.records()).includes("event_from_last_run"),
    "records() exposed a pre-clear record"
  );
  assert.ok(
    !restarted.report().includes("event_from_last_run"),
    "the copied report exposed a pre-clear record"
  );
});

test("diagnostics recorded after a mid-hydration clear still work", async () => {
  const { CoveDiag, diag, storage } = makeDiag({ storage: fakeStorage({ slowGet: 8 }) });
  diag.record("extension.background", "event_from_last_run", "INFO", {});
  await diag.flush();

  const restarted = CoveDiag.createDiagnostics({ storage, context: "background" });
  await restarted.clear();
  restarted.record("extension.background", "after_clear", "INFO", {});
  await restarted.ready;
  await restarted.flush();

  assert.equal(restarted.records().map((r) => r.event).join(","), "after_clear");
  assert.equal(storage.data.coveDiag.map((r) => r.event).join(","), "after_clear");
  assert.ok(!restarted.report().includes("event_from_last_run"));
});

test("an event recorded before a mid-hydration clear is still dropped", async () => {
  const { CoveDiag, diag, storage } = makeDiag({ storage: fakeStorage({ slowGet: 8 }) });
  diag.record("extension.background", "event_from_last_run", "INFO", {});
  await diag.flush();

  const restarted = CoveDiag.createDiagnostics({ storage, context: "background" });
  restarted.record("extension.background", "before_clear", "INFO", {});
  await restarted.clear();
  await restarted.ready;

  assert.equal(restarted.records().length, 0);
  assert.ok(!restarted.report().includes("before_clear"));
});
