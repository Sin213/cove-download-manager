const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");
const harness = vm.createContext({require, console, setTimeout, clearTimeout, URL});
const prefix = fs.readFileSync("tests/extension_background.test.js", "utf8").split("\nasync function settle()")[0];
vm.runInContext(prefix + "\nglobalThis.load = loadBackground;", harness);
const flush = async () => { await new Promise(setImmediate); await new Promise(setImmediate); };
const url = "https://store-eu-par-6.gofile.io/download/web/11111111-aaaa-4aaa-8aaa-111111111111/movie.mp4";
const token = "FAKEsessionToken0123456789abcdef";
const otherToken = "FAKEotherToken0123456789abcdefgh";
const item = {id:1, url, filename:"movie.mp4", referrer:"https://gofile.io/d/ObvAFn9h",
  totalBytes:-1, fileSize:-1, incognito:false, state:"in_progress", cookieStoreId:"firefox-default"};
const tab = {id:7, url:item.referrer, incognito:false, cookieStoreId:item.cookieStoreId};
const load = options => harness.load({timers:true, settings:{enabled:true}, tabsById:new Map([[7, tab]]),
  nativeResult:{status:"ok", proof:"gofile-relay-v1"}, ...options});
const evaluate = (b, source) => vm.runInContext(source, b.context);
const created = (b, value = item) => evaluate(b, "handleCreated")(value);
const downloads = b => b.calls.native.filter(m => m.action.startsWith("download"));
function capture(b, changes = {}) {
  return b.events.beforeSendHeaders.emit({url, method:"GET", type:"main_frame", incognito:false,
    cookieStoreId:item.cookieStoreId, tabId:7, requestId:"fake-request",
    requestHeaders:[{name:"Cookie", value:`other=discard-me; accountToken=${token}; extra=discard-me-too`}], ...changes});
}
async function finishWait(b) { await flush(); b.fireTimers(2000); await flush(); }
async function secretFree(b) {
  b.fireTimers(1000); await flush();
  const native = b.calls.native.map(m => {
    if (m.action !== "download_gofile_relay") return m;
    const {gofileToken, ...otherFields} = m;
    return otherFields;
  });
  const state = JSON.stringify({calls:{...b.calls, native}, store:b.store.data,
    diagnostics:evaluate(b, "coveDiag ? coveDiag.records() : diagPending"), browserDownloads:b.browserDownloads});
  for (const secret of [token, otherToken, "accountToken", "discard-me"]) assert.equal(state.includes(secret), false, secret);
  assert.equal(b.calls.cookies.length, 0);
  for (const msg of downloads(b)) {
    assert.equal(msg.url.includes(token), false); assert.equal((msg.filename || "").includes(token), false);
  }
}
async function untouched(b) {
  assert.equal(b.calls.cancel.length, 0); assert.equal(b.calls.erase.length, 0);
  assert.equal(evaluate(b, `recentIntercepted.has(${JSON.stringify(url)})`), false); await secretFree(b);
}

