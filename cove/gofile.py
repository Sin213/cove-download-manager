"""Gofile share links: recognition, listing, and download resolution.

A Gofile share page (`https://gofile.io/d/<code>`) is a JavaScript web app,
never a file. Handing it to aria2 saves the ~3 KB landing page under whatever
name the task carries, and the download then "completes". This module is the
only way a Gofile share enters Cove: it lists the real files through Gofile's
documented API, and resolves one file at a time to a fresh download request.

Contract (https://gofile.io/api, read 2026-11-01):

* `GET /contents/{contentId}` with `Authorization: Bearer <token>`.
  `contentId` is a share code (8 characters, 6 on legacy folders) or a content
  UUID. A folder returns `children`; a file returns its own payload with a
  `link`. Paged through `page`/`pageSize` with `metadata.hasNextPage`.
* Reading contents through the API is **Premium-only**; other tiers receive
  `error-notPremium`. Cove therefore needs one optional Settings value, the
  user's own Gofile Premium API token. Nothing here mints guest accounts or
  imitates the website's anti-automation token.
* The website downloads a file by following `link` with the account token in
  an `accountToken` cookie. aria2 resends a custom `Cookie` header on every
  redirect hop, so Cove never gives it one. Instead the aria2 daemon loads a
  private Netscape cookie jar once, at startup (`load-cookies` is startup-only;
  RPC ignores it per download): a Secure cookie on `.gofile.io`. aria2's own
  cookie storage then sends it only over HTTPS to Gofile hosts, whatever
  redirects the transfer meets. The queue refuses to launch a Gofile transfer
  unless the running aria2 provably holds a jar for the *current* token (see
  `token_fingerprint`); it never restarts aria2 to make that true.

Everything the service returns is untrusted: names and paths are validated,
sizes are bounded, and the `link` must be an HTTPS Gofile host before the
token is ever sent to it. Error text is always one of the fixed strings below,
never derived from a response, a token, or a download URL.

Qt-free and import-light, like `cove.debrid`.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import unquote, urlencode, urljoin, urlsplit

import requests

from . import debrid

API_BASE = "https://api.gofile.io"
SHARE_HOSTS = frozenset({"gofile.io", "www.gofile.io"})
_DOWNLOAD_HOST = "gofile.io"
_CODE_RE = re.compile(r"[A-Za-z0-9]{6,8}")
_ID_RE = re.compile(r"[A-Za-z0-9-]{6,64}")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")

PAGE_SIZE = 100
MAX_FILES = 1000
MAX_REQUESTS = 60
MAX_DEPTH = 8
MAX_REDIRECTS = 4
PROBE_BYTES = 4096
# The jar's scope. Module-level so tests can point a real aria2 at loopback;
# production values are never overridden.
COOKIE_DOMAIN = ".gofile.io"
COOKIE_SECURE = True
COOKIE_EXPIRES = 2**31 - 1   # a session-long cookie must not lapse inside a long-running aria2

INTAKE_MESSAGE = "Add Gofile share links from Add Download so you can choose files."
UNAVAILABLE = "Gofile is unavailable. Try again shortly."
UNSUPPORTED_LINK = "Cove cannot open this Gofile link form. Use a share link like https://gofile.io/d/AbCd1234."
NEEDS_TOKEN = ("Gofile only lists shared files for Premium accounts. "
               "Add your Gofile Premium API token in Settings to download Gofile shares.")
NOT_PREMIUM = ("This Gofile account is not Premium, so Gofile will not list shared files. "
               "Use a Premium API token in Settings.")
BAD_TOKEN = "Gofile rejected your API token. Check it in Settings."
NOT_FOUND = "This Gofile share is unavailable, expired or was deleted."
NO_ACCESS = "This Gofile share is password protected or not accessible, which Cove cannot open."
RATE_LIMITED = "Gofile is limiting requests. Try again later."
BAD_LISTING = "Gofile returned a file listing Cove could not safely read."
NO_FILES = "No downloadable files were found in this Gofile share."
TOO_MANY = "Gofile shares with more than 1000 files cannot be listed."
FILE_GONE = "This Gofile file is no longer available."
FILE_DENIED = "Gofile refused the download. Check your API token and that the file is still shared."
LANDING_PAGE = ("Gofile returned a webpage instead of the requested file. "
                "The download was not completed.")
COOKIE_EXTERNAL = ("This aria2 engine was not started by Cove, so Cove cannot keep your Gofile token "
                   "limited to Gofile. Gofile downloads need Cove's own engine.")
COOKIE_RESTART = ("The Gofile token changed after Cove's download engine started. Restart Cove to "
                  "apply it; downloads already running are not affected.")
SIZE_MISMATCH = ("The downloaded file does not match the size Gofile reported. "
                 "The download was not completed.")

# Browser refusals are fixed protocol text, never service-supplied diagnostics.
BROWSER_MESSAGES = {
    "gofile_empty": "Cove does not take over empty Gofile files. Firefox is keeping the download.",
    "gofile_needs_token": "Add your Gofile API token in Cove Settings. Firefox is keeping the download.",
    "gofile_denied": "Gofile refused access. Check your token and file access. Firefox is keeping the download.",
    "gofile_gone": "This Gofile file is no longer available. Firefox is keeping the download.",
    "gofile_rate_limited": "Gofile is limiting requests. Try again later. Firefox is keeping the download.",
    "gofile_page": "Gofile did not return the file. Check your Gofile API token in Cove Settings. Firefox is keeping the download.",
    "gofile_unavailable": "Cove could not verify this Gofile download. Try again shortly. Firefox is keeping the download.",
    "gofile_restart": "Restart Cove to apply your Gofile token to its download engine. Firefox is keeping the download.",
    "gofile_engine": "Gofile downloads need Cove's own download engine. Firefox is keeping the download.",
    "gofile_token": "Cove's Gofile token changed during the handoff. Try again. Firefox is keeping the download.",
    "gofile_duplicate": "This Gofile download is already in Cove. Firefox is keeping the download.",
}


class GofileError(Exception):
    """A Gofile failure already reduced to a fixed, safe sentence."""

    def __init__(self, code: str, user_message: str):
        self.code = code
        self.user_message = user_message
        super().__init__(user_message)


def browser_error(reason: str) -> GofileError:
    return GofileError(reason, BROWSER_MESSAGES[reason])


@dataclass(frozen=True)
class GofileFile:
    name: str
    path: tuple[str, ...]
    size: int
    file_id: str
    share: str


@dataclass(frozen=True)
class GofileListing:
    files: tuple[GofileFile, ...]
    unavailable: int = 0


@dataclass(frozen=True)
class GofileDownload:
    """One resolved request. `url` is a secret: never log it."""

    url: str
    size: int


# ---------------------------------------------------------------------------
# Recognition
# ---------------------------------------------------------------------------


def is_share_url(url) -> bool:
    """Any Gofile share-page form, including malformed ones.

    Broken links are claimed on purpose: a recognised Gofile page must never
    fall through to an ordinary HTTP download of its HTML. Exact host match
    only, so look-alike domains are never claimed.
    """
    try:
        parts = urlsplit(url.strip())
        if parts.scheme.lower() not in ("http", "https") or parts.hostname not in SHARE_HOSTS:
            return False
        return (parts.path == "/d" or parts.path.startswith("/d/")
                or (parts.path in ("", "/") and "c=" in parts.query))
    except (TypeError, ValueError, AttributeError):
        return False


def is_gofile_family(url) -> bool:
    """Claim the whole host family for browser refusal, including repaired URLs.

    This is deliberately broader than delivery admission. Decode host escapes,
    apply IDNA and remove trailing dots; ambiguous authorities containing the
    family name are claimed rather than passed to a different URL parser.
    Manual queue intake and the share resolver do not use this predicate.
    """
    if not isinstance(url, str):
        return False
    authority = re.match(r"^(?:https?|ftp):[\\/]*([^/?#]*)", url.strip(), re.IGNORECASE)
    if authority is None:
        return False
    raw = unquote(authority[1])
    try:
        parts = urlsplit(url.strip())
        host = unquote(parts.hostname or "")
        parts.port  # Malformed ports/authorities must use the conservative fallback.
        if not host or "\\" in raw or any(c in host for c in "/@%"):
            raise ValueError
        host = host.encode("idna").decode("ascii").lower().rstrip(".")
        return host == "gofile.io" or host.endswith(".gofile.io")
    except (ValueError, UnicodeError):
        try:
            raw = raw.encode("idna").decode("ascii")
        except UnicodeError:
            pass
        return "gofile.io" in raw.lower()


def is_delivery_url(url) -> bool:
    """Only a credential-free, unambiguous store /download/web file URL."""
    try:
        _download_url(url)  # Includes Requests' re-derived destination check.
        parts = urlsplit(url)
        host = parts.hostname or ""
        path = parts.path.split("/")
        return (re.fullmatch(r"(?!(?:www|api|upload|uploads)\.gofile\.io$)[a-z][a-z0-9]*(?:-[a-z0-9]+)*\.gofile\.io", host) is not None
                and len(host.split(".")[0]) <= 63
                and not parts.netloc.endswith(":")
                and "?" not in url and "#" not in url
                and len(path) == 5 and path[:3] == ["", "download", "web"]
                and _ID_RE.fullmatch(path[3]) is not None and bool(path[4])
                and re.fullmatch(r"(?:\.|%2e){1,2}", path[4], re.IGNORECASE) is None)
    except (GofileError, TypeError, ValueError, AttributeError):
        return False


def is_relay_delivery_url(url) -> bool:
    """Relay admission additionally requires the browser's canonical spelling."""
    return (is_delivery_url(url) and url.startswith("https://")
            and urlsplit(url).netloc.isascii()
            and urlsplit(url).netloc == urlsplit(url).netloc.lower())


