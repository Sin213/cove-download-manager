const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");

const EXTENSION_ORIGIN = "chrome-extension://covetestextensionid/";

function event() {
  const listeners = [];
  return {
    // Exposed so a test can count registrations. The real APIs do not offer
    // this, which is why it is read only from tests and never by the extension.
    listeners,
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
                         rejectSettingsRead = false,
                         breakStorage = false, slowStorage = false,
                         storedDiag = null, media = true, cookies = [],
                         tabs = [], tabsById = new Map(), timers = false,
                         downloadSearch = () => [], downloadState = null, searchHook = null,
                         storedIntercepted = null, eraseThrows = false, eraseFailures = 0,
                         slowInterceptedIds = false, hydrationGate = null,
                         cookieHook = null, nativeHook = null, settingsReadHook = null,
                         settingsWriteHook = null, permissionsHook = null,
                         worker = false, missingScripts = [],
                         installedMenus = new Map(), menuApi = "lenient",
                         // Firefox's built-in data-collection consent. `undefined`
                         // is the Chrome/legacy shape - getAll() answers with no
                         // `data_collection` key at all - and is the default
                         // because that is what the browser the rest of this
                         // suite models actually returns. An array models a
                         // Firefox that has the consent experience, empty for
                         // "the user has not granted it".
                         dataConsent,
                         permissionsApi = "present" } = {}) {
  let confirmationTokenSequence = 0;
  const calls = { native: [], cancel: [], erase: [], menus: [], menuOps: [], imported: [],
                  notifications: [], cookies: [], settingsWrites: [], tabsCreated: [],
                  console: [], order: [], tabsGet: [], searches: [], eraseAttempts: [], settingsReads: 0,
                  // One entry per permissions.getAll(). Counted so a test can
                  // prove the consent state is read fresh for each handoff
                  // rather than cached from startup.
                  permissionChecks: [] };
  const events = {
    downloadCreated: event(),
    downloadChanged: event(),
    contextMenuClicked: event(),
    downloadErased: event(),
    message: event(),
    headersReceived: event(), tabUpdated: event(), tabRemoved: event(), tabActivated: event(),
    // The keyboard shortcut is a settings writer, so a test has to be able to
    // fire it rather than infer it.
    command: event(),
    // A real recorded event, not a silent stub: the pill's whole lifecycle
    // hangs off this notification, so a test has to be able to fire it.
    storageChanged: event(),
  };
  events.calls = calls;
  const browserDownloads = [];
  const records = downloadState === null ? null : new Map(downloadState.map((r) => [r.id, r]));
  function replaceDownloadRecord(id, record) {
    if (!records) throw new Error("downloadState must be enabled");
    if (record === undefined) records.delete(id);
    else records.set(id, record);
  }
  const pendingTimers = new Map();
  let timerId = 0;
  function fireTimers(ms) {
    for (const [id, timer] of [...pendingTimers]) {
      if (timer.ms !== ms) continue;
      pendingTimers.delete(id);
      timer.callback(...timer.args);
    }
  }
  const quietEvent = () => event();
  // A real key/value store, so the diagnostics ring can be inspected the way
  // the popup would read it back.
  // The stored settings are mutable so a test can model the options page
  // saving new ones while a page is open. Reading `settings` directly would
  // hand every read the original fixture forever, which is the one thing a
  // live settings change must not do.
  let storedSettings = settings;
  const store = {
    data: {},
    async get(key) {
      if (key === "settings") {
        calls.settingsReads += 1;
        // A real storage read can fail. background.js swallows that failure
        // and resolves with defaults, so a test cannot infer the failure from
        // the resolved value and has to produce it here instead.
        if (rejectSettingsRead) throw new Error("storage unavailable");
        // The value is snapshotted BEFORE the hook runs, so a hook that commits
        // new settings models the one ordering that matters: an answer computed
        // from settings that were already superseded by the time it arrived.
        const snapshot = storedSettings;
        if (settingsReadHook) await settingsReadHook();
        return snapshot ? { settings: snapshot } : {};
      }
      if (hydrationGate && key === "_interceptedIds") await hydrationGate.promise;
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
      // A settings write has to be visible to the next get("settings") and has
      // to notify, which is what real storage does. Without that, code that
      // merges onto a freshly read snapshot would re-read the original fixture
      // forever and every lost-update case would pass for the wrong reason.
      if ("settings" in obj) {
        if (settingsWriteHook) await settingsWriteHook(obj.settings);
        const oldValue = storedSettings;
        storedSettings = obj.settings;
        calls.settingsWrites.push(obj.settings);
        Object.assign(store.data, obj);
        events.storageChanged.emit(
          { settings: { newValue: obj.settings, oldValue } }, "local",
        );
        return;
      }
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
    commands: { onCommand: events.command },
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
    // The query is recorded, not just the jar returned: a refused target must
    // not be looked up at all, and only the recorded query can show that.
    // cookieHook runs while this read is still pending, which is the only place
    // a test can land a settings change inside the handoff's own await.
    cookies: {
      async getAll(query) {
        calls.cookies.push(query);
        if (cookieHook) await cookieHook(query);
        return cookies;
      },
    },
    downloads: {
      onCreated: events.downloadCreated,
      onChanged: events.downloadChanged,
      onErased: events.downloadErased,
      async cancel(id) {
        calls.order.push("cancel"); calls.cancel.push(id);
        if (records && records.has(id)) {
          const record = records.get(id);
          // Preserve privacy descriptors without reading getters or mutating fixtures.
          records.set(id, Object.create(Object.getPrototypeOf(record), {
            ...Object.getOwnPropertyDescriptors(record),
            state: { value: "interrupted", enumerable: true, writable: true, configurable: true },
          }));
        }
      },
      async erase(query) {
        calls.eraseAttempts.push(query);
        if (eraseThrows || eraseFailures > 0) {
          if (eraseFailures > 0) eraseFailures -= 1;
          throw new Error("erase failed");
        }
        calls.erase.push(query);
        calls.order.push("erase");
        if (records) records.delete(query.id);
      },
      async search(query) {
        calls.searches.push(query);
        if (searchHook) await searchHook(query);
        return records ? (records.has(query.id) ? [records.get(query.id)] : []) : downloadSearch(query);
      },
      async download(options) { browserDownloads.push(options); },
    },
    notifications: { async create(options) {
      calls.order.push("notification"); calls.notifications.push(options);
    } },
    runtime: {
      lastError: null,
      getManifest: () => ({ version: "1.4.4" }),
      // Extension pages are identified by their own origin, so the harness has
      // to have one. A content script's sender.url is the page's address and
      // never starts with this.
      getURL: (path) => EXTENSION_ORIGIN + path,
      onInstalled: quietEvent(),
      onMessage: events.message,
      async sendNativeMessage(_host, message) {
        calls.native.push(message);
        calls.order.push("native:sent");
        if (nativeHook) await nativeHook(message);
        const result = await (typeof nativeResult === "function" ? nativeResult(message) : nativeResult);
        calls.order.push(`native:${result && result.status}`);
        return result;
      },
    },
    storage: {
      local: store,
      session: store,
      onChanged: events.storageChanged,
    },
    tabs: {
      async create(details) { calls.tabsCreated.push(details); return { id: 99, ...details }; },
      async get(id) {
        calls.tabsGet.push(id);
        if (!tabsById.has(id)) throw new Error("tab unavailable");
        const result = tabsById.get(id);
        if (result instanceof Error) throw result;
        return result;
      },
      async query() { return tabs; },
      async sendMessage() {},
      onRemoved: events.tabRemoved,
      onUpdated: events.tabUpdated,
      onActivated: events.tabActivated,
    },
    webRequest: { onHeadersReceived: events.headersReceived },
    windows: { async getCurrent() { return { incognito: false }; } },
    extension: { inIncognitoContext: false },
  };
  // Both browsers expose permissions.getAll(). Firefox's built-in consent adds
  // a `data_collection` array to the answer, and Mozilla documents the
  // presence or absence of that key as the way to feature-detect the consent
  // experience at runtime - so the absent key has to be representable here.
  let consentState = dataConsent;
  if (permissionsApi !== "missing") {
    browser.permissions = {
      async getAll() {
        // Park the real userAgentField await before computing fresh consent.
        // This hook is test-only and does not change the permission answer.
        if (permissionsHook) await permissionsHook();
        calls.permissionChecks.push(
          consentState === undefined ? "no-data-collection-key" : [...consentState].join(","));
        if (permissionsApi === "throws") throw new Error("permissions unavailable");
        const answer = { origins: ["<all_urls>"], permissions: ["downloads"] };
        if (permissionsApi === "garbage") {
          // Present but not an array: a shape the gate cannot read as consent.
          answer.data_collection = "technicalAndInteraction";
          return answer;
        }
        if (consentState !== undefined) answer.data_collection = [...consentState];
        return answer;
      },
    };
  }
  // The user can grant or revoke optional consent at any time from
  // about:addons, so a test has to be able to change it mid-session.
  events.setDataConsent = (next) => { consentState = next; };
  const context = vm.createContext({
    browser,
    console: Object.fromEntries(["log", "error", "warn", "info", "debug"].map(
      (level) => [level, (...args) => calls.console.push({ level, args })],
    )),
    navigator: {
      userAgent: "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
    },
    URL,
    crypto: {
      randomUUID: () => "123e4567-e89b-42d3-a456-" +
        String(confirmationTokenSequence++).padStart(12, "0"),
    },
    setTimeout: timers ? (callback, ms, ...args) => {
      const id = ++timerId;
      pendingTimers.set(id, { callback, ms, args });
      return id;
    } : setTimeout,
    clearTimeout: timers ? (id) => pendingTimers.delete(id) : clearTimeout,
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
  // Models the options page saving: the stored value changes and only then
  // does the change notification fire, which is the order storage produces and
  // the order the stale-decision cases depend on.
  function setSettings(next) {
    const oldValue = storedSettings;
    storedSettings = next;
    events.storageChanged.emit(
      { settings: { newValue: next, oldValue } }, "local",
    );
  }
  // A settings write this context is never told about. Real storage does not
  // guarantee a change notification reaches the context that has to act next -
  // it is why the pill's permission reads storage rather than the cached copy -
  // so a cache that happens to be fresh must never be what a merge relies on.
  function writeSettingsSilently(next) {
    storedSettings = next;
    store.data.settings = next;
  }
  return { calls, events, browserDownloads, store, context, badge, installedMenus,
           fireTimers, replaceDownloadRecord, setSettings, writeSettingsSilently,
           readSettings: () => storedSettings };
}

async function settle() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function popupSender() {
  return { url: EXTENSION_ORIGIN + "popup/popup.html" };
}

function seedStream(loaded, tabId, url) {
  loaded.events.headersReceived.emit({
    tabId, url, responseHeaders: [{ name: "content-type", value: "application/vnd.apple.mpegurl" }],
  });
}

const POPUP_STREAM_URL = "https://example.test/live.m3u8";
const NORMAL_STREAM_TAB = Object.freeze({ id: 7, url: "https://example.test/watch", incognito: false });
const STREAM_DECLINE = "Cove did not take this stream: its window could not be confirmed as a normal window.";

// Explicit normal fixture; loadBackground's raw-returning mocks remain unchanged.
function popupStreamFixture(options = {}, url = POPUP_STREAM_URL) {
  const loaded = loadBackground({ tabsById: new Map([[7, NORMAL_STREAM_TAB]]), ...options });
  seedStream(loaded, 7, url);
  return loaded;
}

function popupStreamClick(loaded, sender = popupSender(), url = POPUP_STREAM_URL) {
  return sendRuntimeMessage(loaded.events, { type: "downloadStream", url, filename: "live.mp4" }, sender);
}

async function popupStreamControl(deferredLookup = false) {
  let nativeDownloads = 0;
  const gate = deferred();
  const loaded = popupStreamFixture({ timers: true, tabs: [NORMAL_STREAM_TAB],
    tabsById: new Map([[7, deferredLookup ? gate.promise : NORMAL_STREAM_TAB]]), nativeHook: (msg) => {
    if (msg.action === "download") nativeDownloads += 1;
  } });
  await settle();
  loaded.calls.native.length = 0;
  const result = popupStreamClick(loaded);
  await settle();
  gate.resolve(NORMAL_STREAM_TAB);
  if (deferredLookup) await settle();
  assert.equal(result.returned, true, "S11 PC: accepted popup keeps response channel open");
  assert.equal(result.responses.length, 1, "S11 PC: accepted popup answers once");
  assert.equal(result.responses[0].ok, true, "S11 PC: normal detected stream accepted");
  assert.equal(nativeDownloads, 1, "S11 PC: native hook observes one handoff");
  assert.equal(downloadsOf(loaded.calls).length, 1, "S11 PC: exactly one native download");
  assert.equal(loaded.calls.permissionChecks.length, 1, "S11 PC: normal send checks user-agent permission once");
  assert.equal(Object.hasOwn(downloadsOf(loaded.calls)[0], "cookies"), false);
  assert.equal(typeof downloadsOf(loaded.calls)[0].userAgent, "string");
  return loaded;
}

function popupStreamDeclined(loaded, result, id, url = POPUP_STREAM_URL) {
  assert.equal(downloadsOf(loaded.calls).length, 0, `${id}: unverified stream never reaches native host`);
  assert.equal(result.responses.length, 1, `${id}: decline answers exactly once`);
  assert.deepEqual(plain(result.responses[0]), { ok: false, error: STREAM_DECLINE }, `${id}: fixed decline sentence`);
  assert.equal(JSON.stringify(result.responses).includes(url), false, `${id}: decline contains no stream URL`);
}

test("S11 GUARD Firefox: normal popup, seeded stream and normal holder preserve native payload", async () => {
  await popupStreamControl();
});

for (const suffix of ["X", ".evil", "/", "../popup.htm", "origin-lookalike", "?x=1", "#x"]) {
  test(`S1c Firefox: popup URL boundary ${suffix}`, async () => {
    const loaded = popupStreamFixture({ timers: true });
    await settle();
    const accepted = suffix === "?x=1" || suffix === "#x";
    const url = suffix === "origin-lookalike" ? "chrome-extension://covetestextensionid.evil/popup/popup.html"
      : EXTENSION_ORIGIN + (suffix === "../popup.htm" ? "popup/popup.htm" : "popup/popup.html" + suffix);
    const result = popupStreamClick(loaded, { url });
    await settle();
    if (!accepted) popupStreamDeclined(loaded, result, "S1c");
    else { assert.equal(result.responses.length, 1); assert.equal(result.responses[0].ok, true);
      assert.equal(downloadsOf(loaded.calls).length, 1, "S1c: query/fragment preserves exact popup authority"); }
  });
}
test("S9b Firefox: timeout during user-agent await declines once and prevents late send", async () => {
  await popupStreamControl();
  const gate = deferred(); let parked = false;
  const loaded = popupStreamFixture({ timers: true, permissionsHook: async () => { parked = true; await gate.promise; } });
  await settle();
  const result = popupStreamClick(loaded);
  await settle();
  assert.equal(parked, true, "S9b: normal holder verification reached user-agent await");
  assert.deepEqual(loaded.calls.tabsGet, [7]);
  loaded.fireTimers(3000);
  popupStreamDeclined(loaded, result, "S9b");
  gate.resolve(); await settle();
  popupStreamDeclined(loaded, result, "S9b");
});

for (const [id, label, sender] of [
  ["S1", "content-script", { tab: NORMAL_STREAM_TAB, url: NORMAL_STREAM_TAB.url, frameId: 0 }],
  ["S1", "missing-url", {}], ["S1", "tabless-page", { url: "https://example.test/page" }],
  ["S1b", "options", { url: EXTENSION_ORIGIN + "options/options.html" }],
  ["S1b", "confirmation", { url: EXTENSION_ORIGIN + "confirm-exclude.html" }],
]) {
  test(`${id} RED Firefox ${label}: only the exact popup page may request a stream`, async () => {
    await popupStreamControl();
    const loaded = popupStreamFixture({ timers: true });
    await settle();
    loaded.calls.native.length = 0;
    const result = popupStreamClick(loaded, sender);
    await settle();
    popupStreamDeclined(loaded, result, id);
    assert.deepEqual(loaded.calls.tabsGet, [], `${id}: invalid sender causes no tab lookup`);
  });
}

for (const [id, label, holder] of [
  ["S4", "private", Object.freeze({ id: 7, incognito: true })],
  ["S5", "rejected", new Error("tab closed")],
  ...[["missing", undefined], ["null", { value: null }], ["string-false", { value: "false" }],
    ["throwing", { get() { throw new Error("privacy getter"); } }]]
    .map(([label, descriptor]) => ["S7", label, () => e2Tab(descriptor)]),
]) {
  test(`${id} RED Firefox ${label}: holder must positively report normal privacy`, async () => {
    await popupStreamControl();
    const loaded = popupStreamFixture({ timers: true,
      tabsById: new Map([[7, typeof holder === "function" ? holder() : holder]]) });
    await settle();
    loaded.calls.native.length = 0;
    const result = popupStreamClick(loaded);
    await settle();
    popupStreamDeclined(loaded, result, id);
  });
}

for (const id of ["S2", "S3", "S6", "S14"]) {
  test(`${id} RED Firefox: stream membership and every actual holder are required`, async () => {
    await popupStreamControl();
    const activeTab = Object.freeze({ id: 9, incognito: false, url: "https://normal-active.test/" });
    const tabMap = new Map([[7, id === "S14" ? { id: 7, incognito: true } : NORMAL_STREAM_TAB],
      [8, { id: 8, incognito: true }], [9, activeTab]]);
    const loaded = id === "S3" ? loadBackground({ timers: true, tabsById: tabMap })
      : popupStreamFixture({ timers: true, tabsById: tabMap, tabs: [activeTab] });
    if (id === "S6") seedStream(loaded, 8, POPUP_STREAM_URL);
    await settle();
    loaded.calls.native.length = 0;
    const url = POPUP_STREAM_URL + (id === "S2" ? "?not-detected=1" : "");
    const result = popupStreamClick(loaded, popupSender(), url);
    await settle();
    popupStreamDeclined(loaded, result, id, url);
  });
}

for (const id of ["G3/S4", "S5b", "S8", "S9", "S12"]) {
  test(`${id} RED Firefox: pending holder verification cannot authorize stale or late sends`, async () => {
    await popupStreamControl(true);
    const gate = deferred();
    gate.promise.catch(() => {}); // Baseline never consumes tabs.get; a synthetic rejection must still be handled.
    const loaded = popupStreamFixture({ timers: true, tabsById: new Map([
      [7, gate.promise], [8, { id: 8, incognito: true }],
    ]) });
    await settle();
    loaded.calls.native.length = 0;
    const result = popupStreamClick(loaded);
    await settle();
    if (id === "S8") loaded.events.tabUpdated.emit(7, { url: "https://elsewhere.test/" });
    if (id === "S12") seedStream(loaded, 8, POPUP_STREAM_URL);
    if (id === "S9") loaded.fireTimers(3000);
    await settle();
    const beforeLateResult = downloadsOf(loaded.calls).length;
    if (id === "S5b") gate.reject(new Error("closed during verification"));
    else gate.resolve(id === "G3/S4" ? Object.freeze({ id: 7, incognito: true }) : NORMAL_STREAM_TAB);
    await settle();
    if (id === "S9") assert.equal(beforeLateResult, 0, "S9: timeout never authorizes native send");
    popupStreamDeclined(loaded, result, id);
    if (id === "S8" || id === "S12") assert.equal(loaded.calls.permissionChecks.length, 0,
      `${id}: changed holder set declines before consulting user-agent permission`);
  });
}

test("S13 RED Firefox: holder membership is rechecked after the final user-agent await", async () => {
  await popupStreamControl();
  let loaded;
  loaded = popupStreamFixture({ timers: true, permissionsHook: async () => {
    if (loaded) loaded.events.tabUpdated.emit(7, { url: "https://elsewhere.test/" });
  } });
  await settle();
  loaded.calls.native.length = 0;
  const result = popupStreamClick(loaded);
  await settle();
  popupStreamDeclined(loaded, result, "S13");
});

test("S10 GUARD Firefox: two normal popup clicks each send and answer once", async () => {
  const loaded = await popupStreamControl();
  const second = popupStreamClick(loaded);
  await settle();
  assert.equal(downloadsOf(loaded.calls).length, 2);
  assert.deepEqual(plain(second.responses), [{ ok: true }]);
});

test("S10 RED Firefox: a second click reclassifies a holder that became private", async () => {
  const tabsById = new Map([[7, NORMAL_STREAM_TAB]]);
  const loaded = popupStreamFixture({ timers: true, tabsById });
  await settle();
  loaded.calls.native.length = 0;
  const first = popupStreamClick(loaded);
  await settle();
  assert.deepEqual(plain(first.responses), [{ ok: true }], "S10 PC: first normal click succeeds");
  assert.equal(downloadsOf(loaded.calls).length, 1, "S10 PC: normal click sends once");
  tabsById.set(7, Object.freeze({ id: 7, incognito: true }));
  const second = popupStreamClick(loaded);
  await settle();
  assert.equal(downloadsOf(loaded.calls).length, 1, "S10: later private holder cannot reuse prior approval");
  assert.equal(loaded.calls.tabsGet.length, 2, "S10: each click verifies independently");
  assert.deepEqual(plain(second.responses), [{ ok: false, error: STREAM_DECLINE }]);
});

test("S10 RED Firefox: concurrent clicks both decline a private holder", async () => {
  await popupStreamControl();
  const loaded = popupStreamFixture({ timers: true, tabsById: new Map([[7, { id: 7, incognito: true }]]) });
  await settle();
  loaded.calls.native.length = 0;
  const clicks = [popupStreamClick(loaded), popupStreamClick(loaded)];
  await settle();
  for (const result of clicks) popupStreamDeclined(loaded, result, "S10");
});

// Only the privacy field varies; no emit-time defaults or coercion.
function unknownItem(descriptor, overrides = {}) {
  const item = freshItem(overrides);
  delete item.incognito;
  if (descriptor) Object.defineProperty(item, "incognito", {
    enumerable: true, configurable: true, ...descriptor,
  });
  return item;
}

const E2_DOC_ROWS = [
  ["E2-P1","PRIVACY.md","  when you do not. On Chrome the user-agent is always included.\n","  when you do not. On Chrome the user-agent is always included.\n- A download that the browser reports as private or incognito is left to the\n  browser and is not handed to Cove; see Private and incognito windows below.\n"],
  ["E2-P2","PRIVACY.md","This version of the extension does not read or store\nbrowser cookie values.\n\n## What is passed to the Cove desktop app","This version of the extension does not read or store\nbrowser cookie values.\n\n### Private and incognito windows\n\nThe extension does not hand a download to Cove when the browser reports it as\nprivate (Firefox) or incognito (Chrome). It relies on the browser's own report\nof whether a download is private. If the browser reports that a download is\nprivate, or does not say, the extension leaves the download to the browser: it\ndoes not send its address to Cove, does not cancel it, does not erase it from\nthe browser's list and does not start a copy of it. The in-page button is not\noffered in tabs the browser reports as private, and the right-click entry and\nthe Firefox popup's stream button send nothing from them.\n\nWhether the extension runs in private windows at all is your browser's setting\n(Firefox: Run in Private Windows; Chrome: Allow in Incognito). Extension builds\nfrom before this change do not apply this rule and can still hand over private\ndownloads that the browser shows them, and this change does not remove anything\nsuch a build already handed to Cove. This covers what the extension hands to\nCove. It does not hide private browsing from your browser, from websites or from\nother extensions, and a download you add in the Cove app yourself is not\naffected.\n\n## What is passed to the Cove desktop app"],
  ["E2-P3","PRIVACY.md","accepted, and fall back to a browser download when Cove is unavailable.","accepted, and fall back to a browser download when Cove is unavailable.\n  Downloads the browser reports as private are not handed to Cove."],
  ["E2-R1","README.md","no longer restores them into download tasks.\n\n### Settings","no longer restores them into download tasks.\n\nDownloads that your browser reports as private (Firefox) or incognito (Chrome) are\nnot taken over by the extension: your browser keeps them, and the in-page button\nis not offered in those tabs.\n\n### Settings\n"],
  ["E2-C1","docs/chrome-store-listing.md","may not download in Cove\n\n","may not download in Cove\n\n• Downloads that the browser reports as incognito are left to the browser; Cove does not take them over\n\n"],
  ["E2-C2","docs/chrome-store-listing.md","| Referrer and user-agent are passed on; cookies are not read or sent | `extension/background.js` native payload; `cove/single_instance.py` | `tests/extension_background.test.js` |\n","| Referrer and user-agent are passed on; cookies are not read or sent | `extension/background.js` native payload; `cove/single_instance.py` | `tests/extension_background.test.js` |\n| Downloads the browser reports as incognito are left to the browser | `extension/background.js` `handleCreated`, `pillIdentityAllowed`, context-menu handler | `tests/extension_background.test.js`; manual proof pending |\n"],
  ["E2-F1","docs/firefox-store-listing.md","browser cookies are not sent\n\n","browser cookies are not sent\n\n• Downloads from private windows that Firefox reports as private are left to the browser; Cove does not take them over\n\n"],
  ["E2-F2","docs/firefox-store-listing.md","`extension/background.js` native payload (`url`, `referrer`, `filename`, `userAgent`, `fileSize`); asserted by `test_firefox_declares_the_data_it_sends_to_the_native_app` |\n","`extension/background.js` native payload (`url`, `referrer`, `filename`, `userAgent`, `fileSize`); asserted by `test_firefox_declares_the_data_it_sends_to_the_native_app` |\n| downloads Firefox reports as private are left to the browser | `extension/background.js` `handleCreated`, `pillIdentityAllowed`, context-menu handler; `extension/media-sites.js` `downloadStream`; `tests/extension_background.test.js`; manual proof pending |\n"],
];

const PRIVATE_URL_SENTINEL = "https://private.s08e.test/synth-9c41/file.zip";
const PRIVATE_TAB_TITLE = "S08E2 private tab title 9c41";
const E2_CONTEXTS = [
  ["private", { value: true }], ["missing", undefined], ["undefined", { value: undefined }],
  ...[["null", null], ["zero", 0], ["one", 1], ["string-false", "false"], ["string-true", "true"],
    ["object", {}], ["array", []], ["NaN", NaN]].map(([label, value]) => [label, { value }]),
  ["throwing", { get() { throw new Error("synthetic privacy getter"); } }],
];

async function e2Created(worker, item, direct = false) {
  const loaded = loadBackground({ worker, timers: true, downloadState: [item] });
  await settle();
  loaded.calls.native.length = 0;
  loaded.calls.order.length = 0;
  loaded.context.e2Item = item;
  evalIn(loaded.context, `globalThis.e2EnsureCalls = 0;
    const e2OriginalEnsure = ensureSettings;
    ensureSettings = () => { e2EnsureCalls += 1; return e2OriginalEnsure(); };`);
  if (direct) await evalIn(loaded.context, "interceptDownload(e2Item)");
  else loaded.events.downloadCreated.emit(item);
  await settle();
  return loaded;
}

function e2NormalCreated(loaded, id) {
  assert.equal(downloadsOf(loaded.calls).length, 1, `${id} PC: normal download reaches Cove`);
  assert.deepEqual(loaded.calls.order.slice(0, 4), ["native:sent", "native:ok", "cancel", "erase"],
    `${id} PC: acknowledgement precedes cancel and erase`);
}

for (const worker of [false, true]) {
  const mode = worker ? "Chrome" : "Firefox";
  for (const [id, oracle] of [
    ["E1", (l) => assert.equal(downloadsOf(l.calls).length, 0, "E1: no private native handoff")],
    ["E2-cancel", (l) => assert.equal(l.calls.cancel.length, 0, "E2: no private cancellation")],
    ["E2-erase", (l) => assert.equal(l.calls.erase.length, 0, "E2: no private erasure")],
    ["E2-state", async (l) => assert.equal((await l.context.browser.downloads.search({ id: 90 }))[0]?.state,
      "in_progress", "E2: browser copy remains running")],
    ["E3/N6", (l) => assert.equal(JSON.stringify(l.calls.console).includes(PRIVATE_URL_SENTINEL), false,
      "E3: private URL absent from console")],
    ["G1", (l) => assert.deepEqual(l.calls.order, [], "G1: private event has no handoff side effects")],
    ["G4", (l) => assert.equal(evalIn(l.context, "interceptedIds.size"), 0, "G4: private id is not tracked")],
    ["G6", (l) => assert.equal((l.store.data._interceptedIds || []).includes(90), false,
      "G6: private id is not persisted")],
    ["E1-mark", (l) => assert.equal(evalIn(l.context,
      `wasRecentlyIntercepted(${JSON.stringify(PRIVATE_URL_SENTINEL)})`), false,
    "E1: private URL is not marked intercepted")],
  ]) {
    test(`${id} RED ${mode}: private created item is left to the browser`, async () => {
      const declined = await e2Created(worker, unknownItem({ value: true }, { url: PRIVATE_URL_SENTINEL }));
      await oracle(declined);
      assert.deepEqual(declined.browserDownloads, [], `${id}: no browser fallback`);
      declined.fireTimers(1000);
      await settle();
      assert.equal(JSON.stringify(declined.store.data.coveDiag || {}).includes(PRIVATE_URL_SENTINEL), false,
        `${id}: private URL absent from stored diagnostics`);
      e2NormalCreated(await e2Created(worker, freshItem({ url: PRIVATE_URL_SENTINEL })), id);
    });
  }
  for (const [label, descriptor] of E2_CONTEXTS) {
    for (const id of ["F1", "G2", "G5"]) {
      test(`${id} RED ${mode} ${label}: only explicit normal privacy proceeds`, async () => {
        const direct = id === "G5";
        const item = Object.freeze(unknownItem(descriptor));
        assert.equal(Object.hasOwn(item, "incognito"), label !== "missing", "raw missing key stays absent");
        const declined = await e2Created(worker, item, direct);
        if (id === "G2") assert.equal(declined.context.e2EnsureCalls, 0, "G2: classification precedes ensureSettings");
        else assert.equal(downloadsOf(declined.calls).length, 0, `${id}: non-normal item never reaches native host`);
        assert.deepEqual(declined.calls.cancel, [], `${id}: no cancellation`);
        assert.deepEqual(declined.calls.erase, [], `${id}: no erasure`);
        assert.equal(evalIn(declined.context, "interceptedIds.size"), 0, `${id}: no tracking`);
        const normal = await e2Created(worker, freshItem(), direct);
        e2NormalCreated(normal, id);
        if (id === "G2") assert.equal(normal.context.e2EnsureCalls, 1, "G2 PC: normal calls ensureSettings once");
      });
    }
  }
  test(`H5 GUARD ${mode}: rejected and malformed acknowledgements leave normal download running`, async () => {
    e2NormalCreated(await e2Created(worker, freshItem()), "H5");
    for (const nativeResult of [null, {}, { status: "error" }, () => Promise.reject(new Error("timeout"))]) {
      const item = freshItem();
      const loaded = loadBackground({ worker, timers: true, nativeResult, downloadState: [item] });
      await settle();
      loaded.events.downloadCreated.emit(item);
      await settle();
      assert.deepEqual(loaded.calls.cancel, []);
      assert.deepEqual(loaded.calls.erase, []);
      assert.equal((await loaded.context.browser.downloads.search({ id: item.id }))[0].state, "in_progress");
    }
  });
}

async function e2Menu(worker, tab, fail = false, url = PRIVATE_URL_SENTINEL) {
  const loaded = loadBackground({ worker, timers: true, nativeResult: fail ? null : { status: "ok" } });
  await settle();
  loaded.calls.native.length = 0;
  await Promise.all(loaded.events.contextMenuClicked.emit(
    { menuItemId: "download-with-cove", linkUrl: url }, tab));
  await settle();
  return loaded;
}

function e2Tab(descriptor) {
  const item = unknownItem(descriptor, { id: 7, url: "https://example.test/watch", title: PRIVATE_TAB_TITLE });
  return Object.freeze(item);
}

for (const worker of [false, true]) {
  const mode = worker ? "Chrome" : "Firefox";
  const normalTab = { id: 7, url: "https://example.test/watch", incognito: false, title: "Normal control" };
  for (const [label, descriptor] of [...E2_CONTEXTS, ["no-tab", undefined]]) {
    test(`F2/K1/L1 RED ${mode} ${label}: context menu declines without handoff`, async () => {
      const normal = await e2Menu(worker, normalTab);
      assert.equal(downloadsOf(normal.calls).length, 1, "F2 PC: normal menu sends");
      const declined = await e2Menu(worker, label === "no-tab" ? undefined : e2Tab(descriptor));
      assert.equal(downloadsOf(declined.calls).length, 0, "F2: non-normal menu never sends");
      assert.deepEqual(declined.browserDownloads, []);
      assert.equal(declined.calls.notifications.length, 1, "F2: one decline notification");
      const notice = JSON.stringify(declined.calls.notifications[0]);
      assert.equal(notice.includes(PRIVATE_URL_SENTINEL) || notice.includes(PRIVATE_TAB_TITLE), false,
        "F2: decline notification contains neither URL nor tab title");
    });
  }
  test(`E3m/N6 RED ${mode}: declined menu never logs a private URL`, async () => {
    const normal = await e2Menu(worker, normalTab);
    assert.equal(JSON.stringify(normal.calls.console).includes(PRIVATE_URL_SENTINEL), true, "E3m PC: normal logs target");
    const declined = await e2Menu(worker, e2Tab({ value: true }));
    assert.equal(JSON.stringify(declined.calls.console).includes(PRIVATE_URL_SENTINEL), false,
      "E3m: declined menu URL absent from console");
  });
  test(`H3 GUARD ${mode}: normal menu retains browser fallback`, async () => {
    assert.equal((await e2Menu(worker, normalTab, true)).browserDownloads.length, 1);
  });
  for (const [label, descriptor] of E2_CONTEXTS) {
    test(`H4 RED ${mode} ${label}: declined menu cannot fall back into browser history`, async () => {
      assert.equal((await e2Menu(worker, normalTab, true)).browserDownloads.length, 1, "H4 PC: normal falls back");
      const declined = await e2Menu(worker, e2Tab(descriptor), true);
      assert.equal(declined.browserDownloads.length, 0, "H4: non-normal menu never starts browser copy");
      assert.equal(evalIn(declined.context, `wasRecentlyIntercepted(${JSON.stringify(PRIVATE_URL_SENTINEL)})`), false);
    });
  }
  test(`F2-notice RED ${mode}: decline notification is constant and non-sensitive`, async () => {
    assert.equal(downloadsOf((await e2Menu(worker, normalTab)).calls).length, 1, "F2 PC: normal menu sends");
    const first = await e2Menu(worker, e2Tab({ value: true }));
    const second = await e2Menu(worker, e2Tab({ value: true }), false, "https://private.s08e.test/other-secret.zip");
    assert.equal(first.calls.notifications.length, 1);
    assert.equal(JSON.stringify(first.calls.notifications), JSON.stringify(second.calls.notifications),
      "F2: decline notification is constant");
    assert.equal(JSON.stringify(first.calls.notifications).includes(PRIVATE_URL_SENTINEL), false);
    assert.equal(JSON.stringify(first.calls.notifications).includes(PRIVATE_TAB_TITLE), false);
  });
  for (const [label, descriptor] of E2_CONTEXTS) {
    for (const route of ["permission", "download", "direct", "exclude"]) {
      const id = { permission: "PM1/F3b", download: "PM2", direct: "G5p", exclude: "PM5" }[route];
      test(`${id} RED ${mode} ${label}: non-normal pill route declines`, async () => {
        async function run(tab) {
          const loaded = loadBackground({ worker, timers: true });
          await settle();
          loaded.calls.native.length = 0;
          const sender = Object.freeze({ tab, url: tab.url, frameId: 0 });
          let reply;
          if (route === "permission") reply = await pillPermission(loaded.events, tab.url, sender);
          else if (route === "exclude") ({ reply } = await requestExclude(loaded.events, sender, "example.test"));
          else if (route === "download") reply = await pillDownload(loaded.events, { url: PRIVATE_URL_SENTINEL }, sender);
          else {
            loaded.context.e2Sender = sender;
            loaded.context.e2Message = { type: "downloadMedia", url: PRIVATE_URL_SENTINEL };
            loaded.context.e2Reply = (value) => { reply = value; };
            evalIn(loaded.context, "CoveMedia.handleMessage(e2Message, e2Sender, e2Reply)");
            await settle();
          }
          return { ...loaded, reply };
        }
        const normal = await run(normalTab);
        assert.equal(route === "permission" ? normal.reply.pillAllowed : normal.reply.ok, true, `${id} PC: normal route allowed`);
        if (route === "download" || route === "direct") assert.equal(downloadsOf(normal.calls).length, 1, `${id} PC: normal sends`);
        if (route === "exclude") assert.equal(normal.calls.tabsCreated.length, 1, "PM5 PC: normal opens confirmation");
        const declined = await run(e2Tab(descriptor));
        assert.equal(route === "permission" ? declined.reply.pillAllowed : declined.reply.ok, false,
          `${id}: non-normal pill route is refused`);
        if (route !== "permission") assert.equal(declined.reply.reason, "unsupported");
        assert.equal(downloadsOf(declined.calls).length, 0);
        assert.equal(evalIn(declined.context, "interceptClaims.size"), 0);
        assert.equal(declined.calls.tabsCreated.length, 0);
        assert.equal(declined.calls.settingsWrites.length, 0);
      });
    }
  }
  test(`F3 GUARD ${mode}: absent sender.tab refuses pill permission`, async () => {
    const loaded = loadBackground({ worker, timers: true });
    const reply = await pillPermission(loaded.events, null, { url: "https://example.test/watch" });
    assert.equal(reply.pillAllowed, false);
  });
}

async function ownSession(worker, options = {}) {
  const item = freshItem();
  const loaded = loadBackground({ worker, timers: true, downloadState: [item], ...options });
  await settle();
  loaded.calls.native.length = 0;
  loaded.events.downloadCreated.emit(item);
  await settle();
  assert.equal(downloadsOf(loaded.calls).length, 1, "OWN PC: normal item is acknowledged by Cove");
  assert.deepEqual(loaded.calls.cancel, [item.id], "OWN PC: acknowledged normal item is cancelled");
  assert.equal(loaded.calls.eraseAttempts.length, 1, "OWN PC: takeover erase is attempted once");
  assert.equal(loaded.calls.erase.length, options.eraseFailures ? 0 : 1, "OWN PC: takeover erase outcome observed");
  return { ...loaded, item, takeoverErases: loaded.calls.erase.length };
}

async function ownTerminal(loaded, count = 1) {
  const pending = Array.from({ length: count }, () => loaded.events.downloadChanged.emit(
    { id: loaded.item.id, state: { current: "interrupted" } })).flat();
  await Promise.all(pending);
  await settle();
}

const ownNormalRecord = (item) => ({ ...item, incognito: false, state: "interrupted" });

for (const worker of [false, true]) {
  const mode = worker ? "Chrome" : "Firefox";
  test(`OWN-18 ${mode}: bookkeeping without ownership never authorizes a terminal cleanup`, async () => {
    const owned = await ownSession(worker);
    owned.replaceDownloadRecord(owned.item.id, ownNormalRecord(owned.item));
    await ownTerminal(owned);
    assert.equal(owned.calls.erase.length - owned.takeoverErases, 1, "OWN-18 PC: matching owned record permits one cleanup");
    const item = ownNormalRecord(freshItem());
    const loaded = loadBackground({ worker, timers: true, downloadState: [item] });
    await settle(); evalIn(loaded.context, "interceptedIds.add(90)");
    assert.equal(evalIn(loaded.context, "ownedDownloads.has(90)"), false);
    await ownTerminal({ ...loaded, item });
    assert.deepEqual(loaded.calls.searches, []); assert.deepEqual(loaded.calls.erase, []);
    assert.deepEqual(loaded.calls.cancel, []); assert.equal(evalIn(loaded.context, "interceptedIds.size"), 0);
    assert.deepEqual(plain(loaded.store.data._interceptedIds), []);
  });
  for (const [label, field, value, equal] of [
    ["missing-time", "startTime", undefined, false], ["null-time", "startTime", null, false],
    ["number-time", "startTime", 0, false], ["equal-number-time", "startTime", 0, true],
    ["empty-time", "startTime", "", true], ["empty-url", "url", "", true],
  ]) {
    test(`OWN-IDENTITY RED ${mode} ${label}: unusable acknowledged identity cannot authorize cleanup`, async () => {
      const item = freshItem({ [field]: value });
      if (label === "missing-time") delete item.startTime;
      const loaded = loadBackground({ worker, timers: true, downloadState: [item] });
      await settle(); loaded.events.downloadCreated.emit(item); await settle();
      assert.equal(downloadsOf(loaded.calls).length, 1, "real intercept is acknowledged");
      assert.deepEqual(loaded.calls.cancel, [item.id]);
      assert.equal(loaded.calls.erase.length, 1, "takeover erase remains permitted and is counted separately");
      const record = ownNormalRecord(item);
      if (!equal) record.startTime = "2099-01-01T00:00:00Z";
      loaded.replaceDownloadRecord(item.id, record);
      await ownTerminal({ ...loaded, item });
      assert.equal(loaded.calls.eraseAttempts.length, 1, "unusable ownership identity prevents cleanup erase");
      assert.equal(loaded.calls.erase.length, 1);
      assert.equal(loaded.calls.searches.length, 1);
      assert.equal(evalIn(loaded.context, "interceptedIds.size"), 0);
      assert.deepEqual(plain(loaded.store.data._interceptedIds), []);
    });
  }
  for (const label of ["normal", "private", "missing", "null", "throwing", "url", "startTime", "empty", "rejected", "in_progress", "erase-failed"]) {
    test(`OWN-PRUNE ${mode} ${label}: acknowledged ownership racing hydration is revalidated`, async () => {
      const hydrationGate = deferred(), item = freshItem();
      const loaded = loadBackground({ worker, timers: true, hydrationGate, storedIntercepted: [901, 902],
        downloadState: [item, { id: 901, incognito: false }, { id: 902, incognito: true }],
        eraseFailures: label === "erase-failed" ? 1 : 0,
        searchHook: async () => { if (label === "rejected") throw new Error("lookup failed"); } });
      await settle(); loaded.events.downloadCreated.emit(item); await settle();
      assert.equal(evalIn(loaded.context, "ownedDownloads.has(90)"), true, "ack precedes hydration");
      assert.equal(loaded.calls.eraseAttempts.length, 0, "takeover erase is still parked");
      let record = ownNormalRecord(item);
      if (label === "private") record.incognito = true;
      if (label === "missing") delete record.incognito;
      if (label === "null") record.incognito = null;
      if (label === "throwing") Object.defineProperty(record, "incognito", { get() { throw new Error("privacy getter"); } });
      if (label === "url") record.url += "?different";
      if (label === "startTime") record.startTime = "2000-01-01T00:00:00Z";
      if (label === "in_progress") record.state = "in_progress";
      loaded.replaceDownloadRecord(item.id, label === "empty" ? undefined : record);
      hydrationGate.resolve(); await evalIn(loaded.context, "interceptedIdsReady"); await settle();
      const retained = ["rejected", "in_progress", "erase-failed"].includes(label);
      assert.deepEqual(plain(loaded.calls.searches), [{ id: item.id }], "inherited extras are never searched");
      assert.deepEqual(loaded.calls.cancel, [item.id], "one takeover cancellation");
      assert.equal(loaded.calls.eraseAttempts.length, ["normal", "erase-failed"].includes(label) ? 2 : 1);
      assert.equal(loaded.calls.erase.length, label === "normal" ? 2 : 1, "count takeover separately from prune");
      assert.ok(loaded.calls.eraseAttempts.every(q => q.id === item.id), "inherited extras are never erased");
      assert.equal(evalIn(loaded.context, "interceptedIds.has(90)"), retained);
      assert.deepEqual(plain(loaded.store.data._interceptedIds), retained ? [item.id] : []);
    });
  }
  test(`HC-HYDRATION GUARD ${mode}: hydration gate parks readiness until explicitly released`, async () => {
    const hydrationGate = deferred();
    const loaded = loadBackground({ worker, timers: true, hydrationGate, storedIntercepted: [901] });
    let ready = false;
    const readiness = evalIn(loaded.context, "interceptedIdsReady").then(() => { ready = true; });
    await settle();
    assert.equal(ready, false);
    assert.deepEqual(plain(loaded.store.data._interceptedIds), [901]);
    hydrationGate.resolve();
    await readiness;
    await settle();
    assert.equal(ready, true);
    assert.deepEqual(plain(loaded.store.data._interceptedIds), []);
  });
  test(`OWN-PRUNE-REVOKE ${mode}: onErased during owned startup lookup prevents cleanup erase`, async () => {
    for (const revoked of [false, true]) {
      const hydrationGate = deferred(), lookupGate = deferred(), item = freshItem();
      const record = ownNormalRecord(item);
      const loaded = loadBackground({ worker, timers: true, hydrationGate, storedIntercepted: [901],
        searchHook: async () => { await lookupGate.promise; }, downloadSearch: () => [record] });
      await settle();
      loaded.events.downloadCreated.emit(item);
      await settle();
      assert.equal(evalIn(loaded.context, "ownedDownloads.has(90)"), true, "real acknowledgement establishes ownership");
      assert.equal(loaded.calls.cancel.length, 0, "takeover waits for hydration readiness");
      hydrationGate.resolve();
      await settle();
      assert.deepEqual(plain(loaded.calls.searches), [{ id: item.id }], "only the owned id is searched");
      const erased = revoked ? loaded.events.downloadErased.emit(item.id) : [];
      // The in-flight browser lookup may still return its earlier normal record after onErased.
      lookupGate.resolve();
      await Promise.all(erased);
      await evalIn(loaded.context, "interceptedIdsReady");
      await settle();
      assert.deepEqual(loaded.calls.cancel, [item.id], "one acknowledged takeover completes");
      assert.equal(loaded.calls.erase.length, revoked ? 1 : 2,
        revoked ? "revoked ownership prevents prune erase; only takeover erase remains" : "positive control: one prune erase plus one takeover erase");
      assert.deepEqual(plain(loaded.store.data._interceptedIds), []);
      assert.equal(evalIn(loaded.context, "interceptedIds.size"), 0);
    }
  });
}

const OWN_RECORD_CASES = [
  ...E2_CONTEXTS.map(([label, descriptor]) => ({ label,
    id: label === "private" ? "IID-C2" : ["missing", "undefined"].includes(label) ? "IID-C3"
      : label === "null" ? "IID-C4" : "IID-C5",
    record: (item) => unknownItem(descriptor, { ...item, state: "interrupted" }) })),
  { label: "no-record", id: "OWN-8/IID-C6", record: () => undefined },
  { label: "rejected", id: "OWN-9/IID-C7", record: ownNormalRecord, rejected: true },
  { label: "different-id", id: "IID-C8", record: (item) => ({ ...ownNormalRecord(item), id: item.id + 1 }) },
  { label: "different-url", id: "OWN-7b", record: (item) => ({ ...ownNormalRecord(item), url: "https://unrelated.test/file.zip" }) },
  { label: "different-startTime", id: "OWN-7b", record: (item) => ({ ...ownNormalRecord(item), startTime: "2000-01-01T00:00:00Z" }) },
];

for (const worker of [false, true]) {
  const mode = worker ? "Chrome" : "Firefox";
  test(`HC-OWN GUARD ${mode}: erase failures, search hooks and raw replacements are controllable`, async () => {
    const item = Object.freeze(freshItem());
    const gate = deferred();
    let failSearch = false;
    const loaded = loadBackground({ worker, timers: true, downloadState: [item], eraseFailures: 1,
      searchHook: async () => { await gate.promise; if (failSearch) throw new Error("lookup failed"); } });
    await settle();
    const downloads = loaded.context.browser.downloads;
    let finished = false;
    const lookup = downloads.search({ id: item.id }).then((rows) => { finished = true; return rows; });
    await settle();
    assert.equal(finished, false);
    const replacement = Object.freeze(unknownItem({ get() { throw new Error("privacy getter"); } }, item));
    loaded.replaceDownloadRecord(item.id, replacement);
    gate.resolve();
    assert.strictEqual((await lookup)[0], replacement);
    await assert.rejects(downloads.erase({ id: item.id }), /erase failed/);
    assert.strictEqual((await downloads.search({ id: item.id }))[0], replacement);
    await downloads.erase({ id: item.id });
    assert.deepEqual(await downloads.search({ id: item.id }), []);
    failSearch = true;
    await assert.rejects(downloads.search({ id: item.id }), /lookup failed/);
    assert.equal(loaded.calls.eraseAttempts.length, 2);
    assert.equal(loaded.calls.erase.length, 1);
    assert.equal(item.state, "in_progress");
  });
  for (const [id, eraseFailures] of [["OWN-1", 0], ["OWN-1", 1], ["OWN-16", 0]]) {
    test(`${id} GUARD ${mode} takeover-failures-${eraseFailures}: real normal acknowledgement permits terminal cleanup`, async () => {
      const loaded = await ownSession(worker, { eraseFailures });
      assert.equal(loaded.takeoverErases, eraseFailures ? 0 : 1, `${id}: takeover erase counted separately`);
      if (!eraseFailures) loaded.replaceDownloadRecord(loaded.item.id, ownNormalRecord(loaded.item));
      await ownTerminal(loaded);
      assert.equal(loaded.calls.erase.length - loaded.takeoverErases, 1);
      assert.equal(loaded.calls.cancel.length, 1);
      assert.equal(evalIn(loaded.context, "interceptedIds.size"), 0);
    });
  }
  for (const [label, descriptor] of E2_CONTEXTS) {
    const id = label === "private" ? "OWN-2" : "OWN-3";
    test(`${id} RED ${mode} ${label}: declined creation establishes no takeover`, async () => {
      await ownSession(worker);
      const loaded = await e2Created(worker, Object.freeze(unknownItem(descriptor)));
      assert.equal(downloadsOf(loaded.calls).length, 0, `${id}: no non-normal native handoff`);
      assert.deepEqual(loaded.calls.cancel, []);
      assert.deepEqual(loaded.calls.erase, []);
      assert.equal(evalIn(loaded.context, "interceptedIds.size"), 0);
    });
  }
  for (const row of OWN_RECORD_CASES) {
    test(`${row.id} RED ${mode} ${row.label}: owned cleanup requires matching normal browser evidence`, async () => {
      let rejectLookup = false;
      const loaded = await ownSession(worker, { searchHook: async () => {
        if (rejectLookup) throw new Error("lookup failed");
      } });
      loaded.replaceDownloadRecord(loaded.item.id, row.record(loaded.item));
      rejectLookup = !!row.rejected;
      await ownTerminal(loaded);
      assert.equal(loaded.calls.erase.length - loaded.takeoverErases, 0, `${row.id}: unsafe cleanup never erases`);
      assert.equal(evalIn(loaded.context, "interceptedIds.has(90)"), !!row.rejected, `${row.id}: lookup-failure retention`);
      assert.equal((loaded.store.data._interceptedIds || []).includes(90), !!row.rejected);
    });
  }
  const inheritedRows = [...OWN_RECORD_CASES,
    { label: "terminal-normal", record: ownNormalRecord },
    { label: "in-progress", record: (item) => ({ ...item, incognito: false }) }];
  for (const row of inheritedRows) {
    const id = row.label === "private" ? "OWN-4" : row.label === "terminal-normal" ? "OWN-6"
      : row.label === "different-url" ? "OWN-7a" : E2_CONTEXTS.some(([label]) => label === row.label) ? "OWN-5" : "OWN-12";
    test(`${id === "OWN-12" ? id : id + "/OWN-12"}/IID-S RED ${mode} ${row.label}: inherited bookkeeping authorizes no lookup or destruction`, async () => {
      await ownSession(worker);
      const item = freshItem();
      const loaded = loadBackground({ worker, timers: true, storedIntercepted: [item.id], downloadState: [],
        searchHook: row.rejected ? async () => { throw new Error("lookup failed"); } : null });
      loaded.replaceDownloadRecord(item.id, row.record(item));
      await settle();
      assert.equal(loaded.calls.searches.length, 0, "IID-S: inherited ids are never searched");
      assert.deepEqual(loaded.calls.cancel, []);
      assert.deepEqual(loaded.calls.erase, []);
      assert.equal(evalIn(loaded.context, "interceptedIds.size"), 0);
      assert.deepEqual(Array.from(loaded.store.data._interceptedIds), []);
    });
  }
  for (const failure of [false, true]) {
    const id = failure ? "OWN-11/IID-X-substitute" : "OWN-10/IID-C-dup";
    test(`${id} RED ${mode}: concurrent terminal events share one lookup outcome`, async () => {
      const gate = deferred();
      const loaded = await ownSession(worker, { searchHook: async () => {
        await gate.promise; if (failure) throw new Error("lookup failed");
      } });
      loaded.replaceDownloadRecord(loaded.item.id, ownNormalRecord(loaded.item));
      const completion = ownTerminal(loaded, 2);
      await settle();
      gate.resolve();
      await completion;
      assert.equal(loaded.calls.erase.length - loaded.takeoverErases, failure ? 0 : 1,
        `${id}: concurrent cleanup erases at most once and never after lookup failure`);
      assert.equal(loaded.calls.searches.length, 1, `${id}: one shared in-flight lookup`);
      assert.equal(evalIn(loaded.context, "interceptedIds.has(90)"), failure);
    });
  }
  // onChanged awaits interceptedIdsReady: initial startup pruning and that event cannot overlap (IID-X).
  test(`OWN-13 RED ${mode}: terminal event for a storage-only id cannot erase or search`, async () => {
    await ownSession(worker);
    const item = freshItem();
    const loaded = loadBackground({ worker, timers: true, storedIntercepted: [item.id], downloadState: [item] });
    await settle();
    loaded.calls.searches.length = 0;
    await ownTerminal({ ...loaded, item });
    assert.equal(loaded.calls.erase.length, 0, "OWN-13: storage-only terminal event never erases");
    assert.equal(loaded.calls.searches.length, 0);
  });
  test(`OWN-14 RED ${mode}: hydrated ids never become current-session ownership`, async () => {
    await ownSession(worker);
    const item = ownNormalRecord(freshItem());
    const loaded = loadBackground({ worker, timers: true, storedIntercepted: [item.id], downloadState: [item] });
    await settle();
    assert.equal(loaded.calls.searches.length, 0, "OWN-14: inherited ids never trigger a browser lookup");
    assert.equal(evalIn(loaded.context, 'typeof ownedDownloads !== "undefined" && ownedDownloads.has(90)'), false,
      "OWN-14: hydration never promotes ownership");
    await ownTerminal({ ...loaded, item });
    assert.equal(loaded.calls.erase.length, 0, "OWN-14: later normal record cannot authorize inherited erase");
  });
  for (const [label, nativeResult] of [["non-ok", { status: "error" }], ["null", null], ["malformed", {}],
    ["rejected", () => Promise.reject(new Error("rejected"))], ["timeout", () => Promise.reject(new Error("native timeout"))]]) {
    test(`OWN-15 GUARD ${mode} ${label}: failed native acknowledgement establishes no ownership`, async () => {
      await ownSession(worker);
      const item = freshItem();
      const loaded = loadBackground({ worker, timers: true, nativeResult, downloadState: [item] });
      await settle();
      loaded.events.downloadCreated.emit(item);
      await settle();
      await ownTerminal({ ...loaded, item });
      assert.equal(evalIn(loaded.context, 'typeof ownedDownloads !== "undefined" && ownedDownloads.has(90)'), false);
      assert.equal(evalIn(loaded.context, "interceptedIds.size"), 0);
      assert.deepEqual(loaded.calls.cancel, []);
      assert.deepEqual(loaded.calls.erase, []);
    });
  }
  test(`OWN-17 RED ${mode}: inherited bookkeeping is forgotten and persisted separately`, async () => {
    await ownSession(worker);
    const item = freshItem();
    const loaded = loadBackground({ worker, timers: true, storedIntercepted: [item.id], downloadState: [item] });
    await settle();
    assert.deepEqual(Array.from(loaded.store.data._interceptedIds), [], "OWN-17: persist forgotten inherited ids");
    assert.deepEqual(loaded.calls.cancel, []);
    assert.deepEqual(loaded.calls.erase, []);
  });
  test(`IID-C-revoked RED ${mode}: erase event revokes ownership during a pending lookup`, async () => {
    const gate = deferred();
    const loaded = await ownSession(worker, { searchHook: () => gate.promise });
    loaded.replaceDownloadRecord(loaded.item.id, ownNormalRecord(loaded.item));
    const completion = ownTerminal(loaded);
    await settle();
    await Promise.all(loaded.events.downloadErased.emit(loaded.item.id));
    gate.resolve();
    await completion;
    assert.equal(loaded.calls.erase.length - loaded.takeoverErases, 0, "IID-C-revoked: recheck ownership after lookup await");
  });
}

const e2Whitespace = (text) => text.replace(/\s+/g, " ").trim();
const e2Occurrences = (text, part) => text.split(part).length - 1;
for (const [id, path, old, replacement] of E2_DOC_ROWS) {
  test(`P1 RED ${id}: approved documentation row occurs exactly once`, () => {
    const wanted = e2Whitespace(replacement), retired = e2Whitespace(old);
    const control = e2Whitespace(old.replace(old, replacement));
    assert.equal(e2Occurrences(control, wanted), 1, `${id} PC: replacement fixture occurs once`);
    if (!wanted.includes(retired)) assert.equal(e2Occurrences(control, retired), 0, `${id} PC: old-only region removed`);
    const current = e2Whitespace(fs.readFileSync(path, "utf8"));
    assert.equal(e2Occurrences(current, wanted), 1, `P1 ${id}: approved text occurs once`);
    if (!wanted.includes(retired)) assert.equal(e2Occurrences(current, retired), 0, `P1 ${id}: old-only region removed`);
  });
}

test("P2 GUARD: existing S08B2 and S08C documentation pins survive", () => {
  const source = fs.readFileSync("tests/test_queue.py", "utf8");
  const b = source.match(/_S08B2_DOC_REPLACEMENTS = \(([\s\S]*?)\n\)/)[1];
  const c = source.match(/_S08C_DOC_REPLACEMENTS = \[([\s\S]*?)\n\]/)[1];
  const bRows = [...b.matchAll(/\("([^"]+)", "([^"]+)", "([^"]+)"\)/g)];
  const cRows = [...c.matchAll(/\("([^"]+)", "([^"]+)"\)/g)];
  assert.equal(bRows.length, 4, "P2 PC: four S08B2 pins loaded");
  assert.equal(cRows.length, 4, "P2 PC: four S08C pins loaded");
  for (const [, path, old, replacement] of bRows) {
    const text = e2Whitespace(fs.readFileSync(path, "utf8"));
    assert.equal(e2Occurrences(text, replacement), 1, `P2: ${path} S08B2 pin`);
    assert.equal(text.includes(old), false);
  }
  for (const [, path, replacement] of cRows) {
    const text = e2Whitespace(fs.readFileSync(path, "utf8"));
    for (const sentence of replacement.split(". ")) assert.equal(e2Occurrences(text, sentence.replace(/\.$/, "")), 1);
  }
});

test("P3 GUARD: privacy date is at least the approved baseline date", () => {
  const match = fs.readFileSync("PRIVACY.md", "utf8").match(/_Last updated: (\d{4}-\d{2}-\d{2})_/);
  assert.ok(match && match[1] >= "2026-10-17");
});

test("P4 GUARD: documentation claims no native-browser verification or universal isolation", () => {
  const forbidden = /verified in Firefox|tested in Chrome|(?:complete|universal|all) private[- ]window isolation/i;
  assert.match("verified in Firefox", forbidden, "P4 PC: unsupported verification claim detected");
  for (const path of new Set(E2_DOC_ROWS.map((row) => row[1]))) {
    assert.doesNotMatch(fs.readFileSync(path, "utf8"), forbidden, `P4: ${path}`);
  }
});

test("P5 GUARD: approved rows preserve existing cookie wording", () => {
  const cookieLines = (text) => text.split("\n").filter((line) => /cookie/i.test(line)).map(e2Whitespace);
  assert.notDeepEqual(cookieLines("cookies are sent"), cookieLines("cookies are absent"), "P5 PC: wording changes detected");
  assert.equal(E2_DOC_ROWS.length, 8);
  for (const [id, , old, replacement] of E2_DOC_ROWS) {
    assert.deepEqual(cookieLines(replacement), cookieLines(old), `P5: ${id} preserves cookie wording`);
  }
});

for (const worker of [false, true]) {
  test(`K2/PM4 GUARD ${worker ? "Chrome" : "Firefox"}: normal menu and pill still send`, async () => {
    const tab = { id: 7, url: "https://example.test/watch", incognito: false };
    assert.equal(downloadsOf((await e2Menu(worker, tab)).calls).length, 1);
    const loaded = loadBackground({ worker, timers: true });
    await settle();
    loaded.calls.native.length = 0;
    assert.equal((await pillPermission(loaded.events, tab.url, { tab, url: tab.url })).pillAllowed, true);
    assert.equal((await pillDownload(loaded.events, { url: PRIVATE_URL_SENTINEL }, { tab, url: tab.url })).ok, true);
    assert.equal(downloadsOf(loaded.calls).length, 1);
  });
}

test("L1/L3 RED Chrome: per-item gate survives a worker reload with normal global context", async () => {
  const privateSends = [];
  for (let wake = 0; wake < 2; wake += 1) {
    const loaded = await e2Created(true, freshItem());
    e2NormalCreated(loaded, "L3");
    assert.equal(loaded.context.browser.extension.inIncognitoContext, false);
    assert.equal((await loaded.context.browser.windows.getCurrent()).incognito, false);
    loaded.events.downloadCreated.emit(unknownItem({ value: true }, { id: 91, url: PRIVATE_URL_SENTINEL }));
    await settle();
    privateSends.push(downloadsOf(loaded.calls).length - 1);
  }
  assert.deepEqual(privateSends, [0, 0], "L3: private declines before and after worker reload");
});

test("L2 GUARD Chrome: unsupported stream message makes no tab lookups", async () => {
  const normal = await e2Created(true, freshItem());
  e2NormalCreated(normal, "L2");
  const loaded = loadBackground({ worker: true, timers: true });
  await settle();
  loaded.calls.native.length = 0;
  const { responses } = sendRuntimeMessage(loaded.events,
    { type: "downloadStream", url: PRIVATE_URL_SENTINEL }, popupSender());
  await settle();
  assert.equal(responses.length, 1);
  assert.equal(responses[0].reason, "unsupported");
  assert.deepEqual(loaded.calls.tabsGet, []);
  assert.equal(downloadsOf(loaded.calls).length, 0);
});

for (const [label, descriptor] of E2_CONTEXTS) {
  for (const popup of [false, true]) {
    test(`K3 RED Firefox ${label} ${popup ? "popup" : "content"}: non-normal stream listing is empty`, async () => {
      async function listing(tab) {
        const loaded = loadBackground({ timers: true, tabs: [tab] });
        await settle();
        seedStream(loaded, tab.id, "https://streams.example.test/live.m3u8");
        await settle();
        const { responses } = sendRuntimeMessage(loaded.events, { type: "getDetectedStreams" },
          popup ? popupSender() : { tab, url: tab.url });
        await settle();
        return responses;
      }
      const normal = await listing({ id: 7, url: "https://example.test/watch", incognito: false });
      assert.equal(normal.length, 1, "K3 PC: one normal response");
      assert.equal(normal[0].length, 1, "K3 PC: normal holder is listed");
      const declined = await listing(e2Tab(descriptor));
      assert.equal(declined.length, 1);
      assert.equal(declined[0].length, 0, "K3: non-normal tab lists no streams");
    });
  }
}

test("K5/L4 GUARD: response-only detector and unchanged manifest privacy surface", () => {
  const source = fs.readFileSync("extension/media-sites.js", "utf8");
  const listener = source.slice(source.indexOf("browser.webRequest.onHeadersReceived.addListener"),
    source.indexOf("function updateStreamBadge"));
  assert.match(listener, /responseHeaders/, "K5 PC: real response-header listener selected");
  assert.doesNotMatch(listener, /requestHeaders/);
  for (const file of ["extension/manifest.json", "extension/manifest.chrome.json"]) {
    const manifest = JSON.parse(fs.readFileSync(file, "utf8"));
    assert.equal(Object.hasOwn(manifest, "incognito"), false);
    assert.equal(Object.hasOwn(manifest, "web_accessible_resources"), false);
    assert.equal(manifest.permissions.includes("cookies"), false);
    if (file.includes("chrome")) assert.equal(manifest.permissions.includes("webRequest"), false);
  }
});

for (const worker of [false, true]) {
  const mode = worker ? "worker" : "Firefox";
  test(`HC-D1 GUARD ${mode}: event delivery preserves raw privacy, identity and frozen fixtures`, async () => {
    const loaded = loadBackground({ worker, timers: true });
    await settle();
    let reads = 0;
    const throwing = { get() { reads += 1; throw new Error("privacy getter"); } };
    const descriptors = [undefined, ...[undefined, null, true, false, "false", "true", 0, 1, {}, [], NaN]
      .map((value) => ({ value })), throwing];
    // Isolate harness delivery from production's legitimate property reads.
    for (const name of ["downloadCreated", "contextMenuClicked", "message"]) {
      const delivery = loaded.events[name];
      delivery.listeners.splice(0);
      let received;
      delivery.addListener((...args) => { received = args; });
      for (const descriptor of descriptors) {
        const item = Object.freeze(unknownItem(descriptor));
        const before = Object.getOwnPropertyDescriptors(item);
        const sender = Object.freeze({ tab: item });
        if (name === "downloadCreated") delivery.emit(item);
        else delivery.emit({}, name === "message" ? sender : item);
        const delivered = name === "downloadCreated" ? received[0]
          : name === "message" ? received[1].tab : received[1];
        assert.strictEqual(delivered, item);
        if (name === "message") assert.strictEqual(received[1], sender);
        assert.deepEqual(Object.getOwnPropertyDescriptors(item), before);
        assert.equal(Object.hasOwn(item, "incognito"), descriptor !== undefined);
        if (descriptor === throwing) assert.throws(() => delivered.incognito, /privacy getter/);
        else if (descriptor) assert.ok(Object.is(delivered.incognito, descriptor.value));
      }
    }
    assert.equal(reads, 3, "only the three explicit throwing-getter assertions read it");
  });

  test(`HC-D2 GUARD ${mode}: tab, search and sender mocks preserve supplied objects`, async () => {
    let reads = 0;
    const fixtures = [undefined, ...[undefined, null, true, false, "false", 0, {}, [], NaN]
      .map((value) => ({ value })), { get() { reads += 1; throw new Error("privacy getter"); } }]
      .map((descriptor, id) => Object.freeze(unknownItem(descriptor, { id })));
    const tabs = Object.freeze(fixtures);
    const answers = Object.freeze(fixtures);
    const loaded = loadBackground({ worker, timers: true, tabs,
      tabsById: new Map(fixtures.map((item) => [item.id, item])), downloadSearch: () => answers });
    await settle();
    const browser = loaded.context.browser;
    assert.strictEqual(await browser.tabs.query({}), tabs);
    assert.strictEqual(await browser.downloads.search({ id: 0 }), answers);
    const stateful = loadBackground({ worker, timers: true, downloadState: fixtures });
    await settle();
    for (const item of fixtures) {
      const before = Object.getOwnPropertyDescriptors(item);
      assert.strictEqual(await browser.tabs.get(item.id), item);
      const sender = Object.freeze({ tab: item });
      assert.strictEqual(completeSender(sender).tab, item);
      assert.strictEqual(pillSender(item.url, sender), sender);
      const downloads = stateful.context.browser.downloads;
      assert.strictEqual((await downloads.search({ id: item.id }))[0], item);
      await downloads.cancel(item.id);
      const updated = (await downloads.search({ id: item.id }))[0];
      assert.deepEqual(Object.getOwnPropertyDescriptor(updated, "incognito"), before.incognito);
      assert.equal(updated.state, "interrupted");
      assert.deepEqual(Object.getOwnPropertyDescriptors(item), before);
    }
    assert.equal(reads, 0, "mocks and sender helpers never read privacy getters");
  });

  test(`HC-D3 GUARD ${mode}: unknown builder changes only privacy; context mocks report normal`, async () => {
    const overrides = { startTime: "2026-10-17T00:00:00Z" };
    const normal = freshItem(overrides);
    delete normal.incognito;
    for (const descriptor of [undefined, { value: null }, { value: "false" }]) {
      const item = unknownItem(descriptor, overrides);
      delete item.incognito;
      assert.deepEqual(item, normal);
    }
    const loaded = loadBackground({ worker, timers: true });
    await settle();
    assert.equal((await loaded.context.browser.windows.getCurrent()).incognito, false);
    assert.equal(loaded.context.browser.extension.inIncognitoContext, false);
    assert.equal(popupSender().url, EXTENSION_ORIGIN + "popup/popup.html");
  });

  test(`HC3 GUARD ${mode}: tabs.get supports objects, rejection, deferred results and getters`, async () => {
    const pending = deferred();
    const lateFailure = deferred();
    const tab = Object.freeze({ id: 1, incognito: false });
    const throwing = Object.freeze({ id: 4, get incognito() { throw new Error("privacy getter"); } });
    const loaded = loadBackground({ worker, timers: true,
      tabsById: new Map([[1, tab], [2, new Error("closed tab")], [3, pending.promise],
        [4, throwing], [5, lateFailure.promise]]) });
    await settle();
    const get = loaded.context.browser.tabs.get;
    assert.strictEqual(await get(1), tab);
    await assert.rejects(get(2), /closed tab/);
    let settled = false;
    const lookup = get(3).then((result) => { settled = true; return result; });
    await settle();
    assert.equal(settled, false);
    pending.resolve(tab);
    assert.strictEqual(await lookup, tab);
    assert.strictEqual(await get(4), throwing);
    assert.throws(() => throwing.incognito, /privacy getter/);
    const rejected = assert.rejects(get(5), /closed later/);
    lateFailure.reject(new Error("closed later"));
    await rejected;
    assert.deepEqual(loaded.calls.tabsGet, [1, 2, 3, 4, 5]);
  });

  test(`HC4 GUARD ${mode}: controllable timers fire once and respect clearing and delay`, async () => {
    const loaded = loadBackground({ worker, timers: true });
    await settle();
    const fired = [];
    const schedule = loaded.context.setTimeout;
    schedule((value) => fired.push(value), 3000, "live");
    const cleared = schedule(() => fired.push("cleared"), 3000);
    schedule(() => fired.push("other delay"), 3001);
    loaded.context.clearTimeout(cleared);
    loaded.fireTimers(3000);
    loaded.fireTimers(3000);
    assert.deepEqual(fired, ["live"]);
    loaded.fireTimers(3001);
    assert.deepEqual(fired, ["live", "other delay"]);
    const real = loadBackground({ worker });
    await settle();
    assert.strictEqual(real.context.setTimeout, setTimeout);
    assert.strictEqual(real.context.clearTimeout, clearTimeout);
  });

  test(`HC5 GUARD ${mode}: settings reads count a normal handoff`, async () => {
    const loaded = loadBackground({ worker, timers: true });
    await settle();
    assert.equal(loaded.calls.settingsReads, 1, "startup loads settings once");
    loaded.events.downloadCreated.emit(freshItem());
    await settle();
    assert.equal(downloadsOf(loaded.calls).length, 1, "positive control reaches native host");
    assert.equal(loaded.calls.settingsReads, 1);
    await loaded.store.get("coveDiag");
    assert.equal(loaded.calls.settingsReads, 1, "other storage reads do not count");
    await loaded.store.get("settings");
    assert.equal(loaded.calls.settingsReads, 2, "another settings read increments the counter");
  });

  test(`HC6 GUARD ${mode}: cancel and erase update state without mutating supplied records`, async () => {
    const item = Object.freeze(freshItem());
    const loaded = loadBackground({ worker, timers: true, downloadState: [item] });
    await settle();
    const downloads = loaded.context.browser.downloads;
    assert.strictEqual((await downloads.search({ id: item.id }))[0], item);
    await downloads.cancel(item.id);
    const cancelled = (await downloads.search({ id: item.id }))[0];
    assert.equal(cancelled.state, "interrupted");
    assert.equal(cancelled.incognito, false);
    assert.equal(item.state, "in_progress", "frozen input remains unchanged");
    await downloads.erase({ id: item.id });
    assert.deepEqual(await downloads.search({ id: item.id }), []);
    assert.equal(loaded.calls.searches.length, 3);
  });
}

test("HC-D4 GUARD Firefox: seedStream uses the registered response-header listener", async () => {
  const loaded = loadBackground({ timers: true });
  await settle();
  const url = "https://streams.example.test/live.m3u8";
  assert.equal(loaded.events.headersReceived.listeners.length, 1);
  seedStream(loaded, 7, url);
  await settle();
  assert.equal(evalIn(loaded.context, "detectedStreams.get(7)[0].url"), url);
  loaded.events.tabUpdated.emit(7, { url: "https://example.test/elsewhere" });
  await settle();
  assert.equal(evalIn(loaded.context, "detectedStreams.has(7)"), false);
  const worker = loadBackground({ worker: true, timers: true });
  await settle();
  assert.equal(worker.events.headersReceived.listeners.length, 0, "worker has no stream adapter");
});

test("restored Chrome download history is never sent to Cove", async () => {
  const { calls, events } = loadBackground();
  await settle();
  calls.native.length = 0; // Ignore the startup ping.

  events.downloadCreated.emit({
    incognito: false,
    id: 1,
    url: "https://example.test/archive.zip",
    filename: "archive.zip",
    state: "complete",
    startTime: new Date().toISOString(),
    totalBytes: 2_000_000,
  });
  events.downloadCreated.emit({
    incognito: false,
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
    incognito: false,
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
    incognito: false,
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
  const { events } = popupStreamFixture({ nativeResult: { status: "error", message: "offline" } });
  await settle();
  let response;

  events.message.emit(
    { type: "downloadStream", url: "https://example.test/live.m3u8", filename: "live.mp4" },
    popupSender(),
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
    incognito: false,
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
      incognito: false,
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
    incognito: false,
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
      { incognito: false }
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
      { incognito: false, url: "https://www.youtube.com/watch?v=abc123", title: "Clip - YouTube" }
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
      { incognito: false, url: "https://www.youtube.com/watch?v=abc123" }
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
      { incognito: false, url: "https://www.youtube.com/watch?v=abc123" }
    )
  );
  await settle();

  // The browser would save the watch page's HTML, not the video.
  assert.equal(browserDownloads.length, 0);
});

test("context menu ignores an unusable blob src off an extractor page", async () => {
  const { calls, events, browserDownloads } = loadBackground({
    cookies: [{ name: "session", value: COOKIE_SENTINEL }],
  });
  await settle();
  calls.native.length = 0;

  await Promise.all(
    events.contextMenuClicked.emit(
      {
        menuItemId: "download-with-cove",
        srcUrl: "blob:https://example.test/2b0f8c1e-0000-4000-8000-000000000000",
        pageUrl: "https://example.test/player",
      },
      { incognito: false, url: "https://example.test/player" }
    )
  );
  await settle();

  assert.equal(calls.native.filter((m) => m.action === "download").length, 0);
  assert.equal(browserDownloads.length, 0);
  assert.deepEqual(calls.cookies, [], "refused menu reads no cookies");
  assert.equal(JSON.stringify(calls.native).includes(COOKIE_SENTINEL), false);
});

// ---- Shared media core, loaded on its own ----
//
// media-core.js must be browser-neutral: it is copied into the Chrome bundle
// and a later slice will load it there with no site adapter present. These
// exercise the default path - buildCoveMedia() with no argument and no
// CoveMediaCapability global - because that is the configuration Chrome will
// run, not a special explicit one.

function loadMediaCore({ capability, nativeResult = { status: "ok" },
                        cookies = [],
                        // Stands in for background.js's consent gate. The
                        // default is the granted answer, because that is what
                        // this route sent before the gate existed and these
                        // tests are about the core's own mechanics; `false`
                        // models a browser where the user withheld the
                        // optional technical-data permission.
                        technicalConsent = true } = {}) {
  const noop = () => {};
  // media-core.js calls back into background.js globals at call time. Standing
  // in for them here is what lets the pill's handoff be driven against the
  // core alone, with no browser bundle around it, and every call recorded.
  const core = { native: [], cookies: [], marked: [], notifications: [], diag: [] };
  // url -> committed timestamp, and url -> Map(token -> claimed-at): the same
  // two structures the background keeps.
  const recentIntercepted = new Map();
  const interceptClaims = new Map();
  let coreTokenSeq = 0;
  const context = vm.createContext({
    globalThis: undefined,
    browser: {
      webRequest: null,
      cookies: {
        async getAll(query) { core.cookies.push(query); return cookies; },
      },
      tabs: {
        onRemoved: { addListener: noop },
        onUpdated: { addListener: noop },
        onActivated: { addListener: noop },
        query: () => Promise.resolve([]),
      },
    },
    recentIntercepted,
    // markIntercepted answers with the token identifying the mark it just set,
    // exactly as the background's does, and releaseIntercepted withdraws it only
    // while that is still the mark present. Modelling the token as production
    // does is what lets the core's own withdrawal logic be exercised here at
    // all; a marker that returned nothing would make every withdrawal look
    // valid.
    // Committing and claiming are modelled as two separate things, as in
    // production: `core.marked` records only what actually reached Cove, and a
    // claim suppresses interception while a handoff runs without committing
    // anything. A stub that merged them would let the core's commit-on-success
    // ordering pass untested.
    markIntercepted(url) {
      core.marked.push(url);
      recentIntercepted.set(url, Date.now());
    },
    claimIntercepted(url) {
      coreTokenSeq += 1;
      const claims = interceptClaims.get(url) || new Map();
      claims.set(coreTokenSeq, Date.now());
      interceptClaims.set(url, claims);
      return coreTokenSeq;
    },
    releaseIntercepted(url, token) {
      const claims = interceptClaims.get(url);
      if (!claims || !claims.delete(token)) return false;
      if (claims.size === 0) interceptClaims.delete(url);
      return true;
    },
    wasRecentlyIntercepted(url) {
      return recentIntercepted.has(url) ||
             (interceptClaims.get(url) || new Map()).size > 0;
    },
    async sendNativeMessage(message) {
      core.native.push(message);
      return nativeResult;
    },
    // Same shape background.js's helper returns: one key, or no key at all.
    async userAgentField() {
      return technicalConsent ? { userAgent: "test-agent" } : {};
    },
    showNotification(title, body) { core.notifications.push({ title, body }); },
    diagRecord(component, event, level, fields, requestId) {
      core.diag.push({ component, event, level, fields, requestId });
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
  return {
    context,
    core,
    recentIntercepted,
    CoveMedia: evalIn(context, "CoveMedia"),
    // buildCoveMedia is a function declaration, so unlike CoveMedia it is a
    // property of the context. Calling it with an explicit capability is the
    // documented alternative to the global, and it has to honour the same
    // hooks the global does.
    buildCoveMedia: evalIn(context, "buildCoveMedia"),
  };
}

// Drives one media message through a core surface exactly as background.js's
// listener does, and resolves with what that listener would return alongside
// the reply the caller received. `kept === true` is the "keep sendResponse
// alive" contract, and `replies` proves it was answered exactly once.
async function coreMediaMessage(media, msg, sender = {}) {
  const replies = [];
  let resolve = null;
  const answered = new Promise((r) => { resolve = r; });
  // The reply is built in the script's realm, so it is compared by structure
  // rather than by identity - the same round-trip plain() does elsewhere.
  const kept = media.handleMessage(msg, sender, (reply) => {
    replies.push(plain(reply));
    resolve();
  });
  await Promise.race([
    answered,
    new Promise((r) => setTimeout(r, 250)),
  ]);
  return { kept, replies };
}

test("the shared core offers video and audio contexts without any site adapter", () => {
  const { CoveMedia } = loadMediaCore();
  // Array.from: the value comes from the script's realm, so it is structurally
  // but not referentially a host array.
  assert.deepEqual(Array.from(CoveMedia.contexts), ["video", "audio"]);
});

test("the shared core sanitises a title into a filename with no site adapter", () => {
  const { CoveMedia } = loadMediaCore();
  const tab = { incognito: false, title: "  spaced   out  title...  ", url: "https://example.test/page" };

  assert.equal(
    CoveMedia.mediaFilename(tab, "https://cdn.example.test/v/clip.mov"),
    "spaced out title.mov",
  );
});

test("the shared core replaces characters a filename cannot contain", () => {
  const { CoveMedia } = loadMediaCore();
  const tab = { incognito: false, title: 'a/b:c*d?e"f<g>h|i', url: "https://example.test/page" };

  assert.equal(
    CoveMedia.mediaFilename(tab, "https://cdn.example.test/v/clip.mkv"),
    "a b c d e f g h i.mkv",
  );
});

test("the shared core caps a filename at 180 characters plus its extension", () => {
  const { CoveMedia } = loadMediaCore();
  const tab = { incognito: false, title: "z".repeat(250), url: "https://example.test/page" };

  const name = CoveMedia.mediaFilename(tab, "https://cdn.example.test/v/clip.mp4");
  assert.equal(name, "z".repeat(180) + ".mp4");
});

test("the shared core infers the extension from the media path", () => {
  const { CoveMedia } = loadMediaCore();
  const tab = { incognito: false, title: "Holiday clip", url: "https://example.test/page" };

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
      { incognito: false, url: "https://www.youtube.com/watch?v=abc123" },
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

// ---- The optional media-target refusal ----
//
// rejectMediaTarget is a hook like every other: supplied by a capability, and
// absent by default. These pin the three ways the core can be configured, so
// the Chrome bundle's behaviour is not the only thing holding the contract up.

const MANIFEST_PILL = {
  type: "downloadMedia",
  url: "https://cdn.example.test/v/stream.m3u8",
  pageUrl: "https://example.test/watch",
};

test("the shared core hands over a manifest when no capability refuses one",
     async () => {
  // The default is not a refusal. Without a capability that publishes one,
  // nothing here knows a playlist from a file, and the address is forwarded.
  const loaded = loadMediaCore();
  const { kept, replies } = await coreMediaMessage(loaded.CoveMedia, MANIFEST_PILL,
                                                   { tab: { incognito: false, title: "Clip" } });

  assert.equal(kept, true);
  assert.deepEqual(replies, [{ ok: true }]);
  assert.equal(loaded.core.native.length, 1);
  assert.equal(loaded.core.native[0].url, MANIFEST_PILL.url);
});

test("an explicit factory capability's refusal is honoured", async () => {
  const seen = [];
  const loaded = loadMediaCore({ cookies: [{ name: "session", value: COOKIE_SENTINEL }] });
  const media = loaded.buildCoveMedia({
    rejectMediaTarget(value) { seen.push(value); return value.endsWith(".m3u8"); },
  });

  const { kept, replies } = await coreMediaMessage(media, MANIFEST_PILL,
                                                   { tab: { incognito: false, title: "Clip" } });

  assert.equal(kept, true);
  assert.deepEqual(replies, [
    { ok: false, reason: "unsupported", error: "Unsupported URL" },
  ]);
  assert.equal(loaded.core.native.length, 0);
  assert.deepEqual(loaded.core.cookies, []);
  assert.deepEqual(loaded.core.marked, []);
  // The capability was asked about the address that was about to be sent, not
  // about the message's raw url or the page it came from.
  assert.deepEqual(seen, [MANIFEST_PILL.url]);
  assert.equal(JSON.stringify(loaded.core.native).includes(COOKIE_SENTINEL), false);
});

test("a global capability's refusal is honoured with no explicit argument",
     async () => {
  const loaded = loadMediaCore({
    capability: { rejectMediaTarget: (value) => value.endsWith(".m3u8") },
  });

  const { replies } = await coreMediaMessage(loaded.CoveMedia, MANIFEST_PILL,
                                             { tab: { incognito: false, title: "Clip" } });

  assert.deepEqual(replies, [
    { ok: false, reason: "unsupported", error: "Unsupported URL" },
  ]);
  assert.equal(loaded.core.native.length, 0);
});

test("a capability that refuses nothing leaves the handoff alone", async () => {
  const loaded = loadMediaCore({
    capability: { rejectMediaTarget: () => false },
  });

  const { replies } = await coreMediaMessage(loaded.CoveMedia, MANIFEST_PILL,
                                             { tab: { incognito: false, title: "Clip" } });

  assert.deepEqual(replies, [{ ok: true }]);
  assert.equal(loaded.core.native.length, 1);
});

test("Firefox publishes no media-target refusal", async () => {
  // Firefox's capability is media-sites.js, and it has no such hook.
  const { context } = loadBackground();
  assert.equal(
    evalIn(context, "typeof CoveMediaCapability.rejectMediaTarget"),
    "undefined",
  );
});

test("Firefox still hands a playlist address to Cove from the pill", async () => {
  // The behavioural half of the line above, and the one that matters: Firefox
  // has stream handling, so a playlist address is something it can act on and
  // must keep acting on. A refusal written into the shared core rather than
  // published by a capability would take this with it, and the capability
  // assertion alone would not notice.
  const loaded = loadBackground();
  await settle();
  loaded.calls.native.length = 0;

  let done = null;
  const replied = new Promise((resolve) => { done = resolve; });
  loaded.events.message.emit(
    {
      type: "downloadMedia",
      url: "https://cdn.example.test/v/stream.m3u8",
      pageUrl: "https://neutral.example.test/watch",
    },
    completeSender({ tab: { incognito: false, id: 7, url: "https://neutral.example.test/watch", title: "Clip" } }),
    done,
  );
  const reply = await replied;

  assert.deepEqual(plain(reply), { ok: true });
  assert.equal(loaded.calls.native.length, 1);
  assert.equal(loaded.calls.native[0].url, "https://cdn.example.test/v/stream.m3u8");
  // The site adapter rejects a playlist extension for the *filename*, which is
  // a separate rule and is unchanged: the download is still named .mp4.
  assert.equal(loaded.calls.native[0].filename, "Clip.mp4");
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
      { incognito: false, url: "https://example.com/watch?v=abc123", title: "Clip" }
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
      { incognito: false, url: "https://example.com/downloads", title: "Downloads" }
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
// A production-shaped sender: the top-level page on `tab.url`, the frame that
// actually sent the message on `url`, and the frame id beside it. Passing
// `sender` replaces the whole record, so a test can also model a frame on a
// different origin, or an identity the browser did not supply at all.
function pillSender(pageUrl, sender) {
  if (sender !== undefined) return sender;
  return { tab: { incognito: false, id: 42, url: pageUrl }, url: pageUrl, frameId: 0 };
}

async function pillDownload(events, msg, sender) {
  let reply;
  events.message.emit(
    { type: "downloadMedia", url: msg.url, pageUrl: msg.pageUrl || msg.url },
    pillSender(msg.pageUrl || msg.url, sender),
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
// The browser supplies both identities for a content-script message: the
// top-level page on tab.url and the frame that actually sent it on url. A test
// that names only the page means "sent by that page's own top frame", which is
// what this fills in. A test that means "the browser could not identify this"
// builds the sender itself and is left exactly as written.
function completeSender(sender) {
  if (!sender || !sender.tab || typeof sender.tab.url !== "string") return sender;
  if (typeof sender.url === "string") return sender;
  return { frameId: 0, ...sender, url: sender.tab.url };
}

async function sendToBackground(events, msg, sender = {}) {
  let captured;
  events.message.emit(msg, completeSender(sender), (reply) => { captured = reply; });
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
    { tab: { incognito: false, id: 4, url: "https://news.example.test/x" } },
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
    { tab: { incognito: false, id: 4, url: "https://news.example.test/x" } },
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
    { tab: { incognito: false, id: 4 } },
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
const COOKIE_SENTINEL = "s08e-synth-cookie-9c41";

// Synthetic handoffs through the shipped VM runtime, never a browser store.
async function s08eHandoff(route, options = {}) {
  const url = "https://cdn.example.test/clip.mp4";
  const loaded = route === "stream" ? popupStreamFixture(options, url) : loadBackground(options);
  await settle();
  loaded.calls.native.length = 0;
  if (route === "interception") {
    loaded.events.downloadCreated.emit(freshItem({ url, filename: "clip.mp4" }));
  } else if (route === "menu") {
    await Promise.all(loaded.events.contextMenuClicked.emit(
      { menuItemId: "download-with-cove", linkUrl: url, pageUrl: "https://example.test/watch" },
      { incognito: false, id: 7, url: "https://example.test/watch" },
    ));
  } else {
    const { responses } = sendRuntimeMessage(loaded.events, {
      type: route === "pill" ? "downloadMedia" : "downloadStream",
      url, filename: "clip.mp4", pageUrl: "https://example.test/watch",
    }, route === "stream" ? popupSender() : pillSender("https://example.test/watch"));
    await settle();
    assert.equal(responses.length, 1, `${route}: one reply`);
    assert.equal(responses[0].ok, true, `${route}: accepted`);
  }
  await settle();
  const sent = downloadsOf(loaded.calls);
  assert.equal(sent.length, 1, `${route}: positive control sends once`);
  assert.equal(sent[0].url, url, `${route}: positive control preserves URL`);
  assert.equal(sent[0].filename, "clip.mp4", `${route}: positive control preserves filename`);
  return loaded;
}

for (const [id, route] of [["A1", "interception"], ["A2", "menu"], ["A3", "pill"]]) {
  test(`S08E ${id}: ${route} never queries cookies`, async () => {
    await s08eHandoff(route); // A cookie-free fixture must still hand off.
    const { calls } = await s08eHandoff(route, {
      cookies: [{ name: "sid", value: COOKIE_SENTINEL }],
    });
    assert.deepEqual(calls.cookies, [], `${id}: cookie API must not be queried`);
  });
}

test("S08E A4: popup stream omits the cookies key", async () => {
  const { calls } = await s08eHandoff("stream");
  assert.equal("cookies" in downloadsOf(calls)[0], false, "A4: cookies key must be absent");
});

test("S08E A6: shipped extension JavaScript contains no cookie API access", () => {
  const scan = (source) => [...source.matchAll(/\b(?:browser\s*\.\s*cookies|cookies\s*\.\s*getAll)\b/g)]
    .map((match) => match[0]);
  const scratch = fs.mkdtempSync("/tmp/cove-s08e1-scan-");
  try {
    const source = fs.readFileSync("extension/diagnostics.js", "utf8");
    assert.deepEqual(scan(source), [], "A6: diagnostics redaction words are ignored");
    fs.writeFileSync(`${scratch}/planted.js`, `${source}\nbrowser.cookies.getAll({url: 'https://example.test'});`);
    assert.ok(scan(fs.readFileSync(`${scratch}/planted.js`, "utf8")).length > 0,
              "A6: scanner detects a planted API call in a temporary copy");
    const matches = fs.readdirSync("extension", { recursive: true })
      .filter((name) => name.endsWith(".js"))
      .flatMap((name) => scan(fs.readFileSync(`extension/${name}`, "utf8"))
        .map((match) => `${name}: ${match}`));
    assert.deepEqual(matches, [], "A6: shipped cookie API accesses must be absent");
  } finally {
    fs.rmSync(scratch, { recursive: true, force: true });
  }
});

const S08E_ROUTES = ["interception", "menu", "pill", "stream"];
for (const route of S08E_ROUTES) {
  test(`S08E B1: ${route} exact payload keys and consent controls`, async () => {
    const allowed = await s08eHandoff(route, { dataConsent: [CONSENT] });
    const denied = await s08eHandoff(route, { dataConsent: [] });
    assert.equal(downloadsOf(allowed.calls)[0].userAgent, REAL_UA, "B1: consent grants user-agent");
    assert.equal("userAgent" in downloadsOf(denied.calls)[0], false, "B1: withheld consent omits user-agent");
    const keys = ["action", "filename", "referrer", "url"];
    if (route !== "menu") keys.push("fileSize");
    for (const [loaded, uaKeys] of [[allowed, ["userAgent"]], [denied, []]]) {
      assert.deepEqual(Object.keys(downloadsOf(loaded.calls)[0]).sort(), [...keys, ...uaKeys].sort(),
                       `B1: ${route} exact keys must omit cookies`);
    }
  });

  test(`S08E ${route === "stream" ? "B2s GUARD" : "B2"}: ${route} sends no sentinel`, async () => {
    const { calls } = await s08eHandoff(route, {
      cookies: [{ name: "sid", value: COOKIE_SENTINEL }],
    });
    assert.equal(JSON.stringify(calls.native).includes(COOKIE_SENTINEL), false,
                 `B2: ${route} native payload must not contain sentinel`);
  });

  test(`S08E B3: ${route} has no nested cookie or header data`, async () => {
    function forbidden(value, path = "$", hits = []) {
      if (typeof value === "string" && value.includes(COOKIE_SENTINEL)) hits.push(path);
      if (value && typeof value === "object") {
        for (const [key, child] of Object.entries(value)) {
          if (/cookie|header/i.test(key)) hits.push(`${path}.${key}`);
          forbidden(child, `${path}.${key}`, hits);
        }
      }
      return hits;
    }
    assert.deepEqual(forbidden({ nested: { headers: { Cookie: COOKIE_SENTINEL } } }),
                     ["$.nested.headers", "$.nested.headers.Cookie", "$.nested.headers.Cookie"],
                     "B3: walker detects planted nested keys and sentinel");
    assert.deepEqual(forbidden({ nested: { url: "https://example.test" } }), [],
                     "B3: walker accepts ordinary nested data");
    const { calls } = await s08eHandoff(route, {
      cookies: [{ name: "sid", value: COOKIE_SENTINEL }],
    });
    assert.deepEqual(forbidden(calls.native), [], `B3: ${route} recursive payload must be clean`);
  });

  for (const [id, field] of [["N1", "diagnostics"], ["N5", "console"]]) {
    test(`S08E ${id} GUARD: ${route} ${field} contains no sentinel`, async () => {
      const loaded = await s08eHandoff(route, {
        cookies: [{ name: "sid", value: COOKIE_SENTINEL }],
      });
      const report = await diagReport(loaded.events);
      assert.ok(report.includes("native_message_sent"), `${id}: diagnostic recorder is active`);
      assert.ok(loaded.calls.console.length > 0, `${id}: console recorder is active`);
      const captured = field === "diagnostics"
        ? report + JSON.stringify(loaded.store.data.coveDiag)
        : JSON.stringify(loaded.calls.console);
      assert.equal(captured.includes(COOKIE_SENTINEL), false, `${id}: ${field} must not contain sentinel`);
    });
  }
}

for (const oracle of ["collection", "drop log"]) {
  test(`S08E B4: oversized jar causes no ${oracle}`, async () => {
    const { calls } = await s08eHandoff("interception", { cookies: jarOfSize(COOKIE_LIMIT + 1) });
    if (oracle === "collection") {
      assert.deepEqual(calls.cookies, [], "B4: oversized jar must not be collected");
    } else {
      assert.equal(JSON.stringify(calls.console).includes("cookie header exceeded"), false,
                   "B4: oversized jar must not produce a drop log");
    }
  });
}

test("S08E D1 GUARD: native ok strictly precedes cancel then erase then notification", async () => {
  let release;
  let entered = 0;
  const gate = new Promise((resolve) => { release = resolve; });
  const loaded = loadBackground({ nativeHook: async (message) => {
    if (message.action === "download") { entered += 1; await gate; }
  } });
  await settle();
  loaded.calls.order.length = 0;
  loaded.events.downloadCreated.emit(freshItem());
  await settle();
  assert.equal(entered, 1, "D1: native reply suspension reached");
  assert.deepEqual(loaded.calls.order, ["native:sent"], "D1: browser untouched before acknowledgement");
  release();
  await settle();
  assert.equal(downloadsOf(loaded.calls).length, 1);
  assert.deepEqual(loaded.calls.order, ["native:sent", "native:ok", "cancel", "erase", "notification"]);
});

for (const [name, settings, item] of [
  ["disabled", { enabled: false }, {}],
  ["blob", {}, { url: "blob:https://example.test/blob" }],
  ["data", {}, { url: "data:application/zip;base64,AA==" }],
  ["excluded domain", { excludedDomains: ["example.test"] }, {}],
  ["extension allowlist", { interceptExtensions: ["mp4"] }, {}],
]) {
  test(`S08E D2 GUARD: ${name} filter leaves the browser download alone`, async () => {
    await s08eHandoff("interception");
    const { calls, events } = loadBackground({ settings });
    await settle();
    events.downloadCreated.emit(freshItem(item));
    await settle();
    assert.deepEqual(downloadsOf(calls), [], `D2: ${name} sends nothing`);
    assert.deepEqual(calls.cancel, []);
    assert.deepEqual(calls.erase, []);
  });
}

// H1 retry and H3 menu fallback are already covered by the pre-existing
// "a failed send is retried..." and "context menu falls back..." tests.
for (const mode of ["error reply", "transport rejection"]) {
  test(`S08E H2 H5 GUARD: observed ${mode} keeps browser ownership and permits retry`, async () => {
    let attempts = 0;
    const loaded = loadBackground({ nativeResult: (message) => {
      if (message.action !== "download" || ++attempts > 1) return { status: "ok" };
      if (mode === "transport rejection") throw new Error("synthetic lost acknowledgement");
      return { status: "error", message: "synthetic refusal" };
    } });
    await settle();
    loaded.events.downloadCreated.emit(freshItem());
    await settle();
    assert.equal(downloadsOf(loaded.calls).length, 1, "H5: mock host observed request");
    assert.deepEqual(loaded.calls.cancel, [], "H5: no cancel after failed acknowledgement");
    assert.deepEqual(loaded.calls.erase, [], "H5: no erase after failed acknowledgement");
    assert.equal(evalIn(loaded.context, "recentIntercepted.size"), 0, "H5: failed mark cleared");
    loaded.events.downloadCreated.emit(freshItem());
    await settle();
    assert.equal(downloadsOf(loaded.calls).length, 2, "H5: retry reaches host");
    assert.deepEqual(loaded.calls.cancel, [90], "H5: positive acknowledgement allows cancellation");
  });
}

function jarOfSize(total) {
  return [{ name: "sid", value: "c".repeat(Math.max(0, total - 4)) }];
}

function freshItem(overrides = {}) {
  return {
    incognito: false,
    id: 90,
    url: "https://example.test/big.zip",
    filename: "big.zip",
    state: "in_progress",
    startTime: new Date().toISOString(),
    totalBytes: 2_000_000,
    ...overrides,
  };
}

test("a cookie jar within the former handoff limit is ignored", async () => {
  const jar = jarOfSize(100);
  const { calls, events } = loadBackground({ cookies: jar });
  await settle();
  calls.native.length = 0;

  events.downloadCreated.emit(freshItem());
  await settle();

  const sent = calls.native.find((m) => m.action === "download");
  assert.ok(sent, "the download still reaches Cove");
  assert.equal("cookies" in sent, false);
});

test("an oversized cookie jar is ignored during handoff", async () => {
  const { calls, events } = loadBackground({ cookies: jarOfSize(COOKIE_LIMIT + 1) });
  await settle();
  calls.native.length = 0;

  events.downloadCreated.emit(freshItem());
  await settle();

  const sent = calls.native.find((m) => m.action === "download");
  assert.ok(sent, "the download must still be handed to Cove");
  assert.equal("cookies" in sent, false);
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
    tabs: [{ incognito: false, id: 7 }],
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
    tabs: [{ incognito: false, id: 7 }],
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

test("IID-S-adapt-1 RED: all inherited ids are forgotten, including in-progress records", async () => {
  await ownSession(false);
  // Approved policy: persisted ids are bookkeeping, never current-session ownership.
  const { context, store } = loadBackground({
    settings: { enabled: true },
    storedIntercepted: [1, 2, 3],
    downloadSearch: ({ id }) => (id === 2 ? [{ incognito: false, id: 2, state: "in_progress" }] : []),
  });
  await settle();

  // Arrays built inside the vm realm are not reference-equal to host ones.
  assert.deepEqual(Array.from(evalIn(context, '[...interceptedIds]')), [], "IID-S-adapt-1: forget inherited ids");
  assert.deepEqual(Array.from(store.data._interceptedIds), []);
});

test("IID-S-adapt-2 RED: pruning forgets an inherited terminal id without erasing", async () => {
  await ownSession(false);
  // Approved ownership policy leaves earlier-session history with the browser.
  const { context, calls, store } = loadBackground({
    settings: { enabled: true },
    storedIntercepted: [5],
    downloadSearch: ({ id }) => [{ incognito: false, id, state: "interrupted" }],
  });
  await settle();

  // Objects built inside the vm realm are not reference-equal to host ones.
  assert.deepEqual(calls.erase, [], "IID-S-adapt-2: no inherited erase");
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

test("IID-S-adapt-3 RED: inherited ids are forgotten even when erase would fail", async () => {
  await ownSession(false);
  // An inherited id authorizes no erase; an erase failure cannot retain it.
  const { context } = loadBackground({
    settings: { enabled: true },
    storedIntercepted: [7],
    downloadSearch: ({ id }) => [{ incognito: false, id, state: "complete" }],
    eraseThrows: true,
  });
  await settle();

  assert.deepEqual(Array.from(evalIn(context, "[...interceptedIds]")), [], "IID-S-adapt-3: forget unowned id");
});

test("IID-S-adapt-4 RED: erase during hydration cannot retain other inherited ids", async () => {
  await ownSession(false);
  // Hydration forgets every inherited id, not only the id in the erase event.
  // The stored set arrives asynchronously. An erase handled before it lands
  // would operate on an empty Set, and the id would come back on the next
  // startup as though it had never been cleaned up.
  const { events, context, store } = loadBackground({
    settings: { enabled: true },
    storedIntercepted: [11, 12],
    downloadSearch: ({ id }) => [{ incognito: false, id, state: "in_progress" }],
    slowInterceptedIds: true,
  });

  events.downloadErased.emit(11);   // fires before hydration completes
  await settle();

  assert.deepEqual(Array.from(evalIn(context, "[...interceptedIds]")), [], "IID-S-adapt-4: forget inherited hydration remainder");
  assert.deepEqual(Array.from(store.data._interceptedIds), []);
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
    { tab: { incognito: false, id: 3, url: "https://example.test/watch" } },
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

const CHROME_TAB = { incognito: false, id: 7, url: "https://example.test/watch", title: "Clip" };

async function chromeMediaMessage(msg, options = {}) {
  const loaded = chromeWorker(options);
  await settle();
  loaded.calls.native.length = 0;

  let reply;
  let resolved = null;
  const done = new Promise((resolve) => { resolved = resolve; });
  const kept = loaded.events.message.emit(msg, completeSender({ tab: CHROME_TAB }), (r) => {
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

// ---- The pill's half of Chrome's media-target policy ----
//
// media-chrome.js publishes one refusal, and the context menu has consulted it
// since it was written. The pill's downloadMedia message is the other way a
// media address reaches the native host, and these pin it to the same policy.
// Everything below drives the real background listener, not the refusal helper
// on its own: what matters is that the address is refused on the route the
// pill actually uses.

const MANIFEST_TARGETS = [
  "https://cdn.example.test/v/stream.m3u8",
  "https://cdn.example.test/v/stream.m3u",
  "https://cdn.example.test/v/stream.mpd",
  // Case is not part of the name: a server is free to serve either.
  "https://cdn.example.test/v/STREAM.M3U8",
  // The address is parsed, so what follows the path cannot hide the suffix.
  "https://cdn.example.test/v/stream.m3u8?token=abc",
  "https://cdn.example.test/v/stream.m3u8#t=10",
  "https://cdn.example.test/v/stream.m3u8?token=abc#t=10",
];

for (const url of MANIFEST_TARGETS) {
  test(`Chrome refuses a pill handoff for the manifest ${url}`, async () => {
    const m = await chromeMediaMessage({
      type: "downloadMedia",
      url,
      pageUrl: "https://example.test/watch",
      requestId: "aaaabbbb",
    });
    await m.done;

    assert.equal(m.calls.native.length, 0, "nothing may reach the native host");
    assert.deepEqual(plain(m.reply()), {
      ok: false, reason: "unsupported", error: "Unsupported URL",
    });
  });
}

// Positive controls. These are the addresses the policy must leave alone, and
// they are what a refusal written too broadly would take with it.
for (const [label, url] of [
  ["a direct file", "https://cdn.example.test/v/clip.mp4"],
  ["an extensionless resource", "https://cdn.example.test/v/clip"],
  // The suffix is in the query, not the path. A search rather than a parse
  // would refuse this ordinary MP4.
  ["an mp4 whose query mentions a playlist",
   "https://cdn.example.test/v/clip.mp4?src=playlist.m3u8"],
  ["an mp4 whose path merely contains the text",
   "https://cdn.example.test/m3u8/clip.mp4"],
]) {
  test(`Chrome still hands over ${label}`, async () => {
    const m = await chromeMediaMessage({
      type: "downloadMedia", url, pageUrl: "https://example.test/watch",
    });
    await m.done;

    assert.deepEqual(plain(m.reply()), { ok: true });
    assert.equal(m.calls.native.length, 1);
    assert.equal(m.calls.native[0].url, url);
  });
}

test("a refused pill handoff leaves nothing behind it", async () => {
  const m = await chromeMediaMessage(
    {
      type: "downloadMedia",
      url: "https://cdn.example.test/v/stream.m3u8",
      pageUrl: "https://example.test/watch",
      requestId: "ccccdddd",
    },
    { cookies: [{ name: "session", value: COOKIE_SENTINEL }] },
  );
  await m.done;

  assert.equal(m.calls.native.length, 0, "no native download");
  assert.deepEqual(m.calls.cookies, [], "no cookie lookup for a refused target");
  assert.deepEqual(m.browserDownloads, [], "no browser fallback download");
  assert.deepEqual(m.calls.notifications, [], "nothing succeeded, so nothing is announced");
  assert.equal(
    evalIn(m.context, "recentIntercepted.has('https://cdn.example.test/v/stream.m3u8')"),
    false,
    "a refused address must not be marked as intercepted",
  );
  assert.equal(JSON.stringify(m.calls.native).includes(COOKIE_SENTINEL), false);
});

test("a refusal does not poison the request that follows it", async () => {
  const loaded = chromeWorker();
  await settle();
  loaded.calls.native.length = 0;

  await chromePillSend(loaded, "https://cdn.example.test/v/stream.m3u8");
  assert.equal(loaded.calls.native.length, 0);

  await chromePillSend(loaded, "https://cdn.example.test/v/clip.mp4");
  assert.equal(loaded.calls.native.length, 1);
  assert.equal(loaded.calls.native[0].url, "https://cdn.example.test/v/clip.mp4");
});

test("a refused pill handoff answers once and keeps the channel open", async () => {
  const loaded = chromeWorker();
  await settle();

  const replies = [];
  let resolve = null;
  const answered = new Promise((r) => { resolve = r; });
  const kept = loaded.events.message.emit(
    {
      type: "downloadMedia",
      url: "https://cdn.example.test/v/stream.m3u8",
      pageUrl: "https://example.test/watch",
    },
    { tab: CHROME_TAB },
    (reply) => { replies.push(reply); resolve(); },
  );
  await answered;
  await settle();

  // true is what the listener must return for an answer that arrives later,
  // and it is what the pill's sendMessage is waiting on.
  assert.deepEqual(kept, [true]);
  assert.equal(replies.length, 1, "the pill must be answered exactly once");
});

test("a refused media address never becomes a download of its page", async () => {
  // Everything a fallback would need is present: a page address on the
  // message, a page address on the sender, and a titled tab.
  const m = await chromeMediaMessage({
    type: "downloadMedia",
    url: "https://cdn.example.test/v/stream.m3u8",
    pageUrl: "https://example.test/watch",
  });
  await m.done;

  assert.equal(m.calls.native.length, 0);
  assert.deepEqual(m.browserDownloads, []);
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
  const sender = completeSender({ tab: { incognito: false, id: 7, url: "https://neutral.example.test/watch",
                                         title: "Neutral clip" } });

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
    { cookies: [{ name: "session", value: COOKIE_SENTINEL }] },
  );
  await m.done;
  await new Promise((resolve) => setTimeout(resolve, 1100));

  const serialised = JSON.stringify(m.store.data.coveDiag || []);
  for (const secret of [COOKIE_SENTINEL, "s3cr3t-token-value", "Clip"]) {
    assert.ok(!serialised.includes(secret),
              `${secret} must not reach the diagnostic ring`);
  }
  assert.ok(serialised.includes("deadbeef"), "the request id must correlate");
  assert.equal(downloadsOf(m.calls).length, 1, "the eligible pill still hands off");
  assert.deepEqual(m.calls.cookies, [], "the eligible pill never queries cookies");
  assert.equal(JSON.stringify(m.calls.native).includes(COOKIE_SENTINEL), false);
});

// ---- Deduplication ----

async function chromePillSend(loaded, url, requestId = "aaaaaaaa") {
  let done = null;
  const replied = new Promise((resolve) => { done = resolve; });
  loaded.events.message.emit(
    { type: "downloadMedia", url, pageUrl: "https://example.test/watch", requestId },
    completeSender({ tab: CHROME_TAB }),
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
    incognito: false,
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
    incognito: false,
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
    incognito: false,
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
    incognito: false,
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

// ---- The minimum-size filter, as the store copy describes it ----
//
// PRIVACY.md and docs/chrome-store-listing.md now say the minimum applies
// "whenever the browser reports a size", and say so because the filter is
// `typeof size === "number" && size > 0 && size < minSizeBytes`. An unreported
// size is deliberately not treated as zero: a response that declares no length
// would otherwise never be intercepted at all. That carve-out is the part a
// user-facing claim can most easily overstate, so both halves are pinned here.

const SIZED_SETTINGS = {
  enabled: true,
  minSizeBytes: 1_000_000,
  excludedDomains: [],
  interceptExtensions: [".zip"],
  mediaPillEnabled: true,
};

test("a download under the minimum size is left to the browser", async () => {
  const { calls, events } = loadBackground({ settings: SIZED_SETTINGS });
  await settle();
  calls.native.length = 0;

  events.downloadCreated.emit({
    incognito: false,
    id: 41,
    url: "https://example.test/small.zip",
    filename: "small.zip",
    state: "in_progress",
    startTime: new Date().toISOString(),
    totalBytes: 500_000,
  });
  await settle();

  assert.deepEqual(downloadsOf(calls), [], "below the minimum, so not handed over");
  assert.deepEqual(calls.cancel, []);
});

test("a download the browser reports no size for is still intercepted",
     async () => {
  const { calls, events } = loadBackground({ settings: SIZED_SETTINGS });
  await settle();
  calls.native.length = 0;

  // 0 is what the browser reports when the response declared no length. It is
  // not a 0-byte file, and treating it as one would silently disable
  // interception for every chunked response.
  events.downloadCreated.emit({
    incognito: false,
    id: 42,
    url: "https://example.test/unknown.zip",
    filename: "unknown.zip",
    state: "in_progress",
    startTime: new Date().toISOString(),
    totalBytes: 0,
  });
  await settle();

  assert.equal(downloadsOf(calls).length, 1,
               "an unreported size is not filtered by the minimum");
  assert.equal(downloadsOf(calls)[0].url, "https://example.test/unknown.zip");
  assert.equal(downloadsOf(calls)[0].fileSize, 0);
});

// ---- Legacy stream dispatch (Tab 2C) ----
//
// downloadStream is the popup's original message for a detected HLS stream.
// The stream detector is Firefox-only and always was, but the forwarding body
// that answered this message sat in the shared background and would happily
// hand any HTTP(S) address to the native host from a Chrome build that has no
// detector, no popup section and no way for a user to reach it. These pin the
// two halves of the split: Firefox keeps the behaviour it shipped, and Chrome
// answers the message without a route behind it.

// Emits one runtime message the way the browser would and reports everything
// the dispatcher is contractually required to get right: what it answered, how
// many times it answered, and what it returned to the browser. Returning true
// keeps the response channel open, undefined means it already answered, and
// false means nothing in this build owns the message - which, for a message the
// popup is still allowed to send, would leave the caller waiting on a channel
// that just closes.
function sendRuntimeMessage(events, msg, sender = {}) {
  const responses = [];
  const returned = events.message.emit(msg, sender, (value) => { responses.push(value); });
  return { responses, returned: returned[0] };
}

const downloadsOf = (calls) => calls.native.filter((m) => m.action === "download");

test("Chrome answers the legacy stream download instead of forwarding it",
     async () => {
  const { calls, events, browserDownloads } = chromeWorker();
  await settle();
  calls.native.length = 0;  // Ignore the startup ping.
  calls.notifications.length = 0;

  const { responses } = sendRuntimeMessage(events, {
    type: "downloadStream",
    url: "http://127.0.0.1:9/live.m3u8",
    filename: "live.mp4",
  });
  await settle();

  assert.equal(responses.length, 1, "exactly one answer");
  assert.equal(responses[0].ok, false);
  assert.equal(responses[0].reason, "unsupported");
  assert.deepEqual(downloadsOf(calls), [], "no native download may be sent");
  assert.deepEqual(browserDownloads, [], "no browser download may stand in for it");
  assert.deepEqual(calls.notifications, [], "nothing succeeded, so nothing is announced");
});

test("the Chrome refusal is not a downloadMedia alias", async () => {
  const { calls, events } = chromeWorker();
  await settle();
  calls.native.length = 0;

  sendRuntimeMessage(events, {
    type: "downloadStream",
    url: "http://127.0.0.1:9/live.m3u8",
    filename: "live.mp4",
  });
  await settle();

  // downloadMedia is the pill's message and reaches the native host. Routing a
  // refused legacy action into it would restore the exact send this removes.
  assert.deepEqual(calls.native, []);
});

test("a build with no media runtime still answers the legacy stream download",
     async () => {
  const { calls, events } = loadBackground({ media: false });
  await settle();
  calls.native.length = 0;

  const { responses } = sendRuntimeMessage(events, {
    type: "downloadStream",
    url: "http://127.0.0.1:9/live.m3u8",
    filename: "live.mp4",
  });
  await settle();

  assert.equal(responses.length, 1);
  assert.equal(responses[0].ok, false);
  assert.equal(responses[0].reason, "unsupported");
  assert.deepEqual(downloadsOf(calls), []);
});

test("a malformed legacy stream payload closes its channel without throwing",
     async () => {
  const { calls, events } = chromeWorker();
  await settle();
  calls.native.length = 0;

  for (const msg of [
    { type: "downloadStream" },
    { type: "downloadStream", url: 42 },
    { type: "downloadStream", url: "ftp://example.test/live.m3u8" },
    { type: "downloadStream", url: "http://127.0.0.1:9/live.m3u8", filename: null },
  ]) {
    const { responses, returned } = sendRuntimeMessage(events, msg);
    assert.equal(responses.length, 1, "one answer for " + JSON.stringify(msg));
    assert.equal(responses[0].ok, false);
    // Answered synchronously, so the browser must not be told to hold the
    // channel open for a second reply that never comes.
    assert.equal(returned, undefined);
  }
  await settle();
  assert.deepEqual(downloadsOf(calls), []);
});

test("Firefox still forwards a detected stream with the message it shipped",
     async () => {
  const { calls, events } = popupStreamFixture();
  await settle();
  calls.native.length = 0;

  const { responses, returned } = sendRuntimeMessage(events, {
    type: "downloadStream",
    url: "https://example.test/live.m3u8",
    filename: "live.mp4",
  }, popupSender());
  assert.equal(returned, true, "the response channel stays open for the native reply");
  await settle();

  assert.deepEqual(plain(calls.native), [{
    action: "download",
    url: "https://example.test/live.m3u8",
    filename: "live.mp4",
    referrer: "",
    fileSize: 0,
    userAgent:
      "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
  }]);
  assert.equal(responses.length, 1, "answered exactly once");
  assert.deepEqual(plain(responses[0]), { ok: true });
});

test("Firefox falls back to an empty stream filename as it did", async () => {
  const { calls, events } = popupStreamFixture();
  await settle();
  calls.native.length = 0;

  sendRuntimeMessage(events, {
    type: "downloadStream",
    url: "https://example.test/live.m3u8",
  }, popupSender());
  await settle();

  assert.equal(calls.native.length, 1);
  assert.equal(calls.native[0].filename, "");
});

test("Firefox refuses a stream URL that is not HTTP(S)", async () => {
  const { calls, events } = popupStreamFixture();
  await settle();
  calls.native.length = 0;

  const { responses, returned } = sendRuntimeMessage(events, {
    type: "downloadStream",
    url: "ftp://example.test/live.m3u8",
    filename: "live.mp4",
  }, popupSender());

  assert.equal(returned, undefined);
  assert.equal(responses.length, 1);
  assert.deepEqual(plain(responses[0]), { ok: false, error: "Unsupported stream URL" });
  assert.deepEqual(calls.native, []);
});

test("Firefox reports a malformed native reply as unavailable", async () => {
  const { events } = popupStreamFixture({ nativeResult: null });
  await settle();

  const { responses } = sendRuntimeMessage(events, {
    type: "downloadStream",
    url: "https://example.test/live.m3u8",
    filename: "live.mp4",
  }, popupSender());
  await settle();

  assert.equal(responses.length, 1);
  assert.deepEqual(plain(responses[0]), { ok: false, error: "Cove is unavailable" });
});

test("Firefox reports a rejected native send as unavailable", async () => {
  const { events } = popupStreamFixture({
    nativeResult: () => Promise.reject(new Error("Native host has exited.")),
  });
  await settle();

  const { responses } = sendRuntimeMessage(events, {
    type: "downloadStream",
    url: "https://example.test/live.m3u8",
    filename: "live.mp4",
  }, popupSender());
  await settle();

  assert.equal(responses.length, 1, "a rejection must still produce one answer");
  assert.equal(responses[0].ok, false);
  assert.equal(responses[0].error, "Native host has exited.");
});

// ---- Tab 3.3: an unknown browser size at the native payload boundary ----
//
// Firefox reports totalBytes -1 for a download whose length it does not know
// yet, and `totalBytes || 0` forwarded that -1 verbatim. The primary's schema
// takes a non-negative integer, so the download was refused outright. Unknown
// is spelled 0 in this protocol, and every value that is not a usable byte
// count is reported unknown rather than coerced, repaired, or guessed at.
//
// Admission is deliberately untouched: the minimum-size filter still applies
// only when the browser states a usable positive size. A download reported as
// unknown is still handed over, and may turn out to be smaller than the
// configured minimum. That limitation is accepted, and it is pinned by the
// tests below rather than papered over.

const FIREFOX_UNKNOWN_SIZE = -1;
const LARGE_BYTES = 3_145_728;
const DEFAULT_MINIMUM_BYTES = 1024 * 1024;

// A fresh, eligible, in-progress download on the shipped defaults. totalBytes
// is the only variable, so nothing but the size can decide the outcome.
function sizedItem(totalBytes, extra = {}) {
  return {
    incognito: false,
    id: 900,
    url: "https://example.test/payload.zip",
    filename: "payload.zip",
    state: "in_progress",
    startTime: new Date().toISOString(),
    totalBytes,
    ...extra,
  };
}

test("the shipped defaults hand Firefox's unknown size over as 0", async () => {
  // No settings argument: this is the real default path, minimum included.
  const { calls, events, context } = loadBackground();
  await settle();
  calls.native.length = 0;

  assert.equal(evalIn(context, "settings.minSizeBytes"), DEFAULT_MINIMUM_BYTES,
               "the default minimum is in force, not disabled for this test");

  events.downloadCreated.emit(sizedItem(FIREFOX_UNKNOWN_SIZE));
  await settle();

  const sent = downloadsOf(calls);
  assert.equal(sent.length, 1, "an unstated size is not filtered by the minimum");
  assert.ok(Object.is(sent[0].fileSize, 0),
            "-1 means the length is unknown, and the protocol spells that 0");
});

test("an unknown size survives serialization as numeric 0", async () => {
  const { calls, events } = loadBackground();
  await settle();
  calls.native.length = 0;

  events.downloadCreated.emit(sizedItem(FIREFOX_UNKNOWN_SIZE));
  await settle();

  // JSON.stringify turns NaN and Infinity into null, so asserting only on the
  // wire form would hide a normalizer that let them through.
  const wire = JSON.parse(JSON.stringify(downloadsOf(calls)[0]));
  assert.equal(wire.fileSize, 0);
  assert.equal(typeof wire.fileSize, "number");
});

test("size metadata that is not a usable byte count is handed over as 0",
     async () => {
  // Every value here is admitted by the unchanged minimum-size filter and then
  // reaches the payload. The fractional case is deliberately above the minimum
  // so the filter admits it: a fractional value below the minimum is filtered
  // by the existing known-size branch, which this repair does not touch.
  const notByteCounts = [
    ["negative one", FIREFOX_UNKNOWN_SIZE],
    ["a larger negative", -4096],
    ["negative zero", -0],
    ["NaN", NaN],
    ["Infinity", Infinity],
    ["-Infinity", -Infinity],
    ["true", true],
    ["false", false],
    ["a numeric string", "2048"],
    ["a fractional byte count", 3_145_728.5],
    ["an unsafe integer", Number.MAX_SAFE_INTEGER + 2],
    ["null", null],
  ];

  for (const [label, value] of notByteCounts) {
    const { calls, events } = loadBackground();
    await settle();
    calls.native.length = 0;

    events.downloadCreated.emit(sizedItem(value));
    await settle();

    const sent = downloadsOf(calls);
    assert.equal(sent.length, 1, `handed over once for ${label}`);
    assert.ok(Object.is(sent[0].fileSize, 0),
              `${label} is not a byte count, so the size is unknown`);
    const wire = JSON.parse(JSON.stringify(sent[0]));
    assert.equal(wire.fileSize, 0, `${label} serializes as 0`);
  }
});

test("a missing size is handed over as 0", async () => {
  const { calls, events } = loadBackground();
  await settle();
  calls.native.length = 0;

  const item = sizedItem(0);
  delete item.totalBytes;
  events.downloadCreated.emit(item);
  await settle();

  assert.equal(downloadsOf(calls).length, 1);
  assert.ok(Object.is(downloadsOf(calls)[0].fileSize, 0));
});

test("a known positive size reaches the native host unchanged", async () => {
  const { calls, events } = loadBackground();
  await settle();
  calls.native.length = 0;

  events.downloadCreated.emit(sizedItem(LARGE_BYTES));
  await settle();

  const sent = downloadsOf(calls);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].fileSize, LARGE_BYTES);
  assert.deepEqual(
    Object.keys(sent[0]).sort(),
    ["action", "fileSize", "filename", "referrer", "url", "userAgent"],
    "the message shape is unchanged by the repair",
  );
  assert.ok(!("requestId" in sent[0]), "no request correlation is added");
  assert.ok(!("statedSize" in sent[0]), "no header-provided size is added");
});

test("a large safe integer size is not rounded or truncated", async () => {
  const { calls, events } = loadBackground();
  await settle();
  calls.native.length = 0;

  events.downloadCreated.emit(sizedItem(Number.MAX_SAFE_INTEGER));
  await settle();

  assert.equal(downloadsOf(calls)[0].fileSize, Number.MAX_SAFE_INTEGER);
});

test("the minimum still filters a size the browser does state", async () => {
  const { calls, events } = loadBackground();
  await settle();
  calls.native.length = 0;

  events.downloadCreated.emit(sizedItem(20724));
  await settle();

  assert.deepEqual(downloadsOf(calls), [],
                   "a stated size below the minimum is left to the browser");
  assert.deepEqual(calls.cancel, []);
});

test("a stated size equal to the minimum is handed over", async () => {
  const { calls, events } = loadBackground();
  await settle();
  calls.native.length = 0;

  events.downloadCreated.emit(sizedItem(DEFAULT_MINIMUM_BYTES));
  await settle();

  assert.equal(downloadsOf(calls).length, 1, "the filter is strictly below");
  assert.equal(downloadsOf(calls)[0].fileSize, DEFAULT_MINIMUM_BYTES);
});

test("Chrome keeps its stated sizes and normalizes an unstated one", async () => {
  for (const [stated, expected] of [[LARGE_BYTES, LARGE_BYTES], [0, 0],
                                    [FIREFOX_UNKNOWN_SIZE, 0]]) {
    const { calls, events } = chromeWorker();
    await settle();
    calls.native.length = 0;

    events.downloadCreated.emit(sizedItem(stated));
    await settle();

    const sent = downloadsOf(calls);
    assert.equal(sent.length, 1, `handed over once for ${String(stated)}`);
    assert.ok(Object.is(sent[0].fileSize, expected),
              `${String(stated)} is sent as ${expected}`);
  }

  const { calls, events } = chromeWorker();
  await settle();
  calls.native.length = 0;
  events.downloadCreated.emit(sizedItem(20724));
  await settle();
  assert.deepEqual(downloadsOf(calls), [],
                   "Chrome's known-small filtering is unchanged");
});

test("normalizing an unknown size costs no lookup, listener or later message",
     async () => {
  let searches = 0;
  const { calls, events, context, store } = loadBackground({
    downloadSearch: () => { searches += 1; return []; },
  });
  await settle();
  calls.native.length = 0;
  const keysBefore = Object.keys(store.data).sort();

  // media-sites.js registers exactly one onHeadersReceived listener for media
  // detection on Firefox. That is pre-existing and unrelated to downloads; the
  // claim here is that this repair adds none of its own.
  const headerListeners =
    evalIn(context, "browser.webRequest.onHeadersReceived.listeners.length");

  events.downloadCreated.emit(sizedItem(FIREFOX_UNKNOWN_SIZE));
  await settle();

  assert.equal(downloadsOf(calls).length, 1);
  assert.equal(searches, 0, "the size is not looked up after the fact");
  assert.equal(
    evalIn(context, "browser.webRequest.onHeadersReceived.listeners.length"),
    headerListeners,
    "no header listener is added to decide a download's size",
  );

  // Ten further turns of the loop: a deferred decision, a timer or a retry
  // would surface as a second attempt here.
  for (let i = 0; i < 10; i += 1) await settle();
  assert.equal(downloadsOf(calls).length, 1, "exactly one native attempt, ever");
  assert.equal(searches, 0);

  // The accepted download's id is persisted by the committed ownership code.
  // Nothing beyond that is written: no pending-decision state, no size cache.
  const added = Object.keys(store.data).filter((k) => !keysBefore.includes(k));
  assert.deepEqual(added.sort(), ["_interceptedIds"],
                   "no new storage key is introduced for size admission");
});

// ---- Excluded domains and the in-page pill (issue #16 A1) ----
//
// The pill asks a different question from the popup: not "what are the
// settings" but "may I show on this page, in this frame". The answer is
// computed per request from the browser's own sender record and is never
// stored, so these drive the real onMessage listener rather than reaching
// into the background's state.

// One pill permission request through the real listener, with a
// production-shaped sender unless the test supplies its own.
async function pillPermission(events, pageUrl, sender) {
  let reply;
  events.message.emit(
    { type: "getSettings", forPill: true },
    pillSender(pageUrl, sender),
    (response) => { reply = response; }
  );
  await settle();
  await settle();
  return reply;
}

// A sender whose top-level page and requesting frame are different origins,
// which is what an embedded player produces and what `all_frames: true` in
// both manifests makes reachable.
function framedSender(pageUrl, frameUrl) {
  return { tab: { incognito: false, id: 7, url: pageUrl }, url: frameUrl, frameId: 3 };
}

test("an excluded page is refused the pill", async () => {
  const { events } = loadBackground({
    settings: { excludedDomains: ["example.test"] },
  });
  const reply = await pillPermission(events, "https://example.test/watch");
  assert.equal(reply.pillAllowed, false);
});

test("a subdomain of an excluded domain is refused the pill", async () => {
  const { events } = loadBackground({
    settings: { excludedDomains: ["example.test"] },
  });
  const reply = await pillPermission(events, "https://sub.example.test/watch");
  assert.equal(reply.pillAllowed, false);
});

test("a lookalike hostname is not caught by an exclusion", async () => {
  const { events } = loadBackground({
    settings: { excludedDomains: ["example.test"] },
  });
  // Neither of these is example.test and neither is below it. The existing
  // matcher is exact-host or dot-suffix, and this is what says so.
  for (const page of ["https://notexample.test/watch",
                      "https://example.test.attacker.test/watch"]) {
    const reply = await pillPermission(events, page);
    assert.equal(reply.pillAllowed, true, page);
  }
});

test("an excluded top-level page refuses its allowed embedded player", async () => {
  const { events } = loadBackground({
    settings: { excludedDomains: ["example.test"] },
  });
  const reply = await pillPermission(
    events, null,
    framedSender("https://example.test/watch", "https://player.other.test/embed"),
  );
  assert.equal(reply.pillAllowed, false);
});

test("an excluded frame is refused inside an allowed page", async () => {
  const { events } = loadBackground({
    settings: { excludedDomains: ["player.other.test"] },
  });
  const reply = await pillPermission(
    events, null,
    framedSender("https://example.test/watch", "https://player.other.test/embed"),
  );
  assert.equal(reply.pillAllowed, false);
});

test("an allowed page and an allowed frame get the pill", async () => {
  const { events } = loadBackground({
    settings: { excludedDomains: ["blocked.test"] },
  });
  const reply = await pillPermission(
    events, null,
    framedSender("https://example.test/watch", "https://player.other.test/embed"),
  );
  assert.equal(reply.pillAllowed, true);
});

test("an identity the browser did not supply is refused the pill", async () => {
  const { events } = loadBackground({ settings: { excludedDomains: [] } });
  // Every one of these is a sender record the pill cannot be decided from.
  // isDomainExcluded() answers "not excluded" for an address it cannot parse,
  // which is right for interception and would be fail-open here.
  const senders = [
    undefined,
    {},
    { tab: { incognito: false } },
    { tab: { incognito: false, id: 1, url: "https://example.test/x" } },       // no frame url
    { url: "https://example.test/x" },                        // no tab
    { tab: { incognito: false, id: 1, url: "not a url" }, url: "https://example.test/x" },
    { tab: { incognito: false, id: 1, url: "https://example.test/x" }, url: "not a url" },
    { tab: { incognito: false, id: 1, url: "" }, url: "" },
  ];
  for (const sender of senders) {
    const reply = await pillPermission(events, null, sender);
    assert.equal(reply.pillAllowed, false, `sender ${String(JSON.stringify(sender))}`);
  }
});

test("a storage read failure is not permission to show the pill", async () => {
  // ensureSettings() still resolves - background.js catches the failure and
  // falls back to defaults - so a caller cannot tell from it that nothing was
  // read. The pill decision has to notice by itself.
  const { events } = loadBackground({ rejectSettingsRead: true });
  const reply = await pillPermission(events, "https://example.test/watch");
  assert.equal(reply.pillAllowed, false);
});

test("a first install with nothing stored still allows an unexcluded page", async () => {
  // The positive control for the case above: a successful read that finds no
  // stored settings is the shipped defaults, not a failure.
  const { events } = loadBackground();
  const reply = await pillPermission(events, "https://example.test/watch");
  assert.equal(reply.pillAllowed, true);
  assert.equal(reply.mediaPillEnabled, true);
});

test("the pill toggle and the page decision stay separate", async () => {
  const off = loadBackground({ settings: { mediaPillEnabled: false } });
  const offReply = await pillPermission(off.events, "https://example.test/watch");
  assert.equal(offReply.mediaPillEnabled, false);
  // The page is not excluded, and the toggle is not what decides that.
  assert.equal(offReply.pillAllowed, true);

  // The ordinary-interception switch is not a pill master switch.
  const disabled = loadBackground({
    settings: { enabled: false, mediaPillEnabled: true },
  });
  const reply = await pillPermission(disabled.events, "https://example.test/watch");
  assert.equal(reply.pillAllowed, true);
  assert.equal(reply.mediaPillEnabled, true);
});

test("concurrent pill requests from different pages do not contaminate each other", async () => {
  const { events } = loadBackground({
    settings: { excludedDomains: ["blocked.test"] },
  });
  const replies = [];
  for (const page of ["https://blocked.test/a", "https://allowed.test/b"]) {
    events.message.emit(
      { type: "getSettings", forPill: true },
      pillSender(page),
      (response) => { replies.push([page, response]); }
    );
  }
  await settle();
  await settle();
  const byPage = Object.fromEntries(replies.map(([p, r]) => [p, r.pillAllowed]));
  assert.equal(byPage["https://blocked.test/a"], false);
  assert.equal(byPage["https://allowed.test/b"], true);
});

test("a pill decision is neither stored nor mixed into the shared settings", async () => {
  const { events, store } = loadBackground({
    settings: { excludedDomains: ["blocked.test"] },
  });
  const keysBefore = Object.keys(store.data);
  await pillPermission(events, "https://blocked.test/a");

  // The popup and the options page keep the original answer, untouched.
  const settings = await sendToBackground(events, { type: "getSettings" });
  assert.equal("pillAllowed" in settings, false,
               "a per-page decision must not become configuration");
  assert.deepEqual(settings.excludedDomains, ["blocked.test"]);
  assert.deepEqual(Object.keys(store.data), keysBefore,
                   "deciding whether a pill may show writes nothing");
});

test("a newly excluded page cannot hand a download over", async () => {
  const { events, calls, browserDownloads, setSettings } = loadBackground({
    settings: { excludedDomains: [] },
    cookies: [{ name: "session", value: COOKIE_SENTINEL }],
  });
  const url = "https://cdn.other.test/clip.mp4";
  const page = "https://example.test/watch";

  // The user excludes the page while the pill is up. A click already in the
  // queue arrives after the change.
  setSettings({ excludedDomains: ["example.test"] });
  const reply = await pillDownload(events, { url, pageUrl: page });

  assert.equal(reply.ok, false);
  assert.equal(reply.reason, "unsupported");
  assert.equal(calls.native.filter((m) => m.action === "download").length, 0,
               "no native download attempt");
  assert.equal(calls.cookies.length, 0, "a refused request reads no cookies");
  assert.equal(calls.notifications.length, 0, "no success feedback");
  assert.equal(browserDownloads.length, 0, "no browser fallback");
  assert.equal(JSON.stringify(calls.native).includes(COOKIE_SENTINEL), false);
});

// Each control is bounded and test-only. The first assertion after entering a
// race is its suspension count; a missing hook fails within twelve turns.
function tiPause(id) {
  let count = 0, entered, open;
  const entry = new Promise((resolve) => { entered = resolve; });
  const gate = new Promise((resolve) => { open = resolve; });
  return {
    get count() { return count; }, open,
    async park() { count += 1; entered(); await gate; },
    async reached() {
      for (let turn = 0; count === 0 && turn < 12; turn += 1) await settle();
      assert.equal(count, 1, `${id}: suspension reached exactly once`);
      await entry;
    },
  };
}

function tiStart(loaded, url, page) {
  return new Promise((resolve) => loaded.events.message.emit(
    { type: "downloadMedia", url, pageUrl: page }, pillSender(page), resolve,
  ));
}

function tiBrowser(loaded, url, id) {
  loaded.events.downloadCreated.emit(freshItem({ id, url, filename: "clip.mp4" }));
}

// TI-1 / M-TI-1: removing the post-UA generation check must fail
// "TI-1: exclusion prevents native dispatch".
test("an exclusion committed while the cookie read is pending stops the handoff", async () => {
  const url = "https://cdn.other.test/clip.mp4", page = "https://example.test/watch";
  for (const exclude of [false, true]) {
    const pause = tiPause("TI-1");
    let armed = true, claimsAtPause;
    const loaded = loadBackground({
      settings: { excludedDomains: [] }, cookies: [{ name: "session", value: COOKIE_SENTINEL }],
      permissionsHook: async () => {
        if (!armed) return;
        armed = false;
        claimsAtPause = evalIn(loaded.context, "interceptClaims.size");
        await pause.park();
      },
    });
    await settle();
    const pending = tiStart(loaded, url, page);
    await pause.reached();
    assert.equal(claimsAtPause, 1, "TI-1: claim open inside UA hook");
    if (exclude) loaded.setSettings({ excludedDomains: ["example.test"] });
    pause.open();
    const reply = await pending;
    assert.equal(downloadsOf(loaded.calls).length, exclude ? 0 : 1,
                 "TI-1: exclusion prevents native dispatch");
    assert.equal(reply.ok, !exclude, "TI-1: unchanged-policy positive control succeeds");
    assert.equal(evalIn(loaded.context, "interceptClaims.size"), 0);
    if (!exclude) continue;
    assert.equal(reply.reason, "unsupported");
    assert.equal(evalIn(loaded.context, `recentIntercepted.has(${JSON.stringify(url)})`), false);
    assert.equal(JSON.stringify(loaded.calls.native).includes(COOKIE_SENTINEL), false);
    assert.equal(loaded.calls.notifications.length, 0);
    assert.equal(loaded.browserDownloads.length, 0);
    loaded.setSettings({ excludedDomains: [] });
    tiBrowser(loaded, url, 11);
    await settle();
    assert.equal(downloadsOf(loaded.calls).length, 1, "TI-1: refused claim leaves browser interception available");
    assert.deepEqual(loaded.calls.cancel, [11]);
    assert.equal((await tiStart(loaded, url, page)).ok, true, "TI-1: allowed retry succeeds");
  }
});

// TI-7 / M-TI-7: a storage hook alone is masked by the real helper's retry.
// Park its resolved final snapshot so removing the caller's generation check
// reaches UA resolution even if the later post-UA guard still refuses. The
// permission-check count kills that masked mutant (control < 40 lines).
test("an exclusion that commits after the final permission snapshot stops the handoff", async () => {
  const url = "https://cdn.other.test/clip.mp4", page = "https://example.test/watch";
  for (const exclude of [false, true]) {
    const pause = tiPause("TI-7");
    let finalReads = 0, armed = true;
    const loaded = loadBackground({
      settings: { excludedDomains: [] }, cookies: [{ name: "session", value: COOKIE_SENTINEL }],
      settingsReadHook: async () => {
        if (armed && evalIn(loaded.context, "interceptClaims.size") > 0) finalReads += 1;
      },
    });
    await settle();
    loaded.context.tiFinalSnapshot = async (answer) => {
      if (armed) { armed = false; await pause.park(); }
      return answer;
    };
    evalIn(loaded.context, `const tiPermission = resolvePillPermission;
      resolvePillPermission = async (sender) => {
        const answer = await tiPermission(sender);
        return interceptClaims.size > 0 ? tiFinalSnapshot(answer) : answer;
      };`);
    const pending = tiStart(loaded, url, page);
    await pause.reached();
    assert.equal(finalReads, 1, "TI-7: real final settings read observed with claim open");
    if (exclude) loaded.setSettings({ excludedDomains: ["example.test"] });
    pause.open();
    const reply = await pending;
    assert.equal(loaded.calls.permissionChecks.length, exclude ? 0 : 1,
                 "TI-7: stale final snapshot must not resolve userAgent");
    assert.equal(downloadsOf(loaded.calls).length, exclude ? 0 : 1,
                 "TI-7: stale final snapshot cannot dispatch");
    assert.equal(reply.ok, !exclude, "TI-7: no-exclusion positive control succeeds");
    if (exclude) {
      assert.equal(reply.reason, "unsupported");
      assert.equal(JSON.stringify(loaded.calls.native).includes(COOKIE_SENTINEL), false);
    }
  }
});

// TI-8 / M-TI-8: a stale admission result must fail the token-count oracle,
// even if the final guard subsequently refuses and releases that claim.
test("a settings change during the permission read unlocks nothing", async () => {
  const url = "https://cdn.other.test/clip.mp4", page = "https://example.test/watch";
  for (const exclude of [false, true]) {
    const pause = tiPause("TI-8");
    let armed = false;
    const loaded = loadBackground({
      settings: { excludedDomains: [] }, cookies: [{ name: "session", value: COOKIE_SENTINEL }],
      settingsReadHook: async () => {
        if (!armed || evalIn(loaded.context, "interceptClaims.size") !== 0) return;
        armed = false;
        await pause.park();
      },
    });
    await settle();
    const before = evalIn(loaded.context, "interceptTokenSeq");
    armed = true;
    const pending = tiStart(loaded, url, page);
    await pause.reached();
    if (exclude) loaded.setSettings({ excludedDomains: ["example.test"] });
    pause.open();
    const reply = await pending;
    assert.equal(evalIn(loaded.context, "interceptTokenSeq"), before + (exclude ? 0 : 1),
                 "TI-8: stale admission must not create a claim");
    assert.equal(reply.ok, !exclude, "TI-8: unchanged admission positive control succeeds");
    assert.equal(downloadsOf(loaded.calls).length, exclude ? 0 : 1);
    assert.equal(evalIn(loaded.context, "interceptClaims.size"), 0);
    if (exclude) {
      assert.equal(reply.reason, "unsupported");
      assert.equal(evalIn(loaded.context, "recentIntercepted.size"), 0);
    }
  }
});

test("a stale allowing answer is not permission to show the pill", async () => {
  let armed = false;
  const loaded = loadBackground({
    settings: { excludedDomains: [] },
    // Fires on every settings read; only the one the pill's question triggers is
    // armed, so startup reads answer from the settings they really saw.
    settingsReadHook: async () => {
      if (!armed) return;
      armed = false;
      loaded.setSettings({ excludedDomains: ["example.test"] });
    },
  });
  await settle();

  armed = true;
  const permission = await pillPermission(loaded.events,
                                          "https://example.test/watch");
  assert.equal(permission.pillAllowed, false,
               "an answer computed from superseded settings is not permission");
});

// Refusing a stale answer must not become refusing the page. The content script
// asks once per change notification, so if the background happens to process the
// same change during that read, a refusal would leave an allowed page without a
// pill until the next change or a reload. The answer has to be re-read instead.
test("an exclusion removed during the permission read still answers allowed",
     async () => {
  let armed = false;
  const loaded = loadBackground({
    settings: { excludedDomains: ["example.test"] },
    settingsReadHook: async () => {
      if (!armed) return;
      armed = false;
      // The user has just un-excluded the page. The content script's question
      // and the background's own notification cross.
      loaded.setSettings({ excludedDomains: [] });
    },
  });
  await settle();

  armed = true;
  const permission = await pillPermission(loaded.events,
                                          "https://example.test/watch");
  assert.equal(permission.pillAllowed, true,
               "a crossed notification must not hide the pill on an allowed page");
});

test("permission gives up fail-closed when settings never settle", async () => {
  const loaded = loadBackground({
    settings: { excludedDomains: [] },
    // Never settles: every read is invalidated by another change.
    settingsReadHook: async () => {
      loaded.setSettings({ excludedDomains: [], minSizeMB: Math.random() });
    },
  });
  await settle();

  const permission = await pillPermission(loaded.events,
                                          "https://example.test/watch");
  assert.equal(permission.pillAllowed, false,
               "an answer that can never be trusted is not permission");
  assert.equal(permission.mediaPillEnabled, false);
});

test("a settings change before the handoff starts does not cancel it",
     async () => {
  const url = "https://cdn.other.test/clip.mp4";
  const page = "https://example.test/watch";
  let touch = null;
  const { events, calls, setSettings } = loadBackground({
    settings: { excludedDomains: [] },
  });
  touch = () => {
    touch = null;
    // The user saves the options page with a different setting changed, and the
    // click comes afterwards. The page is still allowed and nothing changes
    // during the handoff, so it must proceed: the generation guard may not turn
    // "settings were saved at some point" into a refusal.
    setSettings({ excludedDomains: [], minSizeMB: 5 });
  };
  touch();
  const reply = await pillDownload(events, { url, pageUrl: page });

  assert.equal(reply.ok, true);
  assert.equal(calls.native.filter((m) => m.action === "download").length, 1);
});

// TI-2 / M-TI-2: deleting by URL on refusal must fail
// "TI-2: refusal preserves the first download's mark".
test("a late refusal does not drop a dedup mark another download relies on", async () => {
  const url = "https://cdn.shared.test/clip.mp4";
  for (const exclude of [false, true]) {
    const pause = tiPause("TI-2");
    let armed = false;
    const loaded = loadBackground({
      settings: { excludedDomains: [] }, cookies: [{ name: "session", value: COOKIE_SENTINEL }],
      permissionsHook: async () => { if (armed) { armed = false; await pause.park(); } },
    });
    await settle();
    const first = await tiStart(loaded, url, "https://first.test/watch");
    armed = true;
    const pending = tiStart(loaded, url, "https://second.test/watch");
    await pause.reached();
    assert.equal(first.ok, true, "TI-2: first page completed successfully");
    if (exclude) loaded.setSettings({ excludedDomains: ["second.test"] });
    pause.open();
    assert.equal((await pending).ok, !exclude, "TI-2: second page succeeds without exclusion");
    assert.equal(downloadsOf(loaded.calls).length, exclude ? 1 : 2);
    loaded.calls.native.length = 0;
    tiBrowser(loaded, url, 21);
    await settle();
    assert.equal(downloadsOf(loaded.calls).length, 0, "TI-2: refusal preserves the first download's mark");
    assert.deepEqual(loaded.calls.cancel, []);
  }
});

// TI-3 / M-TI-3: deleting a newer mark by URL must fail
// "TI-3: refusal preserves the newer download's mark".
test("a late refusal does not delete a mark a newer request replaced", async () => {
  const url = "https://cdn.shared.test/clip.mp4";
  for (const exclude of [false, true]) {
    const pause = tiPause("TI-3");
    let armed = true;
    const loaded = loadBackground({
      settings: { excludedDomains: [] }, cookies: [{ name: "session", value: COOKIE_SENTINEL }],
      permissionsHook: async () => { if (armed) { armed = false; await pause.park(); } },
    });
    await settle();
    const pending = tiStart(loaded, url, "https://first.test/watch");
    await pause.reached();
    const second = await tiStart(loaded, url, "https://second.test/watch");
    assert.equal(second.ok, true, "TI-3: newer page completes while first is suspended");
    if (exclude) loaded.setSettings({ excludedDomains: ["first.test"] });
    pause.open();
    assert.equal((await pending).ok, !exclude, "TI-3: first page succeeds without exclusion");
    assert.equal(downloadsOf(loaded.calls).length, exclude ? 1 : 2);
    loaded.calls.native.length = 0;
    tiBrowser(loaded, url, 31);
    await settle();
    assert.equal(downloadsOf(loaded.calls).length, 0, "TI-3: refusal preserves the newer download's mark");
    assert.deepEqual(loaded.calls.cancel, [], "TI-3: browser duplicate is never cancelled");
  }
});

// TI-4a/4b / M-TI-4: ignoring claims must fail the duplicate count while
// parked, including after the native request has left but before its reply.
for (const hook of ["permissionsHook", "nativeHook"]) {
  test(hook === "permissionsHook"
    ? "a handoff in flight suppresses interception of the same address"
    : "a handoff in flight suppresses interception of the same address until the native reply (TI-4b)", async () => {
    const url = "https://cdn.shared.test/clip.mp4";
    for (const duplicate of [false, true]) {
      const pause = tiPause(`TI-4 ${hook}`);
      let armed = true;
      const loaded = loadBackground({
        settings: { excludedDomains: [] }, cookies: [{ name: "session", value: COOKIE_SENTINEL }],
        [hook]: async (message) => {
          if (!armed || (message && message.action !== "download")) return;
          armed = false;
          await pause.park();
        },
      });
      await settle();
      loaded.calls.native.length = 0;
      const pending = tiStart(loaded, url, "https://first.test/watch");
      await pause.reached();
      assert.equal(evalIn(loaded.context, "interceptClaims.size"), 1, "TI-4: claim remains open");
      const sentAtPause = hook === "nativeHook" ? 1 : 0;
      assert.equal(downloadsOf(loaded.calls).length, sentAtPause, "TI-4: expected dispatch stage reached");
      if (duplicate) tiBrowser(loaded, url, 51);
      await settle();
      assert.equal(downloadsOf(loaded.calls).length, sentAtPause,
                   "TI-4: in-flight claim absorbs browser duplicate");
      assert.deepEqual(loaded.calls.cancel, []);
      pause.open();
      assert.equal((await pending).ok, true, "TI-4: handoff completes with or without duplicate");
      assert.equal(downloadsOf(loaded.calls).length, 1, "TI-4: exactly one download reaches Cove");
      assert.equal(evalIn(loaded.context, "interceptClaims.size"), 0);
    }
  });
}

// TI-5 / M-TI-5: committing on refusal must fail
// "TI-5: refused handoffs leave browser interception available".
test("two refused handoffs for one address leave it unprotected", async () => {
  const url = "https://cdn.shared.test/clip.mp4";
  for (const exclude of [false, true]) {
    const pauses = [tiPause("TI-5 first"), tiPause("TI-5 second")];
    const queue = [...pauses];
    const loaded = loadBackground({
      settings: { excludedDomains: [] }, cookies: [{ name: "session", value: COOKIE_SENTINEL }],
      permissionsHook: async () => { const pause = queue.shift(); if (pause) await pause.park(); },
    });
    await settle();
    const first = tiStart(loaded, url, "https://first.test/watch");
    await pauses[0].reached();
    const second = tiStart(loaded, url, "https://second.test/watch");
    await pauses[1].reached();
    assert.equal(pauses.reduce((sum, pause) => sum + pause.count, 0), 2, "TI-5: both requests suspended");
    assert.equal(downloadsOf(loaded.calls).length, 0, "TI-5: neither request dispatched while parked");
    if (exclude) loaded.setSettings({ excludedDomains: ["first.test", "second.test"] });
    pauses[0].open();
    const firstReply = await first;
    pauses[1].open();
    const secondReply = await second;
    assert.deepEqual([firstReply.ok, secondReply.ok], [!exclude, !exclude],
                     "TI-5: both succeed without exclusion; both refuse with it");
    assert.equal(downloadsOf(loaded.calls).length, exclude ? 0 : 2);
    assert.equal(evalIn(loaded.context, "interceptClaims.size"), 0);
    if (!exclude) continue;
    tiBrowser(loaded, url, 41);
    await settle();
    assert.equal(downloadsOf(loaded.calls).length, 1,
                 "TI-5: refused handoffs leave browser interception available");
    assert.deepEqual(loaded.calls.cancel, [41]);
  }
});

test("a refused pill download leaves no dedup mark behind", async () => {
  const { events, calls, setSettings } = loadBackground({
    settings: { excludedDomains: ["example.test"] },
  });
  const url = "https://cdn.other.test/clip.mp4";
  const page = "https://example.test/watch";
  await pillDownload(events, { url, pageUrl: page });

  // Un-exclude and retry the same address. A dedup mark left by the refusal
  // would silently swallow this.
  setSettings({ excludedDomains: [] });
  const reply = await pillDownload(events, { url, pageUrl: page });
  assert.equal(reply.ok, true);
  assert.equal(calls.native.filter((m) => m.action === "download").length, 1);
});

test("the pill toggle alone refuses a download on an allowed page", async () => {
  const { events, calls } = loadBackground({
    settings: { mediaPillEnabled: false },
  });
  const reply = await pillDownload(events, {
    url: "https://cdn.other.test/clip.mp4",
    pageUrl: "https://example.test/watch",
  });
  assert.equal(reply.ok, false);
  assert.equal(calls.native.filter((m) => m.action === "download").length, 0);
});

test("ordinary interception being off does not refuse an allowed pill download", async () => {
  const { events, calls } = loadBackground({
    settings: { enabled: false, mediaPillEnabled: true },
  });
  const reply = await pillDownload(events, {
    url: "https://cdn.other.test/clip.mp4",
    pageUrl: "https://example.test/watch",
  });
  assert.equal(reply.ok, true);
  assert.equal(calls.native.filter((m) => m.action === "download").length, 1);
});

test("the media address is not what the exclusion is matched against", async () => {
  // The resource lives on an excluded host; the page does not. The page is
  // what the user excluded, so the handoff goes ahead.
  const allowed = loadBackground({
    settings: { excludedDomains: ["cdn.blocked.test"] },
  });
  const okReply = await pillDownload(allowed.events, {
    url: "https://cdn.blocked.test/clip.mp4",
    pageUrl: "https://example.test/watch",
  });
  assert.equal(okReply.ok, true);

  // And the reverse: an excluded page is refused even though its media is on
  // a host nobody excluded.
  const refused = loadBackground({
    settings: { excludedDomains: ["example.test"] },
  });
  const noReply = await pillDownload(refused.events, {
    url: "https://cdn.allowed.test/clip.mp4",
    pageUrl: "https://example.test/watch",
  });
  assert.equal(noReply.ok, false);
});

test("a pill request answers from current settings, not a stale cache", async () => {
  const { events, setSettings } = loadBackground({
    settings: { excludedDomains: [] },
  });
  // The content script requeries the instant it sees storage.onChanged, and
  // there is no guaranteed ordering between that notification and the
  // background's own. Answering from the cache can therefore answer with the
  // value the user just replaced.
  const before = await pillPermission(events, "https://example.test/watch");
  assert.equal(before.pillAllowed, true);

  setSettings({ excludedDomains: ["example.test"] });
  const after = await pillPermission(events, "https://example.test/watch");
  assert.equal(after.pillAllowed, false);
});

test("the stream and page-url routes are not gated by the pill decision", async () => {
  const { events } = loadBackground({
    settings: { excludedDomains: ["example.test"] },
  });
  const sender = pillSender("https://example.test/watch");
  const streams = await sendToBackground(
    events, { type: "getDetectedStreams" }, sender);
  assert.ok(Array.isArray(streams), "the popup's stream list still answers");
  const pageUrl = await sendToBackground(
    events, { type: "getMediaPageUrl" }, sender);
  assert.ok(pageUrl && typeof pageUrl.url === "string");
});

// ---------------------------------------------------------------------------
// "Exclude this site" from the in-page pill (issue #16 A2).
//
// The pill offers one action that adds the site the user is actually on to the
// excluded domains the options page already owns. Which site that is, is not
// the pill's to decide: the page can embed any player and can put any address
// in a message, so the only identity worth anything here is the top-level page
// the browser itself recorded on sender.tab.url. These drive the real onMessage
// listener for that reason - a helper tested on its own would prove nothing
// about which url the listener actually hands it.
// ---------------------------------------------------------------------------

// One "which site am I on" request through the real listener.
async function pillSiteHost(events, sender) {
  let reply;
  events.message.emit({ type: "getPillSiteHost" }, sender,
                      (response) => { reply = response; });
  await settle();
  await settle();
  return reply;
}

async function requestExclude(events, sender, expectHost, extra = {}) {
  let reply;
  events.message.emit(
    { type: "requestExcludeConfirmation", expectHost, ...extra }, sender,
    (response) => { reply = response; },
  );
  for (let i = 0; i < 6; i += 1) await settle();
  if (!reply || reply.ok !== true) return { reply };
  const opened = events.calls.tabsCreated.at(-1).url;
  return { reply, token: opened.slice(opened.indexOf("#") + 1) };
}

async function confirmExclude(events, token) {
  let reply;
  events.message.emit(
    { type: "confirmExcludeSite", token },
    { id: "cove", url: EXTENSION_ORIGIN + "confirm-exclude.html" },
    (response) => { reply = response; },
  );
  for (let i = 0; i < 6; i += 1) await settle();
  return reply;
}

// One complete request + packaged-page confirmation through the real listener.
function sendExclude(events, sender, expectHost, extra = {}) {
  let reply;
  (async () => {
    const requested = await requestExclude(events, sender, expectHost, extra);
    reply = requested.token
      ? await confirmExclude(events, requested.token)
      : requested.reply;
  })();
  return () => reply;
}

async function excludeSite(events, sender, expectHost, extra = {}) {
  const read = sendExclude(events, sender, expectHost, extra);
  for (let i = 0; i < 16; i += 1) await settle();
  return read();
}

// Excluding from a page whose host is what the menu offered, which is the
// ordinary case and the shape the rest of these vary from.
async function excludeFrom(pageUrl, options = {}) {
  const loaded = loadBackground({ settings: { excludedDomains: [], ...options.settings } });
  await settle();
  const sender = options.sender || pillSender(pageUrl);
  const host = options.expectHost !== undefined
    ? options.expectHost
    : new URL(pageUrl).hostname;
  const reply = await excludeSite(loaded.events, sender, host, options.extra);
  return { ...loaded, reply, sender };
}

// Copied into this realm: a list the background wrote was built inside the vm,
// and a strict deep comparison against a plain array otherwise fails on the
// prototype rather than on the contents anyone cares about.
function excludedNow(loaded) {
  const stored = loaded.readSettings();
  return [...((stored && stored.excludedDomains) || [])];
}

test("the pill's exclude action records the page the user is on", async () => {
  const loaded = await excludeFrom("https://news.example.test/watch");
  assert.equal(loaded.reply.ok, true);
  assert.deepEqual(excludedNow(loaded), ["news.example.test"]);
});

test("an embedded player excludes the page, not the frame that asked", async () => {
  const sender = framedSender("https://news.example.test/watch",
                              "https://player.vendor.test/embed");
  const loaded = await excludeFrom("https://news.example.test/watch", { sender });
  assert.equal(loaded.reply.ok, true);
  assert.deepEqual(excludedNow(loaded), ["news.example.test"],
                   "the player's own host is not what the user excluded");
});

test("the media address never becomes the excluded domain", async () => {
  // The message body is page-reachable. Carrying a media address in it must
  // not move the exclusion onto the CDN.
  const loaded = await excludeFrom("https://news.example.test/watch", {
    extra: { url: "https://cdn.vendor.test/video.mp4",
             pageUrl: "https://cdn.vendor.test/video.mp4" },
  });
  assert.deepEqual(excludedNow(loaded), ["news.example.test"]);
});

test("a hostname supplied in the message body cannot choose the excluded site", async () => {
  // expectHost is a confirmation token, never the authority. A body naming a
  // site the sender is not on is a mismatch and is refused outright.
  const loaded = await excludeFrom("https://news.example.test/watch", {
    expectHost: "bank.example.test",
  });
  assert.equal(loaded.reply.ok, false);
  assert.deepEqual(excludedNow(loaded), [],
                   "a forged host excluded nothing at all");
});

test("a subdomain is excluded as itself, not reduced to its parent", async () => {
  const loaded = await excludeFrom("https://deep.news.example.test/watch");
  assert.deepEqual(excludedNow(loaded), ["deep.news.example.test"]);
});

for (const [name, pageUrl, host] of [
  ["localhost", "http://localhost:8080/watch", "localhost"],
  ["an IPv4 literal", "http://127.0.0.1:9000/watch", "127.0.0.1"],
  ["an IPv6 literal", "http://[::1]:9000/watch", "[::1]"],
  ["a punycode host", "https://xn--bcher-kva.example.test/watch",
   "xn--bcher-kva.example.test"],
]) {
  test(name + " is a site the pill can exclude", async () => {
    const loaded = await excludeFrom(pageUrl);
    assert.equal(loaded.reply.ok, true);
    assert.deepEqual(excludedNow(loaded), [host]);
  });
}

test("a trailing-dot host is stored in the shape that suppresses that page", async () => {
  // The parser keeps the trailing dot, and so does the matcher the pill's
  // permission runs. Storing a prettier spelling would save a setting that
  // never takes the pill away from the page it was saved on.
  const pageUrl = "https://example.test./watch";
  const loaded = await excludeFrom(pageUrl);
  assert.equal(loaded.reply.ok, true);
  const permission = await pillPermission(loaded.events, pageUrl);
  assert.equal(permission.pillAllowed, false,
               "the stored host is the one that matches the visited page");
});

for (const pageUrl of [
  "about:blank",
  "file:///home/user/clip.mp4",
  "data:text/html,<video>",
  "blob:https://example.test/9d1",
  "moz-extension://abc/options.html",
  "not a url at all",
]) {
  test(pageUrl.slice(0, 16) + " is not a site an exclusion can name", async () => {
    const sender = pillSender(pageUrl);
    const loaded = loadBackground({ settings: { excludedDomains: [] } });
    await settle();
    const host = await pillSiteHost(loaded.events, sender);
    assert.equal(host.ok, false);
    assert.equal(host.reason, "unsupported");
    const reply = await excludeSite(loaded.events, sender, "example.test");
    assert.equal(reply.ok, false);
    assert.equal(reply.reason, "unsupported");
    assert.deepEqual(excludedNow(loaded), []);
  });
}

test("a sender the browser gave no tab for excludes nothing", async () => {
  const loaded = loadBackground({ settings: { excludedDomains: [] } });
  await settle();
  const reply = await excludeSite(loaded.events, { url: "https://example.test/watch" },
                                  "example.test");
  assert.equal(reply.ok, false);
  assert.deepEqual(excludedNow(loaded), []);
});

test("the menu is told the top-level host, not the frame's", async () => {
  const loaded = loadBackground();
  await settle();
  const reply = await pillSiteHost(loaded.events,
    framedSender("https://news.example.test/watch", "https://player.vendor.test/embed"));
  assert.equal(reply.ok, true);
  assert.equal(reply.host, "news.example.test");
});

test("a tab that navigated away is not excluded in the old page's place", async () => {
  // The menu was opened on A. By the time the user picked the action the tab
  // is on B, so the sender the browser hands this message names B. The offer
  // the user accepted was about A, and B is not it.
  const loaded = loadBackground({ settings: { excludedDomains: [] } });
  await settle();
  const reply = await excludeSite(loaded.events,
                                  pillSender("https://other.example.test/watch"),
                                  "news.example.test");
  assert.equal(reply.ok, false);
  assert.deepEqual(excludedNow(loaded), [],
                   "neither the page the menu named nor the one it landed on");
});

test("a context that may not show a pill here may not write settings here", async () => {
  // The frame is excluded, so A1 refuses it the pill. A refused frame must not
  // still be able to add domains to the user's settings.
  const loaded = loadBackground({
    settings: { excludedDomains: ["player.vendor.test"] },
  });
  await settle();
  const reply = await excludeSite(loaded.events,
    framedSender("https://news.example.test/watch", "https://player.vendor.test/embed"),
    "news.example.test");
  assert.equal(reply.ok, false);
  assert.deepEqual(excludedNow(loaded), ["player.vendor.test"]);
});

test("the pill toggle being off also refuses an exclusion", async () => {
  const loaded = loadBackground({
    settings: { excludedDomains: [], mediaPillEnabled: false },
  });
  await settle();
  const reply = await excludeSite(loaded.events, pillSender("https://news.example.test/w"),
                                  "news.example.test");
  assert.equal(reply.ok, false);
  assert.deepEqual(excludedNow(loaded), []);
});

// ---- Duplicates and coverage: the shipped matcher decides, not new logic ----

test("excluding a site that is already excluded writes nothing", async () => {
  const loaded = loadBackground({ settings: { excludedDomains: [] } });
  await settle();
  const pending = await requestExclude(loaded.events,
    pillSender("https://news.example.test/watch"), "news.example.test");
  loaded.writeSettingsSilently({ excludedDomains: ["news.example.test"] });
  const reply = await confirmExclude(loaded.events, pending.token);
  assert.equal(reply.ok, true);
  assert.equal(reply.alreadyExcluded, true);
  assert.deepEqual(excludedNow(loaded), ["news.example.test"]);
  assert.deepEqual(loaded.calls.settingsWrites, [],
                   "an already-excluded site costs no storage write");
});

test("a parent already on the list covers the subdomain being excluded", async () => {
  const loaded = loadBackground({ settings: { excludedDomains: [] } });
  await settle();
  const pending = await requestExclude(loaded.events,
    pillSender("https://news.example.test/watch"), "news.example.test");
  loaded.writeSettingsSilently({ excludedDomains: ["example.test"] });
  const reply = await confirmExclude(loaded.events, pending.token);
  assert.equal(reply.alreadyExcluded, true);
  assert.deepEqual(excludedNow(loaded), ["example.test"],
                   "no redundant child beside the parent that already covers it");
});

test("excluding the parent keeps the narrower entry the user already had", async () => {
  const loaded = await excludeFrom("https://example.test/watch", {
    settings: { excludedDomains: ["news.example.test"] },
  });
  assert.equal(loaded.reply.ok, true);
  assert.deepEqual(excludedNow(loaded), ["news.example.test", "example.test"],
                   "appended, and the child it now covers is not pruned");
});

test("an exclusion appends without reordering what was already there", async () => {
  const loaded = await excludeFrom("https://new.example.test/watch", {
    settings: { excludedDomains: ["zeta.test", "alpha.test", "m.test"] },
  });
  assert.deepEqual(excludedNow(loaded),
                   ["zeta.test", "alpha.test", "m.test", "new.example.test"]);
});

test("an exclusion leaves every unrelated setting exactly as it was", async () => {
  const loaded = await excludeFrom("https://news.example.test/watch", {
    settings: {
      enabled: false,
      mediaPillEnabled: true,
      minSizeBytes: 4242,
      interceptExtensions: [".iso"],
      excludedDomains: [],
    },
  });
  const stored = loaded.readSettings();
  assert.deepEqual(excludedNow(loaded), ["news.example.test"],
                   "precondition: the exclusion is what rewrote this object");
  assert.equal(stored.enabled, false);
  assert.equal(stored.minSizeBytes, 4242);
  assert.deepEqual(stored.interceptExtensions, [".iso"]);
  assert.equal(stored.mediaPillEnabled, true);
});

// ---- Overlapping writes ----

test("two exclusions of the same site in flight together add it once", async () => {
  const loaded = loadBackground({ settings: { excludedDomains: [] } });
  await settle();
  const sender = pillSender("https://news.example.test/watch");
  sendExclude(loaded.events, sender, "news.example.test");
  sendExclude(loaded.events, sender, "news.example.test");
  for (let i = 0; i < 12; i += 1) await settle();
  assert.deepEqual(excludedNow(loaded), ["news.example.test"]);
  assert.equal(loaded.calls.settingsWrites.length, 1,
               "the second request found the first one's entry already there");
});

test("an ordinary settings save overlapping an exclusion loses neither", async () => {
  // The options page writes the whole settings object. Held open across the
  // exclusion, an unserialized write would save a snapshot taken before the
  // exclusion existed and quietly drop it.
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  let holdNext = true;
  const loaded = loadBackground({
    settings: { excludedDomains: [], minSizeBytes: 1 },
    settingsWriteHook: async () => {
      if (!holdNext) return;
      holdNext = false;
      await held;
    },
  });
  await settle();

  let saved;
  // From the options page, which is the only context the general settings
  // writer answers to, and carrying the one field the user changed there.
  loaded.events.message.emit(
    { type: "updateSettings", changes: { minSizeBytes: 999 } },
    { id: "cove", url: EXTENSION_ORIGIN + "options/options.html" },
    (r) => { saved = r; });
  await settle();
  sendExclude(loaded.events, pillSender("https://news.example.test/watch"),
              "news.example.test");
  for (let i = 0; i < 4; i += 1) await settle();

  release();
  for (let i = 0; i < 14; i += 1) await settle();

  assert.equal(saved.ok, true);
  const stored = loaded.readSettings();
  assert.equal(stored.minSizeBytes, 999, "the save the user made survived");
  assert.deepEqual(excludedNow(loaded), ["news.example.test"],
                   "and so did the exclusion that followed it");
});

test("an exclusion merges onto what storage holds, not what this context saw", async () => {
  // The options page saved while this background was not listening, or the
  // service worker was asleep and woke on defaults. Merging onto the cached
  // copy would write back the settings the user had already replaced.
  const loaded = loadBackground({
    settings: { excludedDomains: [], minSizeBytes: 1 },
  });
  await settle();
  loaded.writeSettingsSilently({ excludedDomains: ["other.test"], minSizeBytes: 777 });

  const reply = await excludeSite(loaded.events, pillSender("https://news.example.test/w"),
                                  "news.example.test");
  assert.equal(reply.ok, true);
  assert.deepEqual(excludedNow(loaded), ["other.test", "news.example.test"],
                   "the entry the cache never saw is still there");
  assert.equal(loaded.readSettings().minSizeBytes, 777,
               "and so is the unrelated setting it never saw either");
});

test("a storage write that fails excludes nothing and says so", async () => {
  const loaded = loadBackground({
    settings: { excludedDomains: [] }, breakStorage: true,
  });
  await settle();
  const reply = await excludeSite(loaded.events, pillSender("https://news.example.test/w"),
                                  "news.example.test");
  assert.equal(reply.ok, false);
  assert.deepEqual(excludedNow(loaded), []);
});

test("a failed settings write does not stop the next one", async () => {
  let failing = true;
  const loaded = loadBackground({
    settings: { excludedDomains: [] },
    settingsWriteHook: async () => {
      if (failing) throw new Error("QuotaExceededError");
    },
  });
  await settle();
  const sender = pillSender("https://news.example.test/watch");
  const first = await excludeSite(loaded.events, sender, "news.example.test");
  assert.equal(first.ok, false);

  failing = false;
  const second = await excludeSite(loaded.events, sender, "news.example.test");
  assert.equal(second.ok, true, "the chain still runs work behind a rejection");
  assert.deepEqual(excludedNow(loaded), ["news.example.test"]);
});

test("a successful exclusion is what takes the pill away", async () => {
  // Not a second local rule: the setting it wrote is the same one A1 already
  // reads, so the very next permission question answers no.
  const pageUrl = "https://news.example.test/watch";
  const loaded = await excludeFrom(pageUrl);
  const before = loaded.reply;
  assert.equal(before.ok, true);
  const permission = await pillPermission(loaded.events, pageUrl);
  assert.equal(permission.pillAllowed, false);
});

// ---- The options page against the real background ----

// Settings are one stored object, and the options page is the one writer that
// holds a snapshot of it open for as long as the user leaves the tab there.
// These load options.js from source against a document built from options.html
// and send its messages to the real background listener: nothing in between is
// modelled, so a save that loses a setting loses it here the way it would in
// the browser.
function optionsElement(id) {
  return {
    id, value: "", checked: false, textContent: "", hidden: false,
    listeners: {},
    addEventListener(type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    },
    async fire(type) {
      for (const fn of this.listeners[type] || []) await fn({ target: this });
    },
  };
}

// Built from the shipped markup rather than hand-written, so an options page
// that stops shipping a control fails here instead of finding a stub the
// harness kindly created for it.
const OPTIONS_IDS = ["enabled", "media-pill-enabled", "min-size", "min-size-unit",
                     "extensions", "excluded-domains", "reset-extensions",
                     "test-connection", "test-result", "save", "save-status",
                     "media-pill-section"];

function optionsDocument() {
  const html = fs.readFileSync("extension/options/options.html", "utf8");
  const nodes = new Map();
  for (const id of OPTIONS_IDS) {
    assert.ok(html.includes(`id="${id}"`), `options.html no longer ships #${id}`);
    nodes.set(id, optionsElement(id));
  }
  return { getElementById: (id) => nodes.get(id) || null };
}

function loadOptions(loaded) {
  const document = optionsDocument();
  const sent = [];
  const timeouts = [];
  const sender = { id: "cove", url: EXTENSION_ORIGIN + "options/options.html" };
  const context = vm.createContext({
    document,
    console: { log() {}, error() {} },
    setTimeout(fn, ms) { timeouts.push({ fn, ms }); return timeouts.length; },
    clearTimeout() {},
    globalThis: undefined,
  });
  context.globalThis = context;
  const browser = {
    runtime: {
      getManifest: () => ({ content_scripts: [{}] }),
      sendMessage(message) {
        sent.push(message);
        return new Promise((resolve, reject) => {
          const replies = loaded.events.message.emit(message, sender, resolve);
          if (!replies.some((value) => value === true)) resolve(undefined);
        });
      },
    },
    storage: {
      onChanged: {
        addListener: (fn) => loaded.events.storageChanged.addListener(fn),
      },
    },
  };
  context.browser = browser;
  context.chrome = browser;
  vm.runInContext(fs.readFileSync("extension/options/options.js", "utf8"),
                  context, { filename: "extension/options/options.js" });
  return { sent, timeouts, sender, el: (id) => document.getElementById(id) };
}

const MB = 1048576;
const BASE_SETTINGS = {
  enabled: true,
  mediaPillEnabled: true,
  minSizeBytes: MB,
  interceptExtensions: [".zip"],
  excludedDomains: [],
};

async function openOptionsPage(settings = {}) {
  const loaded = loadBackground({ settings: { ...BASE_SETTINGS, ...settings } });
  await settle();
  const page = loadOptions(loaded);
  await settle();
  return { loaded, page };
}

// A save is a message round trip plus a queued write plus a storage
// notification, so nothing about it is decided in one turn.
async function saveOptions(page) {
  await page.el("save").fire("click");
  for (let i = 0; i < 6; i += 1) await settle();
}

// S1 - the finding. The page's snapshot of excludedDomains is older than what
// is stored, and the user changes something else entirely.
test("a stale options page saving one field does not erase a newer exclusion",
     async () => {
  const { loaded, page } = await openOptionsPage();
  // Silently: a merge that relies on having been notified is not a merge, and
  // real storage makes no such promise to a context that is about to write.
  loaded.writeSettingsSilently({ ...BASE_SETTINGS, excludedDomains: ["example.test"] });

  page.el("min-size").value = "5";
  await page.el("min-size").fire("input");
  await saveOptions(page);

  assert.deepEqual(excludedNow(loaded), ["example.test"],
                   "a save that did not touch excluded domains erased one");
  assert.equal(loaded.readSettings().minSizeBytes, 5 * MB,
               "and the field the user did change was still written");
});

test("a stale options page saves nothing at all when the user changed nothing",
     async () => {
  const { loaded, page } = await openOptionsPage();
  loaded.writeSettingsSilently({ ...BASE_SETTINGS, excludedDomains: ["example.test"] });
  await saveOptions(page);
  assert.deepEqual(excludedNow(loaded), ["example.test"]);
});

// S2 - the keyboard shortcut is the other whole-object writer.
test("the keyboard toggle does not erase an exclusion it never saw", async () => {
  const loaded = loadBackground({ settings: { ...BASE_SETTINGS } });
  await settle();
  loaded.writeSettingsSilently({ ...BASE_SETTINGS, excludedDomains: ["example.test"] });

  loaded.events.command.emit("toggle-intercept");
  for (let i = 0; i < 8; i += 1) await settle();

  assert.deepEqual(excludedNow(loaded), ["example.test"]);
  assert.equal(loaded.readSettings().enabled, false, "and the toggle still toggled");
});

// S3 - two options pages opened from the same snapshot, editing different things.
test("two options pages editing different fields both persist", async () => {
  const { loaded, page: first } = await openOptionsPage();
  const second = loadOptions(loaded);
  await settle();

  first.el("enabled").checked = false;
  await first.el("enabled").fire("change");
  second.el("min-size").value = "7";
  await second.el("min-size").fire("input");

  await saveOptions(first);
  await saveOptions(second);

  assert.equal(loaded.readSettings().enabled, false);
  assert.equal(loaded.readSettings().minSizeBytes, 7 * MB);
});

// S4 - both pages deliberately changed the SAME field. No merge can satisfy
// both, and the bounded rule is that the last save committed for that field
// wins. What is being pinned here is that it is only that field.
test("two options pages editing the same field end on the last one saved",
     async () => {
  const { loaded, page: first } = await openOptionsPage({ enabled: true });
  const second = loadOptions(loaded);
  await settle();
  loaded.writeSettingsSilently({ ...BASE_SETTINGS, excludedDomains: ["example.test"] });

  first.el("enabled").checked = false;
  await first.el("enabled").fire("change");
  second.el("enabled").checked = true;
  await second.el("enabled").fire("change");

  await saveOptions(first);
  await saveOptions(second);

  assert.equal(loaded.readSettings().enabled, true, "the later save for that field won");
  assert.deepEqual(excludedNow(loaded), ["example.test"],
                   "and neither of them touched anything else");
});

// S5 - an open page that is not being edited follows the stored value.
test("an external settings change updates an options control nobody is editing",
     async () => {
  const { loaded, page } = await openOptionsPage();
  loaded.setSettings({ ...BASE_SETTINGS, excludedDomains: ["example.test"] });
  await settle();
  assert.equal(page.el("excluded-domains").value, "example.test");
});

// S6 - and stops at the edge of what the user has typed.
test("an external settings change does not overwrite an unsaved local edit",
     async () => {
  const { loaded, page } = await openOptionsPage();
  page.el("excluded-domains").value = "typed.test";
  await page.el("excluded-domains").fire("input");

  loaded.setSettings({ ...BASE_SETTINGS, excludedDomains: ["example.test"] });
  await settle();

  assert.equal(page.el("excluded-domains").value, "typed.test");
  assert.equal(page.el("enabled").checked, true, "the untouched control still followed");
});

test("an edit made while a save is in flight is not lost", async () => {
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  const loaded = loadBackground({
    settings: { ...BASE_SETTINGS },
    settingsWriteHook: () => held,
  });
  await settle();
  const page = loadOptions(loaded);
  await settle();

  page.el("min-size").value = "5";
  await page.el("min-size").fire("input");
  const saving = saveOptions(page);
  await settle();
  page.el("min-size").value = "9";
  await page.el("min-size").fire("input");
  release();
  await saving;

  assert.equal(loaded.readSettings().minSizeBytes, 5 * MB, "the submitted value landed");
  await saveOptions(page);
  assert.equal(loaded.readSettings().minSizeBytes, 9 * MB,
               "and the newer edit was still pending, not discarded as saved");
});

test("a failed settings write is reported and the next save still works",
     async () => {
  let failNext = true;
  const loaded = loadBackground({
    settings: { ...BASE_SETTINGS },
    settingsWriteHook: () => {
      if (!failNext) return;
      failNext = false;
      throw new Error("QuotaExceededError");
    },
  });
  await settle();
  const page = loadOptions(loaded);
  await settle();

  page.el("min-size").value = "5";
  await page.el("min-size").fire("input");
  await saveOptions(page);

  assert.notEqual(page.el("save-status").textContent, "Saved",
                  "a write that failed must not report success");
  assert.equal(loaded.readSettings().minSizeBytes, MB, "and nothing was committed");

  await saveOptions(page);
  assert.equal(loaded.readSettings().minSizeBytes, 5 * MB,
               "the rejected write did not poison the settings chain");
});

test("a content script cannot update settings through the options message",
     async () => {
  const loaded = loadBackground({ settings: { ...BASE_SETTINGS } });
  await settle();
  let reply;
  loaded.events.message.emit(
    { type: "updateSettings", changes: { excludedDomains: ["evil.test"], enabled: false } },
    pillSender("https://evil.test/watch"),
    (response) => { reply = response; },
  );
  for (let i = 0; i < 6; i += 1) await settle();

  assert.notEqual(reply && reply.ok, true);
  assert.deepEqual(excludedNow(loaded), []);
  assert.equal(loaded.readSettings().enabled, true);
});

test("the options page cannot write a settings field it does not own", async () => {
  const loaded = loadBackground({ settings: { ...BASE_SETTINGS } });
  await settle();
  const page = loadOptions(loaded);
  await settle();
  let reply;
  loaded.events.message.emit(
    { type: "updateSettings", changes: { enabled: false, nativeHost: "attacker" } },
    page.sender,
    (response) => { reply = response; },
  );
  for (let i = 0; i < 6; i += 1) await settle();

  assert.equal(reply && reply.ok, true);
  assert.equal(loaded.readSettings().enabled, false);
  assert.equal("nativeHost" in loaded.readSettings(), false);
});

// ---- Firefox optional technical-data consent (technicalAndInteraction) ----
//
// Mozilla requires `technicalAndInteraction` to be OPTIONAL and says it
// "cannot be required". The browser's user-agent is browser information, which
// that category covers, so a Firefox handoff may only carry it while the user
// currently grants the permission. Mozilla documents feature-detecting the
// consent experience through the presence or absence of the `data_collection`
// key in permissions.getAll(), which is what these tests model: an absent key
// is Chrome or a Firefox without the model, an array is a Firefox with it.
//
// The payload carries no user-agent by OMITTING the key. That is the existing
// "none supplied" representation: cove/native_messaging.py reads
// msg.get("userAgent", "") and the downstream consumers gate on truthiness
// (cove/extractor.py, cove/hls.py), so an absent key needs no protocol change.

const CONSENT = "technicalAndInteraction";
const REAL_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0";

// The two production routes that put navigator.userAgent into a native
// handoff: the downloads interception path and the context-menu path.
async function firefoxDownloadHandoff(options) {
  const loaded = loadBackground(options);
  await settle();
  loaded.calls.native.length = 0;
  loaded.events.downloadCreated.emit(sizedItem(5 * 1024 * 1024));
  await settle();
  return loaded;
}

// A second handoff needs its own address: the interception path dedupes by URL
// inside DEDUP_WINDOW_MS, so reusing one would be dropped before it ever
// reached the consent gate and would prove nothing about consent.
function nextItem(n) {
  return sizedItem(5 * 1024 * 1024, {
    incognito: false,
    id: 900 + n,
    url: `https://example.test/payload-${n}.zip`,
    filename: `payload-${n}.zip`,
  });
}

async function menuHandoff(loaded) {
  loaded.calls.native.length = 0;
  await Promise.all(loaded.events.contextMenuClicked.emit(
    {
      menuItemId: "download-with-cove",
      srcUrl: "https://cdn.example.test/v/clip.mp4",
      pageUrl: "https://example.test/watch",
    },
    { incognito: false, id: 7, url: "https://example.test/watch" },
  ));
  await settle();
  return downloadsOf(loaded.calls);
}

test("U1 Firefox with technicalAndInteraction granted still sends the user-agent",
     async () => {
  const { calls } = await firefoxDownloadHandoff({ dataConsent: [CONSENT] });

  const sent = downloadsOf(calls);
  assert.equal(sent.length, 1, "the download is handed off");
  assert.equal(sent[0].userAgent, REAL_UA,
               "granted consent means the real user-agent travels");
});

test("U2 Firefox without the grant hands off the download but no user-agent",
     async () => {
  const { calls } = await firefoxDownloadHandoff({ dataConsent: [] });

  const sent = downloadsOf(calls);
  assert.equal(sent.length, 1,
               "the download itself must not be blocked by a technical-data refusal");
  assert.ok(!("userAgent" in sent[0]),
            "the key is omitted, which the native host already reads as none");
  assert.ok(!JSON.stringify(sent[0]).includes("Gecko"),
            "no part of the real user-agent leaks through another field");
});

test("U2b an unrelated granted data permission does not unlock the user-agent",
     async () => {
  const { calls } = await firefoxDownloadHandoff({ dataConsent: ["websiteContent"] });

  const sent = downloadsOf(calls);
  assert.equal(sent.length, 1);
  assert.ok(!("userAgent" in sent[0]),
            "only technicalAndInteraction governs technical data");
});

test("U3 a permission lookup failure fails closed for the user-agent only",
     async () => {
  const { calls } = await firefoxDownloadHandoff({
    dataConsent: [CONSENT], permissionsApi: "throws",
  });

  const sent = downloadsOf(calls);
  assert.equal(sent.length, 1,
               "a consent lookup failure must not block the download");
  assert.ok(!("userAgent" in sent[0]),
            "unknown consent is not consent");
});

test("U3b a data_collection value that is not a list also fails closed",
     async () => {
  const { calls } = await firefoxDownloadHandoff({
    dataConsent: [CONSENT], permissionsApi: "garbage",
  });

  const sent = downloadsOf(calls);
  assert.equal(sent.length, 1);
  assert.ok(!("userAgent" in sent[0]),
            "a shape the gate cannot read is not consent");
});

test("U3c a browser with no permissions API at all fails closed for the UA",
     async () => {
  const { calls } = await firefoxDownloadHandoff({ permissionsApi: "missing" });

  const sent = downloadsOf(calls);
  assert.equal(sent.length, 1);
  assert.ok(!("userAgent" in sent[0]));
});

test("U4 revoking the permission stops the next handoff carrying the user-agent",
     async () => {
  const loaded = await firefoxDownloadHandoff({ dataConsent: [CONSENT] });
  const first = downloadsOf(loaded.calls);
  assert.equal(first.length, 1);
  assert.equal(first[0].userAgent, REAL_UA, "granted at first handoff");

  // The user turns it off in about:addons. No restart, no reload.
  loaded.events.setDataConsent([]);

  loaded.calls.native.length = 0;
  loaded.events.downloadCreated.emit(nextItem(1));
  await settle();

  const second = downloadsOf(loaded.calls);
  assert.equal(second.length, 1, "the download still happens");
  assert.ok(!("userAgent" in second[0]),
            "a revoked permission takes effect on the very next handoff");
});

test("U4b granting the permission mid-session lets the next handoff carry it",
     async () => {
  const loaded = await firefoxDownloadHandoff({ dataConsent: [] });
  assert.ok(!("userAgent" in downloadsOf(loaded.calls)[0]));

  loaded.events.setDataConsent([CONSENT]);

  loaded.calls.native.length = 0;
  loaded.events.downloadCreated.emit(nextItem(2));
  await settle();

  assert.equal(downloadsOf(loaded.calls)[0].userAgent, REAL_UA);
});

test("U4c the consent state is read for every handoff, never cached", async () => {
  const loaded = await firefoxDownloadHandoff({ dataConsent: [CONSENT] });
  const afterFirst = loaded.calls.permissionChecks.length;
  assert.ok(afterFirst >= 1, "the first handoff consults the live permission state");

  loaded.calls.native.length = 0;
  loaded.events.downloadCreated.emit(nextItem(3));
  await settle();

  assert.ok(loaded.calls.permissionChecks.length > afterFirst,
            "the second handoff consults it again rather than reusing an answer");
});

test("U5 Chrome keeps its user-agent handoff and never consults data consent",
     async () => {
  // No dataConsent: getAll() answers without a `data_collection` key, which is
  // exactly what Chrome returns and what Mozilla says to feature-detect on.
  const { calls, events } = chromeWorker();
  await settle();
  calls.native.length = 0;
  calls.permissionChecks.length = 0;

  const sent = await menuHandoff({ calls, events });
  assert.equal(sent.length, 1);
  assert.equal(sent[0].userAgent, REAL_UA,
               "Chrome behaviour is unchanged by the Firefox consent gate");
});

test("U6 the context-menu route is gated too, not only the downloads route",
     async () => {
  const granted = loadBackground({ dataConsent: [CONSENT] });
  await settle();
  const withConsent = await menuHandoff(granted);
  assert.equal(withConsent.length, 1);
  assert.equal(withConsent[0].userAgent, REAL_UA);

  const denied = loadBackground({ dataConsent: [] });
  await settle();
  const withoutConsent = await menuHandoff(denied);
  assert.equal(withoutConsent.length, 1,
               "the menu download still happens without technical consent");
  assert.ok(!("userAgent" in withoutConsent[0]),
            "no production route may bypass the gate");
});

test("U6b no native handoff of any action carries the user-agent without consent",
     async () => {
  // A sweep rather than a named-route check: whatever the extension sends,
  // nothing may contain the real user-agent while consent is absent.
  const loaded = await firefoxDownloadHandoff({ dataConsent: [] });
  await menuHandoff(loaded);
  await settle();

  for (const message of loaded.calls.native) {
    assert.ok(!JSON.stringify(message).includes("Gecko"),
              `a ${message.action} message leaked the user-agent`);
  }
});

// The real-browser run found these two routes after the first pass gated only
// background.js. media-core.js (the in-page pill and context-menu media
// handoff) and media-sites.js (the detected-stream handoff) build their own
// native payloads, so each needs the same gate. U6 is only true if all three
// routes obey it.

test("U6c the media/pill route carries the user-agent only with consent",
     async () => {
  const granted = loadBackground({ dataConsent: [CONSENT] });
  await settle();
  granted.calls.native.length = 0;
  await requestMedia(granted.events, { requestId: "aa11bb22" });
  const sentGranted = downloadsOf(granted.calls);
  assert.equal(sentGranted.length, 1, "the media handoff happens");
  assert.equal(sentGranted[0].userAgent, REAL_UA);

  const denied = loadBackground({ dataConsent: [] });
  await settle();
  denied.calls.native.length = 0;
  await requestMedia(denied.events, { requestId: "aa11bb22" });
  const sentDenied = downloadsOf(denied.calls);
  assert.equal(sentDenied.length, 1,
               "the media handoff still happens without technical consent");
  assert.ok(!("userAgent" in sentDenied[0]),
            "the pill route must not bypass the consent gate");
  assert.ok("requestId" in sentDenied[0],
            "the rest of the media payload is untouched");
});

test("U6d the detected-stream route carries the user-agent only with consent",
     async () => {
  const granted = popupStreamFixture({ dataConsent: [CONSENT] });
  await settle();
  granted.calls.native.length = 0;
  sendRuntimeMessage(granted.events, {
    type: "downloadStream",
    url: "https://example.test/live.m3u8",
    filename: "live.mp4",
  }, popupSender());
  await settle();
  const sentGranted = downloadsOf(granted.calls);
  assert.equal(sentGranted.length, 1, "the stream handoff happens");
  assert.equal(sentGranted[0].userAgent, REAL_UA);

  const denied = popupStreamFixture({ dataConsent: [] });
  await settle();
  denied.calls.native.length = 0;
  const { returned } = sendRuntimeMessage(denied.events, {
    type: "downloadStream",
    url: "https://example.test/live.m3u8",
    filename: "live.mp4",
  }, popupSender());
  assert.equal(returned, true,
               "the reply is still asynchronous: the contract is unchanged");
  await settle();
  const sentDenied = downloadsOf(denied.calls);
  assert.equal(sentDenied.length, 1,
               "the stream handoff still happens without technical consent");
  assert.ok(!("userAgent" in sentDenied[0]),
            "the stream route must not bypass the consent gate");
});

test("U6e a denied-consent sweep across all three routes leaks no user-agent",
     async () => {
  const loaded = popupStreamFixture({ dataConsent: [] });
  await settle();
  loaded.calls.native.length = 0;

  loaded.events.downloadCreated.emit(nextItem(7));   // interception route
  await settle();
  await menuHandoff(loaded);                          // context-menu route
  await requestMedia(loaded.events, { requestId: "cc33dd44" });  // pill route
  sendRuntimeMessage(loaded.events, {                 // stream route
    type: "downloadStream",
    url: "https://example.test/live.m3u8",
    filename: "live.mp4",
  }, popupSender());
  await settle();

  const sent = downloadsOf(loaded.calls);
  assert.ok(sent.length >= 3, `expected several handoffs, saw ${sent.length}`);
  for (const message of loaded.calls.native) {
    assert.ok(!JSON.stringify(message).includes("Gecko"),
              `a ${message.action} message leaked the user-agent`);
  }
});

// TI-6 / M-TI-6: resolving UA before final revalidation must fail
// "TI-6: final-read revocation omits userAgent".
test("U7 consent revoked during the pill's settings read is still honoured", async () => {
  for (const revoke of [false, true]) {
    const pause = tiPause("TI-6");
    let armed = false, revoked = false;
    const loaded = loadBackground({
      dataConsent: [CONSENT], cookies: [{ name: "session", value: COOKIE_SENTINEL }],
      settingsReadHook: async () => {
        if (!armed || evalIn(loaded.context, "interceptClaims.size") === 0) return;
        armed = false;
        await pause.park();
      },
    });
    await settle();
    armed = true;
    const pending = tiStart(loaded, "https://cdn.example.test/v/movie.mp4", "https://news.example.test/x");
    await pause.reached();
    assert.equal(evalIn(loaded.context, "interceptClaims.size"), 1, "TI-6: final read has an open claim");
    if (revoke) { revoked = true; loaded.events.setDataConsent([]); }
    pause.open();
    await pending;
    const sent = downloadsOf(loaded.calls);
    assert.equal(sent.length, 1, "TI-6: download still goes through");
    assert.equal(revoked, revoke, "TI-6: revocation was performed at the final read");
    assert.equal("userAgent" in sent[0], !revoke, "TI-6: final-read revocation omits userAgent");
    if (!revoke) assert.equal(sent[0].userAgent, REAL_UA, "TI-6: no-revocation positive control keeps UA");
  }
});

// S03: exercise the real background + core after the final permission read,
// parked specifically inside userAgentField's permissions.getAll await.
const S03_POLICY_SETTINGS = {
  enabled: true,
  mediaPillEnabled: true,
  excludedDomains: [],
  minSizeBytes: 0,
  interceptExtensions: [".mp4"],
};
const S03_POLICY_URL = "https://cdn.policy.test/clip.mp4";
const S03_POLICY_PAGE = "https://page.policy.test/watch";
const S03_COOKIE_SECRET = "s03-collected-cookie";

async function parkS03FinalPermission({ competingPermissionsHook = null } = {}) {
  let armed = false;
  let parked = false;
  let open;
  const gate = new Promise((resolve) => { open = resolve; });
  const loaded = loadBackground({
    settings: { ...S03_POLICY_SETTINGS },
    cookies: [{ name: "session", value: S03_COOKIE_SECRET }],
    dataConsent: [CONSENT],
    permissionsHook: async () => {
      if (!armed) {
        if (competingPermissionsHook) await competingPermissionsHook();
        return;
      }
      armed = false; // one-shot; subsequent attempts and interception can run
      parked = true;
      await gate;
    },
  });
  await settle();
  loaded.calls.native.length = 0; // ignore startup status traffic
  loaded.calls.notifications.length = 0;
  armed = true;
  const response = new Promise((resolve) => {
    loaded.events.message.emit(
      { type: "downloadMedia", url: S03_POLICY_URL, pageUrl: S03_POLICY_PAGE },
      pillSender(S03_POLICY_PAGE), resolve,
    );
  });
  await settle();
  assert.ok(parked, "fixture must reach the final UA permission await");
  assert.equal(loaded.calls.cookies.length, 0, "the final await never reads cookies");
  assert.equal(downloadsOf(loaded.calls).length, 0, "native dispatch has not started while parked");
  assert.equal(evalIn(loaded.context, `interceptClaims.get(${JSON.stringify(S03_POLICY_URL)}).size`), 1,
               "this request owns one in-flight claim");
  const generation = evalIn(loaded.context, "settingsGeneration");
  return {
    ...loaded, generation,
    async release() {
      open();
      const reply = await response;
      await settle();
      return reply;
    },
  };
}

function assertS03FinalRefusal(loaded, reply) {
  assert.equal(downloadsOf(loaded.calls).length, 0,
               "policy changed during final UA await: no native download may be sent");
  assert.deepEqual(JSON.parse(JSON.stringify(reply)), {
    ok: false, reason: "unsupported", error: "Downloads are turned off for this site",
  });
  assert.equal(JSON.stringify(loaded.calls.native).includes(S03_COOKIE_SECRET), false,
               "collected cookies must never leave on a rejected handoff");
  assert.equal(loaded.calls.notifications.length, 0, "a rejected request has no success notification");
  assert.equal(evalIn(loaded.context, `interceptClaims.has(${JSON.stringify(S03_POLICY_URL)})`), false,
               "the rejected request's claim must be released");
  assert.equal(evalIn(loaded.context, `recentIntercepted.has(${JSON.stringify(S03_POLICY_URL)})`), false,
               "a rejected request must not commit an interception mark");
}

function emitS03BrowserDownload(loaded, id) {
  loaded.events.downloadCreated.emit({
    incognito: false,
    id, url: S03_POLICY_URL, filename: "clip.mp4", state: "in_progress",
    startTime: new Date().toISOString(), totalBytes: 20_000_000,
  });
}

for (const [id, changeName, change] of [
  ["P1", "site exclusion", (loaded) => {
    loaded.setSettings({ ...S03_POLICY_SETTINGS, excludedDomains: ["page.policy.test"] });
  }],
  ["P2", "pill disabled", (loaded) => {
    loaded.setSettings({ ...S03_POLICY_SETTINGS, mediaPillEnabled: false });
  }],
  ["P3", "consent revoked with a settings change", (loaded) => {
    loaded.events.setDataConsent([]);
    loaded.setSettings({ ...S03_POLICY_SETTINGS, minSizeBytes: 1 });
  }],
  ["P5", "equivalent settings write", (loaded) => {
    loaded.setSettings({ ...S03_POLICY_SETTINGS, excludedDomains: [] });
  }],
]) {
  test(`S03 ${id}: ${changeName} during final UA await refuses without native effects`, async () => {
    const loaded = await parkS03FinalPermission();
    change(loaded);
    assert.ok(evalIn(loaded.context, "settingsGeneration") > loaded.generation,
              "storage.onChanged must advance the authorizing generation");
    const reply = await loaded.release();
    assertS03FinalRefusal(loaded, reply);

    // Restoring policy and starting the browser's own download of this exact
    // URL proves both the claim and any stale mark are absent after refusal.
    loaded.setSettings({ ...S03_POLICY_SETTINGS });
    emitS03BrowserDownload(loaded, 803);
    await settle();
    await settle();
    assert.equal(downloadsOf(loaded.calls).length, 1,
                 "the rejected handoff must not suppress a later browser download");
    assert.deepEqual(loaded.calls.cancel, [803]);
  });
}

test("S03 P3b: consent revoked alone during final UA await sends once without userAgent", async () => {
  const loaded = await parkS03FinalPermission();
  loaded.events.setDataConsent([]);
  assert.equal(evalIn(loaded.context, "settingsGeneration"), loaded.generation,
               "consent-only revocation does not synthesize a settings change");
  const reply = await loaded.release();
  assert.equal(reply.ok, true);
  const sent = downloadsOf(loaded.calls);
  assert.equal(sent.length, 1, "today's consent-only behavior still permits the handoff");
  assert.equal("userAgent" in sent[0], false, "revoked consent omits the userAgent key");
  assert.equal("cookies" in sent[0], false);
  assert.equal(loaded.calls.notifications.length, 1);
  assert.equal(evalIn(loaded.context, `interceptClaims.has(${JSON.stringify(S03_POLICY_URL)})`), false);
});

test("S03 P4: unchanged policy dispatches once, commits one mark and releases its claim", async () => {
  const loaded = await parkS03FinalPermission();
  const reply = await loaded.release();
  assert.equal(reply.ok, true);
  const sent = downloadsOf(loaded.calls);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].url, S03_POLICY_URL);
  assert.equal("cookies" in sent[0], false);
  assert.equal(sent[0].userAgent, REAL_UA);
  assert.equal(loaded.calls.notifications.length, 1, "one success notification");
  assert.equal(loaded.calls.notifications[0].title, "Download sent to Cove");
  assert.equal(evalIn(loaded.context, "recentIntercepted.size"), 1, "one committed address");
  assert.equal(evalIn(loaded.context, `recentIntercepted.has(${JSON.stringify(S03_POLICY_URL)})`), true);
  assert.equal(evalIn(loaded.context, "interceptClaims.size"), 0, "the successful request released its claim");
  emitS03BrowserDownload(loaded, 804);
  await settle();
  assert.equal(downloadsOf(loaded.calls).length, 1, "the committed mark suppresses duplicate browser interception");
  assert.equal(loaded.calls.notifications.length, 1);
});

test("S03 P6: final-await rejection releases its claim and a later genuine attempt succeeds", async () => {
  const loaded = await parkS03FinalPermission();
  loaded.setSettings({ ...S03_POLICY_SETTINGS, mediaPillEnabled: false });
  const rejected = await loaded.release();
  const rejectedNativeCount = downloadsOf(loaded.calls).length;
  const rejectedClaimRemains = evalIn(loaded.context, `interceptClaims.has(${JSON.stringify(S03_POLICY_URL)})`);
  const rejectedMarkRemains = evalIn(loaded.context, `recentIntercepted.has(${JSON.stringify(S03_POLICY_URL)})`);
  loaded.setSettings({ ...S03_POLICY_SETTINGS });
  // Background receives the legitimate retry's message; gesture origin is
  // covered by G4/G5 in the content tests, not forged in this background VM.
  const retry = await pillDownload(loaded.events, { url: S03_POLICY_URL, pageUrl: S03_POLICY_PAGE });
  assert.equal(rejectedNativeCount, 0, "the rejected attempt must not have dispatched native authority");
  assert.equal(rejected.ok, false);
  assert.equal(rejected.reason, "unsupported");
  assert.equal(rejectedClaimRemains, false, "the rejected attempt's own claim is gone before retry");
  assert.equal(rejectedMarkRemains, false, "the rejected attempt committed no mark");
  assert.equal(retry.ok, true, "the next genuine attempt for the same URL must succeed");
  assert.equal(downloadsOf(loaded.calls).length, 1, "only the genuine retry reaches native");
  assert.equal(loaded.calls.notifications.length, 1);
  assert.equal(evalIn(loaded.context, "interceptClaims.size"), 0);
});

test("S03 P6b: final refusal preserves a competing request's claim and committed mark", async () => {
  // Two schedules within one ownership test: B either still holds its claim
  // or has completed and committed its mark when A withdraws. Deleting the
  // whole claims entry loses B in the first; clearing the mark loses B in the
  // second. Neither protection belongs to A's refusing request.
  for (const protection of ["claim", "mark"]) {
    let parkB = false;
    let bParked = false;
    let releaseB;
    const bGate = new Promise((resolve) => { releaseB = resolve; });
    const loaded = await parkS03FinalPermission({
      competingPermissionsHook: async () => {
        if (!parkB) return;
        parkB = false;
        bParked = true;
        await bGate;
      },
    });
    const claimTokens = () => JSON.parse(evalIn(loaded.context,
      `JSON.stringify(Array.from(interceptClaims.get(${JSON.stringify(S03_POLICY_URL)})?.keys() || []))`));
    const aToken = claimTokens()[0];
    loaded.setSettings({ ...S03_POLICY_SETTINGS, excludedDomains: ["page.policy.test"] });
    const otherPage = "https://other.policy.test/watch";
    parkB = protection === "claim";
    const bResponse = new Promise((resolve) => {
      loaded.events.message.emit(
        { type: "downloadMedia", url: S03_POLICY_URL, pageUrl: otherPage },
        pillSender(otherPage), resolve,
      );
    });
    await settle();
    let expectedClaims = [];
    if (protection === "claim") {
      assert.ok(bParked, "B must be parked holding its own claim");
      const tokens = claimTokens();
      assert.equal(tokens.length, 2, "A and B each hold a separate claim");
      expectedClaims = tokens.filter((token) => token !== aToken);
      assert.equal(expectedClaims.length, 1);
    } else {
      assert.equal((await bResponse).ok, true, "B completes on its still-allowed page");
      assert.deepEqual(claimTokens(), [aToken], "only A's claim remains after B succeeds");
    }
    const expectedMark = evalIn(loaded.context,
      `recentIntercepted.get(${JSON.stringify(S03_POLICY_URL)})`);
    if (protection === "mark") assert.equal(typeof expectedMark, "number", "B committed a dedup mark");
    const sentBeforeA = downloadsOf(loaded.calls).length;
    const notificationsBeforeA = loaded.calls.notifications.length;

    const aReply = await loaded.release();
    assert.deepEqual(JSON.parse(JSON.stringify(aReply)), {
      ok: false, reason: "unsupported", error: "Downloads are turned off for this site",
    });
    assert.equal(downloadsOf(loaded.calls).length, sentBeforeA, "A adds no native dispatch");
    assert.equal(loaded.calls.notifications.length, notificationsBeforeA, "A adds no success notification");
    assert.deepEqual(claimTokens(), expectedClaims, "A releases only its token, preserving B's claim");
    assert.equal(evalIn(loaded.context, `recentIntercepted.get(${JSON.stringify(S03_POLICY_URL)})`),
                 expectedMark, "A must not erase B's committed mark");

    emitS03BrowserDownload(loaded, 806);
    await settle();
    assert.equal(downloadsOf(loaded.calls).length, sentBeforeA,
                 `B's ${protection} must still suppress duplicate browser interception`);
    assert.deepEqual(loaded.calls.cancel, [], "suppressed duplicate is not intercepted");
    if (protection === "claim") {
      releaseB();
      assert.equal((await bResponse).ok, true, "B still finishes after A is refused");
      await settle();
    }
    assert.equal(downloadsOf(loaded.calls).length, 1, "only B reaches native authority");
    assert.equal(loaded.calls.notifications.length, 1);
    assert.deepEqual(claimTokens(), []);
  }
});
