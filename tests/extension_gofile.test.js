const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");
const harness = vm.createContext({require, console, setTimeout, clearTimeout, URL});
const prefix = fs.readFileSync("tests/extension_background.test.js", "utf8").split("\nasync function settle()")[0];
vm.runInContext(prefix + "\nglobalThis.load = loadBackground;", harness);
const flush = async () => { await new Promise(setImmediate); await new Promise(setImmediate); };
const url = "https://store-eu-par-6.gofile.io/download/web/11111111-aaaa-4aaa-8aaa-111111111111/movie.mp4";
const item = {id:1, url, filename:"movie.mp4", referrer:"https://gofile.io/d/ObvAFn9h",
  totalBytes:-1, fileSize:-1, incognito:false, state:"in_progress", cookieStoreId:"firefox-default"};
const load = options => harness.load({timers:true, settings:{enabled:true}, nativeResult:{status:"ok", proof:"gofile-v1"}, ...options});
const created = (b, value = item) => vm.runInContext("handleCreated", b.context)(value);
const downloads = b => b.calls.native.filter(m => m.action.startsWith("download"));

const familyPages = [
  "https://gofile.io/file", "https://other.gofile.io/file", "https://api.gofile.io/file",
  "https://store-1.gofile.io/download/abcdef/movie.mp4", "https://store-1.gofile.io/download/direct/abcdef/movie.mp4",
  "https://STORE-1.GOFILE.IO/file", "https://store-1.gofile.io./file", "https://bücher.gofile.io/file",
  "https://ｇｏｆｉｌｅ.io/file", "https://gofile。io/file", "https://%67ofile.io/file",
  "https://store-1.%67ofile%2eio/file", url.replace("https:", "http:"), url.replace("https:", "ftp:"),
  url.replace(".io/", ".io:444/"), url.replace("https://", "https://user@"), url + "?token=not-a-real-token",
  url + "#fragment", url.replace(".io/", ".io:/"), "https://store-1.gofile.io:bad/file",
  "https://[gofile.io/file", "https:///gofile.io/file", "https:/gofile.io/file", "https://gofile.io\\@other.test/file",
];
for (const candidate of familyPages) {
  test(`Firefox leaves non-delivery Gofile family URL alone: ${candidate}`, async () => {
    const b = load(); await created(b, {...item, url:candidate}); await flush();
    assert.equal(downloads(b).length, 0);
    assert.equal(b.calls.cancel.length, 0);
  });
}

test("Chrome interception of other Gofile URLs remains unchanged", async () => {
  const b = load();
  b.context.navigator.userAgent = "Chrome/159.0";
  await created(b, {...item, url:"https://other.gofile.io/file"}); await flush();
  assert.equal(downloads(b).length, 1);
  assert.equal(b.calls.cancel.length, 1);
});

test("family predicate does not claim lookalikes or path mentions", () => {
  const b = load();
  const family = vm.runInContext("isGofileFamily", b.context);
  assert.equal(family(url), true);
  for (const candidate of ["https://evil-gofile.io/file", "https://gofile.io.evil.test/file",
    "https://example.test/gofile.io/file", "https://gofile.io@other.test/file", "file://gofile.io/file", null]) {
    assert.equal(family(candidate), false);
  }
});

test("real Firefox event waits for proof before cancel", async () => {
  let finish;
  const pending = new Promise(resolve => {finish = resolve;});
  const b = load({nativeResult:msg => msg.action.startsWith("download") ? pending : {status:"ok"}});
  await created(b); await flush();
  assert.equal(downloads(b).length, 1);
  assert.equal(downloads(b)[0].action, "download_gofile");
  assert.equal(downloads(b)[0].fileSize, 0);
  assert.equal(downloads(b)[0].filename, "movie.mp4");
  assert.equal(b.calls.cancel.length, 0);
  finish({status:"ok", proof:"gofile-v1"}); await flush();
  assert.deepEqual(Array.from(b.calls.cancel), [1]);
});

for (const result of [{status:"ok"}, {status:"ok", proof:"wrong"}, {status:"error", message:"Unknown action"}]) {
  test(`mixed-version host response keeps Firefox: ${JSON.stringify(result)}`, async () => {
    const b = load({nativeResult:result}); await created(b); await flush();
    assert.equal(b.calls.cancel.length, 0);
    assert.equal(vm.runInContext("recentIntercepted.has(" + JSON.stringify(url) + ")", b.context), false);
    assert.equal(b.calls.notifications.some(n => n.title === "Download sent to Cove"), false);
  });
}

test("older host refuses the new action before it can queue a Gofile request", async () => {
  let queued = 0;
  const b = load({nativeResult:message => {
    if (message.action === "download") { queued++; return {status:"ok"}; }
    return {status:"error", message:"Unknown action"};
  }});
  await created(b); await flush();
  assert.equal(queued, 0);
  assert.equal(b.calls.cancel.length, 0);
});

