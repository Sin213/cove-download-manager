const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");
const EXTENSION_ORIGIN = "chrome-extension://covetestextensionid/";
const PAGE_SENDER = { id: "cove", url: EXTENSION_ORIGIN + "confirm-exclude.html" };
function event() {
  const listeners = [];
  return {
    addListener(listener) { listeners.push(listener); },
    emit(...args) { return listeners.map((listener) => listener(...args)); },
  };
}
function storageArea(data, onSet) {
  return {
    async get(key) {
      if (key === null || key === undefined) return structuredClone(data);
      if (Array.isArray(key)) {
        return structuredClone(Object.fromEntries(key.filter((name) => name in data)
          .map((name) => [name, data[name]])));
      }
      return structuredClone(key in data ? { [key]: data[key] } : {});
    },
    async set(values) {
      if (onSet) await onSet(values);
      Object.assign(data, values);
    },
    async remove(key) {
      for (const name of Array.isArray(key) ? key : [key]) delete data[name];
    },
  };
}
function loadBackground({
  sessionData = {}, now = 1_700_000_000_000,
  tokens = ["123e4567-e89b-42d3-a456-426614174000",
            "123e4567-e89b-42d3-a456-426614174001"],
  settings = { enabled: true, mediaPillEnabled: true, excludedDomains: [] },
  settingsWriteHook = null,
} = {}) {
  const message = event();
  const storageChanged = event();
  const tabsCreated = [];
  const localData = { settings };
  const local = storageArea(localData, async (values) => {
    if (!("settings" in values)) return;
    if (settingsWriteHook) await settingsWriteHook(values.settings);
    storageChanged.emit({ settings: { newValue: values.settings } }, "local");
  });
  const session = storageArea(sessionData);
  let tokenIndex = 0;
  class TestDate extends Date {
    static now() { return now; }
  }
  const quiet = () => event();
  const browser = {
    action: { async setBadgeText() {}, async setBadgeBackgroundColor() {} },
    commands: { onCommand: quiet() },
    contextMenus: {
      create(_props, callback) { if (callback) callback(); },
      async removeAll() {},
      onClicked: quiet(),
    },
    cookies: { async getAll() { return []; } },
    downloads: {
      onCreated: quiet(), onChanged: quiet(), onErased: quiet(),
      async cancel() {}, async erase() {}, async search() { return []; },
      async download() {},
    },
    notifications: { async create() {} },
    runtime: {
      id: "cove", lastError: null,
      getManifest: () => ({ version: "1.4.4" }),
      getURL: (path) => EXTENSION_ORIGIN + path,
      onInstalled: quiet(), onMessage: message,
      async sendNativeMessage() { return { status: "ok" }; },
    },
    storage: { local, session, onChanged: storageChanged },
    tabs: {
      async create(details) { tabsCreated.push(details); return { id: 99, ...details }; },
      async query() { return []; }, async sendMessage() {},
      onRemoved: quiet(), onUpdated: quiet(), onActivated: quiet(),
    },
    webRequest: { onHeadersReceived: quiet() },
  };
  const context = vm.createContext({
    browser, globalThis: undefined, console: { log() {}, error() {}, warn() {} },
    navigator: { userAgent: "test" }, URL, Date: TestDate, Promise, Map, Set,
    crypto: { randomUUID: () => tokens[tokenIndex++] }, setTimeout, clearTimeout,
  });
  context.globalThis = context;
  vm.runInContext(fs.readFileSync("extension/background.js", "utf8"), context,
                  { filename: "extension/background.js" });
  return {
    browser, context, message, sessionData, tabsCreated,
    excluded: () => [...(localData.settings.excludedDomains || [])],
    setNow(value) { now = value; },
  };
}
async function settle() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}
async function send(loaded, body, sender) {
  let reply;
  loaded.message.emit(body, sender, (value) => { reply = value; });
  for (let i = 0; i < 8; i += 1) await settle();
  return reply;
}
function contentSender(url = "https://news.example.test/watch", tabId = 42) {
  return { tab: { id: tabId, url }, url, frameId: 0 };
}
async function createIntent(loaded, sender = contentSender()) {
  const before = loaded.tabsCreated.length;
  const reply = await send(loaded, {
    type: "requestExcludeConfirmation", expectHost: new URL(sender.tab.url).hostname,
  }, sender);
  assert.equal(reply && reply.ok, true, "the request must create an intent");
  assert.equal(loaded.tabsCreated.length, before + 1, "the request must open one confirm page");
  const url = loaded.tabsCreated.at(-1).url;
  assert.match(url, /^chrome-extension:\/\/covetestextensionid\/confirm-exclude\.html#[^#?]+$/);
  const token = url.slice(url.indexOf("#") + 1);
  assert.match(token, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  return token;
}
test("clickjacking boundary: a content message cannot directly write an exclusion", async () => {
  const loaded = loadBackground();
  const reply = await send(loaded,
    { type: "excludeCurrentSite", expectHost: "news.example.test" }, contentSender());
  assert.notEqual(reply && reply.ok, true);
  assert.deepEqual(loaded.excluded(), []);
});
test("confirmation authority: only the packaged confirmation page can commit", async () => {
  const loaded = loadBackground();
  const token = await createIntent(loaded);
  for (const sender of [
    {},
    contentSender(),
    { id: "cove", url: EXTENSION_ORIGIN + "options/options.html" },
    { id: "cove", url: EXTENSION_ORIGIN + "popup/popup.html" },
  ]) {
    for (const type of ["getExcludeConfirmation", "confirmExcludeSite",
                        "cancelExcludeConfirmation"]) {
      const reply = await send(loaded, { type, token }, sender);
      assert.notEqual(reply && reply.ok, true);
    }
  }
  assert.deepEqual(loaded.excluded(), []);
  const reply = await send(loaded,
    { type: "confirmExcludeSite", token, host: "bank.example.test" }, PAGE_SENDER);
  assert.equal(reply && reply.ok, true);
  assert.deepEqual(loaded.excluded(), ["news.example.test"]);
});

test("token security: a confirmed token is one-time and cannot be replayed", async () => {
  const loaded = loadBackground();
  const token = await createIntent(loaded);
  assert.equal((await send(loaded, { type: "confirmExcludeSite", token }, PAGE_SENDER)).ok,
               true);
  const replay = await send(loaded, { type: "confirmExcludeSite", token }, PAGE_SENDER);
  assert.notEqual(replay && replay.ok, true);
  assert.deepEqual(loaded.excluded(), ["news.example.test"]);
});

test("token security: an expired token is consumed and refused", async () => {
  const loaded = loadBackground();
  const token = await createIntent(loaded);
  loaded.setNow(1_700_000_120_000);
  const expired = await send(loaded, { type: "getExcludeConfirmation", token }, PAGE_SENDER);
  assert.equal(expired && expired.ok, false);
  const confirm = await send(loaded, { type: "confirmExcludeSite", token }, PAGE_SENDER);
  assert.notEqual(confirm && confirm.ok, true);
  assert.deepEqual(loaded.excluded(), []);
});

test("worker restart: an intent resolves from storage.session", async () => {
  const sessionData = {};
  const first = loadBackground({ sessionData });
  const token = await createIntent(first);
  const restarted = loadBackground({ sessionData });
  const reply = await send(restarted,
    { type: "getExcludeConfirmation", token }, PAGE_SENDER);
  assert.equal(reply && reply.ok, true);
  assert.equal(reply && reply.host, "news.example.test");
  assert.equal(reply && reply.status, "pending");
  assert.equal(reply && reply.expiresAt, 1_700_000_120_000);
});
test("cancel consumes the token without changing settings", async () => {
  const loaded = loadBackground();
  const token = await createIntent(loaded);
  assert.equal((await send(loaded,
    { type: "cancelExcludeConfirmation", token }, PAGE_SENDER)).ok, true);
  assert.equal((await send(loaded,
    { type: "confirmExcludeSite", token }, PAGE_SENDER)).ok, false);
  assert.deepEqual(loaded.excluded(), []);
});
test("unknown tokens and duplicate pending requests are bounded", async () => {
  const loaded = loadBackground();
  await createIntent(loaded);
  const duplicate = await send(loaded,
    { type: "requestExcludeConfirmation", expectHost: "news.example.test" },
    contentSender());
  assert.equal(duplicate && duplicate.ok, false);
  assert.equal(duplicate && duplicate.reason, "pending");
  assert.equal(loaded.tabsCreated.length, 1);
  const unknown = await send(loaded,
    { type: "confirmExcludeSite", token: "unknown" }, PAGE_SENDER);
  assert.equal(unknown && unknown.ok, false);
});

test("multiple confirmation tabs stay bound to their own trusted records", async () => {
  const loaded = loadBackground();
  const older = await createIntent(loaded,
    contentSender("https://older.example.test/watch", 42));
  const newer = await createIntent(loaded,
    contentSender("https://newer.example.test/watch", 42));

  const shown = await send(loaded, {
    type: "getExcludeConfirmation", token: older,
    host: "newer.example.test", sourceTabId: 999,
  }, PAGE_SENDER);
  assert.equal(shown.host, "older.example.test",
               "the token resolves only its background-owned record");
  assert.equal((await send(loaded,
    { type: "confirmExcludeSite", token: newer, host: "older.example.test" },
    PAGE_SENDER)).ok, true);
  assert.equal((await send(loaded,
    { type: "confirmExcludeSite", token: older, host: "newer.example.test" },
    PAGE_SENDER)).ok, true);
  assert.deepEqual(loaded.excluded(), ["newer.example.test", "older.example.test"]);
});

test("message-body tab ids cannot collide distinct authenticated source tabs", async () => {
  const loaded = loadBackground();
  for (const tabId of [42, 43]) {
    const reply = await send(loaded, {
      type: "requestExcludeConfirmation", expectHost: "news.example.test",
      sourceTabId: 777, host: "attacker.example.test",
    }, contentSender("https://news.example.test/watch", tabId));
    assert.equal(reply && reply.ok, true);
  }
  assert.equal(loaded.tabsCreated.length, 2,
               "the body cannot forge the per-source-tab duplicate key");
  const records = Object.values(loaded.sessionData._excludeConfirmations);
  assert.deepEqual(records.map((record) => record.sourceTabId), [42, 43]);
  assert.deepEqual(records.map((record) => record.host),
                   ["news.example.test", "news.example.test"]);
});

test("malformed and prototype-shaped tokens cannot read or consume a record", async () => {
  const loaded = loadBackground();
  const token = await createIntent(loaded);
  const hostile = ["", "__proto__", "constructor", "hasOwnProperty",
    "x".repeat(257), 0, {}, [], null, undefined];
  for (const candidate of hostile) {
    for (const type of ["getExcludeConfirmation", "confirmExcludeSite",
                        "cancelExcludeConfirmation"]) {
      const reply = await send(loaded, { type, token: candidate }, PAGE_SENDER);
      assert.notEqual(reply && reply.ok, true, `${type} accepted ${String(candidate)}`);
    }
  }
  const valid = await send(loaded, { type: "getExcludeConfirmation", token }, PAGE_SENDER);
  assert.equal(valid && valid.host, "news.example.test",
               "hostile lookups did not damage the real record");
  assert.deepEqual(loaded.excluded(), []);
});

test("overlapping confirms, replay, cancel, and request equal sequential effects", async () => {
  const loaded = loadBackground({ tokens: [
    "123e4567-e89b-42d3-a456-426614174000",
    "123e4567-e89b-42d3-a456-426614174001",
    "123e4567-e89b-42d3-a456-426614174002",
    "123e4567-e89b-42d3-a456-426614174003",
  ] });
  const first = await createIntent(loaded, contentSender("https://one.test/w", 1));
  const second = await createIntent(loaded, contentSender("https://two.test/w", 2));
  const cancelled = await createIntent(loaded, contentSender("https://three.test/w", 3));
  const replies = await Promise.all([
    send(loaded, { type: "confirmExcludeSite", token: first }, PAGE_SENDER),
    send(loaded, { type: "confirmExcludeSite", token: first }, PAGE_SENDER),
    send(loaded, { type: "confirmExcludeSite", token: second }, PAGE_SENDER),
    send(loaded, { type: "cancelExcludeConfirmation", token: cancelled }, PAGE_SENDER),
    send(loaded, { type: "requestExcludeConfirmation", expectHost: "four.test" },
         contentSender("https://four.test/w", 4)),
  ]);
  assert.equal(replies.slice(0, 2).filter((reply) => reply && reply.ok).length, 1,
               "exactly one racing use consumes a token");
  assert.equal(replies[2].ok, true);
  assert.equal(replies[3].ok, true);
  assert.equal(replies[4].ok, true);
  assert.deepEqual(loaded.excluded(), ["one.test", "two.test"]);
  assert.equal(Object.keys(loaded.sessionData._excludeConfirmations).length, 1,
               "only the independent request remains pending");
});

test("the exact TTL boundary expires the old record before replacement", async () => {
  const loaded = loadBackground();
  const oldToken = await createIntent(loaded);
  loaded.setNow(1_700_000_119_999);
  const stillPending = await send(loaded, {
    type: "requestExcludeConfirmation", expectHost: "news.example.test",
  }, contentSender());
  assert.equal(stillPending.reason, "pending");
  assert.equal((await send(loaded,
    { type: "getExcludeConfirmation", token: oldToken }, PAGE_SENDER)).ok, true);

  loaded.setNow(1_700_000_120_000);
  const replacement = await createIntent(loaded);
  assert.notEqual(replacement, oldToken);
  assert.equal((await send(loaded,
    { type: "confirmExcludeSite", token: oldToken }, PAGE_SENDER)).ok, false);
  assert.equal((await send(loaded,
    { type: "getExcludeConfirmation", token: replacement }, PAGE_SENDER)).ok, true);
});

test("a failed settings write leaves its token consumed", async () => {
  let failures = 1;
  const loaded = loadBackground({
    settingsWriteHook() {
      if (failures-- > 0) throw new Error("QuotaExceededError");
    },
  });
  const token = await createIntent(loaded);
  assert.equal((await send(loaded,
    { type: "confirmExcludeSite", token }, PAGE_SENDER)).ok, false);
  assert.equal((await send(loaded,
    { type: "confirmExcludeSite", token }, PAGE_SENDER)).ok, false);
  assert.deepEqual(loaded.excluded(), []);
  const fresh = await createIntent(loaded);
  assert.notEqual(fresh, token);
  assert.equal((await send(loaded,
    { type: "confirmExcludeSite", token: fresh }, PAGE_SENDER)).ok, true);
  assert.deepEqual(loaded.excluded(), ["news.example.test"]);
});

test("confirmed exclusions serialize overlapping settings writes", async () => {
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  let holdFirst = true;
  const loaded = loadBackground({ settingsWriteHook: async () => {
    if (!holdFirst) return;
    holdFirst = false;
    await held;
  } });
  const first = await createIntent(loaded, contentSender("https://one.test/w", 1));
  const second = await createIntent(loaded, contentSender("https://two.test/w", 2));
  const firstWrite = send(loaded, { type: "confirmExcludeSite", token: first }, PAGE_SENDER);
  await settle();
  const secondWrite = send(loaded, { type: "confirmExcludeSite", token: second }, PAGE_SENDER);
  await settle();
  release();
  assert.equal((await firstWrite).ok, true);
  assert.equal((await secondWrite).ok, true);
  assert.deepEqual(loaded.excluded(), ["one.test", "two.test"]);
});

test("confirmation page renders trusted host as text and sends token only", async () => {
  const nodes = Object.fromEntries(["question", "status", "cancel", "confirm"]
    .map((id) => [id, {
      textContent: "", disabled: id === "confirm", listeners: {},
      addEventListener(type, listener) { this.listeners[type] = listener; },
    }]));
  const sent = [];
  const context = vm.createContext({
    globalThis: undefined,
    location: { hash: "#opaque-token" },
    document: { getElementById: (id) => nodes[id] },
    browser: { runtime: { async sendMessage(message) {
      sent.push(message);
      if (message.type === "getExcludeConfirmation") {
        return { ok: true, host: "<img src=x onerror=alert(1)>" };
      }
      return { ok: true };
    } } },
  });
  context.globalThis = context;
  const source = fs.readFileSync("extension/confirm-exclude.js", "utf8");
  assert.doesNotMatch(source, /innerHTML/);
  vm.runInContext(source, context, { filename: "extension/confirm-exclude.js" });
  await settle();
  assert.equal(nodes.question.textContent,
    "Exclude <img src=x onerror=alert(1)> from Cove?");
  await nodes.confirm.listeners.click();
  assert.equal(JSON.stringify(sent.at(-1)),
               JSON.stringify({ type: "confirmExcludeSite", token: "opaque-token" }));
});