def _canonical_parts(url):
    try:
        parts = urlsplit(url.strip())
        default_port = 443 if parts.scheme.lower() == "https" else 80
        if (parts.scheme.lower() not in ("http", "https") or parts.hostname not in SHARE_HOSTS
                or parts.username is not None or parts.password is not None
                or parts.port not in (None, default_port)):
            return None
        return parts
    except (TypeError, ValueError, AttributeError):
        return None


def parse_share_url(url) -> str | None:
    """The share code of a plain `/d/<code>` link, else None."""
    parts = _canonical_parts(url)
    if parts is None or parts.query or parts.fragment:
        return None
    match = re.fullmatch(r"/d/([A-Za-z0-9]+)/?", parts.path)
    if match is None or _CODE_RE.fullmatch(match[1]) is None:
        return None
    return match[1]


def file_ref(share: str, file_id: str) -> str:
    """Stable per-file identity stored on the task row.

    It names the file, never a download location: the real link is
    account-bound and can change, so it is fetched again on every launch.
    """
    if _CODE_RE.fullmatch(share or "") is None or _ID_RE.fullmatch(file_id or "") is None:
        raise GofileError("bad_identity", BAD_LISTING)
    return f"https://gofile.io/d/{share}#{file_id}"


def parse_file_ref(url) -> tuple[str, str] | None:
    parts = _canonical_parts(url)
    if parts is None or parts.query:
        return None
    match = re.fullmatch(r"/d/([A-Za-z0-9]+)", parts.path)
    if (match is None or _CODE_RE.fullmatch(match[1]) is None
            or _ID_RE.fullmatch(parts.fragment) is None):
        return None
    return match[1], parts.fragment


