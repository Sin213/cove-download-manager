"""Sanitized diagnostics for Cove.

This module is deliberately Qt-free and free of import-time side effects so
the GUI, the native messaging host and the test suite can all use it. Its
first job is redaction: nothing reaches an in-memory ring, a log file, the
Diagnostics window, the clipboard or a saved report until it has passed
through the sanitizers below.

The rule the rest of the codebase relies on: a sanitizer never returns the
original value on failure. It returns a placeholder.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
import traceback as _traceback
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

REDACTED = "<redacted>"
WORK_ID = "<work-id>"

# Caps. Diagnostics must stay bounded no matter what a caller hands us.
MAX_TEXT_LEN = 2000
MAX_TRACEBACK_LEN = 4000
MAX_FIELDS = 40
MAX_SEQ = 20
MAX_DEPTH = 2

_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")

# Subdomains that carry no account identity. Anything else in the leftmost
# label of a multi-label host is treated as a per-user server id and dropped
# (Real-Debrid delivery hosts look like sg5.download.real-debrid.com).
_SAFE_SUBDOMAINS = {
    "www", "api", "cdn", "static", "web", "m", "mobile",
    "download", "downloads", "files", "media", "video", "img", "images",
}

# A first path segment this short and this plain is a route marker ("/d/"),
# not a token, and keeping it is what makes an incident identifiable.
_ROUTE_SEG_RE = re.compile(r"^[A-Za-z0-9_-]{1,4}$")


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------


def sanitize_host(value):
    """Return only the safe part of a URL's (or bare) host."""
    try:
        return _host_of(value)
    except Exception:
        return REDACTED


def _host_of(value):
    if not isinstance(value, str):
        return REDACTED
    text = value.strip()
    if "://" in text:
        host = urlsplit(text).netloc
    else:
        host = text
    if not host:
        return REDACTED
    host = host.rsplit("@", 1)[-1]          # drop any userinfo
    if host.startswith("["):                # IPv6 literal
        host = host.split("]", 1)[0] + "]"
    else:
        host = host.split(":", 1)[0]        # drop the port
    host = host.lower()
    labels = host.split(".")
    if len(labels) > 2 and labels[0] not in _SAFE_SUBDOMAINS:
        labels[0] = REDACTED
    return ".".join(labels)


def sanitize_url_route(value):
    """Return the sanitized path component of a URL, without host or query."""
    try:
        if not isinstance(value, str):
            return REDACTED
        return _route_of(urlsplit(value.strip()).path)
    except Exception:
        return REDACTED


def _route_of(path):
    if not path or path == "/":
        return path or ""
    segments = [s for s in path.split("/") if s]
    if segments and _ROUTE_SEG_RE.match(segments[0]):
        head = "/" + segments[0]
        return head + "/" + REDACTED if len(segments) > 1 else head
    return "/" + REDACTED


def sanitize_url(value):
    """Render a URL as scheme + safe host + route class only.

    Query strings, fragments, userinfo and every opaque path token are
    dropped: a Real-Debrid share link, a delivery link and a magnet URI all
    carry account credentials in exactly those places.
    """
    try:
        if not isinstance(value, str) or not value.strip():
            return REDACTED
        text = value.strip()
        lower = text.lower()
        if lower.startswith("magnet:"):
            return "magnet:" + REDACTED
        parts = urlsplit(text)
        scheme = parts.scheme.lower()
        if not scheme or not parts.netloc:
            return REDACTED
        if scheme not in ("http", "https", "ftp", "ftps"):
            return scheme + ":" + REDACTED
        return "{}://{}{}".format(scheme, _host_of(parts.netloc), _route_of(parts.path))
    except Exception:
        return REDACTED


# ---------------------------------------------------------------------------
# Filesystem paths
# ---------------------------------------------------------------------------

_WORK_DIR_RE = re.compile(r"\.cove-work-[A-Za-z0-9._-]+")
_WIN_PROFILE_RE = re.compile(r"^([A-Za-z]:[\\/]+Users[\\/]+[^\\/]+)(.*)$", re.IGNORECASE)
_UNIX_HOME_RE = re.compile(r"^((?:/home|/Users)/[^/]+)(.*)$")


