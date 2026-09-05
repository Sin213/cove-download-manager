// The popup, as each browser actually receives it.
//
// Which scripts and stylesheets a popup ships is exactly what Tab 2C changes,
// so every test here loads a bundle produced by scripts/build_extension.py and
// executes the script list its built popup.html names, in the order it names
// them. Reading extension/popup/ directly would test the template instead of
// the artifact, and hand-writing the script list would let a build that forgot
// to include a module still pass.

const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const REPO = path.resolve(__dirname, "..");

// One build per process, through the real entry point, into a directory this
// test owns. build() removes its destination first, so it is never pointed at
// anything shared.
let builtDist = null;
function bundles() {
  if (builtDist) return builtDist;
  const dist = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "cove-popup-")), "dist");
  execFileSync(
    "python3",
    [
      "-c",
      "import sys; sys.path.insert(0, sys.argv[1]); " +
        "from build_extension import build; build(dist=sys.argv[2])",
      path.join(REPO, "scripts"),
      dist,
    ],
    { cwd: REPO, stdio: "pipe" },
  );
  builtDist = dist;
  return dist;
}

// ---- A DOM small enough to read and honest enough to fail ----

const VOID_TAGS = new Set(["meta", "link", "img", "br", "input", "hr"]);

function makeElement(tag) {
  const element = {
    tagName: String(tag).toLowerCase(),
    id: "",
    className: "",
    textContent: "",
    title: "",
    style: {},
    dataset: {},
    disabled: false,
    listeners: {},
    children: [],
    parent: null,
    addEventListener(type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    },
    async click() {
      for (const fn of this.listeners.click || []) await fn();
    },
    appendChild(child) {
      child.parent = this;
      this.children.push(child);
      return child;
    },
    append(...nodes) {
      for (const node of nodes) this.appendChild(node);
    },
    replaceChildren(...nodes) {
      for (const child of this.children) child.parent = null;
      this.children = [];
      for (const node of nodes) this.appendChild(node);
    },
    before(node) {
      const siblings = this.parent.children;
      node.parent = this.parent;
      siblings.splice(siblings.indexOf(this), 0, node);
    },
  };
  // Rendering an untrusted filename or address through textContent is a
  // security property of this popup, not a style choice. A module that reaches
  // for HTML insertion instead has to fail here rather than ship.
  Object.defineProperty(element, "innerHTML", {
    set() { throw new Error("the popup must not insert HTML"); },
    get() { return undefined; },
  });
  return element;
}

function parseStyle(value) {
  const style = {};
  for (const rule of String(value).split(";")) {
    const [name, ...rest] = rule.split(":");
    if (!name.trim() || rest.length === 0) continue;
    style[name.trim()] = rest.join(":").trim();
  }
  return style;
}

// popup.html is a small, fully closed document, so a tag-level walk is enough.
// Building the tree from the artifact rather than hand-writing it is the point:
// a popup that stops shipping #toggle-btn must fail here rather than find a
// stub the harness kindly created for it.
function parseBody(html) {
  const body = html
    .slice(html.indexOf("<body>") + "<body>".length, html.indexOf("</body>"))
    .replace(/<!--[\s\S]*?-->/g, "");
  const root = makeElement("body");
  const stack = [root];
  const tagRe = /<(\/?)([a-zA-Z][a-zA-Z0-9-]*)((?:\s[^>]*?)?)\s*(\/?)>|([^<]+)/g;
  let match;
  while ((match = tagRe.exec(body)) !== null) {
    const [, closing, tag, attrs, selfClosing, text] = match;
    if (text !== undefined) {
      const trimmed = text.trim();
      if (trimmed) stack[stack.length - 1].textContent = trimmed;
      continue;
    }
    if (closing) {
      stack.pop();
      continue;
    }
    const element = makeElement(tag);
    for (const [, name, value] of (attrs || "").matchAll(/([a-zA-Z-]+)="([^"]*)"/g)) {
      if (name === "id") element.id = value;
      else if (name === "class") element.className = value;
      else if (name === "style") element.style = parseStyle(value);
      else if (name === "title") element.title = value;
    }
    stack[stack.length - 1].appendChild(element);
    if (!selfClosing && !VOID_TAGS.has(element.tagName)) stack.push(element);
  }
  return root;
}

