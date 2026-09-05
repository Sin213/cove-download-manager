// Firefox-only: the popup's detected-stream section.
//
// Stream detection is a Firefox feature - media-sites.js watches responses for
// HLS manifests and only that bundle ships it - but the section listing them,
// and the button that asked Cove to fetch one, used to live in the shared
// popup. Chrome therefore advertised a list nothing could fill. Everything
// stream-specific is now here, and scripts/build_extension.py composes this
// script into the Firefox popup only.
//
// Loaded before popup.js, so the hook below is published by the time the
// shared popup looks for it. The section is built here rather than in
// popup.html so there is still exactly one popup template.

(function () {
  const browser = globalThis.browser || globalThis.chrome;

  const footer = document.querySelector(".footer");
  if (!footer) {
    // The insertion point moved. Say so rather than appending the section
    // somewhere arbitrary: the shared popup degrades to no stream list, which
    // is visible, while a section in the wrong place is not.
    throw new Error("Cove: popup footer missing, stream section not installed");
  }

  const section = document.createElement("div");
  section.id = "streams-section";
  section.style.display = "none";

  const header = document.createElement("div");
  header.className = "section-header";
  header.textContent = "Detected Streams";

  const list = document.createElement("div");
  list.id = "streams-list";

  section.append(header, list);
  // Its original position: after the downloads list, before the footer.
  footer.before(section);

  function renderStream(stream) {
    const item = document.createElement("div");
    item.className = "stream-item";

    const urlSpan = document.createElement("span");
    urlSpan.className = "stream-url";
    const shortUrl = stream.url.split("?")[0].split("/").slice(-2).join("/");
    // textContent, never HTML: the address comes from a page's own traffic.
    urlSpan.textContent = shortUrl;
    urlSpan.title = stream.url;

    const btn = document.createElement("button");
    btn.className = "stream-download-btn";
    btn.textContent = "Download";
    btn.addEventListener("click", async () => {
      const filename = shortUrl.split("/").pop().replace(".m3u8", ".mp4") || "stream.mp4";
      btn.textContent = "Sending...";
      btn.disabled = true;
      try {
        const response = await browser.runtime.sendMessage({
          type: "downloadStream",
          url: stream.url,
          filename: filename,
        });
        btn.textContent = response && response.ok ? "Sent!" : "Cove unavailable";
      } catch {
        btn.textContent = "Cove unavailable";
      }
      setTimeout(() => { btn.textContent = "Download"; btn.disabled = false; }, 2000);
    });

    item.appendChild(urlSpan);
    item.appendChild(btn);
    return item;
  }

  async function refresh() {
    try {
      const streams = await browser.runtime.sendMessage({ type: "getDetectedStreams" });

      if (!streams || streams.length === 0) {
        section.style.display = "none";
        return;
      }

      section.style.display = "block";
      // Replaced rather than appended, so a refresh cannot accumulate rows or
      // leave a second click listener behind on a row that is still there.
      list.replaceChildren();
      for (const stream of streams) list.appendChild(renderStream(stream));
    } catch {}
  }

  // The hook the shared popup calls: once at initialisation and once per
  // existing refresh tick. No timer is installed here - a second recurring
  // loop is exactly what this contract exists to avoid.
  globalThis.CovePopupStreams = { refresh };
})();