def sanitize_path(value, home=None):
    """Replace the user's profile root, mask the work-id, elide the middle.

    What survives is the shape of the path (root class, tail, work dir), which
    is what identifies an extractor publication failure. What does not survive
    is the account name or the random work suffix.
    """
    try:
        return _sanitize_path_once(value, home)
    except Exception:
        return REDACTED


def _sanitize_path_once(value, home=None):
    if not isinstance(value, str) or not value.strip():
        return REDACTED
    text = value.strip()
    windows = bool(re.match(r"^[A-Za-z]:[\\/]", text)) or "\\" in text
    sep = "\\" if windows else "/"

    root, rest = _split_root(text, home)
    rest = _WORK_DIR_RE.sub(".cove-work-" + WORK_ID, rest)
    segments = [s for s in re.split(r"[\\/]+", rest) if s]
    if len(segments) > 3:
        # With a placeholder root the account name is already gone, so two
        # tail segments are safe and show the work directory. Without one -
        # a download folder on another volume, a temp directory - the parent
        # segment is just as likely to be a person's name, so only the
        # basename survives. The masked work directory is the one exception:
        # it carries no identity and it is the whole point of the evidence.
        keep = segments[-2:] if root else segments[-1:]
        if not root and len(segments) >= 2 and segments[-2] == ".cove-work-" + WORK_ID:
            keep = segments[-2:]
        segments = ["..."] + keep
    if root:
        return root + (sep + sep.join(segments) if segments else "")
    return (sep if text.startswith(("/", "\\")) else "") + sep.join(segments)


def _split_root(text, home=None):
    """Return (placeholder-root, remainder) for a user-rooted path."""
    if home and isinstance(home, str):
        normal = text.replace("\\", "/")
        base = home.replace("\\", "/").rstrip("/")
        if base and normal.lower().startswith(base.lower()):
            return "~", text[len(base):]

    m = _WIN_PROFILE_RE.match(text)
    if m:
        rest = m.group(2)
        stripped = rest.replace("/", "\\").lstrip("\\")
        low = stripped.lower()
        if low.startswith("appdata\\local"):
            return "%LOCALAPPDATA%", stripped[len("appdata\\local"):]
        if low.startswith("appdata\\roaming"):
            return "%APPDATA%", stripped[len("appdata\\roaming"):]
        return "%USERPROFILE%", rest

    m = _UNIX_HOME_RE.match(text)
    if m:
        return "~", m.group(2)
    return "", text


def path_facts(value, expected_root=None):
    """Safe structural facts about a path: no names, only shape.

    ``expected_root`` enables the containment facts the extractor publication
    incident needs. Nothing here inspects the filesystem; callers that want
    ``exists``/``is_file`` pass them in as plain booleans.
    """
    facts = {
        "absolute": None,
        "drive": None,
        "depth": None,
        "ext": None,
        "same_drive": None,
        "within_expected_root": None,
    }
    try:
        if not isinstance(value, str) or not value.strip():
            return facts
        text = value.strip()
        p = Path(text)
        facts["absolute"] = bool(re.match(r"^([A-Za-z]:[\\/]|[\\/])", text))
        drive = p.drive or (text[:2] if re.match(r"^[A-Za-z]:", text) else "")
        facts["drive"] = drive.rstrip("\\/") or None
        segments = [s for s in re.split(r"[\\/]+", text) if s and not s.endswith(":")]
        facts["depth"] = len(segments)
        suffix = Path(segments[-1]).suffix if segments else ""
        facts["ext"] = suffix.lower() or None
        if expected_root and isinstance(expected_root, str):
            root_drive = Path(expected_root).drive.rstrip("\\/") or None
            # POSIX has no drives, so both sides are None and the answer is
            # trivially "yes" - which is the honest answer to "could this path
            # and the work root be on the same volume namespace".
            facts["same_drive"] = (facts["drive"] or "").lower() == (
                root_drive or ""
            ).lower()
            facts["within_expected_root"] = _is_within(text, expected_root)
    except Exception:
        pass
    return facts


def _is_within(path_text, root_text):
    """Lexical containment only - never resolves, never touches the disk."""
    try:
        a = re.split(r"[\\/]+", path_text.rstrip("\\/"))
        b = re.split(r"[\\/]+", root_text.rstrip("\\/"))
        if len(a) <= len(b):
            return False
        return [s.lower() for s in a[: len(b)]] == [s.lower() for s in b]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Free-form text
