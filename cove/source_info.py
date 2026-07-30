"""Safe, read-only view of where a download came from.

Backs the per-task "View source" action. Everything here is deliberately
conservative: a task carries values that are either private (browser
cookies) or short-lived secrets (`resolved_url`, the debrid delivery
node), and none of those may reach the screen. What is shown is the
information the user needs to recognise a download - the URL, the page it
came from, where it is being saved - with credentials and signed-link
parameters masked.

No network access, no filesystem access: this module only reshapes the
task fields already in memory.
"""
from __future__ import annotations

from urllib.parse import unquote_plus, urlsplit, urlunsplit

REDACTED = "[redacted]"

# Substrings that mark a query parameter as a credential or a signed-link
# component. Matched on whole "words" of the parameter name so that
# ordinary names which merely contain these letters (monkey, keyboard)
# stay readable.
_SECRET_WORDS = frozenset(
    {
        "token",
        "key",
        "apikey",
        # Private-tracker credentials, the query-parameter form of the
        # passkey already masked inside magnet tr= values.
        "passkey",
        "authkey",
        "torrentpass",
        "sig",
        "signature",
        "secret",
        "password",
        "passwd",
        "pwd",
        "auth",
        "credential",
        "credentials",
        "session",
        "sid",
        "access",
        "hash",
    }
)

# Magnet parameters whose value is itself a URL that commonly embeds a
# private tracker passkey in its path (tr=tracker, ws/as/xs=web seeds and
# fallback sources). The whole value is masked: the passkey can sit
# anywhere in it, and the tracker host is not what the user is checking.
_SECRET_MAGNET_PARAMS = frozenset({"tr", "ws", "as", "xs"})

# Shortest path segment treated as a possible bare credential. Real
# passkeys and signed-link tokens are 32 hex characters or more; 20 keeps a
# margin without reaching into ordinary directory names.
_MIN_CREDENTIAL_LEN = 20


def _is_secret_param(name: str, scheme: str = "") -> bool:
    if scheme == "magnet" and name.lower() in _SECRET_MAGNET_PARAMS:
        return True
    # Split on the separators used by real signed URLs: token, api_key,
    # X-Amz-Signature. Case boundaries count as separators too, so
    # accessToken splits into access + token; lowercasing first would hide
    # the boundary and leak the value.
    words: list[str] = []
    current = ""
    for ch in name:
        if not ch.isalnum():
            words.append(current)
            current = ""
        elif ch.isupper() and current and not current[-1].isupper():
            words.append(current)
            current = ch
        else:
            current += ch
    words.append(current)
    return any(word.lower() in _SECRET_WORDS for word in words if word)


def redact_url(url) -> str:
    """Return `url` with credentials and secret parameters masked.

    Falls back to a fully masked value rather than the original if the URL
    cannot be parsed, so a malformed link never leaks by accident.
    """
    if not url or not isinstance(url, str):
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return REDACTED

    netloc = parts.netloc
    if "@" in netloc:
        host = netloc.rsplit("@", 1)[1]
        netloc = f"{REDACTED}@{host}"

    return urlunsplit(
        (
            parts.scheme,
            netloc,
            _redact_path(parts.path),
            _redact_query(parts.query, (parts.scheme or "").lower()),
            # A fragment is parameter-shaped in the OAuth implicit flow
            # (#access_token=...), so it gets the same treatment as a query.
            # A plain anchor has no "=" and passes through untouched.
            _redact_query(parts.fragment, (parts.scheme or "").lower()),
        )
    )


def _redact_path(path: str) -> str:
    """Mask path segments that are shaped like a bare credential.

    A private tracker's passkey, and many signed-link tokens, sit in the
    path rather than the query, where no parameter name gives them away.
    The only usable signal left is shape, so this masks exactly one thing:
    a long unbroken run of hex or base32, which is what those keys are and
    what ordinary path segments are not. Names, dates, titles and file
    extensions all contain separators or lowercase words and survive.

    This is a shape test, not a guarantee: a short or word-like secret in a
    path (`/PASSKEY123/`) is indistinguishable from a directory name and is
    left alone, which is why the dialog does not claim otherwise.
    """
    if not path or "/" not in path:
        return path
    return "/".join(
        REDACTED if _is_credential_shaped(seg) else seg for seg in path.split("/")
    )


def _is_credential_shaped(segment: str) -> bool:
    if len(segment) < _MIN_CREDENTIAL_LEN:
        return False
    lowered = segment.lower()
    if all(ch in "0123456789abcdef" for ch in lowered):
        return True
    # Base32 (aria2/torrent tooling emits these) - letters and digits 2-7,
    # with no vowel-bearing structure to read. Require a digit so that a
    # long lowercase word is never mistaken for a key.
    if all(ch.isalnum() for ch in segment) and any(ch.isdigit() for ch in segment):
        return all(ch in "abcdefghijklmnopqrstuvwxyz234567" for ch in lowered)
    return False


def _redact_query(query: str, scheme: str = "") -> str:
    """Mask secret parameter values, byte for byte otherwise.

    Deliberately hand-rolled rather than parse_qsl/urlencode: a round trip
    through those rewrites the escaping of every surviving parameter
    (`a%2Fb` becomes `a/b`), and the point of this view is to show the user
    the link they actually have.
    """
    if not query:
        return ""
    out: list[str] = []
    for item in query.split("&"):
        name, sep, value = item.partition("=")
        if sep and value and _is_secret_param(unquote_plus(name), scheme):
            out.append(f"{name}={REDACTED}")
        else:
            out.append(item)
    return "&".join(out)


def source_details(task) -> list[tuple[str, str]]:
    """Label/value rows describing where `task` came from.

    Empty fields are omitted. Cookies are acknowledged but never shown,
    and the resolved debrid delivery URL is never included at all.
    """
    rows: list[tuple[str, str]] = [("Source URL", redact_url(task.url))]

    if getattr(task, "referrer", ""):
        rows.append(("Referrer", redact_url(task.referrer)))
    if getattr(task, "user_agent", ""):
        rows.append(("User agent", task.user_agent))
    if getattr(task, "cookies", ""):
        rows.append(("Browser cookies", "Stored, not shown"))
    if getattr(task, "debrid_provider", ""):
        rows.append(("Debrid provider", task.debrid_provider))
    if getattr(task, "source_type", ""):
        rows.append(("Source type", task.source_type))
    if getattr(task, "torrent_name", ""):
        rows.append(("Torrent name", task.torrent_name))
    if getattr(task, "info_hash", ""):
        rows.append(("Info hash", task.info_hash))
    if getattr(task, "filename", None):
        rows.append(("File name", task.filename))
    if getattr(task, "out_dir", ""):
        rows.append(("Save folder", task.out_dir))
    if getattr(task, "backend", ""):
        rows.append(("Backend", task.backend))
    return rows
