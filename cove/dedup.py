"""Canonical identity for duplicate-download detection.

Pure helpers: no network access, no filesystem scanning, no database
access and no Qt. Everything here answers one question - "are these two
submitted sources the same download?" - and answers it conservatively.

A false positive merges two genuinely different resources and silently
costs the user a download they asked for. A false negative just means
they get a second copy, which is what they asked for anyway. So the rules
below only ever collapse spellings that cannot address different bytes:
scheme/host case, a redundant default port, a fragment. Query strings,
query order, path case, percent encoding and trailing slashes are all
left exactly as submitted, because on real hosts every one of them can
select a different file.
"""

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from . import torrent

# Identity kinds, strongest first.
ID_INFO_HASH = "info_hash"
ID_PROVIDER = "provider"
ID_URL = "url"

# Match categories.
LIVE = "live"
COMPLETED = "completed"

_DEFAULT_PORTS = {"http": 80, "https": 443, "ftp": 21}


@dataclass(frozen=True)
class Candidate:
    """One thing the user is trying to add, before anything is started."""

    url: str
    source_type: str = ""
    info_hash: str = ""
    debrid_route: str = ""
    debrid_item_id: str = ""
    # Human-facing label, used by the batch summary. Never a full URL.
    name: str = ""


@dataclass(frozen=True)
class DuplicateMatch:
    """What the caller needs to describe an existing download, and no more.

    Deliberately absent: resolved provider URLs, credentials, whole
    database rows. Nothing here is transient or sensitive.
    """

    category: str  # LIVE | COMPLETED
    identity: str  # ID_INFO_HASH | ID_PROVIDER | ID_URL
    task_id: int | None = None
    status: str = ""
    name: str = ""
    out_dir: str = ""
    filename: str = ""
    # False only where the engine genuinely cannot run the download twice
    # (a live torrent with this info hash), so the UI never offers an
    # action it would have to refuse.
    can_duplicate: bool = True


def magnet_info_hash(url) -> str:
    """The normalized v1 info hash of a magnet, or "" for anything else.

    Malformed magnets, v2-only magnets and non-magnets are all "not a
    torrent identity" here; deciding what to do about them stays with the
    existing add path, which already reports them.
    """
    if not torrent.is_magnet(url):
        return ""
    try:
        return torrent.parse_magnet(url).info_hash
    except Exception:
        return ""


def normalize_info_hash(value) -> str:
    """Lowercase hex form of a hex or base32 info hash, or "" if unusable."""
    if not value:
        return ""
    try:
        return torrent.normalize_info_hash(value)
    except Exception:
        return ""


def canonical_url(url) -> str:
    """Collapse only the URL spellings that cannot address different bytes.

    Lowercases the scheme and host, drops a port that is the default for
    the scheme, and drops the fragment. Everything else - query string,
    query order, path case, percent encoding, trailing slash, userinfo,
    non-default port - is preserved byte for byte.
    """
    if not isinstance(url, str):
        return ""
    text = url.strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").lower()
        port = parts.port
        username = parts.username
        password = parts.password
    except ValueError:
        # An unparseable authority (bad port, malformed IPv6 literal).
        # Compare it verbatim rather than guessing at its structure.
        return text
    if not host:
        # magnet:, data:, or something we do not understand well enough to
        # rewrite. Verbatim is the safe answer.
        return text
    netloc = host
    if ":" in host and not host.startswith("["):
        netloc = f"[{host}]"
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{netloc}:{port}"
    if username is not None:
        userinfo = username if password is None else f"{username}:{password}"
        netloc = f"{userinfo}@{netloc}"
    return urlunsplit((scheme, netloc, parts.path, parts.query, ""))


def identity(cand: Candidate) -> tuple[str, str] | None:
    """The strongest identity this candidate supports, or None.

    Precedence: info hash, then a stable provider item, then the original
    URL. `resolved_url` is never an input here - it is a short-lived
    delivery link, not an identity.
    """
    info_hash = normalize_info_hash(cand.info_hash) or magnet_info_hash(cand.url)
    if info_hash:
        return (ID_INFO_HASH, info_hash)
    route = (cand.debrid_route or "").strip()
    item_id = (cand.debrid_item_id or "").strip()
    if route and item_id:
        return (ID_PROVIDER, f"{route}\x00{item_id}")
    canon = canonical_url(cand.url)
    if canon:
        return (ID_URL, canon)
    return None


def same(a: Candidate, b: Candidate) -> bool:
    ident_a = identity(a)
    return ident_a is not None and ident_a == identity(b)


def safe_label(cand: Candidate) -> str:
    """A short, non-sensitive way to name a candidate in the UI.

    Never the full URL: a signed link's query carries the token, and a
    private-tracker magnet carries a passkey. Host plus the last path
    segment is enough for the user to recognise the item.
    """
    if cand.name:
        return cand.name
    if torrent.is_magnet(cand.url):
        info_hash = magnet_info_hash(cand.url)
        return f"torrent {info_hash[:8]}" if info_hash else "torrent"
    try:
        parts = urlsplit((cand.url or "").strip())
        host = (parts.hostname or "").lower()
        tail = parts.path.rsplit("/", 1)[-1]
    except ValueError:
        return "download"
    if host and tail:
        return f"{host}/{tail}"
    return host or "download"
