// extension/diagnostics.js
//
// Extension-local sanitized diagnostics.
//
// When Cove is closed, the native host cannot reach it, or native messaging
// itself fails, this ring is the only record of what happened - the desktop
// log has nothing to say about a request that never arrived. So it has to be
// self-sufficient: bounded, persisted in storage.local, and copyable from the
// popup with no working connection at all.
//
// It is also the surface with the most to leak. A content script sees every
// page URL, media URL and title on a tab, so the rule here is stricter than on
// the desktop side: page-identifying fields are dropped by name before
// anything else happens, and only allowlisted primitives survive.
(function (global) {
  "use strict";

  const MAX_RECORDS = 300;
  const MAX_FIELDS = 20;
  const MAX_TEXT = 300;
  const STORAGE_KEY = "coveDiag";
  const REDACTED = "<redacted>";

  const SANITIZATION_NOTICE =
    "Secrets, page addresses and media addresses are removed from this " +
    "report. It contains only event names, timings and connection states.";

  // Never recorded, whatever the value looks like. Matching on the field name
  // is the only reliable guard: a page URL is a perfectly ordinary URL.
  const FORBIDDEN_FIELDS = new Set([
    "url", "pageurl", "mediaurl", "srcurl", "linkurl", "documenturl",
    "referrer", "referer", "title", "pagetitle", "filename", "path",
    "cookie", "cookies", "authorization", "token", "apikey", "api_key",
    "payload", "body", "message", "msg", "response", "request", "settings",
    "useragent", "user_agent",
  ]);

  const SAFE_SUBDOMAINS = new Set([
    "www", "api", "cdn", "static", "web", "m", "mobile",
    "download", "downloads", "files", "media", "video", "img", "images",
  ]);

  const KEY_RE = /^[A-Za-z][A-Za-z0-9_]{0,39}$/;
  const REQUEST_ID_RE = /^[A-Za-z0-9_-]{1,32}$/;
  const ROUTE_SEGMENT_RE = /^[A-Za-z0-9_-]{1,4}$/;
  const LONG_TOKEN_RE = /[A-Za-z0-9]{20,}/g;
  const UUID_RE =
    /\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b/g;

  function sanitizeHost(host) {
    const bare = String(host || "").split("@").pop().split(":")[0].toLowerCase();
    if (!bare) return REDACTED;
    const labels = bare.split(".");
    if (labels.length > 2 && !SAFE_SUBDOMAINS.has(labels[0])) labels[0] = REDACTED;
    return labels.join(".");
  }

  function sanitizeRoute(pathname) {
    if (!pathname || pathname === "/") return pathname || "";
    const segments = pathname.split("/").filter(Boolean);
    if (segments.length && ROUTE_SEGMENT_RE.test(segments[0])) {
      const head = "/" + segments[0];
      return segments.length > 1 ? head + "/" + REDACTED : head;
    }
    return "/" + REDACTED;
  }

  // Exported mainly so a caller can log a route class deliberately. Ordinary
  // field values never reach it: a "url" field is dropped, not sanitized.
  function sanitizeUrl(value) {
    try {
      if (typeof value !== "string" || !value.trim()) return REDACTED;
      const text = value.trim();
      if (/^magnet:/i.test(text)) return "magnet:" + REDACTED;
      const parsed = new URL(text);
      const scheme = parsed.protocol.replace(":", "").toLowerCase();
      if (!["http", "https", "ftp", "ftps"].includes(scheme)) {
        return scheme + ":" + REDACTED;
      }
      return scheme + "://" + sanitizeHost(parsed.hostname) +
        sanitizeRoute(parsed.pathname);
    } catch (e) {
      return REDACTED;
    }
  }

  function sanitizeText(value) {
    try {
      if (typeof value !== "string") return REDACTED;
      let text = value.length > MAX_TEXT ? value.slice(0, MAX_TEXT) : value;
      text = text.replace(/(?:https?|ftps?):\/\/[^\s"'<>]+/gi, (m) => sanitizeUrl(m));
      text = text.replace(/magnet:\?[^\s"'<>]*/gi, "magnet:" + REDACTED);
      text = text.replace(UUID_RE, REDACTED);
      text = text.replace(LONG_TOKEN_RE, REDACTED);
      return text;
    } catch (e) {
      return REDACTED;
    }
  }

  function sanitizeFields(fields) {
    const out = {};
    try {
      if (!fields || typeof fields !== "object") return out;
      for (const key of Object.keys(fields)) {
        if (Object.keys(out).length >= MAX_FIELDS) break;
        if (!KEY_RE.test(key)) continue;
        if (FORBIDDEN_FIELDS.has(key.toLowerCase())) continue;
        const value = fields[key];
        if (value === null || typeof value === "boolean") {
          out[key] = value;
        } else if (typeof value === "number" && Number.isFinite(value)) {
          out[key] = value;
        } else if (typeof value === "string") {
          out[key] = sanitizeText(value);
        }
        // Objects, arrays and functions are dropped: nothing structured in
        // this extension is worth the risk of carrying a payload along.
      }
    } catch (e) {
      return {};
    }
    return out;
  }

  // Family and major version only. The full user agent is a fingerprint, and
  // "Firefox 140" is all a support report actually needs.
  const BROWSER_PATTERNS = [
    [/\bFirefox\/(\d+)/, "Firefox"],
    [/\bEdg\/(\d+)/, "Edge"],
    [/\bOPR\/(\d+)/, "Opera"],
    [/\bChrome\/(\d+)/, "Chrome"],
    [/\bVersion\/(\d+).*\bSafari\//, "Safari"],
  ];

  function browserLabel(userAgent) {
    try {
      if (typeof userAgent !== "string" || !userAgent) return "unknown";
      for (const [pattern, name] of BROWSER_PATTERNS) {
        const match = userAgent.match(pattern);
        if (match) return name + " " + match[1];
      }
      return "unknown";
    } catch (e) {
      return "unknown";
    }
  }

  function newId(length) {
    let out = "";
    while (out.length < length) {
      out += Math.floor(Math.random() * 16).toString(16);
    }
    return out.slice(0, length);
  }

  function newRequestId() {
    return newId(8);
  }

  function normalizeRequestId(value) {
    return typeof value === "string" && REQUEST_ID_RE.test(value) ? value : null;
  }

  function createDiagnostics(options) {
    const config = options || {};
    const storage = config.storage || null;
    const session = newId(8);
    const context = typeof config.context === "string" ? config.context : "unknown";

    let ring = [];
    let retention = MAX_RECORDS;
    let memoryOnly = !storage;
    // Hydration runs once, starts on construction, and is what `ready`
    // resolves on. createDiagnostics is synchronous but reading the stored
    // ring is not, so anything needing the whole history - a report, a clear
    // - waits on `ready` rather than hoping load() was called first.
    let loadPromise = null;

    // Hydration, flush and clear all write the same key, and all three are
    // async. Run through one chain so the last operation issued is the last
    // one applied: otherwise a flush started during hydration overwrites the
    // stored history with a partial ring, and a flush issued around a clear
    // puts the deleted records back.
    let chain = Promise.resolve();

    // Bumped synchronously by clear(). Hydration captures it before reading
    // and refuses to merge if it moved, because a read that started before
    // the user pressed Clear is describing history they just deleted.
    let clearGeneration = 0;

    function serialize(operation) {
      const run = chain.then(operation, operation);
      chain = run.then(() => {}, () => {});
      return run;
    }
    let environment = {
      extensionVersion: config.version || "unknown",
      browser: config.browser || "unknown",
      appVersion: "unknown",
    };

    function setEnvironment(values) {
      try {
        if (values && typeof values === "object") {
          Object.assign(environment, values);
        }
      } catch (e) { /* diagnostics never throw */ }
    }

    function record(component, event, level, fields, requestId) {
      try {
        const entry = {
          ts: new Date().toISOString(),
          level: ["DEBUG", "INFO", "WARNING", "ERROR"].includes(level) ? level : "INFO",
          component: typeof component === "string" ? component : "extension",
          event: typeof event === "string" ? event : "unknown",
          session: session,
          context: context,
          fields: sanitizeFields(fields),
        };
        const request = normalizeRequestId(requestId);
        if (request) entry.request = request;
        ring.push(entry);
        if (ring.length > retention) ring = ring.slice(ring.length - retention);
        return entry;
      } catch (e) {
        return null;
      }
    }

    function flush() {
      return serialize(writeRing);
    }

    async function writeRing() {
      if (!storage) {
        memoryOnly = true;
        return false;
      }
      try {
        // A copy: the stored value must not alias the live ring, or a later
        // record() would silently rewrite what is supposedly on disk.
        await storage.set({ [STORAGE_KEY]: ring.slice() });
        memoryOnly = false;
        return true;
      } catch (e) {
        // Almost always a quota failure. Halve what we keep and try once
        // more; a second failure means memory-only for this session.
        try {
          retention = Math.max(10, Math.floor(retention / 2));
          ring = ring.slice(Math.max(0, ring.length - retention));
          await storage.set({ [STORAGE_KEY]: ring.slice() });
          memoryOnly = false;
          return true;
        } catch (e2) {
          memoryOnly = true;
          return false;
        }
      }
    }

    function load() {
      if (!loadPromise) {
        // Captured here, when the read is scheduled, not when the queued
        // function finally runs: a clear() arriving in between is exactly
        // the case this guards, and by then the counter has already moved.
        const generation = clearGeneration;
        loadPromise = serialize(() => hydrate(generation));
      }
      return loadPromise;
    }

    async function hydrate(generation) {
      if (!storage) return false;
      try {
        const stored = await storage.get(STORAGE_KEY);
        if (generation !== clearGeneration) {
          // Cleared while this read was in flight. The result is stale by
          // definition: discard it and keep whatever was recorded since.
          return false;
        }
        const values = stored && Array.isArray(stored[STORAGE_KEY])
          ? stored[STORAGE_KEY] : [];
        // Re-sanitize on the way in: the stored copy may predate a change to
        // the rules, or have been written by an older version.
        const restored = values.slice(-retention).map((entry) => ({
          ts: typeof entry.ts === "string" ? entry.ts : "",
          level: entry.level,
          component: entry.component,
          event: entry.event,
          session: entry.session,
          context: entry.context,
          request: normalizeRequestId(entry.request) || undefined,
          fields: sanitizeFields(entry.fields),
        }));
        // Merge, never replace. The read is asynchronous, so events recorded
        // while it was in flight - the startup ping above all - are already
        // in the ring, and overwriting would silently drop exactly the events
        // a support report is being collected for. Stored records are older,
        // so they go in front, and the newest survive the trim.
        ring = restored.concat(ring);
        if (ring.length > retention) ring = ring.slice(ring.length - retention);
        return true;
      } catch (e) {
        return false;
      }
    }

    // Started here so a caller that never calls load() still gets a settled
    // `ready`, and so a second load() cannot merge the stored ring twice.
    const ready = storage ? load() : Promise.resolve(false);

    function clear() {
      // Emptied straight away so the view stops showing records the user
      // asked to drop, even if the write below is still queued or fails.
      // The generation bump is what stops an in-flight hydration from
      // putting them back after the fact.
      clearGeneration += 1;
      ring = [];
      retention = MAX_RECORDS;
      return serialize(wipeStorage);
    }

    async function wipeStorage() {
      if (!storage) return true;
      try {
        await storage.set({ [STORAGE_KEY]: [] });
        return true;
      } catch (e) {
        return false;
      }
    }

    function renderRecord(entry) {
      const parts = [entry.ts, entry.level, "[" + entry.context + "]",
                     entry.component + "/" + entry.event];
      if (entry.request) parts.push("request=" + entry.request);
      const fields = entry.fields || {};
      for (const key of Object.keys(fields).sort()) {
        parts.push(key + "=" + fields[key]);
      }
      return parts.join(" ");
    }

    function report() {
      try {
        const header = [
          "Cove extension diagnostics",
          "extension version: " + environment.extensionVersion,
          "browser: " + environment.browser,
          "last seen Cove version: " + environment.appVersion,
          "session: " + session,
          SANITIZATION_NOTICE,
          "",
        ];
        return header.concat(ring.map(renderRecord)).join("\n") + "\n";
      } catch (e) {
        return SANITIZATION_NOTICE + "\n";
      }
    }

    return {
      session,
      context,
      ready,
      record,
      records() { return ring.slice(); },
      flush,
      load,
      clear,
      report,
      setEnvironment,
      get memoryOnly() { return memoryOnly; },
      get environment() { return Object.assign({}, environment); },
    };
  }

  global.CoveDiag = {
    MAX_RECORDS,
    STORAGE_KEY,
    SANITIZATION_NOTICE,
    createDiagnostics,
    browserLabel,
    sanitizeUrl,
    sanitizeText,
    sanitizeFields,
    newRequestId,
    normalizeRequestId,
  };
})(typeof globalThis !== "undefined" ? globalThis : this);
