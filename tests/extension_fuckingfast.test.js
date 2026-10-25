const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");
const NONCE = "a".repeat(32), ID = "6oavo2x52dq0";
const page = "https://fuckingfast.co/" + ID;
const flush = async () => { await new Promise(setImmediate); await new Promise(setImmediate); };

function content({hash = "#cove-fetch=" + NONCE, path = "/" + ID, reply = {ok:true, hint:"a file.zip"},
                  title = "", control = false, frame = false} = {}) {
  let now = 0, interval, clicks = 0, queries = 0, calls = [], replaced = [];
  let node = control ? {hx: "/f/" + ID + "/go"} : null;
  const element = {isConnected:true, getAttribute: () => node && node.hx, click: () => clicks++};
  const document = {title, readyState:"complete", querySelector(selector) {
    queries++;
    if (selector.startsWith("a[")) return node && node.hx === "/f/" + ID + "/go" ? element : null;
    if (selector.includes("iframe")) return frame ? {} : null;
    return null;
  }};
  const context = vm.createContext({document, location:{hash, pathname:path, search:"", hostname:"fuckingfast.co"},
    history:{replaceState: (...args) => replaced.push(args)}, Date:{now: () => now},
    browser:{runtime:{sendMessage: async msg => {calls.push(msg); return reply;}}},
    setInterval: fn => {interval = fn; return 1;}, clearInterval: () => {interval = null;}});
  vm.runInContext(fs.readFileSync("extension/content/fuckingfast.js", "utf8"), context);
  return {calls, replaced, document, clicks:()=>clicks, queries:()=>queries,
    control: (hx = "/f/" + ID + "/go") => {node = {hx};},
    step: async ms => {now += ms; if(interval) interval(); await flush();}};
}

for (const opts of [{hash:""}, {hash:"#cove-fetch=bad"}, {path:"/help"}, {path:"/f/LOGIN"}]) {
  test("ordinary page is inert " + JSON.stringify(opts), async () => {
    const c=content({...opts, control:true}); await c.step(1000);
    assert.equal(c.queries(),0); assert.equal(c.clicks(),0); assert.equal(c.calls.length,0);
  });
}
test("waits for exact control, claims once, restores hint and clicks once", async () => {
  const c=content(); assert.equal(c.calls.length,0);
  c.control("/f/wrong/go"); await c.step(250); assert.equal(c.calls.length,0);
  c.control(); await c.step(250); await c.step(250);
  assert.equal(c.calls.length,1); assert.equal(c.clicks(),1);
  assert.equal(c.calls[0].nonce, NONCE);
  assert.ok(c.replaced[0][2].endsWith("#a%20file.zip"));
});
test("challenge is not claimed and refused claim never clicks", async () => {
  const c=content({title:"Just a moment...",control:true}); await c.step(500);
  assert.equal(c.calls.length,0); assert.equal(c.clicks(),0);
  const d=content({control:true,reply:{ok:false}}); await d.step(250);
  assert.equal(d.calls.length,1); assert.equal(d.clicks(),0);
  const f=content({frame:true,control:true}); await f.step(150001);
  assert.equal(f.calls.length,0); assert.equal(f.clicks(),0);
});
test("missing control stops after ten seconds; a changed control cannot be clicked", async () => {
  const c=content(); await c.step(10001); c.control(); await c.step(250);
  assert.equal(c.calls.length,0);
  let release;
  const reply=new Promise(resolve=>{release=resolve;});
  const d=content({control:true,reply}); d.control("/f/wrong/go");
  release({ok:true,hint:""}); await flush(); assert.equal(d.clicks(),0);
});

