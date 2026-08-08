// Chromium exposes `chrome`, Firefox exposes `browser`. Page scripts don't
// inherit the background shim, so define it here too.
const browser = globalThis.browser || globalThis.chrome;

const toggleBtn = document.getElementById("toggle-btn");
const connectionStatus = document.getElementById("connection-status");
const statusBar = document.getElementById("status-bar");
const downloadsList = document.getElementById("downloads-list");

function formatBytes(bytes) {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(k)));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + " " + units[i];
}

function formatSpeed(bytesPerSec) {
  return formatBytes(bytesPerSec) + "/s";
}

// ---- Diagnostics ----
//
// The ring itself lives in the background context (the manifest cannot load
// diagnostics.js into two places at once), so the popup reports events there
// and asks for the report back. When the background cannot answer, it falls
// back to reading storage.local directly - being able to copy a report with
// nothing running is the entire point of this surface.

// Remembered from the last ping so the disconnected fallback report can still
// name the Cove the popup was talking to a moment ago.
let lastSeenAppVersion = null;

function coveDiag(event, level, fields) {
  try {
    Promise.resolve(browser.runtime.sendMessage({
      type: "coveDiag",
      component: "extension.popup",
      event: event,
      level: level,
      fields: fields || {},
    })).catch(() => {});
  } catch (e) { /* a diagnostic must never break the popup */ }
}

async function diagnosticsReport() {
  try {
    const reply = await browser.runtime.sendMessage({ type: "coveDiagReport" });
    if (reply && reply.text) return reply.text;
  } catch (e) { /* fall through to the stored copy */ }
  try {
    const fallback = CoveDiag.createDiagnostics({
      storage: browser.storage.local,
      context: "popup",
      version: (browser.runtime.getManifest && browser.runtime.getManifest().version) ||
        "unknown",
      browser: CoveDiag.browserLabel(navigator && navigator.userAgent),
    });
    if (lastSeenAppVersion) fallback.setEnvironment({ appVersion: lastSeenAppVersion });
    await fallback.load();
    return fallback.report();
  } catch (e) {
    return CoveDiag.SANITIZATION_NOTICE + "\n";
  }
}

async function checkConnection() {
  let result = null;
  try {
    result = await browser.runtime.sendMessage({ type: "ping" });
  } catch (e) {
    result = null;
  }
  // The status shown here is a point-in-time sample of one ping, which is
  // exactly why it can disagree with a video pill a moment later. Record what
  // was actually rendered, so the two can be compared afterwards.
  if (result && result.status === "ok") {
    connectionStatus.textContent = "Connected - Cove v" + result.version;
    statusBar.className = "status-bar connected";
    lastSeenAppVersion = result.version || null;
    coveDiag("connection_status_rendered", "INFO",
             { state: "connected", appVersion: result.version || "unknown" });
  } else {
    connectionStatus.textContent = "Not connected to Cove";
    statusBar.className = "status-bar error";
    coveDiag("connection_status_rendered", "WARNING", {
      state: "not_connected",
      replied: !!result,
    });
  }
}

function wireDiagnosticsButtons() {
  const copyBtn = document.getElementById("copy-diagnostics");
  if (copyBtn) {
    copyBtn.addEventListener("click", async () => {
      const original = copyBtn.textContent;
      try {
        const text = await diagnosticsReport();
        await navigator.clipboard.writeText(text);
        copyBtn.textContent = "Copied";
      } catch (e) {
        copyBtn.textContent = "Copy failed";
      }
      setTimeout(() => { copyBtn.textContent = original; }, 2000);
    });
  }
  const clearBtn = document.getElementById("clear-diagnostics");
  if (clearBtn) {
    clearBtn.addEventListener("click", async () => {
      const original = clearBtn.textContent;
      try {
        // Only the background knows whether persistent storage actually gave
        // the records up. Claiming success on a refused write would tell the
        // user their diagnostics are gone when they are still on disk.
        const reply = await browser.runtime.sendMessage({ type: "coveDiagClear" });
        clearBtn.textContent = reply && reply.ok ? "Cleared" : "Clear failed";
      } catch (e) {
        clearBtn.textContent = "Clear failed";
      }
      setTimeout(() => { clearBtn.textContent = original; }, 2000);
    });
  }
}