function findAll(node, predicate, out = []) {
  if (predicate(node)) out.push(node);
  for (const child of node.children) findAll(child, predicate, out);
  return out;
}

function makeDocument(html) {
  const body = parseBody(html);
  const hasClass = (node, name) => node.className.split(/\s+/).includes(name);
  return {
    body,
    getElementById(id) {
      return findAll(body, (node) => node.id === id)[0] || null;
    },
    querySelector(selector) {
      if (selector.startsWith(".")) {
        return findAll(body, (node) => hasClass(node, selector.slice(1)))[0] || null;
      }
      if (selector.startsWith("#")) return this.getElementById(selector.slice(1));
      return findAll(body, (node) => node.tagName === selector)[0] || null;
    },
    createElement: makeElement,
  };
}

function fakeStorage() {
  const data = {};
  return {
    data,
    async get(key) { return key in data ? { [key]: data[key] } : {}; },
    async set(object) { Object.assign(data, object); },
    async remove(key) { delete data[key]; },
  };
}

const scriptSources = (html) =>
  [...html.matchAll(/<script\s+src="([^"]+)"\s*><\/script>/g)].map((m) => m[1]);

const styleHrefs = (html) =>
  [...html.matchAll(/<link\s+rel="stylesheet"\s+href="([^"]+)"\s*>/g)].map((m) => m[1]);

// ---- Loading a built popup ----

function loadPopup(browserName, {
  streams = [],
  pingReply = { status: "ok", version: "3.6.1" },
  statusReply = { status: "ok", downloads: [] },
  settingsReply = { enabled: true },
  streamReply = { ok: true },
  streamThrows = false,
  reportReply = { ok: true, text: "REPORT BODY" },
  clearReply = { ok: true },
  failReport = false,
} = {}) {
  const dir = path.join(bundles(), browserName, "popup");
  const html = fs.readFileSync(path.join(dir, "popup.html"), "utf8");
  const document = makeDocument(html);

  const sent = [];
  const clipboard = [];
  const openedOptions = [];
  const intervals = [];
  const timeouts = [];

  const browser = {
    runtime: {
      getManifest: () => ({ version: "1.4.7" }),
      async sendMessage(message) {
        sent.push(message);
        switch (message.type) {
          case "ping": return pingReply;
          case "getSettings": return settingsReply;
          case "saveSettings": return { ok: true };
          case "getStatus": return statusReply;
          case "getDetectedStreams": return streams;
          case "downloadStream":
            if (streamThrows) throw new Error("background is gone");
            return streamReply;
          case "coveDiag": return { ok: true };
          case "coveDiagReport":
            if (failReport) throw new Error("background is gone");
            return reportReply;
          case "coveDiagClear": return clearReply;
          default: return {};
        }
      },
      openOptionsPage() { openedOptions.push(true); },
    },
    storage: { local: fakeStorage() },
  };

  const context = vm.createContext({
    document,
    navigator: {
      userAgent: "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
      clipboard: { async writeText(text) { clipboard.push(text); } },
    },
    console: { log() {}, error() {} },
    // Controlled: nothing here waits on the wall clock, and the number of
    // recurring loops a popup installs is itself under test.
    setInterval(fn, ms) { intervals.push({ fn, ms }); return intervals.length; },
    setTimeout(fn, ms) { timeouts.push({ fn, ms }); return timeouts.length; },
    clearTimeout() {},
    URL,
    globalThis: undefined,
  });
  context.globalThis = context;
  context.browser = browser;
  context.chrome = browser;

  const loaded = [];
  for (const src of scriptSources(html)) {
    const file = path.resolve(dir, src);
    assert.ok(fs.existsSync(file), `${browserName} popup references a missing script: ${src}`);
    vm.runInContext(fs.readFileSync(file, "utf8"), context, { filename: src });
    loaded.push(src);
  }

  const tick = () => { for (const entry of intervals) entry.fn(); };
  const runTimeouts = () => {
    const pending = timeouts.splice(0, timeouts.length);
    for (const entry of pending) entry.fn();
  };

  return { dir, html, document, sent, clipboard, openedOptions,
           intervals, timeouts, tick, runTimeouts, loaded, context };
}