const familyPages = [
  "https://gofile.io/file", "https://other.gofile.io/file", "https://api.gofile.io/file",
  "https://store-1.gofile.io/download/abcdef/movie.mp4", "https://store-1.gofile.io/download/direct/abcdef/movie.mp4",
  "https://STORE-1.GOFILE.IO/file", "https://store-1.gofile.io./file", "https://bücher.gofile.io/file",
  "https://ｇｏｆｉｌｅ.io/file", "https://gofile。io/file", "https://%67ofile.io/file",
  "https://store-1.%67ofile%2eio/file", url.replace("https:", "http:"), url.replace("https:", "ftp:"),
  url.replace(".io/", ".io:444/"), url.replace("https://", "https://user@"), url + "?token=fake",
  url + "#fragment", url.replace(".io/", ".io:/"), "https://store-1.gofile.io:bad/file",
  "https://[gofile.io/file", "https:///gofile.io/file", "https:/gofile.io/file", "https://gofile.io\\@other.test/file",
];
for (const label of ["store", "store3", "store12", "store-eu-par-6", "store-na-phx-1", "store3-eu",
  "cold-na-phx-8", "zone-1", "a1", "evilstore3", "store3x", "www1", "upload1"]) {
  test(`storage node capture and relay handoff: ${label}`, async () => {
    const b = load(), target = url.replace("store-eu-par-6", label);
    assert.equal(evaluate(b, "gofileStoreDelivery")(target), true);
    capture(b, {url:target}); await created(b, {...item, url:target}); await flush();
    assert.deepEqual(JSON.parse(JSON.stringify(downloads(b))),
      [{action:"download_gofile_relay", url:target, gofileToken:token, filename:"movie.mp4"}]);
    assert.deepEqual(Array.from(b.calls.cancel), [1]);
    assert.deepEqual(JSON.parse(JSON.stringify(b.calls.erase)), [{id:1}]); await secretFree(b);
  });
}
for (const authority of ["store3.gofile.io.evil.test", "www.gofile.io", "api.gofile.io", "upload.gofile.io", "uploads.gofile.io",
  "gofile.io", "a.b.gofile.io", "cold.na.gofile.io", "-cold.gofile.io", "cold_8.gofile.io", "cold-.gofile.io",
  "cold--8.gofile.io", "1evil.gofile.io", "cold-na-phx-8.gofile.io.evil.test", "evilgofile.io",
  "cold8.gofile.io:444", "a".repeat(64) + ".gofile.io",
  "store3.gofile.io:444", "store..gofile.io", "-store3.gofile.io", "store_3.gofile.io"]) {
  test(`storage node lookalike never captured: ${authority}`, async () => {
    const b = load(), target = url.replace("store-eu-par-6.gofile.io", authority);
    assert.equal(evaluate(b, "gofileStoreDelivery")(target), false);
    capture(b, {url:target}); await created(b); await finishWait(b);
    assert.equal(downloads(b).length, 0); assert.equal(evaluate(b, "gofileCaptures.size"), 0); await untouched(b);
  });
}
const coldURL = url.replace("store-eu-par-6", "cold-na-phx-8");
for (const target of [coldURL.replace("https:", "http:"), coldURL.replace("https://", "https://user@"),
  coldURL + "?query=1", coldURL + "#fragment", coldURL.replace("/download/web/", "/download/direct/"),
  coldURL.replace("/download/web/", "/web/")]) {
  test(`storage node URL constraints reject ${target}`, async () => {
    const b = load(); assert.equal(evaluate(b, "gofileStoreDelivery")(target), false);
    capture(b, {url:target}); await created(b); await finishWait(b);
    assert.equal(downloads(b).length, 0); assert.equal(evaluate(b, "gofileCaptures.size"), 0); await untouched(b);
  });
}
const newerInvalidCaptures = [
  {requestHeaders:[]}, {requestHeaders:undefined}, {requestHeaders:[{name:"Cookie", value:"other=1"}]},
  {requestHeaders:[{name:"Cookie", value:""}]},
  {requestHeaders:[{name:"Cookie", value:`accountToken=${token}`}, {name:"Cookie", value:"other=1"}]},
  {requestHeaders:[{name:"Cookie", value:`accountToken=${token}; accountToken=${otherToken}`}]},
  {requestHeaders:[{name:"Cookie", value:"accountToken=bad!"}]},
  {method:"POST"}, {type:"image"}, {type:"media"}, {incognito:true}, {tabId:-1},
];
for (const [i, change] of newerInvalidCaptures.entries()) test(`C1 newer invalid capture invalidates old session ${i}`, async () => {
  const b = load(); capture(b); capture(b, change); await created(b); await finishWait(b);
  assert.equal(downloads(b).length, 0); await untouched(b);
  capture(b, {requestHeaders:[{name:"Cookie", value:`accountToken=${otherToken}`} ]});
  await created(b, {...item, id:2}); await flush();
  assert.equal(downloads(b).length, 1); assert.equal(downloads(b)[0].gofileToken, otherToken); await secretFree(b);
});

test("C2 cross-tab evidence survives consumption while tab lookup is pending", async () => {
  let resolveTab;
  const pending = new Promise(resolve => {resolveTab = resolve;});
  const b = load({tabsById:new Map([[7, pending], [8, {...tab, id:8}]])});
  capture(b); await created(b); await flush();
  assert.deepEqual(Array.from(b.calls.tabsGet), [7]);
  capture(b, {tabId:8, requestHeaders:[{name:"Cookie", value:`accountToken=${otherToken}`} ]});
  resolveTab(tab); await flush();
  assert.equal(downloads(b).length, 0); await untouched(b);
  await created(b, {...item, id:2}); await finishWait(b);
  assert.equal(downloads(b).length, 0); await untouched(b);
});

