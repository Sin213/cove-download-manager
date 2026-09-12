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

// The settings the user has changed since this page loaded. This page is the
// one settings writer that can sit open for hours, so what it sends on Save is
// these fields and nothing else: the rest are whatever they have become since,
// and sending the load-time snapshot back would erase anything that changed in
// the meantime - an exclusion the pill added, the keyboard toggle, another tab.
const dirty = new Set();

// Which control carries which setting, how to read it, and how to show it.
// Loading writes these controls directly and fires no events, so it never marks
// anything dirty; only a user gesture does.
const FIELD_SPECS = {
  enabled: {
    controls: [enabledCheckbox],
    read: () => enabledCheckbox.checked,
    show: (s) => { enabledCheckbox.checked = s.enabled !== false; },
  },
  mediaPillEnabled: {
    controls: [mediaPillEnabledCheckbox],
    read: () => mediaPillEnabledCheckbox.checked,
    show: (s) => { mediaPillEnabledCheckbox.checked = s.mediaPillEnabled !== false; },
  },
  minSizeBytes: {
    controls: [minSizeInput, minSizeUnit],
    // A cleared input parses to NaN, which would silently disable the size
    // filter (NaN comparisons are always false); treat it as 0.
    read: () => (parseInt(minSizeInput.value, 10) || 0) * parseInt(minSizeUnit.value, 10),
    show: (s) => {
      const bytes = s.minSizeBytes || 0;
      const unit = bytes >= 1073741824 && bytes % 1073741824 === 0 ? 1073741824
        : bytes >= 1048576 && bytes % 1048576 === 0 ? 1048576 : 1024;
      minSizeInput.value = unit === 1024 ? Math.round(bytes / 1024) : bytes / unit;
      minSizeUnit.value = String(unit);
    },
  },
  interceptExtensions: {
    controls: [extensionsTextarea],
    read: () => extensionsTextarea.value
      .split(",").map((s) => s.trim().toLowerCase()).filter((s) => s.startsWith(".")),
    show: (s) => { extensionsTextarea.value = (s.interceptExtensions || []).join(", "); },
  },
  excludedDomains: {
    controls: [excludedDomainsTextarea],
    read: () => excludedDomainsTextarea.value
      .split("\n").map((s) => s.trim().toLowerCase()).filter(Boolean),
    show: (s) => { excludedDomainsTextarea.value = (s.excludedDomains || []).join("\n"); },
  },
};
const FIELDS = Object.keys(FIELD_SPECS);

for (const field of FIELDS) {
  for (const element of FIELD_SPECS[field].controls) {
    // Both, because which one a control emits depends on the control: a
    // textarea reports "input", a select reports "change", and a checkbox can
    // report either.
    for (const type of ["input", "change"]) {
      element.addEventListener(type, () => { dirty.add(field); });
    }
  }
}

function flashStatus(text) {
  saveStatus.textContent = text;
  setTimeout(() => { saveStatus.textContent = ""; }, 2000);
}

async function loadSettings() {
  const s = await browser.runtime.sendMessage({ type: "getSettings" });

  if (!mediaPillAvailable()) {
    const section = document.getElementById("media-pill-section");
    if (section) section.hidden = true;
  }

  for (const field of FIELDS) FIELD_SPECS[field].show(s || {});
}

// Settings this page is not editing follow the stored value while it sits open,
// so an exclusion the pill just added is visible here rather than only after a
// reload. A field the user has unsaved edits in is theirs until they save or
// reload it: their typing is never replaced by someone else's write.
browser.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !changes.settings) return;
  const s = changes.settings.newValue || {};
  for (const field of FIELDS) {
    if (!dirty.has(field)) FIELD_SPECS[field].show(s);
  }
});

saveBtn.addEventListener("click", async () => {
  if (dirty.size === 0) {
    flashStatus("Saved");
    return;
  }
  const changes = {};
  for (const field of dirty) changes[field] = FIELD_SPECS[field].read();

  let result;
  try {
    result = await browser.runtime.sendMessage({ type: "updateSettings", changes });
  } catch {
    result = null;
  }
  if (!result || result.ok !== true) {
    // Bounded and the same either way: the page cannot tell a quota failure
    // from a background that went away, and neither is the user's to debug.
    flashStatus("Could not save settings");
    return;
  }

  // Cleared per field, and only where the control still holds what was sent.
  // An edit made while this was in flight is newer than what landed, so that
  // field stays pending and the next Save carries it.
  for (const field of Object.keys(changes)) {
    if (JSON.stringify(FIELD_SPECS[field].read()) === JSON.stringify(changes[field])) {
      dirty.delete(field);
    }
  }
  flashStatus("Saved");
});

resetExtensionsBtn.addEventListener("click", () => {
  extensionsTextarea.value = DEFAULT_EXTENSIONS.join(", ");
  dirty.add("interceptExtensions");
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
