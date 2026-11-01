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
from dataclasses import dataclass
from urllib.parse import urlencode, urljoin, urlsplit

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


class GofileError(Exception):
    """A Gofile failure already reduced to a fixed, safe sentence."""

    def __init__(self, code: str, user_message: str):
        self.code = code
        self.user_message = user_message
        super().__init__(user_message)


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


def verify_download(path, expected_size: int | None, name: str) -> None:
    """Refuse a finished file that is not the one Gofile described.

    Bounded: reads the first few KB, never the whole file. `None` means no size
    was ever learned; zero is a real size and is enforced like any other.
    """
    with open(path, "rb") as handle:
        head = handle.read(PROBE_BYTES)
        size = os.fstat(handle.fileno()).st_size
    if looks_like_web_page(head, name):
        raise GofileError("landing_page", LANDING_PAGE)
    if expected_size is not None and size != expected_size:
        raise GofileError("size_mismatch", SIZE_MISMATCH)


def _probe(session, link: str, token: str, name: str, size: int | None = None) -> str:
    """Prove `link` serves file bytes before aria2 is involved.

    Follows redirects by hand so the token can only ever travel to Gofile
    hosts, and reads at most a few KB of the body.
    """
    getter = (debrid.requests if session is None else session).get
    for _ in range(MAX_REDIRECTS + 1):
        link = _download_url(link)
        try:
            headers = {"Cookie": f"accountToken={token}", "Accept-Encoding": "identity"}
            if size != 0:  # no byte range exists in an empty file; asking is unsatisfiable (416)
                headers["Range"] = f"bytes=0-{PROBE_BYTES - 1}"
            resp = getter(link, headers=headers,
                          timeout=debrid.HTTP_TIMEOUT, allow_redirects=False, stream=True)
        except Exception:
            raise GofileError("transport", UNAVAILABLE) from None
        try:
            status = int(resp.status_code)
            if status in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                if not isinstance(location, str) or not location:
                    raise GofileError("denied", FILE_DENIED)
                link = urljoin(link, location)
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
                head = next(resp.iter_content(PROBE_BYTES), b"")
            except Exception:
                raise GofileError("transport", UNAVAILABLE) from None
            content_type = str(resp.headers.get("Content-Type", "")).lower()
            if looks_like_web_page(head, name) or (
                content_type.startswith("text/html")
                and os.path.splitext(name)[1].lower() not in _HTML_NAMES
            ):
                raise GofileError("landing_page", LANDING_PAGE)
            return link
        finally:
            close = getattr(resp, "close", None)
            if close is not None:
                close()
    raise GofileError("denied", FILE_DENIED)


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
