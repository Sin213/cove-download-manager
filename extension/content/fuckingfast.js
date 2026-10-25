// Firefox only: ordinary browsing installs no listeners and touches no DOM.
(() => {
  const marker = location.hash.match(/^#cove-fetch=([0-9a-f]{32})$/);
  if (!marker) return;
  const path = location.pathname.match(/^\/(?:f\/)?([A-Za-z0-9_-]{4,64})\/?$/);
  const reserved = new Set("help login logout terms privacy contact speed speedtest register signup upload api static about faq dmca account admin dashboard".split(" "));
  if (!path || reserved.has(path[1].toLowerCase())) return;
  const id = path[1], target = "/f/" + id + "/go";
  let claimed = false, completeSince = null, timer;
  const started = Date.now();
  const challenge = () => /^Just a moment/i.test(document.title.trim()) ||
    !!document.querySelector('iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]');
  function tick() {
    if (claimed || Date.now() - started >= 150000) { clearInterval(timer); return; }
    if (challenge()) { completeSince = null; return; }
    const control = document.querySelector('a[hx-post="' + target + '"]');
    if (!control) {
      if (document.readyState === "complete") {
        if (completeSince === null) completeSince = Date.now();
        if (Date.now() - completeSince >= 10000) clearInterval(timer);
      }
      return;
    }
    if (control.getAttribute("hx-post") !== target) return;
    claimed = true;
    clearInterval(timer);
    browser.runtime.sendMessage({type:"cove-fetch-claim", nonce:marker[1], id}).then(reply => {
      if (!reply || reply.ok !== true || typeof reply.hint !== "string" ||
          challenge() || !control.isConnected || control.getAttribute("hx-post") !== target) return;
      history.replaceState(null, "", location.pathname + location.search +
                           (reply.hint ? "#" + encodeURIComponent(reply.hint) : ""));
      control.click();
    }).catch(() => {});
  }
  timer = setInterval(tick, 250);
  tick();
})();