for (const stage of ["native reply", "persistence", "deadline"]) {
  test(`post-final correlation recheck after ${stage} leaves Firefox and ownership alone`, async () => {
    let finishNative, finishWrite;
    const nativePending = new Promise(resolve => {finishNative = resolve;});
    const writePending = new Promise(resolve => {finishWrite = resolve;});
    const b = load({nativeResult:msg => msg.action === "download_gofile_relay" ? nativePending : {status:"ok"}});
    await flush();
    let writes = 0;
    const originalSet = b.store.set;
    if (stage === "persistence") b.store.set = async obj => {
      if ("_interceptedIds" in obj) {writes++; await writePending;}
      return originalSet(obj);
    };
    capture(b); await created(b); await flush();
    assert.equal(downloads(b).length, 1); assert.deepEqual(Array.from(b.calls.tabsGet), [7]);
    if (stage === "persistence") {
      finishNative({status:"ok", proof:"gofile-relay-v1"}); await flush();
      assert.equal(writes, 1, "pause in the real intercepted-id storage write");
    }
    capture(b, {tabId:8, requestHeaders:[{name:"Cookie", value:`accountToken=${otherToken}`} ]});
    if (stage === "deadline") b.fireTimers(30000);
    else if (stage === "persistence") finishWrite();
    else finishNative({status:"ok", proof:"gofile-relay-v1"});
    await flush();
    assert.equal(b.calls.cancel.length, 0); assert.equal(b.calls.erase.length, 0);
    assert.equal(evaluate(b, "interceptedIds.has(1) || ownedDownloads.has(1)"), false);
    assert.equal(evaluate(b, "gofilePending.size"), 0);
    assert.equal(b.calls.notifications.length, 1);
    assert.equal(b.calls.notifications[0].message,
      "Gofile download left to Firefox because two tabs requested it. Firefox is keeping the download.");
    if (stage === "deadline") {finishNative({status:"ok", proof:"gofile-relay-v1"}); await flush();}
    await untouched(b);
  });
}

test("post-final uncontested persistence wait still cancels and erases exactly once", async () => {
  let finishWrite;
  const gate = new Promise(resolve => {finishWrite = resolve;});
  const b = load(); await flush(); const originalSet = b.store.set;
  let writes = 0;
  b.store.set = async obj => {
    if ("_interceptedIds" in obj && ++writes === 1) await gate;
    return originalSet(obj);
  };
  capture(b); await created(b); await flush(); assert.equal(writes, 1);
  assert.equal(b.calls.cancel.length, 0);
  finishWrite(); await flush();
  assert.deepEqual(Array.from(b.calls.cancel), [1]);
  assert.deepEqual(JSON.parse(JSON.stringify(b.calls.erase)), [{id:1}]);
  assert.equal(evaluate(b, "interceptedIds.has(1) && ownedDownloads.has(1)"), true);
  assert.equal(b.store.data._interceptedIds.includes(1), true); await secretFree(b);
});

test("post-final conflict during cancel await prevents erasure and ownership", async () => {
  let finishCancel;
  const pending = new Promise(resolve => {finishCancel = resolve;});
  const b = load(); await flush();
  b.context.browser.downloads.cancel = async id => {b.calls.cancel.push(id); await pending;};
  capture(b); await created(b); await flush();
  assert.deepEqual(Array.from(b.calls.cancel), [1]); assert.equal(b.calls.erase.length, 0);
  assert.equal(evaluate(b, "interceptedIds.has(1) || ownedDownloads.has(1)"), false);
  capture(b, {tabId:8}); finishCancel(); await flush();
  assert.equal(b.calls.erase.length, 0);
  assert.equal(evaluate(b, "interceptedIds.has(1) || ownedDownloads.has(1)"), false);
  assert.equal(evaluate(b, `recentIntercepted.has(${JSON.stringify(url)})`), false);
  assert.equal(b.calls.notifications.some(n => n.title === "Download sent to Cove"), false); await secretFree(b);
});

test("post-final expired correlation rejects even a positive native proof", async () => {
  let finish;
  const b = load({nativeResult:msg => msg.action === "download_gofile_relay"
    ? new Promise(resolve => {finish = resolve;}) : {status:"ok"}});
  capture(b); await created(b); await flush();
  b.fireTimers(10000); finish({status:"ok", proof:"gofile-relay-v1"}); await flush();
  assert.equal(evaluate(b, "interceptedIds.has(1) || ownedDownloads.has(1)"), false);
  await untouched(b);
});

test("C2 token-free conflict records are TTL-bounded and capacity-bounded", async () => {
  const b = load(); capture(b); await created(b); await flush();
  assert.equal(evaluate(b, "gofileCorrelations.size"), 1);
  assert.equal(evaluate(b, "JSON.stringify([...gofileCorrelations.values()])").includes(token), false);
  for (let i=0; i<9; i++) capture(b, {url:url.replace("movie.mp4", `file${i}.mp4`)});
  assert.equal(evaluate(b, "gofileCorrelations.size"), 8);
  b.fireTimers(10000); assert.equal(evaluate(b, "gofileCorrelations.size"), 0); await secretFree(b);
});

