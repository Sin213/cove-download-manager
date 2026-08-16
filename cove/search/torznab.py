"""Torznab capability and search-feed parsing.

The protocol foundation for Search v2's generic-Torznab boundary. Torznab
servers answer a ``caps`` document describing their limits, search modes and
categories, and an RSS search response whose items carry torrent identity in a
``torznab:`` attribute namespace. This module turns those two documents into a
deterministic, bounded, Cove-normalised intermediate representation - and
nothing else.

It is deliberately pure: no network, no Qt, no settings, no filesystem writes
and no dependency on the rest of the Search subsystem beyond the normalised
:mod:`cove.search.models` and the shared hash/magnet helpers. The documents are
untrusted future network input, so every parse is bounded and hostile XML is
rejected before it can be expanded.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from xml.etree import ElementTree

from cove.search.magnet import build_magnet, extract_info_hash, normalize_info_hash
from cove.search.models import Category

# A legitimate Torznab response is small: caps is a few KB, and even a full
# search feed of a few hundred items stays well under a megabyte. These bounds
# are independent of the transport's 4 MB cap - they exist so a hostile server
# cannot make the parser itself spend pathological memory or CPU.
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 1000
MAX_FIELD_LENGTH = 4096
MAX_NUMERIC_LENGTH = 32

# The Torznab extended-attribute namespace. Elements are matched by this URI,
# never by the literal ``torznab:`` prefix, because a producer may legally bind
# the namespace to any prefix it likes.
TORZNAB_NAMESPACE = "http://torznab.com/schemas/2015/feed"
_TORZNAB_ATTR = f"{{{TORZNAB_NAMESPACE}}}attr"

# The only search modes Cove can put to use for torrents. ``audio-search`` and
# ``book-search`` are real Torznab modes but do not map to a torrent category.
_TORRENT_SEARCH_MODES = ("search", "tv-search", "movie-search")

# A simple bounded pre-parse check: any document that declares a DTD or an
# entity is rejected outright, because ElementTree *will* expand internal
# entities and there is no reason for a Torznab feed to carry either.
# The declarations are pure-ASCII tokens, so the guard must also match the
# null-interleaved encodings a UTF-16/UTF-32 document produces, otherwise a
# hostile feed could simply re-encode the same document and slip past the
# ASCII substring check.
_PROHIBITED_DECLARATIONS = ("<!DOCTYPE", "<!ENTITY")

_PROHIBITED_DECLARATION_BYTES = tuple(
    token.encode(encoding)
    for token in _PROHIBITED_DECLARATIONS
    for encoding in ("utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")
)


class TorznabParseError(ValueError):
    """A Torznab document could not be parsed into the intermediate contract."""


class TorznabIdentity(Enum):
    """How a parsed row identifies its torrent.

    These three states are deliberately distinct: a row that can only be
    downloaded as a ``.torrent`` file is not the same as a row that has no
    torrent identity at all, and neither is the same as a usable magnet.
    """

    USABLE_MAGNET_IDENTITY = "usable_magnet"
    TORRENT_DOWNLOAD_ONLY = "torrent_download_only"
    NO_USABLE_IDENTITY = "no_usable_identity"


@dataclass(frozen=True)
class TorznabCategory:
    """One category (or subcategory) a server advertises in its caps."""

    id: int
    name: str


@dataclass(frozen=True)
class TorznabCaps:
    """The smallest useful capability summary for later Search v2 slices.

    ``search_modes`` holds the torrent search modes the server advertises as
    available, in document order. ``categories`` is a flat list of every
    category and subcategory (hierarchy is not modelled - the id-family mapping
    in :func:`map_torznab_category` needs only the numeric ids).
    """

    default_limit: int | None
    max_limit: int
    search_modes: tuple[str, ...]
    categories: tuple[TorznabCategory, ...]


@dataclass(frozen=True)
class TorznabItem:
    """One torrent row, normalised enough for later slices to act on.

    ``info_hash`` is lower-case 40-character hex and ``magnet`` always carries
    that same hash, exactly as :class:`~cove.search.models.SearchResult` does.
    Both are ``None`` for the two non-magnet identity states. ``size_bytes`` is
    ``None`` when the server does not report it; ``seeders``/``leechers`` fall
    back to ``0`` on bad input, matching Cove's swarm-coercion convention.
    """

    title: str
    size_bytes: int | None
    seeders: int
    leechers: int
    info_hash: str | None
    magnet: str | None
    enclosure_url: str | None
    category_ids: tuple[int, ...]
    guid: str | None
    published_at: int | None
    identity: TorznabIdentity

    def __post_init__(self) -> None:
        if not self.title:
            raise ValueError("title must be non-empty")
        has_identity = self.info_hash is not None or self.magnet is not None
        if self.identity is TorznabIdentity.USABLE_MAGNET_IDENTITY:
            if not (self.info_hash and self.magnet):
                raise ValueError("usable magnet identity requires info_hash and magnet")
            if extract_info_hash(self.magnet) != self.info_hash:
                raise ValueError("magnet does not carry info_hash")
        elif has_identity:
            raise ValueError("only usable magnet identity may carry a hash or magnet")

        if self.identity is TorznabIdentity.TORRENT_DOWNLOAD_ONLY:
            if not self.enclosure_url:
                raise ValueError(
                    "torrent-download-only identity requires an enclosure URL"
                )
        elif self.identity is TorznabIdentity.NO_USABLE_IDENTITY:
            if self.enclosure_url is not None:
                raise ValueError(
                    "no-usable-identity may not carry an enclosure URL"
                )


def map_torznab_category(category_id: int) -> Category | None:
    """The Cove :class:`Category` for a Torznab/Newznab category id, or None.

    The id families come straight from the Newznab category tree: 1000 is
    Console, 2000 Movies, 4000 PC, 5000 TV. Anime (5070) sits inside the TV
    family but must win over it, so it is checked first.
    """
    if category_id == 5070:
        return Category.ANIME
    if 2000 <= category_id < 3000:
        return Category.MOVIES
    if 5000 <= category_id < 6000:
        return Category.TV
    if 1000 <= category_id < 2000:
        return Category.GAMES
    if 4000 <= category_id < 5000:
        return Category.GAMES
    return None


def parse_caps(raw: bytes) -> TorznabCaps:
    """A server's ``caps`` document into a :class:`TorznabCaps`.

    Raises :class:`TorznabParseError` for non-XML, a non-``caps`` root, missing
    or invalid limits, a missing or mode-less ``searching`` section, or a
    category id that cannot be parsed as an integer.
    """
    root = _parse_xml_root(raw, "caps")

    limits = _first_child(root, "limits")
    if limits is None:
        raise TorznabParseError("caps has no <limits> element")
    max_limit = _limit_value(limits, "max")
    default_limit = _limit_value(limits, "default")
    if max_limit is None:
        raise TorznabParseError("caps <limits> has no usable max")
    if default_limit is not None and default_limit > max_limit:
        raise TorznabParseError("caps default limit exceeds max limit")

    searching = _first_child(root, "searching")
    if searching is None:
        raise TorznabParseError("caps has no <searching> section")
    search_modes = _search_modes(searching)
    if not search_modes:
        raise TorznabParseError("caps advertises no usable torrent search mode")

    return TorznabCaps(
        default_limit=default_limit,
        max_limit=max_limit,
        search_modes=search_modes,
        categories=_categories(_first_child(root, "categories")),
    )


def parse_search_feed(raw: bytes) -> tuple[TorznabItem, ...]:
    """A Torznab RSS search response into intermediate :class:`TorznabItem` rows.

    Document-level failure (malformed/unsafe XML, wrong root, a missing channel,
    or more than :data:`MAX_ITEMS`) raises :class:`TorznabParseError`. A single
    malformed item is dropped without touching the other rows; duplicate rows
    are returned as separate items - de-duplication belongs to the Search
    aggregation layer, not here.
    """
    root = _parse_xml_root(raw, "rss")
    channel = _first_child(root, "channel")
    if channel is None:
        raise TorznabParseError("Torznab feed has no <channel>")
    items = [child for child in channel if _local_name(child.tag) == "item"]
    if len(items) > MAX_ITEMS:
        raise TorznabParseError(f"Torznab feed has more than {MAX_ITEMS} items")

    results: list[TorznabItem] = []
    for item in items:
        parsed = _parse_item(item)
        if parsed is not None:
            results.append(parsed)
    return tuple(results)


def _parse_xml_root(raw: bytes, expected_local: str) -> ElementTree.Element:
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise TorznabParseError(
            f"Torznab document exceeds {MAX_DOCUMENT_BYTES} bytes"
        )
    _reject_forbidden_declarations(raw)
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as error:
        raise TorznabParseError(f"not valid XML: {error}") from error
    except LookupError as error:
        # ElementTree raises LookupError (not ParseError) for an unsupported
        # declared encoding; translate it so callers keep a single deterministic
        # exception boundary.
        raise TorznabParseError(f"unsupported XML encoding: {error}") from error
    if _local_name(root.tag) != expected_local:
        raise TorznabParseError(
            f"expected <{expected_local}> root, got <{_local_name(root.tag)}>"
        )
    return root


def _reject_forbidden_declarations(raw: bytes) -> None:
    # A pre-parse substring check, deliberately simple: a Torznab document has
    # no reason to carry a DTD or entity declaration, and ElementTree would
    # expand internal entities. The check may false-positive on a comment that
    # literally spells out <!DOCTYPE or <!ENTITY, which no real feed does.
    for declaration in _PROHIBITED_DECLARATION_BYTES:
        if declaration in raw:
            raise TorznabParseError("document contains a prohibited declaration")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _first_child(parent, local: str):
    for child in parent:
        if _local_name(child.tag) == local:
            return child
    return None


def _child_text(parent, local: str) -> str | None:
    child = _first_child(parent, local)
    return child.text if child is not None else None


def _enclosure_url(item) -> str | None:
    enclosure = _first_child(item, "enclosure")
    return enclosure.attrib.get("url") if enclosure is not None else None


def _bounded_field(text: str | None) -> str | None:
    """Stripped field text, or None when empty, absent, or oversized.

    Both empty and oversized text become None; a caller that needs a required
    field treats None as "invalid item", an optional caller as "absent".
    """
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped or len(stripped) > MAX_FIELD_LENGTH:
        return None
    return stripped


def _torznab_attrs(item) -> dict[str, list[str]]:
    attrs: dict[str, list[str]] = {}
    for child in item:
        if child.tag != _TORZNAB_ATTR:
            continue
        name = child.attrib.get("name")
        value = child.attrib.get("value")
        if name is None or value is None:
            continue
        attrs.setdefault(name, []).append(value)
    return attrs


def _first(attrs: dict[str, list[str]], name: str) -> str | None:
    values = attrs.get(name)
    return values[0] if values else None


def _coerce_count(value: str | None) -> int:
    """A bounded non-negative swarm count, or 0 (Cove's "unknown" default)."""
    if value is None or len(value) > MAX_NUMERIC_LENGTH:
        return 0
    try:
        count = int(value)
    except ValueError:
        return 0
    return count if count >= 0 else 0


def _coerce_size(value: str | None) -> int | None:
    """A bounded non-negative byte size, or None when not usable.

    Unknown size must not become 0: zero and unknown are not the same thing.
    """
    if value is None or len(value) > MAX_NUMERIC_LENGTH:
        return None
    try:
        size = int(value)
    except ValueError:
        return None
    return size if size >= 0 else None


def _limit_value(limits, name: str) -> int | None:
    raw = limits.attrib.get(name)
    if raw is None:
        return None
    if len(raw) > MAX_NUMERIC_LENGTH:
        raise TorznabParseError(f"caps <limits> {name} is too long")
    try:
        value = int(raw)
    except ValueError:
        raise TorznabParseError(f"caps <limits> {name} is not an integer")
    if value < 0:
        raise TorznabParseError(f"caps <limits> {name} is negative")
    return value


def _search_modes(searching) -> tuple[str, ...]:
    modes: list[str] = []
    for child in searching:
        name = _local_name(child.tag)
        if name not in _TORRENT_SEARCH_MODES:
            continue
        available = child.attrib.get("available")
        if available is not None and available.strip().lower() == "no":
            continue
        modes.append(name)
    return tuple(modes)


def _category_id(element) -> int | None:
    raw = element.attrib.get("id")
    if raw is None or len(raw) > MAX_NUMERIC_LENGTH:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _categories(section) -> tuple[TorznabCategory, ...]:
    if section is None:
        return ()
    result: list[TorznabCategory] = []
    for category in section:
        if _local_name(category.tag) != "category":
            continue
        cid = _category_id(category)
        if cid is None:
            raise TorznabParseError("caps category has an invalid id")
        result.append(TorznabCategory(id=cid, name=_name_of(category)))
        for subcat in category:
            if _local_name(subcat.tag) != "subcat":
                continue
            sid = _category_id(subcat)
            if sid is None:
                raise TorznabParseError("caps subcat has an invalid id")
            result.append(TorznabCategory(id=sid, name=_name_of(subcat)))
    return tuple(result)


def _name_of(element) -> str:
    return (element.attrib.get("name") or "").strip()


def _category_ids(attrs: dict[str, list[str]]) -> tuple[int, ...]:
    ids: list[int] = []
    seen: set[int] = set()
    for value in attrs.get("category", []):
        if len(value) > MAX_NUMERIC_LENGTH:
            continue
        try:
            cid = int(value)
        except ValueError:
            continue
        if cid < 0 or cid in seen:
            continue
        seen.add(cid)
        ids.append(cid)
    return tuple(ids)


def _parse_pubdate(text: str | None) -> int | None:
    if not text or not text.strip():
        return None
    try:
        dt = parsedate_to_datetime(text)
        if dt is None:
            return None
        # ``-0000`` (RFC 822 "unknown local time") and timezone-less dates come
        # back naive; interpret them as UTC so the result is deterministic
        # regardless of the host machine's local timezone.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (TypeError, ValueError, OverflowError):
        return None


def _is_magnet_scheme(text: str | None) -> bool:
    # URI schemes are case-insensitive.
    return bool(text) and text.lower().startswith("magnet:")


def _extract_magnet_hash(text: str | None) -> str | None:
    if not text:
        return None
    # extract_info_hash only accepts the lowercase spelling, so normalise just
    # the scheme (the case-insensitive part) before delegating.
    if text[:8].lower() == "magnet:?":
        text = "magnet:?" + text[8:]
    return extract_info_hash(text)


def _resolve_identity(
    raw_infohash: str | None,
    raw_magnet: str | None,
    enclosure_url: str | None,
    title: str,
) -> tuple[str, str, TorznabIdentity] | None:
    """The resolved (info_hash, magnet, identity), or None on a conflict.

    A conflicting infohash/magnet pair rejects the whole item deterministically:
    neither value is "preferred", because preferring one over the other would
    silently hand Cove a hash the indexer itself disagrees about.
    """
    info_hash = normalize_info_hash(raw_infohash)
    magnet_hash = _extract_magnet_hash(raw_magnet)
    # A Torznab enclosure may itself be a magnet URI, not only a .torrent URL,
    # so a magnet-scheme enclosure is a third identity source.
    enclosure_hash = _extract_magnet_hash(enclosure_url)

    hashes = {candidate for candidate in (info_hash, magnet_hash, enclosure_hash) if candidate}
    if len(hashes) > 1:
        return None

    if hashes:
        resolved = hashes.pop()
        return resolved, build_magnet(resolved, title), TorznabIdentity.USABLE_MAGNET_IDENTITY

    # No usable BTIH. Only a genuine (non-magnet) enclosure URL is a torrent
    # download; a magnet-scheme enclosure without a usable BTIH is neither a
    # download nor a usable magnet.
    if enclosure_url and not _is_magnet_scheme(enclosure_url):
        return None, None, TorznabIdentity.TORRENT_DOWNLOAD_ONLY

    return None, None, TorznabIdentity.NO_USABLE_IDENTITY


def _parse_item(item) -> TorznabItem | None:
    title = _bounded_field(_child_text(item, "title"))
    if title is None:
        return None

    attrs = _torznab_attrs(item)
    enclosure_url = _bounded_field(_enclosure_url(item))
    raw_infohash = _bounded_field(_first(attrs, "infohash"))
    raw_magnet = _bounded_field(_first(attrs, "magneturl"))

    resolved = _resolve_identity(raw_infohash, raw_magnet, enclosure_url, title)
    if resolved is None:
        return None
    info_hash, magnet, identity = resolved
    if identity is TorznabIdentity.NO_USABLE_IDENTITY:
        # A non-download enclosure (a magnet with no usable BTIH) is not a
        # fetchable .torrent URL; drop it so the item carries no identity.
        enclosure_url = None

    return TorznabItem(
        title=title,
        size_bytes=_coerce_size(_first(attrs, "size")),
        seeders=_coerce_count(_first(attrs, "seeders")),
        leechers=_coerce_count(_first(attrs, "leechers")),
        info_hash=info_hash,
        magnet=magnet,
        enclosure_url=enclosure_url,
        category_ids=_category_ids(attrs),
        guid=_bounded_field(_child_text(item, "guid")),
        published_at=_parse_pubdate(_child_text(item, "pubDate")),
        identity=identity,
    )