# ---------------------------------------------------------------------------

_MAGNET_RE = re.compile(r"magnet:\?[^\s\"'<>]*", re.IGNORECASE)
_URL_RE = re.compile(r"(?:https?|ftps?)://[^\s\"'<>\\]+", re.IGNORECASE)
_WIN_PATH_RE = re.compile(r"[A-Za-z]:\\{1,2}[^\s\"'<>]*")
# Any absolute POSIX path with at least three segments. Personal data is not
# confined to $HOME: a download directory, a temp directory or a mount point
# can carry the account name just as easily. Two segments is the floor so a
# route marker like "/d/<redacted>" is left alone.
# The lookbehind keeps this off a URL the URL rule has already rewritten:
# without it, "https://real-debrid.com/d/<redacted>" is re-read as a
# filesystem path and loses the route class the incident is identified by.
_UNIX_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9./:_-])/(?:[^\s\"'<>,;:)/]+/){2,}[^\s\"'<>,;:)/]*"
)
_BEARER_RE = re.compile(r"\bbearer\s+[A-Za-z0-9._\-~+/=]+", re.IGNORECASE)
_KEYED_SECRET_RE = re.compile(
    r"\b(authorization|cookie|set-cookie|x-api-key|api[_-]?key|apikey|token|"
    r"passkey|secret|password|passwd|auth|session[_-]?id|rpc[_-]?secret)\b"
    r"\s*[:=]\s*[^\s;,&\"']+",
    re.IGNORECASE,
)
# A long unbroken alphanumeric run is a token, a hash or a base64 segment.
# Separators are excluded from the class on purpose: "real_debrid_generated_link"
# and "invalid_engine_output_path" are stable event vocabulary, not secrets.
_LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9]{20,}")
# Dashed identifiers are still opaque, so match the UUID shape explicitly.
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def scrub_text(value):
    """Redact anything secret-shaped out of a free-form string."""
    try:
        if not isinstance(value, str):
            return REDACTED
        return _scrub_once(value)
    except Exception:
        return REDACTED


def _scrub_once(text, limit=MAX_TEXT_LEN):
    if len(text) > limit:
        text = text[:limit]
    text = _MAGNET_RE.sub("magnet:" + REDACTED, text)
    text = _URL_RE.sub(lambda m: sanitize_url(m.group(0)), text)
    text = _WIN_PATH_RE.sub(lambda m: sanitize_path(m.group(0)), text)
    text = _UNIX_PATH_RE.sub(lambda m: sanitize_path(m.group(0)), text)
    text = _scrub_local_home(text)
    text = _WORK_DIR_RE.sub(".cove-work-" + WORK_ID, text)
    text = _BEARER_RE.sub("Bearer " + REDACTED, text)
    text = _KEYED_SECRET_RE.sub(lambda m: m.group(1) + "=" + REDACTED, text)
    text = _UUID_RE.sub(REDACTED, text)
    text = _LONG_TOKEN_RE.sub(REDACTED, text)
    if len(text) > limit:
        text = text[:limit] + "..."
    return text


def _scrub_local_home(text):
    """Drop this machine's own home directory even when it is not under /home."""
    try:
        home = str(Path.home())
    except Exception:
        return text
    if not home or home in ("/", "\\"):
        return text
    placeholder = "%USERPROFILE%" if re.match(r"^[A-Za-z]:", home) else "~"
    out = text.replace(home, placeholder)
    return out.replace(home.replace("\\", "\\\\"), placeholder)


# ---------------------------------------------------------------------------
# Structured fields
# ---------------------------------------------------------------------------


def sanitize_fields(mapping):
    """Allowlist-shaped structured values only. Unknown objects are dropped.

    Settings objects, HTTP responses, native-message bodies and queue rows all
    fall into the "unknown object" bucket on purpose, so no caller can persist
    one by accident.
    """
    out = {}
    try:
        if not isinstance(mapping, dict):
            return out
        for key, value in mapping.items():
            if len(out) >= MAX_FIELDS:
                break
            if not isinstance(key, str) or not _KEY_RE.match(key):
                continue
            out[key] = _sanitize_value(value, 0)
    except Exception:
        return {}
    return out