# ---------------------------------------------------------------------------
# Validation of untrusted listing data
# ---------------------------------------------------------------------------


def _token(settings) -> str:
    token = getattr(settings, "gofile_api_token", "")
    token = token.strip() if isinstance(token, str) else ""
    if not token:
        raise GofileError("missing_token", NEEDS_TOKEN)
    if _TOKEN_RE.fullmatch(token) is None:
        raise GofileError("bad_token", BAD_TOKEN)
    return token


def _path(parts) -> tuple[str, ...]:
    if not isinstance(parts, tuple) or not 1 <= len(parts) <= debrid._MAX_TREE_DEPTH:
        raise GofileError("unsafe_path", BAD_LISTING)
    for part in parts:
        # Both validators can clean their input; a path must survive both
        # unchanged, or it is not the name the service actually reported.
        try:
            clean = (isinstance(part, str)
                     and debrid._torrent_component(part, debrid.ALL_DEBRID) == part
                     and debrid._safe_filename(part) == part)
        except debrid.DebridError:
            clean = False
        if not clean:
            raise GofileError("unsafe_path", BAD_LISTING)
    return parts


def _size(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63 - 1:
        raise GofileError("bad_size", BAD_LISTING)
    return value


def token_fingerprint(token: str) -> str:
    """Identity of the token an aria2 daemon holds; compared, never displayed."""
    return hashlib.sha256(("cove-gofile:" + token).encode("utf-8")).hexdigest()


def cookie_jar_text(token: str) -> str:
    """The account cookie as a Netscape jar line, host-scoped and Secure."""
    if not isinstance(token, str) or _TOKEN_RE.fullmatch(token) is None:
        raise GofileError("bad_token", BAD_TOKEN)
    subdomains = "TRUE" if COOKIE_DOMAIN.startswith(".") else "FALSE"
    return ("# Netscape HTTP Cookie File\n"
            f"{COOKIE_DOMAIN}\t{subdomains}\t/\t{'TRUE' if COOKIE_SECURE else 'FALSE'}\t"
            f"{COOKIE_EXPIRES}\taccountToken\t{token}\n")


def _is_gofile_host(host: str) -> bool:
    return host == _DOWNLOAD_HOST or host.endswith("." + _DOWNLOAD_HOST)


def _download_url(value) -> str:
    """Only an HTTPS Gofile host may ever receive the account token.

    urllib and Requests disagree about some inputs (a backslash ends the host
    for Requests but not for urllib), so a URL is accepted only when it has
    no backslash or whitespace, and the destination Requests would actually
    contact is re-derived and checked as well. Only the host must be ASCII;
    real file names may be anything in the path.
    """
    try:
        if (not isinstance(value, str) or "\\" in value
                or not value.isprintable() or any(c.isspace() for c in value)):
            raise ValueError
        sent = urlsplit(requests.Request("GET", value).prepare().url)
        parts = urlsplit(value)
        valid = (parts.netloc.isascii() and parts.scheme == "https" and sent.scheme == "https"
                 and sent.hostname == parts.hostname
                 and _is_gofile_host(parts.hostname or "")
                 and parts.username is None and parts.password is None
                 and sent.username is None and sent.password is None
                 and not parts.netloc.endswith(":")
                 and parts.port in (None, 443) and sent.port in (None, 443))
    except Exception:
        valid = False
    if not valid:
        raise GofileError("unsafe_url", FILE_DENIED)
    return value


# ---------------------------------------------------------------------------
# API access
# ---------------------------------------------------------------------------


_STATUS_ERRORS = {
    "error-notPremium": ("not_premium", NOT_PREMIUM),
    "error-token": ("bad_token", BAD_TOKEN),
    "error-wrongToken": ("bad_token", BAD_TOKEN),
    "error-notFound": ("not_found", NOT_FOUND),
    "error-rateLimit": ("rate_limited", RATE_LIMITED),
}


def _http_error(status: int, body) -> GofileError:
    api_status = body.get("status") if isinstance(body, dict) else None
    if api_status in _STATUS_ERRORS:
        return GofileError(*_STATUS_ERRORS[api_status])
    if status == 429:
        return GofileError(*_STATUS_ERRORS["error-rateLimit"])
    if status in (401, 403):
        return GofileError("denied", BAD_TOKEN)
    if status in (404, 410):
        return GofileError(*_STATUS_ERRORS["error-notFound"])
    if status >= 500:
        return GofileError("unavailable", UNAVAILABLE)
    return GofileError("unsupported", BAD_LISTING)


def _api_get(session, token: str, content_id: str, page: int | None = None):
    """One bounded `GET /contents/{id}`; returns (data, metadata)."""
    if _ID_RE.fullmatch(content_id) is None:
        raise GofileError("bad_identity", BAD_LISTING)
    url = f"{API_BASE}/contents/{content_id}"
    if page is not None:
        url += "?" + urlencode({"page": page, "pageSize": PAGE_SIZE})
    getter = (debrid.requests if session is None else session).get
    try:
        resp = getter(url, headers={"Authorization": f"Bearer {token}"},
                      timeout=debrid.HTTP_TIMEOUT, allow_redirects=False)
        status = int(resp.status_code)
    except Exception:
        raise GofileError("transport", UNAVAILABLE) from None
    try:
        body = resp.json()
    except Exception:
        body = None
    if status != 200:
        raise _http_error(status, body)
    if not isinstance(body, dict):
        raise GofileError("unsupported", BAD_LISTING)
    if body.get("status") != "ok":
        raise _http_error(200, body)
    data = body.get("data")
    if not isinstance(data, dict):
        raise GofileError("unsupported", BAD_LISTING)
    metadata = body.get("metadata")
    return data, metadata if isinstance(metadata, dict) else {}


def _folder_children(session, token: str, content_id: str, budget: dict):
    """Every child of one folder across pages, plus the first page's data."""
    first, children, page = None, {}, 1
    while True:
        budget["requests"] += 1
        if budget["requests"] > MAX_REQUESTS:
            raise GofileError("too_large", TOO_MANY)
        data, metadata = _api_get(session, token, content_id, page)
        if first is None:
            first = data
        if data.get("type") != "folder":
            return first, None
        if data.get("canAccess") is False:
            raise GofileError("no_access", NO_ACCESS)
        page_children = data.get("children", {})
        if not isinstance(page_children, dict):
            raise GofileError("unsupported", BAD_LISTING)
        for key, node in page_children.items():
            if key in children or not isinstance(node, dict) or node.get("id") != key:
                raise GofileError("unsupported", BAD_LISTING)
            children[key] = node
        if metadata.get("hasNextPage") is not True:
            return first, children
        page += 1


def list_share(url, settings, *, session=None) -> GofileListing:
    """List every file in a share through the documented API.

    Never falls back to fetching the share page itself.
    """
    code = parse_share_url(url)
    if code is None:
        raise GofileError("unsupported_link", UNSUPPORTED_LINK)
    token = _token(settings)
    budget = {"requests": 0}
    files, unavailable = [], 0
    pending = [(code, (), 0)]
    seen = set()
    while pending:
        content_id, prefix, depth = pending.pop()
        first, children = _folder_children(session, token, content_id, budget)
        if children is None:
            if content_id != code or first.get("type") != "file":
                raise GofileError("unsupported", BAD_LISTING)
            children, prefix = {first.get("id"): first}, ()
        for key, node in children.items():
            kind = node.get("type")
            if kind == "folder":
                if depth + 1 > MAX_DEPTH or not isinstance(node.get("name"), str):
                    unavailable += 1
                    continue
                try:
                    folder = _path(prefix + (node["name"],))
                except GofileError:
                    unavailable += 1
                    continue
                pending.append((key, folder, depth + 1))
            elif kind == "file":
                if key in seen:
                    raise GofileError("unsupported", BAD_LISTING)
                seen.add(key)
                if node.get("isFrozen") is True or _ID_RE.fullmatch(key) is None:
                    unavailable += 1
                    continue
                try:
                    name = node.get("name")
                    parts = _path(prefix + (name,))
                    size = _size(node.get("size"))
                except GofileError:
                    unavailable += 1
                    continue
                files.append(GofileFile(parts[-1], parts, size, key, code))
                if len(files) > MAX_FILES:
                    raise GofileError("too_large", TOO_MANY)
            else:
                raise GofileError("unsupported", BAD_LISTING)
    if not files:
        raise GofileError("empty", NO_FILES)
    return GofileListing(tuple(sorted(files, key=lambda f: (f.path, f.file_id))), unavailable)


# ---------------------------------------------------------------------------
# Download resolution and verification
# ---------------------------------------------------------------------------

_HTML_NAMES = (".html", ".htm", ".xhtml")
# Gofile's error envelope, whatever the whitespace, field order or quoting style.
_API_ERROR_RE = re.compile(rb'"status"\s*:\s*"error-')


def looks_like_web_page(head: bytes, name: str = "") -> bool:
    """True when the first bytes are a web page or API error, not the named file.

    Judged from the leading bytes only. A file legitimately named like a web
    page is not second-guessed for generic HTML, only for Gofile's own pages.
    """
    text = head[:PROBE_BYTES].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    # Gofile's own landing page and API error envelope are refused under any
    # filename: no real shared file looks like them, and a hostile or broken
    # response could otherwise pick a name that exempts it.
    if b"gofile needs javascript" in text or (
        text.startswith(b"{") and _API_ERROR_RE.search(text) is not None
    ):
        return True
    suffix = os.path.splitext(name)[1].lower()
    return suffix not in _HTML_NAMES and text.startswith(
        (b"<!doctype html", b"<html", b"<head", b"<body", b"<script"))


def browser_error_body(head):
    return (not head or looks_like_web_page(head)
            or bool(re.search(rb'"error"\s*:|"status"\s*:\s*"error', head)))


def verify_download(path, expected_size: int | None, name: str, *, browser=False) -> None:
    """Refuse a finished file that is not the one Gofile described.

    Bounded: reads the first few KB, never the whole file. `None` means no size
    was ever learned; zero is a real size and is enforced like any other.
    """
    with open(path, "rb") as handle:
        head = handle.read(PROBE_BYTES)
        size = os.fstat(handle.fileno()).st_size
    if looks_like_web_page(head, name) or (browser and browser_error_body(head)):
        raise GofileError("landing_page", LANDING_PAGE)
    if expected_size is not None and size != expected_size:
        raise GofileError("size_mismatch", SIZE_MISMATCH)


def _probe(session, link: str, token: str, name: str, size: int | None = None,
           *, deadline=None, advertised=None, browser=False) -> str:
    """Prove `link` serves file bytes before aria2 is involved.

    Follows redirects by hand so the token can only ever travel to Gofile
    hosts, and reads at most a few KB of the body.
    """
    getter = (debrid.requests if session is None else session).get
    for _ in range(MAX_REDIRECTS + 1):
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise GofileError("transport", UNAVAILABLE)
        link = _download_url(link)
        try:
            headers = {"Cookie": f"accountToken={token}", "Accept-Encoding": "identity"}
            if size != 0:  # no byte range exists in an empty file; asking is unsatisfiable (416)
                headers["Range"] = f"bytes=0-{PROBE_BYTES - 1}"
            resp = getter(link, headers=headers,
                          timeout=debrid.HTTP_TIMEOUT if remaining is None else min(remaining, 5),
                          allow_redirects=False, stream=True)
        except Exception:
            raise GofileError("transport", UNAVAILABLE) from None
        try:
            status = int(resp.status_code)
            if status in (301, 302, 303, 307, 308):
                if browser:  # Only the new browser-token relay forbids safe redirects.
                    raise GofileError("denied", FILE_DENIED)
                location = resp.headers.get("Location")
                if not isinstance(location, str) or not location:
                    raise GofileError("denied", FILE_DENIED)
                try:
                    if urlsplit(location).scheme:
                        _download_url(location)
                    link = urljoin(link, location)
                except ValueError:
                    raise GofileError("denied", FILE_DENIED) from None
                continue
            if status in (401, 403):
                raise GofileError("denied", FILE_DENIED)
            if status in (404, 410):
                raise GofileError("gone", FILE_GONE)
            if status == 429:
                raise GofileError("rate_limited", RATE_LIMITED)
            if status not in (200, 206):
                raise GofileError("unavailable", UNAVAILABLE)
            try:
                head = bytearray()
                for chunk in resp.iter_content(PROBE_BYTES):
                    if deadline is not None and time.monotonic() >= deadline:
                        raise GofileError("transport", UNAVAILABLE)
                    head.extend(chunk[:PROBE_BYTES - len(head)])
                    if len(head) == PROBE_BYTES:
                        break
                head = bytes(head)
            except Exception:
                raise GofileError("transport", UNAVAILABLE) from None
            content_type = str(resp.headers.get("Content-Type", "")).lower()
            if (looks_like_web_page(head, name)
                or (browser and head and browser_error_body(head))) or (
                content_type.startswith("text/html")
                and os.path.splitext(name)[1].lower() not in _HTML_NAMES
            ):
                raise GofileError("landing_page", LANDING_PAGE)
            if deadline is not None and time.monotonic() >= deadline:
                raise GofileError("transport", UNAVAILABLE)
            if browser:
                length = str(resp.headers.get("Content-Length", ""))
                if not head or length == "0":
                    raise browser_error("gofile_empty")
                if (not re.fullmatch(r"[0-9]{1,19}", length) or not 0 < int(length) <= 2**62
                        or len(head) != min(PROBE_BYTES, int(length))
                        or str(resp.headers.get("Content-Encoding", "identity")).lower() != "identity"):
                    raise GofileError("unavailable", UNAVAILABLE)
                for key in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                    value = str(resp.headers.get(key, ""))
                    if len(value) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in value):
                        raise GofileError("unavailable", UNAVAILABLE)
            if advertised is not None:
                total = None
                if status == 206:
                    match = re.fullmatch(r"bytes ([0-9]{1,19})-([0-9]{1,19})/([0-9]{1,19})", str(resp.headers.get("Content-Range", "")))
                    first, last, total = map(int, match.groups()) if match else (-1, -1, 0)
                    if first != 0 or not 0 <= last < total <= 2**62:
                        raise GofileError("unavailable", UNAVAILABLE)
                    if browser and (last != min(PROBE_BYTES, total) - 1
                                    or int(length) != last + 1 or len(head) != last + 1):
                        raise GofileError("unavailable", UNAVAILABLE)
                elif str(resp.headers.get("Content-Encoding", "")).strip().lower() in ("", "identity"):
                    value = resp.headers.get("Content-Length", "")
                    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value) and int(value) <= 2**62:
                        total = int(value)
                if total == 0 or not head:
                    raise browser_error("gofile_empty")
                if total is not None:
                    advertised.append(total)
            return link
        finally:
            close = getattr(resp, "close", None)
            if close is not None:
                close()
    raise GofileError("denied", FILE_DENIED)