async function settle() {
  for (let i = 0; i < 4; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

const countOf = (sent, type) => sent.filter((m) => m.type === type).length;
const streamSection = (document) => document.getElementById("streams-section");
const streamRows = (document) => {
  const list = document.getElementById("streams-list");
  return list ? list.children : [];
};

// ---- Group A: Chrome ships no stream feature ----

test("the built Chrome popup ships no stream module or stylesheet", () => {
  const popup = path.join(bundles(), "chrome", "popup");
  assert.equal(fs.existsSync(path.join(popup, "streams.js")), false);
  assert.equal(fs.existsSync(path.join(popup, "streams.css")), false);

  const html = fs.readFileSync(path.join(popup, "popup.html"), "utf8");
  assert.deepEqual(
    scriptSources(html).filter((src) => src.includes("streams")), [],
    "no stream script may be referenced",
  );
  assert.deepEqual(
    styleHrefs(html).filter((href) => href.includes("streams")), [],
    "no stream stylesheet may be referenced",
  );
});

test("the built Chrome popup markup has no stream section", () => {
  const html = fs.readFileSync(
    path.join(bundles(), "chrome", "popup", "popup.html"), "utf8");
  for (const marker of ["streams-section", "streams-list", "Detected Streams"]) {
    assert.equal(html.includes(marker), false, `Chrome popup still ships ${marker}`);
  }
});

test("the built Chrome popup builds no stream section at runtime", async () => {
  const { document } = loadPopup("chrome", { streams: [{ url: "https://a.test/x.m3u8" }] });
  await settle();

  assert.equal(streamSection(document), null);
  assert.equal(document.getElementById("streams-list"), null);
});

test("the built Chrome popup never asks for detected streams", async () => {
  const { sent, tick } = loadPopup("chrome", {
    streams: [{ url: "https://a.test/live.m3u8" }],
  });
  await settle();
  assert.equal(countOf(sent, "getDetectedStreams"), 0, "not even once at open");

  for (let i = 0; i < 3; i += 1) tick();
  await settle();

  assert.equal(countOf(sent, "getDetectedStreams"), 0);
  assert.equal(countOf(sent, "downloadStream"), 0);
});

// ---- Group D: Firefox popup parity ----

test("the built Firefox popup loads its stream module before the shared popup",
     () => {
  const popup = path.join(bundles(), "firefox", "popup");
  assert.ok(fs.existsSync(path.join(popup, "streams.js")));

  const html = fs.readFileSync(path.join(popup, "popup.html"), "utf8");
  const scripts = scriptSources(html);
  assert.ok(scripts.includes("streams.js"), "the module must be referenced");
  assert.ok(
    scripts.indexOf("streams.js") < scripts.indexOf("popup.js"),
    "the hook has to be published before the shared popup initialises",
  );
  assert.equal(scripts.filter((s) => s === "streams.js").length, 1, "referenced once");
});

test("the built Firefox popup hides its stream section when nothing was detected",
     async () => {
  const { document } = loadPopup("firefox", { streams: [] });
  await settle();

  const section = streamSection(document);
  assert.ok(section, "the section exists so it can be shown later");
  assert.equal(section.style.display, "none");
});

test("the built Firefox popup renders detected streams above the footer",
     async () => {
  const { document } = loadPopup("firefox", {
    streams: [
      { url: "https://one.test/hls/live.m3u8?token=abc" },
      { url: "https://two.test/vod/movie.m3u8" },
    ],
  });
  await settle();

  const section = streamSection(document);
  assert.equal(section.style.display, "block");

  const siblings = document.body.children;
  const footerNode = document.querySelector(".footer");
  assert.equal(
    siblings.indexOf(section) + 1, siblings.indexOf(footerNode),
    "the section keeps its original position, immediately before the footer",
  );

  const header = section.children[0];
  assert.equal(header.className, "section-header");
  assert.equal(header.textContent, "Detected Streams");

  const rows = streamRows(document);
  assert.equal(rows.length, 2);
  assert.deepEqual(rows.map((row) => row.children[0].textContent),
                   ["hls/live.m3u8", "vod/movie.m3u8"]);
  assert.deepEqual(rows.map((row) => row.children[0].title),
                   ["https://one.test/hls/live.m3u8?token=abc",
                    "https://two.test/vod/movie.m3u8"]);
  assert.deepEqual(rows.map((row) => row.className), ["stream-item", "stream-item"]);
  assert.deepEqual(rows.map((row) => row.children[0].className),
                   ["stream-url", "stream-url"]);
  assert.deepEqual(rows.map((row) => row.children[1].className),
                   ["stream-download-btn", "stream-download-btn"]);
  assert.deepEqual(rows.map((row) => row.children[1].textContent),
                   ["Download", "Download"]);
});

test("a Firefox stream button sends one request with the derived filename",
     async () => {
  const { document, sent } = loadPopup("firefox", {
    streams: [{ url: "https://one.test/hls/live.m3u8?token=abc" }],
  });
  await settle();

  const button = streamRows(document)[0].children[1];
  await button.click();
  await settle();

  const requests = sent.filter((m) => m.type === "downloadStream");
  assert.equal(requests.length, 1, "one click, one request");
  assert.equal(requests[0].url, "https://one.test/hls/live.m3u8?token=abc");
  assert.equal(requests[0].filename, "live.mp4");
  assert.equal(button.textContent, "Sent!");
});

test("a Firefox stream button reports an unavailable Cove", async () => {
  const { document } = loadPopup("firefox", {
    streams: [{ url: "https://one.test/hls/live.m3u8" }],
    streamReply: { ok: false, error: "offline" },
  });
  await settle();

  const button = streamRows(document)[0].children[1];
  await button.click();
  await settle();

  assert.equal(button.textContent, "Cove unavailable");
});

test("a Firefox stream button survives a background that is gone", async () => {
  const { document } = loadPopup("firefox", {
    streams: [{ url: "https://one.test/hls/live.m3u8" }],
    streamThrows: true,
  });
  await settle();

  const button = streamRows(document)[0].children[1];
  await button.click();
  await settle();

  assert.equal(button.textContent, "Cove unavailable");
});

test("a Firefox stream button re-enables itself after its reset", async () => {
  const { document, runTimeouts } = loadPopup("firefox", {
    streams: [{ url: "https://one.test/hls/live.m3u8" }],
  });
  await settle();

  const button = streamRows(document)[0].children[1];
  await button.click();
  await settle();
  assert.equal(button.disabled, true, "disabled while the request is in flight");

  runTimeouts();

  assert.equal(button.textContent, "Download");
  assert.equal(button.disabled, false);
});

// ---- Group F: initialisation and refresh cost ----

test("the built Chrome popup installs one loop and polls downloads only",
     async () => {
  const { sent, intervals, tick } = loadPopup("chrome");
  await settle();

  assert.equal(intervals.length, 1, "exactly one recurring loop");
  assert.equal(intervals[0].ms, 2000, "at the cadence it shipped with");
  const before = countOf(sent, "getStatus");

  tick();
  await settle();

  assert.equal(countOf(sent, "getStatus"), before + 1);
  assert.equal(countOf(sent, "getDetectedStreams"), 0);
});

test("the built Firefox popup keeps one stream refresh per existing tick",
     async () => {
  const { sent, intervals, tick } = loadPopup("firefox", {
    streams: [{ url: "https://one.test/hls/live.m3u8" }],
  });
  await settle();

  assert.equal(intervals.length, 1, "no second recurring loop for streams");
  assert.equal(countOf(sent, "getDetectedStreams"), 1, "one refresh at open");

  for (let i = 0; i < 3; i += 1) tick();
  await settle();

  assert.equal(countOf(sent, "getDetectedStreams"), 4);
  assert.equal(countOf(sent, "getStatus"), 4, "downloads keep their own cadence");
});

test("the Firefox stream section is built once however often it refreshes",
     async () => {
  const { document, tick } = loadPopup("firefox", {
    streams: [{ url: "https://one.test/hls/live.m3u8" },
              { url: "https://two.test/vod/movie.m3u8" }],
  });
  await settle();

  for (let i = 0; i < 3; i += 1) tick();
  await settle();

  const sections = findAll(document.body, (node) => node.id === "streams-section");
  assert.equal(sections.length, 1, "one section, not one per refresh");
  assert.equal(streamRows(document).length, 2, "rows are replaced, not appended");

  const button = streamRows(document)[0].children[1];
  assert.equal((button.listeners.click || []).length, 1, "one listener per button");
});

// ---- Group E: the shared popup, in both browsers ----

for (const browserName of ["chrome", "firefox"]) {
  test(`the ${browserName} popup reports a connected Cove`, async () => {
    const { document } = loadPopup(browserName, {
      pingReply: { status: "ok", version: "3.6.1" },
    });
    await settle();

    assert.equal(document.getElementById("connection-status").textContent,
                 "Connected - Cove v3.6.1");
    assert.equal(document.getElementById("status-bar").className, "status-bar connected");
  });

  test(`the ${browserName} popup reports a Cove it cannot reach`, async () => {
    const { document } = loadPopup(browserName, { pingReply: null });
    await settle();

    assert.equal(document.getElementById("connection-status").textContent,
                 "Not connected to Cove");
    assert.equal(document.getElementById("status-bar").className, "status-bar error");
  });

  test(`the ${browserName} popup renders active downloads as text`, async () => {
    const { document } = loadPopup(browserName, {
      statusReply: {
        status: "ok",
        downloads: [{
          files: [{ path: "/tmp/<img src=x onerror=alert(1)>.zip" }],
          totalLength: "2000000", completedLength: "1000000", downloadSpeed: "500000",
        }],
      },
    });
    await settle();

    const items = document.getElementById("downloads-list").children;
    assert.equal(items.length, 1);
    // textContent, never HTML insertion: the element stub throws on innerHTML.
    assert.equal(items[0].children[0].textContent, "<img src=x onerror=alert(1)>.zip");
  });

  test(`the ${browserName} popup toggles interception`, async () => {
    const { document, sent } = loadPopup(browserName, { settingsReply: { enabled: true } });
    await settle();

    const toggle = document.getElementById("toggle-btn");
    assert.equal(toggle.textContent, "ON");

    await toggle.click();
    await settle();

    const saved = sent.filter((m) => m.type === "saveSettings");
    assert.equal(saved.length, 1);
    assert.equal(saved[0].settings.enabled, false);
    assert.equal(toggle.textContent, "OFF");
  });

  test(`the ${browserName} popup opens the options page`, async () => {
    const { document, openedOptions } = loadPopup(browserName);
    await settle();

    await document.getElementById("open-options").click();
    assert.deepEqual(openedOptions, [true]);
  });

  test(`the ${browserName} popup copies a diagnostics report`, async () => {
    const { document, clipboard } = loadPopup(browserName);
    await settle();

    const copy = document.getElementById("copy-diagnostics");
    await copy.click();
    await settle();

    assert.deepEqual(clipboard, ["REPORT BODY"]);
    assert.equal(copy.textContent, "Copied");
  });

  test(`the ${browserName} popup still reports with no background to ask`,
       async () => {
    const { document, clipboard } = loadPopup(browserName, { failReport: true });
    await settle();

    await document.getElementById("copy-diagnostics").click();
    await settle();

    assert.equal(clipboard.length, 1, "the stored copy is the fallback");
    assert.ok(clipboard[0].length > 0);
  });

  test(`the ${browserName} popup confirms a clear that happened`, async () => {
    const { document } = loadPopup(browserName, { clearReply: { ok: true } });
    await settle();

    const clear = document.getElementById("clear-diagnostics");
    await clear.click();
    await settle();

    assert.equal(clear.textContent, "Cleared");
  });

  test(`the ${browserName} popup refuses to claim a clear that did not happen`,
       async () => {
    const { document } = loadPopup(browserName, { clearReply: { ok: false } });
    await settle();

    const clear = document.getElementById("clear-diagnostics");
    await clear.click();
    await settle();

    assert.equal(clear.textContent, "Clear failed");
  });

  test(`the ${browserName} popup never sends the legacy stream action itself`,
       async () => {
    const { sent, tick } = loadPopup(browserName);
    await settle();
    for (let i = 0; i < 2; i += 1) tick();
    await settle();

    // Firefox sends it only from a stream row the user clicked, and there are
    // no rows here; Chrome has no route to it at all.
    assert.equal(countOf(sent, "downloadStream"), 0);
  });
}
