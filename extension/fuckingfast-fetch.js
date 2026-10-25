// Firefox-only capability. Chrome excludes this entire file.
(() => {
  const CLAIM_TTL_MS = 30000;
  const reserved = new Set("help login logout terms privacy contact speed speedtest register signup upload api static about faq dmca account admin dashboard".split(" "));
  const nonceRule = /^[0-9a-f]{32}$/;
  let armed = null, claiming = false, timer;
  function page(value) {
    try {
      const u = new URL(value);
      const match = u.pathname.match(/^\/(?:f\/)?([A-Za-z0-9_-]{4,64})\/?$/);
      if (u.protocol !== "https:" || !["fuckingfast.co", "www.fuckingfast.co"].includes(u.hostname) ||
          u.username || u.password || (u.port && u.port !== "443") ||
          !match || reserved.has(match[1].toLowerCase())) return null;
      return match[1];
    } catch { return null; }
  }
  function delivery(value) {
    try {
      const u = new URL(value);
      return u.protocol === "https:" && !u.username && !u.password &&
        (!u.port || u.port === "443") &&
        (u.hostname === "fuckingfast.co" || u.hostname.endsWith(".fuckingfast.co"));
    } catch { return false; }
  }
  function clear(expected = armed) {
    if (expected !== armed) return null;
    const old = armed;
    armed = null;
    clearTimeout(timer);
    return old;
  }
  async function claim(msg, sender) {
    if (armed && Date.now() >= armed.expires) clear();
    if (!msg || !sender || !sender.tab || !Number.isInteger(sender.tab.id) ||
        typeof msg.nonce !== "string" || !nonceRule.test(msg.nonce) ||
        typeof msg.id !== "string" || page(sender.url) !== msg.id || armed || claiming)
      return {ok:false};
    claiming = true;
    try {
      await ensureSettings();
      if (settings.enabled !== true) return {ok:false, reason:"intercept_off"};
      const result = await sendNativeMessage({action:"fetch_ticket", nonce:msg.nonce, id:msg.id});
      if (!result || result.status !== "ok" || typeof result.hint !== "string" ||
          settings.enabled !== true) return {ok:false};
      armed = {tabId:sender.tab.id, id:msg.id, hint:result.hint,
        directory:typeof result.directory === "string" ? result.directory : "",
        downloadIdentity:null, expires:Date.now() + CLAIM_TTL_MS};
      const current = armed;
      timer = setTimeout(() => clear(current), CLAIM_TTL_MS);
      return {ok:true, hint:result.hint};
    } catch { return {ok:false}; }
    finally { claiming = false; }
  }
  globalThis.CoveFuckingfast = {
    CLAIM_TTL_MS,
    claim,
    get armed() { return armed; },
    owns(item) {
      if (armed && Date.now() >= armed.expires) clear();
      if (!armed || !delivery(item.url)) return false;
      const identity = Number.isInteger(item.id) ? "id:" + item.id : "url:" + item.url;
      if (armed.downloadIdentity === null) armed.downloadIdentity = identity;
      return armed.downloadIdentity === identity;
    },
    async onIntercepted(expected = armed) {
      const old = clear(expected);
      if (!old) return;
      try {
        const tab = await browser.tabs.get(old.tabId);
        if (tab && tab.id === old.tabId && delivery(tab.url)) await browser.tabs.remove(old.tabId);
      } catch {}
    },
    onRefused(expected = armed) { clear(expected); },
  };
  browser.runtime.onMessage.addListener((msg, sender) => {
    if (msg && msg.type === "cove-fetch-claim") return claim(msg, sender);
    return undefined;
  });
})();