def _sanitize_value(value, depth):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        return scrub_text(value)
    if depth >= MAX_DEPTH:
        return REDACTED
    if isinstance(value, dict):
        nested = {}
        for key, item in value.items():
            if len(nested) >= MAX_FIELDS:
                break
            if not isinstance(key, str) or not _KEY_RE.match(key):
                continue
            nested[key] = _sanitize_value(item, depth + 1)
        return nested
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(v, depth + 1) for v in list(value)[:MAX_SEQ]]
    return REDACTED


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


def sanitize_exception(exc):
    """Safe facts about a failure: types, errno/winerror, scrubbed text."""
    out = {
        "type": REDACTED,
        "cause": None,
        "errno": None,
        "winerror": None,
        "msg": REDACTED,
        "traceback": None,
    }
    try:
        if not isinstance(exc, BaseException):
            return out
        out["type"] = type(exc).__name__
        chain = _exception_chain(exc)
        cause = exc.__cause__ or exc.__context__
        if cause is not None:
            out["cause"] = type(cause).__name__
        for item in chain:
            if out["errno"] is None and isinstance(getattr(item, "errno", None), int):
                out["errno"] = item.errno
            if out["winerror"] is None and isinstance(getattr(item, "winerror", None), int):
                out["winerror"] = item.winerror
        out["msg"] = scrub_text(str(exc))
        try:
            rendered = "".join(
                _traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
            out["traceback"] = _scrub_once(rendered, MAX_TRACEBACK_LEN)
        except Exception:
            out["traceback"] = REDACTED
    except Exception:
        return {
            "type": REDACTED,
            "cause": None,
            "errno": None,
            "winerror": None,
            "msg": REDACTED,
            "traceback": None,
        }
    return out


def _exception_chain(exc, limit=5):
    chain = []
    seen = set()
    current = exc
    while current is not None and len(chain) < limit and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
LEVEL_RANK = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}

RING_SIZE = 500
MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 3
NATIVE_MAX_BYTES = 512 * 1024
NATIVE_BACKUPS = 2

APP_LOG_NAME = "cove.jsonl"
NATIVE_LOG_NAME = "native-host.jsonl"
LOG_DIR_NAME = "logs"

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,59}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _now_iso():
    """UTC ISO-8601 with milliseconds, always Z-suffixed."""
    seconds = time.time()
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds))
    return "{}.{:03d}Z".format(base, int((seconds % 1) * 1000))


def _safe_name(value, fallback):
    if isinstance(value, str) and _NAME_RE.match(value):
        return value
    return fallback


def normalize_request_id(value):
    """Accept only an opaque, short, charset-restricted request id."""
    if isinstance(value, str) and _REQUEST_ID_RE.match(value):
        return value
    return None


