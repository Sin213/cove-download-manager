"""Info-hash normalisation and magnet URIs.

Indexers disagree about how to spell a BitTorrent info hash - upper-case hex,
lower-case hex, base32 - and Cove needs exactly one representation so that
results from different sources can be compared and de-duplicated. Everything
here is total: malformed remote input returns None rather than raising, so a
single bad row can never take down a whole search.
"""
from __future__ import annotations

import base64
import binascii
from urllib.parse import parse_qs, quote_plus, urlparse

# Public trackers attached to every magnet Cove synthesises. Most sources hand
# out a bare info hash, so without these the torrent would have to rely on DHT
# alone. Fixed data, in a fixed order: nothing here is fetched at runtime, and
# tests must never need one of these hosts to be reachable.
TRACKERS: tuple[str, ...] = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
)

_ZERO_HASH = "0" * 40


def normalize_info_hash(value: str | None) -> str | None:
    """`value` as lower-case 40-character hex, or None if it is not a hash.

    Accepts the two shapes indexers actually use: 40-character hex and
    32-character base32. The all-zero hash is rejected as well - it is a
    placeholder some APIs return for "no torrent", never a real swarm.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) == 40:
        try:
            binascii.unhexlify(text)
        except (binascii.Error, ValueError):
            return None
        lowered = text.lower()
        return None if lowered == _ZERO_HASH else lowered
    if len(text) == 32:
        try:
            raw = base64.b32decode(text.upper())
        except (binascii.Error, ValueError):
            return None
        if len(raw) != 20:
            return None
        lowered = raw.hex()
        return None if lowered == _ZERO_HASH else lowered
    return None


def extract_info_hash(magnet: str | None) -> str | None:
    """The normalised BTIH carried by `magnet`, or None.

    Parameter order is not assumed: a magnet may list any number of trackers
    before or after its ``xt``, and some sources percent-encode the whole
    ``urn:btih:`` prefix.
    """
    if not isinstance(magnet, str) or not magnet.startswith("magnet:?"):
        return None
    try:
        query = urlparse(magnet).query
    except ValueError:
        return None
    for value in parse_qs(query).get("xt", []):
        lowered = value.lower()
        if lowered.startswith("urn:btih:"):
            normalized = normalize_info_hash(value[len("urn:btih:") :])
            if normalized:
                return normalized
    return None


def build_magnet(
    info_hash: str,
    name: str,
    trackers: tuple[str, ...] = TRACKERS,
) -> str:
    """A magnet URI for `info_hash`, named `name`.

    Deterministic by construction - same inputs, same string - so results can
    be compared and cached without normalising the URI again.
    """
    normalized = normalize_info_hash(info_hash)
    if not normalized:
        raise ValueError(f"not a usable info hash: {info_hash!r}")
    parts = [f"magnet:?xt=urn:btih:{normalized}"]
    if name:
        parts.append(f"dn={quote_plus(name)}")
    parts.extend(f"tr={quote_plus(tracker)}" for tracker in trackers)
    return "&".join(parts)