test("C3 unresolved relay expires, releases exclusivity and ignores late positive proof", async () => {
  let finish;
  const b = load({nativeResult:msg => msg.action === "download_gofile_relay"
    ? new Promise(resolve => {finish = resolve;}) : {status:"ok"}});
  capture(b); await created(b); await flush();
  assert.equal(downloads(b).length, 1); b.fireTimers(30000); await flush();
  assert.equal(evaluate(b, "gofilePending.size"), 0); await untouched(b);
  assert.equal(evaluate(b, "GOFILE_RELAY_DEADLINE_MS"), 30000);
  const late = finish;
  capture(b, {requestHeaders:[{name:"Cookie", value:`accountToken=${otherToken}`} ]});
  await created(b, {...item, id:2}); await flush(); assert.equal(downloads(b).length, 2);
  late({status:"ok", proof:"gofile-relay-v1"}); await flush();
  assert.equal(b.calls.cancel.length, 0); assert.equal(b.calls.erase.length, 0);
  finish({status:"ok", proof:"gofile-relay-v1"}); await flush();
  assert.deepEqual(Array.from(b.calls.cancel), [2]); await secretFree(b);
});

test("C3 normal relay completion clears its deadline timer", async () => {
  const b = load(); const deadlineIds = [], cleared = [];
  const schedule = b.context.setTimeout, clear = b.context.clearTimeout;
  b.context.setTimeout = (fn, ms, ...args) => {const id = schedule(fn, ms, ...args); if(ms===30000) deadlineIds.push(id); return id;};
  b.context.clearTimeout = id => {cleared.push(id); clear(id);};
  capture(b); await created(b); await flush();
  assert.equal(deadlineIds.length, 1); assert.ok(cleared.includes(deadlineIds[0])); await secretFree(b);
});

for (const route of ["context", "pill", "popup", "bridge", "direct interception"]) {
  test(`C5 ${route} never dispatches generic Gofile download`, async () => {
    const b = load(); await flush();
    for (let i=0; i<2; i++) {
      if (route === "context") await Promise.all(b.events.contextMenuClicked.emit(
        {menuItemId:"download-with-cove", linkUrl:url, pageUrl:item.referrer}, tab));
      else if (route === "direct interception") {
        b.context.testGofileItem = {...item, url:"https://other.gofile.io/movie.mp4"};
        await evaluate(b, "interceptDownload(testGofileItem)");
      } else if (route === "bridge") await evaluate(b, "sendNativeMessage")({action:"download",url});
      else {
        if (route === "popup") b.events.headersReceived.emit({tabId:7,url,
          responseHeaders:[{name:"content-type",value:"application/vnd.apple.mpegurl"}]});
        await new Promise(resolve => b.events.message.emit(
          {type:route === "pill" ? "downloadMedia" : "downloadStream",url,pageUrl:item.referrer},
          route === "pill" ? {tab,url:tab.url,frameId:0}
            : {id:"covetestextensionid",url:"chrome-extension://covetestextensionid/popup/popup.html"}, resolve));
      }
      await flush();
    }
    assert.equal(downloads(b).length, 0); assert.equal(b.browserDownloads.length, 0);
    assert.equal(b.calls.notifications.length, 1);
    assert.equal(b.calls.notifications[0].message,
      "Gofile downloads are handed to Cove from the Gofile page's Download button. Firefox is keeping the download.");
    await untouched(b);
  });
}
for (const candidate of familyPages) test(`Firefox leaves non-delivery family URL alone: ${candidate}`, async () => {
  const b = load(); capture(b, {url:candidate}); await created(b, {...item, url:candidate}); await finishWait(b);
  assert.equal(downloads(b).length, 0); await untouched(b);
});

test("header-before-created sends only captured session relay payload and proof cancels matching id", async () => {
  const b = load(); capture(b); await created(b); await flush();
  assert.deepEqual(JSON.parse(JSON.stringify(downloads(b))), [{action:"download_gofile_relay", url, gofileToken:token, filename:"movie.mp4"}]);
  assert.deepEqual(Array.from(b.calls.cancel), [1]);
  assert.deepEqual(JSON.parse(JSON.stringify(b.calls.erase)), [{id:1}]);
  assert.equal(b.calls.notifications.length, 1); assert.equal(JSON.stringify(b.calls.notifications).includes(url), false);
  assert.equal(evaluate(b, "gofileCaptures.size"), 0);
  assert.equal(evaluate(b, "ownedDownloads.has(1) && !ownedDownloads.has(2)"), true); await secretFree(b);
});
test("created-before-header waits and consumes late capture", async () => {
  const b = load(); await created(b); await flush();
  assert.equal(downloads(b).length, 0); assert.equal(b.calls.cancel.length, 0);
  capture(b); await flush(); b.fireTimers(2000); await flush();
  assert.equal(downloads(b).length, 1); assert.equal(downloads(b)[0].gofileToken, token);
  assert.deepEqual(Array.from(b.calls.cancel), [1]); assert.equal(evaluate(b, "gofileCaptures.size"), 0); await secretFree(b);
});
test("no headers waits bounded fallback then keeps Firefox", async () => {
  const b = load(); await created(b); await flush();
  assert.equal(evaluate(b, "GOFILE_CAPTURE_WAIT_MS"), 2000); assert.equal(evaluate(b, "gofilePending.size"), 1);
  assert.equal(downloads(b).length, 0); await finishWait(b);
  assert.equal(downloads(b).length, 0); assert.equal(evaluate(b, "gofilePending.size"), 0); await untouched(b);
});