async function loadSettings() {
  const s = await browser.runtime.sendMessage({ type: "getSettings" });
  toggleBtn.textContent = s.enabled ? "ON" : "OFF";
  toggleBtn.dataset.enabled = s.enabled;
}

toggleBtn.addEventListener("click", async () => {
  const s = await browser.runtime.sendMessage({ type: "getSettings" });
  s.enabled = !s.enabled;
  await browser.runtime.sendMessage({ type: "saveSettings", settings: s });
  toggleBtn.textContent = s.enabled ? "ON" : "OFF";
  toggleBtn.dataset.enabled = s.enabled;
});

document.getElementById("open-options").addEventListener("click", () => {
  browser.runtime.openOptionsPage();
});

function renderDownloads(downloads) {
  downloadsList.replaceChildren();

  if (!downloads || downloads.length === 0) {
    const emptyState = document.createElement("div");
    emptyState.className = "empty-state";
    emptyState.textContent = "No active downloads";
    downloadsList.appendChild(emptyState);
    return;
  }

  for (const dl of downloads) {
    const files = dl.files || [];
    const filename = files[0]?.path?.split("/").pop() || "Unknown";
    const total = parseInt(dl.totalLength || 0);
    const completed = parseInt(dl.completedLength || 0);
    const speed = parseInt(dl.downloadSpeed || 0);
    const pct = total > 0 ? Math.round((completed / total) * 100) : 0;

    const item = document.createElement("div");
    item.className = "download-item";

    const filenameElement = document.createElement("div");
    filenameElement.className = "download-filename";
    filenameElement.title = filename;
    filenameElement.textContent = filename;

    const progress = document.createElement("div");
    progress.className = "download-progress";

    const progressBar = document.createElement("div");
    progressBar.className = "progress-bar";

    const progressFill = document.createElement("div");
    progressFill.className = "progress-fill";
    progressFill.style.width = `${pct}%`;
    progressBar.appendChild(progressFill);

    const speedElement = document.createElement("span");
    speedElement.className = "download-speed";
    speedElement.textContent = formatSpeed(speed);
    progress.append(progressBar, speedElement);

    const meta = document.createElement("div");
    meta.className = "download-meta";
    const metaText = document.createElement("span");
    metaText.textContent = `${pct}% - ${formatBytes(completed)} / ${formatBytes(total)}`;
    meta.appendChild(metaText);

    item.append(filenameElement, progress, meta);
    downloadsList.appendChild(item);
  }
}

async function refreshDownloads() {
  try {
    const result = await browser.runtime.sendMessage({ type: "getStatus" });
    if (result && result.status === "ok") {
      renderDownloads(result.downloads);
    }
  } catch {
    // Background worker unreachable; the 2s interval retries, and an
    // unhandled rejection here would just spam the console.
  }
}

async function refreshStreams() {
  try {
    const streams = await browser.runtime.sendMessage({ type: "getDetectedStreams" });
    const section = document.getElementById("streams-section");
    const list = document.getElementById("streams-list");
    if (!section || !list) return;

    if (!streams || streams.length === 0) {
      section.style.display = "none";
      return;
    }

    section.style.display = "block";
    list.replaceChildren();

    for (const stream of streams) {
      const item = document.createElement("div");
      item.className = "stream-item";

      const urlSpan = document.createElement("span");
      urlSpan.className = "stream-url";
      const shortUrl = stream.url.split("?")[0].split("/").slice(-2).join("/");
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
      list.appendChild(item);
    }
  } catch {}
}

wireDiagnosticsButtons();
checkConnection();
loadSettings();
refreshDownloads();
refreshStreams();

setInterval(() => {
  refreshDownloads();
  refreshStreams();
}, 2000);