def upstream_session(settings):
    """Use Cove's selected interface and explicit proxy for browser transfers."""
    from . import netiface
    from urllib.parse import quote

    kind = settings.proxy_type
    if kind == "socks5":
        try:
            import socks  # noqa: F401
        except ImportError:
            raise browser_error("gofile_unavailable") from None
    session = netiface.bound_requests_session(settings.torrent_network_interface or "")
    if kind != "none":
        # Match Aria2RPC._build_proxy_url; credentials never enter errors/logs.
        scheme = kind if kind in ("http", "https", "socks5") else "http"
        auth = ""
        if settings.proxy_username:
            auth = quote(settings.proxy_username, safe="")
            if settings.proxy_password:
                auth += ":" + quote(settings.proxy_password, safe="")
            auth += "@"
        port = f":{settings.proxy_port}" if settings.proxy_port else ""
        proxy = f"{scheme}://{auth}{settings.proxy_host}{port}"
        session.trust_env = False
        session.proxies = {"http": proxy, "https": proxy}
    return session


def verify_browser_download(url, filename, settings, *, session=None, deadline, browser_token=None):
    """Prove file access within an absolute monotonic deadline (at most 15s).

    Requests' read timeout limits idle time, not a trickling body or DNS. A
    daemon worker bounds the caller as well. It can never admit a task; after
    expiry every subsequent redirect is stopped by _probe's deadline check.
    The short-lived native host owns this worker and its HTTP session.
    """
    try:
        token = _token(settings) if browser_token is None else browser_token
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            raise browser_error("gofile_denied")
    except GofileError as exc:
        raise browser_error("gofile_needs_token" if exc.code == "missing_token" else "gofile_denied") from None
    if not is_delivery_url(url):
        raise browser_error("gofile_page")
    deadline = min(deadline, time.monotonic() + 15)
    done, outcome, advertised = threading.Event(), [], []

    def run():
        owned = None
        try:
            owned = requests.Session() if session is None else None
            _probe(session if session is not None else owned, url, token, "",
                   deadline=deadline, advertised=advertised, browser=browser_token is not None)
            outcome.append(None)
        except GofileError as exc:
            reason = {"denied": "gofile_denied", "unsafe_url": "gofile_denied",
                      "gofile_empty": "gofile_empty",
                      "gone": "gofile_gone", "rate_limited": "gofile_rate_limited",
                      "landing_page": "gofile_page"}.get(exc.code, "gofile_unavailable")
            outcome.append(reason)
        except Exception:
            outcome.append("gofile_unavailable")
        finally:
            if owned is not None:
                try:
                    owned.close()
                except Exception:
                    pass
            done.set()

    if deadline <= time.monotonic():
        raise browser_error("gofile_unavailable")
    threading.Thread(target=run, daemon=True, name="gofile-browser-proof").start()
    if not done.wait(max(0, deadline - time.monotonic())) or time.monotonic() >= deadline:
        raise browser_error("gofile_unavailable")
    if outcome[0] is not None:
        raise browser_error(outcome[0])
    return advertised[0] if advertised else None


def resolve_file(share: str, file_id: str, name: str, settings, *, session=None) -> GofileDownload:
    """A fresh, verified download request for one listed file."""
    token = _token(settings)
    data, _ = _api_get(session, token, file_id)
    if data.get("type") != "file" or data.get("id") != file_id:
        raise GofileError("gone", FILE_GONE)
    if data.get("isFrozen") is True:
        raise GofileError("gone", FILE_GONE)
    size = _size(data.get("size"))
    link = _probe(session, _download_url(data.get("link")), token, name, size)
    return GofileDownload(link, size)