const harness = vm.createContext({require, console, setTimeout, clearTimeout, URL});
const prefix = fs.readFileSync("tests/extension_background.test.js","utf8").split("\nasync function settle()")[0];
vm.runInContext(prefix + "\nglobalThis.load = loadBackground;", harness);
function background(options={}) {
  const b=harness.load({timers:true, settings:{enabled:true, minSizeBytes:99999,
    interceptExtensions:["exe"]},nativeResult:msg=>msg.action==="fetch_ticket" ?
      {status:"ok",hint:"hint.zip"} : {status:"ok"}, ...options});
  b.closed=[];
  b.context.browser.tabs.remove=async id=>b.closed.push(id);
  vm.runInContext(fs.readFileSync("extension/fuckingfast-fetch.js","utf8"),b.context);
  b.fetch=b.context.CoveFuckingfast;
  return b;
}
const sender={tab:{id:7},url:page};
const claim=b=>b.fetch.claim({type:"cove-fetch-claim",nonce:NONCE,id:ID},sender);
test("claim arms only one authorized fetch and expires", async () => {
  const b=background(); assert.equal((await claim(b)).ok,true);
  assert.equal((await claim(b)).ok,false);
  assert.equal(b.fetch.owns({url:"https://node.fuckingfast.co/dl/token"}),true);
  for(const url of ["http://fuckingfast.co/a","https://evilfuckingfast.co/a","https://127.0.0.1/a"])
    assert.equal(b.fetch.owns({url}),false);
  b.fireTimers(b.fetch.CLAIM_TTL_MS); assert.equal(b.fetch.owns({url:"https://fuckingfast.co/dl/a"}),false);
});
test("bad sender/id/disabled/native error cannot arm", async () => {
  for(const s of [{}, {tab:{id:7},url:"https://evil.com/"+ID}, {tab:{id:7},url:page+"x"}]) {
    const b=background();
    assert.equal((await b.fetch.claim({nonce:NONCE,id:ID},s)).ok,false);
  }
  const off=background({settings:{enabled:false}});
  assert.equal((await claim(off)).ok,false);
  const fail=background({nativeResult:{status:"error",message:"SECRET"}});
  assert.equal((await claim(fail)).ok,false);
  const b=background();
  assert.equal((await b.fetch.claim({nonce:"../bad",id:ID},sender)).ok,false);
  assert.equal(b.calls.native.some(m=>m.action==="fetch_ticket"),false);
});
test("only armed downloads bypass filters; accepted closes its tab, refusal leaves it", async () => {
  const b=background({tabsById:new Map([[7,{id:7,url:page,incognito:false}]])});
  await flush();
  const item={id:10,url:"https://node.fuckingfast.co/dl/token",filename:"",totalBytes:1,incognito:false};
  await vm.runInContext("handleCreated",b.context)(item); await flush();
  assert.equal(b.calls.native.filter(m=>m.action==="download").length,0);
  assert.equal((await claim(b)).ok,true);
  await vm.runInContext("handleCreated",b.context)({...item,id:11,url:"https://example.com/a.zip"});
  assert.equal(b.calls.native.filter(m=>m.action==="download").length,0);
  await vm.runInContext("handleCreated",b.context)(item); await flush();
  const sent=b.calls.native.find(m=>m.action==="download");
  assert.ok(sent); assert.equal(sent.filename,"hint.zip"); assert.deepEqual(b.closed,[7]);
  const r=background(); assert.equal((await claim(r)).ok,true);
  r.fetch.onRefused(); assert.equal(r.closed.length,0);
  assert.equal(JSON.stringify(b.calls.console).includes("/dl/token"),false);
});

test("native refusal preserves browser download; excluded domains still win", async () => {
  for(const nativeResult of [msg=>msg.action==="fetch_ticket" ? {status:"ok",hint:""} :
    {status:"error",message:"SECRET"}, msg=> {if(msg.action==="fetch_ticket") return {status:"ok",hint:""};
    throw Error("SECRET");}]) {
    const b=background({nativeResult}); assert.equal((await claim(b)).ok,true);
    await vm.runInContext("handleCreated",b.context)(
      {id:20,url:"https://node.fuckingfast.co/dl/token",totalBytes:1,incognito:false});
    await flush();
    assert.equal(b.calls.cancel.length,0); assert.equal(b.closed.length,0);
    assert.equal(b.fetch.armed,null); assert.equal(JSON.stringify(b.calls.console).includes("SECRET"),false);
  }
  const b=background({settings:{enabled:true,excludedDomains:["fuckingfast.co"]}});
  assert.equal((await claim(b)).ok,true);
  await vm.runInContext("handleCreated",b.context)(
    {id:21,url:"https://node.fuckingfast.co/dl/token",totalBytes:1,incognito:false});
  assert.equal(b.calls.native.filter(m=>m.action==="download").length,0);
});