def new_id(length=8):
    return secrets.token_hex(max(1, length // 2))[:length]


class DiagLogger:
    """Process-local sanitized event log.

    Every public entry point swallows its own errors: diagnostics must never
    be the reason a download fails or the app refuses to start. When the sink
    cannot be opened the logger degrades to memory-only and says so once.
    """

    def __init__(
        self,
        log_dir=None,
        filename=APP_LOG_NAME,
        max_bytes=MAX_BYTES,
        backups=BACKUPS,
        ring_size=RING_SIZE,
        source="app",
    ):
        self.log_dir = Path(log_dir) if log_dir is not None else None
        self.filename = filename
        self.max_bytes = int(max_bytes)
        self.backups = int(backups)
        self.source = source
        self.session = new_id()
        self.memory_only = self.log_dir is None
        self.skipped_writes = 0

        self._lock = threading.RLock()
        self._ring = deque(maxlen=max(1, int(ring_size)))
        self._observers = []
        self._debug = False
        self._size = 0
        self._path = None
        self._closed = False

        if self.log_dir is not None:
            self._open_sink()

    # -- sink management ---------------------------------------------------

    def _open_sink(self):
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            self._path = self.log_dir / self.filename
            self._size = self._path.stat().st_size if self._path.exists() else 0
            # Prove the sink is usable now rather than losing the first error.
            with open(self._path, "a", encoding="utf-8"):
                pass
            self.memory_only = False
        except Exception as exc:
            self._path = None
            self.memory_only = True
            self.emit(
                "diagnostics",
                "log_sink_unavailable",
                "WARNING",
                reason=type(exc).__name__,
            )

    def _write_line(self, line):
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(line)
        self._size += len(line)

    def _rotate(self):
        """Shift backups down by one. Truncate if the shift is impossible."""
        try:
            oldest = self.log_dir / "{}.{}".format(self.filename, self.backups)
            if oldest.exists():
                os.remove(str(oldest))
            for index in range(self.backups - 1, 0, -1):
                src = self.log_dir / "{}.{}".format(self.filename, index)
                if src.exists():
                    os.replace(str(src), str(self.log_dir / "{}.{}".format(self.filename, index + 1)))
            if self._path.exists():
                os.replace(str(self._path), str(self.log_dir / "{}.1".format(self.filename)))
            self._size = 0
        except Exception:
            # A locked or unrenameable backup must not stop the active log
            # from staying bounded, so fall back to dropping its contents.
            try:
                with open(self._path, "w", encoding="utf-8"):
                    pass
                self._size = 0
            except Exception:
                self.memory_only = True

    # -- emitting ----------------------------------------------------------

    def emit(self, component, event, level="INFO", task_id=None, request_id=None,
             exc=None, **fields):
        try:
            self._emit(component, event, level, task_id, request_id, exc, fields)
        except Exception:
            # Absolutely nothing escapes into product code.
            try:
                self.skipped_writes += 1
            except Exception:
                pass

    def _emit(self, component, event, level, task_id, request_id, exc, fields):
        level = level.upper() if isinstance(level, str) else "INFO"
        if level not in LEVELS:
            level = "INFO"
        if level == "DEBUG" and not self._debug:
            return

        record = {
            "ts": _now_iso(),
            "level": level,
            "component": _safe_name(component, "unknown"),
            "event": _safe_name(event, "unknown"),
            "session": self.session,
        }
        if isinstance(task_id, int) and not isinstance(task_id, bool):
            record["task"] = task_id
        request = normalize_request_id(request_id)
        if request:
            record["request"] = request
        safe_fields = sanitize_fields(fields)
        if safe_fields:
            record["fields"] = safe_fields
        if exc is not None:
            record["exc"] = sanitize_exception(exc)

        with self._lock:
            self._ring.append(record)
            self._persist(record)
        self._notify(record)

    def _persist(self, record):
        if self.memory_only or self._closed or self._path is None:
            return
        try:
            line = json.dumps(record, ensure_ascii=False) + "\n"
        except Exception:
            self.skipped_writes += 1
            return
        try:
            if self._size + len(line) > self.max_bytes:
                self._rotate()
            if not self.memory_only:
                self._write_line(line)
        except Exception:
            self.skipped_writes += 1

    # -- observers ---------------------------------------------------------

    def add_observer(self, callback):
        """Register a callback for newly accepted, already sanitized records."""
        with self._lock:
            self._observers.append(callback)

    def remove_observer(self, callback):
        with self._lock:
            self._observers = [c for c in self._observers if c != callback]

    def _notify(self, record):
        for callback in list(self._observers):
            try:
                callback(dict(record))
            except Exception:
                # An observer is a view, never a gate on logging.
                pass

    # -- reading -----------------------------------------------------------

    def records(self):
        with self._lock:
            return [dict(r) for r in self._ring]

    def clear(self):
        with self._lock:
            self._ring.clear()

    @property
    def debug(self):
        return self._debug

    def set_debug(self, enabled):
        """Temporary, in-memory only. Never persisted, never widens redaction."""
        self._debug = bool(enabled)

    def render(self, records=None):
        return format_records(self.records() if records is None else records,
                              source=self.source)

    def close(self):
        with self._lock:
            self._closed = True


def read_jsonl(path, limit=None):
    """Return (records, skipped). Malformed lines are counted, never raised."""
    records = []
    skipped = 0
    try:
        path = Path(path)
        if not path.exists():
            return [], 0
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except Exception:
                    skipped += 1
                    continue
                if isinstance(parsed, dict):
                    records.append(parsed)
                else:
                    skipped += 1
    except Exception:
        return records, skipped
    if limit is not None and len(records) > limit:
        records = records[-limit:]
    return records, skipped


def read_log_tail(log_dir, filename, limit=200):
    """Read the newest records across the active file and its backups."""
    records = []
    skipped = 0
    try:
        directory = Path(log_dir)
        names = ["{}.{}".format(filename, i) for i in range(BACKUPS, 0, -1)]
        names.append(filename)
        for name in names:
            part, part_skipped = read_jsonl(directory / name)
            records.extend(part)
            skipped += part_skipped
    except Exception:
        pass
    if limit is not None and len(records) > limit:
        records = records[-limit:]
    return records, skipped


def format_records(records, source=None):
    """One human-readable line per record. Input is assumed already sanitized."""
    lines = []
    try:
        for record in records or []:
            if not isinstance(record, dict):
                continue
            lines.append(format_record(record, source))
    except Exception:
        return "\n".join(lines)
    return "\n".join(lines)


def format_record(record, source=None):
    try:
        label = "[{}] ".format(source) if source else ""
        head = "{} {:<7} {}{}/{}".format(
            record.get("ts", "?"),
            str(record.get("level", "?")),
            label,
            record.get("component", "?"),
            record.get("event", "?"),
        )
        extras = []
        if record.get("task") is not None:
            extras.append("task={}".format(record["task"]))
        if record.get("request"):
            extras.append("request={}".format(record["request"]))
        fields = record.get("fields")
        if isinstance(fields, dict):
            for key in sorted(fields):
                extras.append("{}={}".format(key, fields[key]))
        exc = record.get("exc")
        if isinstance(exc, dict):
            extras.append("exception={}".format(exc.get("type")))
            if exc.get("cause"):
                extras.append("cause={}".format(exc["cause"]))
            for key in ("errno", "winerror"):
                if exc.get(key) is not None:
                    extras.append("{}={}".format(key, exc[key]))
            if exc.get("msg"):
                extras.append("msg={}".format(exc["msg"]))
        return head + (" " + " ".join(extras) if extras else "")
    except Exception:
        return "<unreadable record>"


# ---------------------------------------------------------------------------
# Python logging bridge (cove logger only - never the root logger)
# ---------------------------------------------------------------------------

COVE_LOGGER_NAME = "cove"


class _DiagHandler(logging.Handler):
    def __init__(self, diag):
        super().__init__()
        self._diag = diag

    def emit(self, record):
        try:
            level = record.levelname.upper()
            if level == "CRITICAL":
                level = "ERROR"
            if level not in LEVELS:
                level = "INFO"
            self._diag.emit(
                _safe_name(record.name, "cove"),
                "log",
                level,
                msg=record.getMessage(),
                exc=record.exc_info[1] if record.exc_info else None,
            )
        except Exception:
            pass


def attach_python_logging(diag):
    """Route ``cove.*`` logging into diagnostics. Third-party logs stay out."""
    handler = _DiagHandler(diag)
    logger = logging.getLogger(COVE_LOGGER_NAME)
    logger.addHandler(handler)
    return handler


def detach_python_logging(handler):
    try:
        logging.getLogger(COVE_LOGGER_NAME).removeHandler(handler)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Process-wide logger (explicit init, no import-time side effects)
# ---------------------------------------------------------------------------

_LOGGER = None
_LOGGER_LOCK = threading.RLock()


def log_dir_for(data_dir):
    return Path(data_dir) / LOG_DIR_NAME


def get_logger():
    return _LOGGER


def init_app_logger(data_dir):
    global _LOGGER
    with _LOGGER_LOCK:
        if _LOGGER is not None:
            return _LOGGER
        try:
            _LOGGER = DiagLogger(log_dir=log_dir_for(data_dir), filename=APP_LOG_NAME,
                                 source="app")
        except Exception:
            _LOGGER = DiagLogger(log_dir=None, source="app")
        return _LOGGER


def init_native_host_logger(data_dir):
    """Separate file and separate writer - never shared with the GUI."""
    try:
        return DiagLogger(
            log_dir=log_dir_for(data_dir),
            filename=NATIVE_LOG_NAME,
            max_bytes=NATIVE_MAX_BYTES,
            backups=NATIVE_BACKUPS,
            source="host",
        )
    except Exception:
        return DiagLogger(log_dir=None, source="host")


def shutdown_logger():
    global _LOGGER
    with _LOGGER_LOCK:
        if _LOGGER is not None:
            _LOGGER.close()
        _LOGGER = None


def emit(component, event, level="INFO", task_id=None, request_id=None, exc=None,
         **fields):
    """Module-level emit. A no-op before init, and never raises."""
    log = _LOGGER
    if log is None:
        return
    log.emit(component, event, level, task_id=task_id, request_id=request_id,
             exc=exc, **fields)


# ---------------------------------------------------------------------------
# Support-report helpers
# ---------------------------------------------------------------------------

SANITIZATION_NOTICE = (
    "Secrets and personal paths are sanitized. Extension-local logs are not "
    "shown here when the extension cannot reach Cove. Use Copy diagnostics in "
    "the extension popup for disconnected extension failures."
)


def install_mode():
    """source, appimage, installed or portable - the shapes Cove ships in.

    An AppImage runs Cove from source inside its mounted AppDir, so without
    the AppImage check it reported "source" and a support log could not tell
    the two apart. Labelling only: nothing about DATA_DIR, the IPC endpoint
    name or the native-host manifest is derived from this.
    """
    try:
        import sys

        try:
            from .magnet_identity import APPIMAGE, build_identity

            if build_identity() == APPIMAGE:
                return "appimage"
        except Exception:
            pass
        if not getattr(sys, "frozen", False):
            return "source"
        try:
            from .portable import is_portable

            return "portable" if is_portable() else "installed"
        except Exception:
            return "installed"
    except Exception:
        return "source"


def environment_facts():
    """Machine facts that are safe to publish in a support report."""
    facts = {
        "app_version": "unknown",
        "os": "unknown",
        "os_version": "unknown",
        "arch": "unknown",
        "mode": "source",
    }
    try:
        import platform

        from . import __version__

        facts["app_version"] = str(__version__)
        facts["os"] = platform.system() or "unknown"
        # release() only; version() can carry a build/host string.
        facts["os_version"] = platform.release() or "unknown"
        facts["arch"] = platform.machine() or "unknown"
        facts["mode"] = install_mode()
    except Exception:
        pass
    return {key: scrub_text(str(value)) for key, value in facts.items()}


def support_header(session=None, filters=None, extra=None):
    """The block that precedes copied or saved diagnostics."""
    facts = environment_facts()
    lines = [
        "Cove diagnostics report",
        "app version: {}".format(facts["app_version"]),
        "os: {} {}".format(facts["os"], facts["os_version"]),
        "arch: {}".format(facts["arch"]),
        "install mode: {}".format(facts["mode"]),
        "session: {}".format(session or "unknown"),
        "filters: {}".format(filters or "none"),
    ]
    if extra:
        for key in sorted(extra):
            lines.append("{}: {}".format(key, scrub_text(str(extra[key]))))
    lines.append(SANITIZATION_NOTICE)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Component taxonomy
# ---------------------------------------------------------------------------

COMPONENTS = (
    "app",
    "queue",
    "extractor",
    "extractor.publish",
    "aria2",
    "debrid",
    "native_host",
    "extension.popup",
    "extension.background",
    "extension.content",
    "extension.native_bridge",
    "diagnostics",
)


def sanitize_record(record):
    """Re-sanitize a record that was read back off disk.

    Records written by this process are already clean, but the Diagnostics
    window also merges the native host's file, which is written by a separate
    process and could be stale, hand-edited or truncated. Nothing reaches the
    view without passing through here.
    """
    if not isinstance(record, dict):
        return None
    try:
        out = {
            "ts": scrub_text(str(record.get("ts", ""))) or "?",
            "level": record.get("level") if record.get("level") in LEVELS else "INFO",
            "component": _safe_name(record.get("component"), "unknown"),
            "event": _safe_name(record.get("event"), "unknown"),
            "session": _safe_name(str(record.get("session", "")), "unknown"),
        }
        task = record.get("task")
        if isinstance(task, int) and not isinstance(task, bool):
            out["task"] = task
        request = normalize_request_id(record.get("request"))
        if request:
            out["request"] = request
        fields = sanitize_fields(record.get("fields"))
        if fields:
            out["fields"] = fields
        exc = record.get("exc")
        if isinstance(exc, dict):
            out["exc"] = {
                "type": _safe_name(exc.get("type"), REDACTED),
                "cause": _safe_name(exc.get("cause"), None) if exc.get("cause") else None,
                "errno": exc.get("errno") if isinstance(exc.get("errno"), int) else None,
                "winerror": (
                    exc.get("winerror") if isinstance(exc.get("winerror"), int) else None
                ),
                "msg": scrub_text(str(exc.get("msg", ""))),
                "traceback": (
                    scrub_text(str(exc.get("traceback"))) if exc.get("traceback") else None
                ),
            }
        return out
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Classification helpers
#
# These exist so the queue can describe an incident without the diagnostics
# code having to reach into debrid routing or output-path validation. They
# observe, they never decide.
# ---------------------------------------------------------------------------

_SHARE_LINK_ROUTES = {
    "real-debrid.com": ("/d/", "real_debrid", "real_debrid_generated_link"),
    "alldebrid.com": ("/f/", "all_debrid", "all_debrid_share_link"),
}

_DELIVERY_HOST_SUFFIXES = (
    ".download.real-debrid.com",
    ".debrid.it",
    ".alldebrid.com",
)


def url_facts(value):
    """Safe intake facts: scheme, sanitized host, route class, provider."""
    facts = {
        "scheme": None,
        "host": None,
        "route": None,
        "provider": None,
        "classification": "other",
    }
    try:
        if not isinstance(value, str) or not value.strip():
            return facts
        text = value.strip()
        if text.lower().startswith("magnet:"):
            facts["scheme"] = "magnet"
            facts["classification"] = "magnet"
            return facts
        parts = urlsplit(text)
        scheme = (parts.scheme or "").lower()
        if not scheme or not parts.netloc:
            return facts
        facts["scheme"] = scheme
        facts["host"] = _host_of(parts.netloc)
        facts["route"] = _route_of(parts.path)

        raw_host = parts.netloc.rsplit("@", 1)[-1].split(":", 1)[0].lower()
        if raw_host.startswith("www."):
            raw_host = raw_host[4:]
        entry = _SHARE_LINK_ROUTES.get(raw_host)
        if entry and parts.path.startswith(entry[0]):
            facts["provider"] = entry[1]
            facts["classification"] = entry[2]
            return facts
        if raw_host.endswith(_DELIVERY_HOST_SUFFIXES):
            facts["classification"] = "debrid_delivery_link"
            return facts
        if parts.path.lower().endswith(".torrent"):
            facts["classification"] = "torrent_file"
            return facts
        if scheme in ("http", "https"):
            facts["classification"] = "http_direct"
        elif scheme in ("ftp", "ftps"):
            facts["classification"] = "ftp_direct"
    except Exception:
        pass
    return facts


# Message prefix -> stable rule name. The messages come from
# cove/output_paths.py; matching on them keeps validation itself untouched.
_OUTPUT_PATH_RULES = (
    ("Engine output file does not exist", "engine_output_missing"),
    ("Invalid engine output path", "invalid_engine_output_path"),
    ("Engine output is outside its private directory", "outside_private_directory"),
    ("Engine output is the private directory", "output_is_private_directory"),
    ("Engine output contains a symlink", "symlink_in_output"),
    ("Engine output is not a regular file", "not_a_regular_file"),
    ("Engine did not report a final output path", "no_reported_output_path"),
    ("did not report a final output path", "no_reported_output_path"),
    ("Private output directory is not on the destination filesystem",
     "work_directory_wrong_filesystem"),
    ("Private output directory is missing", "work_directory_missing"),
    ("Private output directory ownership changed", "work_directory_ownership_changed"),
    ("Could not create private output directory", "work_directory_create_failed"),
    ("Destination directory is missing", "destination_missing"),
    ("Destination directory ownership changed", "destination_ownership_changed"),
    ("Destination is not a directory", "destination_not_a_directory"),
    ("Validated Windows directory identity is unavailable",
     "windows_identity_unavailable"),
)


def classify_output_path_error(message):
    """Name the validation rule that rejected a path, without echoing it."""
    try:
        if not isinstance(message, str):
            return "other"
        for prefix, rule in _OUTPUT_PATH_RULES:
            if prefix in message:
                return rule
    except Exception:
        pass
    return "other"
