// Chromium exposes `chrome`, Firefox exposes `browser`. Page scripts don't
// inherit the background shim, so define it here too.
const browser = globalThis.browser || globalThis.chrome;

const DEFAULT_EXTENSIONS = [
  ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
  ".exe", ".msi", ".dmg", ".iso", ".img",
  ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
  ".mp3", ".flac", ".aac", ".ogg", ".wav",
  ".pdf", ".torrent",
  ".deb", ".rpm", ".appimage",
];

const enabledCheckbox = document.getElementById("enabled");
const mediaPillEnabledCheckbox = document.getElementById("media-pill-enabled");
const minSizeInput = document.getElementById("min-size");
const minSizeUnit = document.getElementById("min-size-unit");
const extensionsTextarea = document.getElementById("extensions");
const excludedDomainsTextarea = document.getElementById("excluded-domains");
const saveBtn = document.getElementById("save");
const saveStatus = document.getElementById("save-status");
const resetExtensionsBtn = document.getElementById("reset-extensions");
const testConnectionBtn = document.getElementById("test-connection");
const testResult = document.getElementById("test-result");

// The Chrome bundle ships no pill content script, so the setting that turns
// it on would control nothing. Detect that from the manifest rather than the
// browser name: it is the bundle, not Chromium, that lacks the feature.
function mediaPillAvailable() {
  try {
    const manifest = browser.runtime.getManifest();
    return Array.isArray(manifest.content_scripts) && manifest.content_scripts.length > 0;
  } catch {
    return false;
  }
}

async function loadSettings() {
  const s = await browser.runtime.sendMessage({ type: "getSettings" });

  if (!mediaPillAvailable()) {
    const section = document.getElementById("media-pill-section");
    if (section) section.hidden = true;
  }

  enabledCheckbox.checked = s.enabled;
  mediaPillEnabledCheckbox.checked = s.mediaPillEnabled !== false;
  extensionsTextarea.value = (s.interceptExtensions || []).join(", ");
  excludedDomainsTextarea.value = (s.excludedDomains || []).join("\n");

  const bytes = s.minSizeBytes || 0;
  if (bytes >= 1073741824 && bytes % 1073741824 === 0) {
    minSizeInput.value = bytes / 1073741824;
    minSizeUnit.value = "1073741824";
  } else if (bytes >= 1048576 && bytes % 1048576 === 0) {
    minSizeInput.value = bytes / 1048576;
    minSizeUnit.value = "1048576";
  } else {
    minSizeInput.value = Math.round(bytes / 1024);
    minSizeUnit.value = "1024";
  }
}

saveBtn.addEventListener("click", async () => {
  const newSettings = {
    enabled: enabledCheckbox.checked,
    mediaPillEnabled: mediaPillEnabledCheckbox.checked,
    // A cleared input parses to NaN, which would silently disable the size
    // filter (NaN comparisons are always false); treat it as 0.
    minSizeBytes: (parseInt(minSizeInput.value, 10) || 0) * parseInt(minSizeUnit.value, 10),
    interceptExtensions: extensionsTextarea.value
      .split(",")
      .map((s) => s.trim().toLowerCase())
      .filter((s) => s.startsWith(".")),
    excludedDomains: excludedDomainsTextarea.value
      .split("\n")
      .map((s) => s.trim().toLowerCase())
      .filter(Boolean),
  };

  await browser.runtime.sendMessage({ type: "saveSettings", settings: newSettings });
  saveStatus.textContent = "Saved";
  setTimeout(() => { saveStatus.textContent = ""; }, 2000);
});

resetExtensionsBtn.addEventListener("click", () => {
  extensionsTextarea.value = DEFAULT_EXTENSIONS.join(", ");
});

testConnectionBtn.addEventListener("click", async () => {
  testResult.textContent = "Testing...";
  testResult.className = "";
  const result = await browser.runtime.sendMessage({ type: "ping" });
  if (result && result.status === "ok") {
    testResult.textContent = "Connected - Cove v" + result.version;
    testResult.className = "ok";
  } else {
    testResult.textContent = "Failed - " + (result?.message || "Cannot reach Cove");
    testResult.className = "error";
  }
});

loadSettings();