const invalidCaptures = [
  ...familyPages.map(url => ({url})), {url:"https://evil-gofile.io/download/web/abcdef/movie.mp4"},
  {url:url.replace("gofile.io", "gofile.io.evil.test")}, {type:"image"}, {type:"media"}, {type:"sub_frame"},
  {method:"POST"}, {method:"get"}, {incognito:true}, {incognito:undefined},
  {cookieStoreId:""}, {cookieStoreId:undefined}, {tabId:-1}, {tabId:undefined}, {tabId:7.5}, {tabId:"7"},
  {requestHeaders:[]}, {requestHeaders:[{name:"Cookie", value:"other=1"}]},
  {requestHeaders:[{name:"Cookie", value:`accountToken=${token}`}, {name:"cOoKiE", value:"other=1"}]},
  ...["", "short", "a".repeat(129), "fake token with spaces", "a".repeat(31) + "!", '"' + token + '"',
    token + "; accountToken=" + otherToken].map(value => ({requestHeaders:[{name:"Cookie", value:`accountToken=${value}`}]})),
  {requestHeaders:[{name:"Cookie", value:`AccountToken=${token}`}]},
];
for (const [i, change] of invalidCaptures.entries()) test(`unsafe capture rejected ${i}: ${JSON.stringify(change)}`, async () => {
  const b = load(); capture(b, change); await created(b); await finishWait(b);
  assert.equal(downloads(b).length, 0); assert.equal(evaluate(b, "gofileCaptures.size"), 0); await untouched(b);
});
test("one case-insensitive Cookie header accepts only exact token pair", async () => {
  const b = load(); capture(b, {requestHeaders:[{name:"cOoKiE", value:`other=x;  accountToken=${token}  ; extra=y`}]});
  await created(b); await flush(); assert.equal(downloads(b)[0].gofileToken, token); await secretFree(b);
});
test("two tabs same URL and container are ambiguous including rapid reclick", async () => {
  const b = load(); capture(b); capture(b, {tabId:8}); capture(b);
  assert.equal(evaluate(b, "Array.from(gofileCaptures.values())[0].token"), null);
  await created(b); await flush(); assert.equal(downloads(b).length, 0); await untouched(b);
  capture(b); await created(b, {...item, id:2}); await flush();
  assert.equal(downloads(b).length, 0); await untouched(b);
  b.fireTimers(10000); assert.equal(evaluate(b, "gofileCaptures.size"), 0);
});
test("same tab replaces token and original TTL timer cannot delete replacement", async () => {
  const b = load(); const timers = []; const original = b.context.setTimeout;
  b.context.setTimeout = (fn, ms, ...args) => { if (ms === 10000) timers.push(fn); return original(fn, ms, ...args); };
  capture(b); capture(b, {requestHeaders:[{name:"Cookie", value:`accountToken=${otherToken}`} ]}); timers[0]();
  await created(b); await flush(); assert.equal(downloads(b)[0].gofileToken, otherToken);
  assert.equal(evaluate(b, "gofileCaptures.size"), 0); await secretFree(b);
});
test("URL and container correlation never mix sessions with identical filenames", async () => {
  for (const change of [{url:url.replace("111111111111", "222222222222")}, {cookieStoreId:"firefox-container-2"}]) {
    const b = load(); capture(b, change); await created(b); await finishWait(b);
    assert.equal(downloads(b).length, 0); await untouched(b);
  }
  const secondURL = url.replace("111111111111", "222222222222"); const b = load(); capture(b);
  capture(b, {url:secondURL, requestHeaders:[{name:"Cookie", value:`accountToken=${otherToken}`} ]});
  await created(b, {...item, url:secondURL}); await flush();
  assert.equal(downloads(b)[0].url, secondURL); assert.equal(downloads(b)[0].gofileToken, otherToken); await secretFree(b);
});
test("capture single use after refusal clears URL dedup", async () => {
  const b = load({nativeResult:{status:"ok"}}); capture(b); await created(b); await flush();
  await created(b, {...item, id:2}); await finishWait(b);
  assert.equal(downloads(b).length, 1); assert.equal(evaluate(b, "gofileCaptures.size"), 0); await untouched(b);
});
test("TTL expiry, on-use staleness and capacity eight eviction", async () => {
  const expired = load(); capture(expired); expired.fireTimers(10000);
  await created(expired); await finishWait(expired); assert.equal(downloads(expired).length, 0); await untouched(expired);
  const stale = load(); capture(stale); evaluate(stale, "for (const e of gofileCaptures.values()) e.at -= 10001");
  await created(stale); await flush(); assert.equal(downloads(stale).length, 0); await untouched(stale);
  const b = load(); for (let i = 0; i < 9; i++) capture(b, {url:url.replace("movie.mp4", `movie${i}.mp4`)});
  assert.equal(evaluate(b, "GOFILE_CAPTURE_TTL_MS"), 10000); assert.equal(evaluate(b, "GOFILE_CAPTURE_MAX"), 8);
  assert.equal(evaluate(b, "gofileCaptures.size"), 8);
  await created(b, {...item, url:url.replace("movie.mp4", "movie0.mp4")}); await finishWait(b);
  assert.equal(downloads(b).length, 0); b.fireTimers(10000); assert.equal(evaluate(b, "gofileCaptures.size"), 0); await untouched(b);
});