test("pages and delivery without full share referrer stay in Firefox", async () => {
  for (const change of [{url:item.referrer}, {url:"https://gofile.io/"}, {url:"https://api.gofile.io/contents/abc"},
    {referrer:""}, {referrer:"https://evil.test/d/ObvAFn9h"}, {referrer:"https://gofile.io/"}]) {
    const b = load(); await created(b, {...item, ...change}); await flush();
    assert.equal(downloads(b).length, 0, JSON.stringify(change));
    assert.equal(b.calls.cancel.length, 0);
  }
});

for (const reason of ["gofile_needs_token", "gofile_empty"]) test(`refusal notifies once without cancellation: ${reason}`, async () => {
  const message = reason === "gofile_empty" ? "Cove does not take over empty Gofile files. Firefox is keeping the download." :
    "Add your Gofile token in Settings. Firefox is keeping the download.";
  const b = load({nativeResult:{status:"error", reason, message}});
  await created(b); await flush();
  await created(b, {...item, id:2}); await flush();
  assert.equal(b.calls.cancel.length, 0);
  assert.equal(downloads(b).length, 2);
  assert.equal(b.calls.notifications.length, 1);
  assert.equal(b.calls.notifications[0].message, message);
  assert.equal(JSON.stringify(b.calls.console).includes(url), false);
});

test("concurrent identical events and delayed response hand off only once", async () => {
  let finish;
  const pending = new Promise(resolve => {finish = resolve;});
  const b = load({nativeResult:msg => msg.action.startsWith("download") ? pending : {status:"ok"}});
  await Promise.all([created(b), created(b, {...item, id:2})]); await flush();
  assert.equal(downloads(b).length, 1);
  assert.equal(b.calls.cancel.length, 0);
  finish({status:"ok", proof:"gofile-v1"}); await flush();
  assert.equal(b.calls.cancel.length, 1);
});

test("slow proof stays exclusive beyond the dedup window", async () => {
  let finish;
  const pending = new Promise(resolve => {finish = resolve;});
  const b = load({nativeResult:msg => msg.action.startsWith("download") ? pending : {status:"ok"}});
  await created(b); await flush();
  vm.runInContext("recentIntercepted.clear()", b.context); // models expiry during the 15s proof
  await created(b, {...item, id:2}); await flush();
  assert.equal(downloads(b).length, 1);
  finish({status:"error", reason:"gofile_denied", message:"Firefox is keeping the download."});
  await flush();
  assert.equal(b.calls.cancel.length, 0);
  assert.equal(vm.runInContext("gofilePending.size", b.context), 0);
});

test("classifiers reject ambiguous authorities and mismatched referrers", () => {
  const b = load();
  const classify = vm.runInContext("gofileDownloadKind", b.context);
  assert.equal(classify(item), "delivery");
  for (const candidate of [url + "?", url + "#", url.replace(".io/", ".io:444/"),
    url.replace("https://", "https://user@"), url.replace(".io/", ".io\\evil/"),
    url.replace("https://", "https:///"), url.replace("gofile.io", "gofile.io.evil"),
    url.replace("store-eu-par-6", "store--bad"), url.replace("gofile", "gofıle"),
    url.replace("store-eu-par-6", "%73tore-eu-par-6"),
    url.slice(0, url.lastIndexOf("/")) + "/.", url.slice(0, url.lastIndexOf("/")) + "/%2e%2e",
    url.replace("store-eu-par-6", "xn--store-9za"), url + "/extra", url + "\n"]) {
    assert.equal(classify({...item, url:candidate}), null, candidate);
  }
  for (const referrer of ["http://gofile.io/d/ObvAFn9h", "https://user@gofile.io/d/ObvAFn9h",
    "https://gofile.io:444/d/ObvAFn9h", "https://gofile.io.evil/d/ObvAFn9h"]) {
    assert.equal(classify({...item, referrer}), null);
  }
});

test("transport failures and older-host refusals leave Firefox alive", async () => {
  for (const nativeResult of [null, {status:"error", message:"Cove is not available"},
    () => {throw Error("transport unavailable");}]) {
    const b = load({nativeResult}); await created(b); await flush();
    assert.equal(b.calls.cancel.length, 0);
    assert.equal(b.calls.notifications.length, 0);
  }
});

test("restart cannot claim old browser history or erase a download it does not own", async () => {
  const b = load({storedIntercepted:[1], downloadState:[{...item, startTime:"2020-01-01T00:00:00Z"}]});
  await created(b, {...item, startTime:"2020-01-01T00:00:00Z"}); await flush();
  await Promise.all(b.events.downloadChanged.emit({id:1, state:{current:"complete"}})); await flush();
  assert.equal(downloads(b).length, 0);
  assert.equal(b.calls.cancel.length, 0);
  assert.equal(b.calls.erase.length, 0);
});

test("enabled, privacy, exclusions, size and extension filters still apply", async () => {
  for (const options of [{settings:{enabled:false}}, {settings:{enabled:true, excludedDomains:["gofile.io"]}},
    {settings:{enabled:true, interceptExtensions:["zip"]}},
    {settings:{enabled:true, minSizeBytes:10000}, small:true}, {private:true}]) {
    const b = load(options);
    await created(b, {...item, totalBytes:options.small ? 10 : -1, incognito:!!options.private}); await flush();
    assert.equal(downloads(b).length, 0);
  }
});