test("concurrent claims serialize; changed tab is never closed", async () => {
  let release;
  const pending=new Promise(resolve=>{release=resolve;});
  const b=background({nativeResult:msg=>msg.action==="fetch_ticket" ? pending : {status:"ok"},
    tabsById:new Map([[7,{id:7,url:"https://foreign.example/",incognito:false}]])});
  const first=claim(b); await flush(); assert.equal((await claim(b)).ok,false);
  release({status:"ok",hint:"file.zip"}); assert.equal((await first).ok,true);
  await b.fetch.onIntercepted(); assert.equal(b.closed.length,0); assert.equal(b.fetch.armed,null);
});

test("claim expires after thirty seconds and a following claim succeeds", async () => {
  const b=background(); assert.equal((await claim(b)).ok,true);
  b.fireTimers(30000);
  assert.equal((await claim(b)).ok,true);
  assert.equal(b.fetch.CLAIM_TTL_MS,30000);
  const delayed=background(); await claim(delayed);
  delayed.context.expiredAt=delayed.fetch.armed.expires;
  vm.runInContext("Date.now = () => expiredAt",delayed.context);
  assert.equal((await claim(delayed)).ok,true); // Timer callback has not run yet.
});

test("one claim binds synchronously to one download identity and clears on every exit", async () => {
  for(const exit of ["accepted","refused","expired"]) {
    const b=background(); assert.equal((await claim(b)).ok,true);
    const item={id:31,url:"https://node.fuckingfast.co/dl/one"};
    assert.equal(b.fetch.owns(item),true); assert.equal(b.fetch.owns({...item}),true);
    assert.equal(b.fetch.owns({...item,id:32}),false);
    if(exit==="accepted") await b.fetch.onIntercepted();
    else if(exit==="refused") b.fetch.onRefused();
    else b.fireTimers(b.fetch.CLAIM_TTL_MS || 60000);
    assert.equal(b.fetch.armed,null);
    assert.equal((await claim(b)).ok,true);
    assert.equal(b.fetch.owns({...item,id:32}),true);
  }
  const b=background(); await claim(b);
  assert.equal(b.fetch.owns({url:"https://node.fuckingfast.co/dl/one"}),true);
  assert.equal(b.fetch.owns({url:"https://node.fuckingfast.co/dl/two"}),false);
});

test("concurrent delivery downloads bypass filters exactly once", async () => {
  let release;
  const pending=new Promise(resolve=>{release=resolve;});
  const b=background({nativeResult:msg=>msg.action==="fetch_ticket" ? {status:"ok",hint:"file.zip"} : pending});
  assert.equal((await claim(b)).ok,true);
  const created=vm.runInContext("handleCreated",b.context);
  await Promise.all([created({id:41,url:"https://node.fuckingfast.co/dl/one",totalBytes:1,incognito:false}),
    created({id:42,url:"https://node.fuckingfast.co/dl/two",totalBytes:1,incognito:false})]);
  await flush();
  assert.equal(b.calls.native.filter(m=>m.action==="download").length,1);
  release({status:"ok"}); await flush();
  assert.deepEqual(Array.from(b.calls.cancel),[41]);
});

test("directory travels only with the one armed download", async () => {
  const b=background({settings:{enabled:true,minSizeBytes:0,interceptExtensions:[]},
    nativeResult:msg=>msg.action==="fetch_ticket" ? {status:"ok",hint:"file.zip",directory:"/PRIVATE_FOLDER"} : {status:"ok"}});
  await claim(b);
  const created=vm.runInContext("handleCreated",b.context);
  await created({id:51,url:"https://node.fuckingfast.co/dl/file",totalBytes:1,incognito:false}); await flush();
  await created({id:52,url:"https://example.com/file",totalBytes:1,incognito:false}); await flush();
  const messages=b.calls.native.filter(m=>m.action==="download");
  assert.equal(messages[0].directory,"/PRIVATE_FOLDER");
  assert.equal(Object.hasOwn(messages[1],"directory"),false);
  assert.equal(JSON.stringify(b.calls.console).includes("PRIVATE_FOLDER"),false);
});