for (const [name, replacement] of Object.entries({closed:undefined, rejects:new Error("closed"),
  private:{...tab, incognito:true}, unknownPrivacy:{...tab, incognito:undefined}, container:{...tab, cookieStoreId:"firefox-container-2"},
  differentPage:{...tab, url:"https://gofile.io/d/Abcdef"}, wrongHost:{...tab, url:"https://evil.test/d/ObvAFn9h"},
  insecure:{...tab, url:"http://gofile.io/d/ObvAFn9h"}, userinfo:{...tab, url:"https://user@gofile.io/d/ObvAFn9h"}})) {
  test(`tab verification rejects ${name} and consumes capture`, async () => {
    const b = load({tabsById:new Map(replacement === undefined ? [] : [[7, replacement]])});
    capture(b); await created(b); await flush(); assert.equal(downloads(b).length, 0);
    assert.equal(evaluate(b, "gofileCaptures.size"), 0); await untouched(b);
  });
}
test("tab without container field verifies same share host and path", async () => {
  const b = load({tabsById:new Map([[7, {url:item.referrer + "?view=1", incognito:false}]])});
  capture(b); await created(b); await flush(); assert.equal(downloads(b).length, 1); await secretFree(b);
});
for (const result of [{status:"ok"}, {status:"ok", proof:"gofile-v1"}, {status:"ok", proof:"wrong"},
  null, {}, "bad", {status:"error", message:"Unknown action"},
  {status:"error", reason:"gofile_denied", message:"Firefox is keeping the download."}]) {
  test(`only relay proof grants ownership: ${JSON.stringify(result)}`, async () => {
    const b = load({nativeResult:result}); capture(b); await created(b); await flush();
    assert.equal(downloads(b).length, 1); assert.equal(downloads(b)[0].action, "download_gofile_relay");
    assert.equal(evaluate(b, "gofileCaptures.size"), 0); assert.equal(evaluate(b, "ownedDownloads.size"), 0);
    assert.equal(evaluate(b, "interceptedIds.size"), 0);
    assert.equal(b.calls.notifications.some(n => n.title === "Download sent to Cove"), false); await untouched(b);
  });
}
for (const mode of ["transport", "timeout", "exception"]) test(`${mode} stays silent and releases session`, async () => {
  let finish;
  const b = load({nativeResult:msg => {
    if (msg.action !== "download_gofile_relay") return {status:"ok"};
    if (mode === "timeout") return new Promise(resolve => {finish = resolve;});
    throw new Error(token + " accountToken");
  }});
  capture(b); await created(b); await flush();
  if (mode === "timeout") { finish({status:"error", transport:"error", message:token + " accountToken"}); await flush(); }
  assert.equal(downloads(b).length, 1); assert.equal(b.calls.notifications.length, 0);
  assert.equal(evaluate(b, "gofileCaptures.size"), 0); await untouched(b);
});
test("malformed host reply cannot echo session into diagnostics or notifications", async () => {
  for (const nativeResult of [{status:token}, {status:"error", reason:"gofile_" + token, message:token + " accountToken"}]) {
    const b = load(); await flush();
    // The shared mock itself copies reply.status into calls.order. Model a
    // malformed reply without that mock-only echo, so the oracle measures
    // production diagnostics and notification sinks.
    b.context.browser.runtime.sendNativeMessage = async (_host, message) => {
      b.calls.native.push(message); b.calls.order.push("native:sent"); return nativeResult;
    };
    capture(b); await created(b); await flush(); await untouched(b);
  }
});
test("synchronous native exception clears capture, dedup and pending guard", async () => {
  const b = load(); capture(b);
  b.context.browser.runtime.sendNativeMessage = () => {throw new Error(token + " accountToken");};
  await created(b); await flush(); assert.equal(evaluate(b, "gofileCaptures.size + gofilePending.size"), 0);
  await untouched(b);
});
test("relay refusal text never directs users to API credentials", () => {
  const b = load();
  for (const message of Object.values(evaluate(b, "GOFILE_RELAY_REFUSALS"))) {
    assert.doesNotMatch(message, /token|API|Settings/i);
    assert.ok(message.endsWith("Firefox is keeping the download."));
  }
});
const relayRefusalCases = [
  ["gofile_empty", "Cove does not take over empty Gofile files. Firefox is keeping the download."],
  ["gofile_needs_token", "Cove could not prepare this Gofile download. Download it again from Gofile in Firefox. Firefox is keeping the download."],
  ["gofile_denied", "Gofile refused access to this file. Download it again from Gofile in Firefox. Firefox is keeping the download."],
  ["gofile_page", "Gofile did not return the file. Download it again from Gofile in Firefox. Firefox is keeping the download."],
  ["gofile_restart", "Update or restart Cove to hand this Gofile download over. Firefox is keeping the download."],
  ["gofile_token", "Cove could not complete this Gofile handoff. Download it again from Gofile in Firefox. Firefox is keeping the download."],
  [null, "Update Cove to hand this Gofile download over. Firefox is keeping the download."],
];
for (const [reason, expected] of relayRefusalCases) test(`refusal notification deduplicates ${reason || "older Cove"}`, async () => {
  // The frozen backend may still return legacy text; only the fixed relay
  // message selected by the extension should reach the user.
  const message = "Legacy API token Settings text must not be shown.";
  const b = load({nativeResult:reason ? {status:"error", reason, message} : {status:"error", message:"Unknown action"}});
  for (let i = 0; i < 2; i++) { capture(b); await created(b, {...item, id:i + 1}); await flush(); }
  assert.equal(downloads(b).length, 2); assert.equal(b.calls.notifications.length, 1);
  assert.equal(b.calls.notifications[0].message, expected); await untouched(b);
});
test("delayed proof remains exclusive through rapid events and dedup expiry", async () => {
  let finish;
  const b = load({nativeResult:msg => msg.action.startsWith("download") ? new Promise(resolve => {finish = resolve;}) : {status:"ok"}});
  capture(b); await Promise.all([created(b), created(b, {...item, id:2})]); await flush();
  evaluate(b, "recentIntercepted.clear()"); await created(b, {...item, id:3}); await flush();
  assert.equal(downloads(b).length, 1); assert.equal(b.calls.cancel.length, 0);
  finish({status:"ok", proof:"gofile-relay-v1"}); await flush();
  assert.deepEqual(Array.from(b.calls.cancel), [1]); assert.equal(evaluate(b, "gofileCaptures.size"), 0); await secretFree(b);
});
test("restart has no captures and cannot own or erase old history", async () => {
  const first = load(); capture(first); await secretFree(first);
  const b = load({storedIntercepted:[1], downloadState:[{...item, startTime:"2020-01-01T00:00:00Z"}]});
  await created(b); await finishWait(b); await created(b, {...item, startTime:"2020-01-01T00:00:00Z"});
  await Promise.all(b.events.downloadChanged.emit({id:1, state:{current:"complete"}})); await flush();
  assert.equal(downloads(b).length, 0); assert.equal(evaluate(b, "gofileCaptures.size"), 0); await untouched(b);
});
test("enabled, privacy, exclusions, size and extension filters still apply", async () => {
  for (const options of [{settings:{enabled:false}}, {settings:{enabled:true, excludedDomains:["gofile.io"]}},
    {settings:{enabled:true, interceptExtensions:[".zip"]}}, {settings:{enabled:true, minSizeBytes:10000}, small:true},
    {private:true}, {unknownPrivacy:true}]) {
    const b = load(options); capture(b);
    await created(b, {...item, totalBytes:options.small ? 10 : -1, incognito:options.unknownPrivacy ? undefined : !!options.private});
    await finishWait(b); assert.equal(downloads(b).length, 0); await untouched(b);
  }
  for (const cookieStoreId of [undefined, ""]) {
    const b = load(); capture(b); await created(b, {...item, cookieStoreId}); await finishWait(b);
    assert.equal(downloads(b).length, 0); await untouched(b);
  }
});
test("pages and delivery without full share referrer stay in Firefox", async () => {
  for (const change of [{url:item.referrer}, {url:"https://gofile.io/"}, {url:"https://api.gofile.io/contents/abc"},
    {referrer:""}, {referrer:"https://evil.test/d/ObvAFn9h"}, {referrer:"https://gofile.io/"}]) {
    const b = load(); capture(b); await created(b, {...item, ...change}); await finishWait(b);
    assert.equal(downloads(b).length, 0); await untouched(b);
  }
});
test("family and delivery classifiers reject lookalikes and ambiguous authorities", () => {
  const b = load(); const family = evaluate(b, "isGofileFamily"); const classify = evaluate(b, "gofileDownloadKind");
  assert.equal(family(url), true); assert.equal(classify(item), "delivery");
  for (const candidate of ["https://evil-gofile.io/file", "https://gofile.io.evil.test/file", "https://example.test/gofile.io/file",
    "https://gofile.io@other.test/file", "file://gofile.io/file", null]) assert.equal(family(candidate), false);
  for (const candidate of [url + "?", url + "#", url.replace(".io/", ".io:444/"),
    url.replace("https://", "https://user@"), url.replace(".io/", ".io\\evil/"), url.replace("https://", "https:///"),
    url.replace("gofile.io", "gofile.io.evil"), url.replace("store-eu-par-6", "store--bad"), url.replace("gofile", "gofıle"),
    url.replace("store-eu-par-6", "%73tore-eu-par-6"), url.slice(0, url.lastIndexOf("/")) + "/.",
    url.slice(0, url.lastIndexOf("/")) + "/%2e%2e", url.replace("store-eu-par-6", "xn--store-9za"), url + "/extra", url + "\n"])
    assert.equal(classify({...item, url:candidate}), null, candidate);
  for (const referrer of ["http://gofile.io/d/ObvAFn9h", "https://user@gofile.io/d/ObvAFn9h", "https://gofile.io:444/d/ObvAFn9h",
    "https://gofile.io.evil/d/ObvAFn9h"]) assert.equal(classify({...item, referrer}), null);
});
test("Chrome leaves storage delivery alone, refuses generic Gofile, and keeps ordinary downloads unchanged", async () => {
  const b = load(); b.context.navigator.userAgent = "Chrome/159.0";
  capture(b); await created(b); await finishWait(b); assert.equal(downloads(b).length, 0); await untouched(b);
  await created(b, {...item, url:"https://other.gofile.io/file"}); await flush();
  assert.equal(downloads(b).length, 0);
  await created(b, {...item, url:"https://example.test/file.mp4"}); await flush();
  assert.equal(downloads(b).length, 1); assert.equal(downloads(b)[0].action, "download"); assert.equal(b.calls.cancel.length, 1);
});
test("capture registration is Firefox-only non-blocking and needs no cookies API", async () => {
  const b = load(); assert.equal(b.events.beforeSendHeaders.listeners.length, 1);
  b.context.browser.cookies = null; capture(b); await created(b); await flush(); assert.equal(downloads(b).length, 1); await secretFree(b);
  const chrome = vm.createContext({require, console, setTimeout, clearTimeout, URL});
  vm.runInContext(prefix.replace("Firefox/140.0", "Chrome/159.0") + "\nglobalThis.load = loadBackground;", chrome);
  const worker = chrome.load({worker:true, timers:true});
  assert.equal(worker.events.beforeSendHeaders.listeners.length, 0);
  const absent = vm.createContext({require, console, setTimeout, clearTimeout, URL});
  vm.runInContext(prefix.replace("webRequest: { onHeadersReceived: events.headersReceived, onBeforeSendHeaders: events.beforeSendHeaders }", "webRequest: null") +
    "\nglobalThis.load = loadBackground;", absent);
  assert.equal(absent.load({worker:true, timers:true}).events.beforeSendHeaders.listeners.length, 0);
  const source = fs.readFileSync("extension/background.js", "utf8");
  const listener = source.slice(source.indexOf("browser.webRequest.onBeforeSendHeaders.addListener"));
  assert.match(listener, /urls:\s*\["https:\/\/\*\.gofile\.io\/download\/web\/\*"\]/);
  assert.match(listener, /types:\s*\["main_frame"\]/); assert.match(listener, /\["requestHeaders"\]/);
  assert.doesNotMatch(listener.split("async function handleCreated")[0], /"blocking"/);
  assert.doesNotMatch(source, /"download_gofile"|"gofile-v1"/);
});
