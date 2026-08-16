"""A generic Torznab search source for one user-configured indexer.

Search v2 slices S3 and S4. One :class:`TorznabSource` is built from one
:class:`~cove.search.indexers.CustomTorznabIndexer` record and reuses Cove's
existing :class:`~cove.search.sources.base.SearchHttp` transport and the S1
protocol parser. It performs bounded caps discovery plus bounded ``offset`` /
``limit`` paging, and returns ordinary Cove :class:`SearchResult` rows.

Before any request the configured endpoint passes through the S4 custom-endpoint
network-security policy: literal local/private destinations run over ordinary
direct transport (no interface binding, no environment proxy), while
public/unresolved destinations require HTTPS and keep the caller-selected
interface. Local routing privilege never makes endpoint responses trusted.

It is still deliberately disconnected from the rest of Search: nothing here
registers the source, reads Settings globally, or touches SearchService,
registry or UI.
"""
from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit, urlunsplit
from xml.etree import ElementTree

from cove.search.custom_endpoint import resolve_custom_torznab_transport
from cove.search.indexers import CustomTorznabIndexer
from cove.search.models import Category, SearchResult, SourceError, SourceErrorKind
from cove.search.sources.base import MAX_RESULTS, SearchHttp, Source
from cove.search.torznab import (
    TorznabIdentity,
    TorznabParseError,
    map_torznab_category,
    parse_caps,
    parse_search_feed,
)

# One direct search performs at most one caps request plus this many search-page
# requests. Not configurable: there is no Settings field and no UI for it.
MAX_PAGES = 3

# The per-source ceiling on parsed Torznab feed items. Distinct from the number
# of usable SearchResult rows: unusable/download-only rows still consume
# upstream response and parser work, so they count against the budget too.
RAW_ITEM_BUDGET = MAX_RESULTS

# Fallback page size when the endpoint advertises no usable default limit.
DEFAULT_PAGE_LIMIT = 50

# The Cove-owned request parameters this source writes. Any same-named parameter
# already present in the configured endpoint query is replaced, never
# duplicated.
_RESERVED_PARAMS = ("t", "q", "cat", "limit", "offset", "apikey")

# Torznab capability names are not request tokens: the caps ``<searching>``
# section spells modes as ``tv-search`` / ``movie-search`` / ``search`` while
# the request uses ``t=tvsearch`` / ``t=movie`` / ``t=search``.
_CAPS_MODE_TO_REQUEST_TOKEN = {
    "search": "search",
    "tv-search": "tvsearch",
    "movie-search": "movie",
}

# The pre-parse guard for the source's own re-read of the feed. The S1 parser
# rejects any DTD/entity declaration before parsing because ElementTree expands
# internal entities, but the source must inspect the feed (raw item count and
# page-size truncation) *before* handing it to that parser. The same guard is
# therefore applied here first, over the same encodings, so a hostile feed can
# never make the re-parse expand an entity the parser would have rejected.
_PROHIBITED_DECLARATION_BYTES = tuple(
    token.encode(encoding)
    for token in ("<!DOCTYPE", "<!ENTITY")
    for encoding in ("utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")
)


class TorznabSource(Source):
    """A single custom Torznab indexer, normalised into the Source contract.

    Construction is cheap and side-effect free: it stores the record and splits
    its endpoint URL, and performs no network, DNS, persistence or registry
    work. Capability discovery happens per ``search`` call - there is no caps
    cache, because a future slice may need fresh caps for a Test Connection
    flow, and a stale cache is worse than a repeat request.
    """

    def __init__(self, indexer: CustomTorznabIndexer):
        self.indexer = indexer
        # The persisted id is authoritative; name/URL/key never substitute for it.
        self.id = indexer.id
        self.label = indexer.name
        # Snapshot the configured URL exactly once: the security policy, the
        # displayed homepage and every request must act on the same immutable
        # endpoint, not a mutable field that could change after construction.
        self._endpoint = indexer.url
        self.homepage = self._endpoint
        # Real capability is only known after caps discovery, so this source
        # claims broad eligibility up front and then rejects unsupported
        # categories locally after discovery. It is not registered globally
        # yet, so this broad claim has no global effect.
        self.categories = (Category.MOVIES, Category.TV, Category.ANIME, Category.GAMES)
        self.reports_swarm = True
        parts = urlsplit(self._endpoint)
        self._base_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        self._preserved_params = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key not in _RESERVED_PARAMS
        ]

    def search(
        self,
        query: str,
        category: Category,
        http: SearchHttp,
    ) -> list[SearchResult]:
        """Normalised results, or [] when the endpoint cannot serve the request.

        Raises :class:`SourceError` on network, timeout, HTTP or parse failure,
        and on an endpoint the custom-endpoint security policy rejects before
        transport.
        """
        policy = resolve_custom_torznab_transport(self._endpoint, http.interface)
        if not policy.allowed:
            raise SourceError(SourceErrorKind.PARSE, policy.reason)
        http.apply_routing(
            policy.effective_interface if policy.effective_interface is not None else "",
            suppress_env_proxy=policy.suppress_env_proxy,
        )
        caps = self._discover_caps(http)
        token = self._select_search_token(caps, category)
        if token is None:
            return []
        category_ids = self._select_category_ids(caps, category)
        if category is not Category.ALL and not category_ids:
            return []
        return self._paginate(http, query, token, category_ids, self._page_limit(caps))

    def _discover_caps(self, http: SearchHttp):
        raw = self._get_bytes(http, [("t", "caps")])
        try:
            return parse_caps(raw)
        except TorznabParseError as error:
            raise SourceError(SourceErrorKind.PARSE, f"Torznab caps is not usable: {error}") from error

    def _select_search_token(self, caps, category: Category) -> str | None:
        modes = caps.search_modes
        if category is Category.TV:
            if "tv-search" in modes:
                return _CAPS_MODE_TO_REQUEST_TOKEN["tv-search"]
            if "search" in modes:
                return _CAPS_MODE_TO_REQUEST_TOKEN["search"]
            return None
        if category is Category.MOVIES:
            if "movie-search" in modes:
                return _CAPS_MODE_TO_REQUEST_TOKEN["movie-search"]
            if "search" in modes:
                return _CAPS_MODE_TO_REQUEST_TOKEN["search"]
            return None
        # ANIME, GAMES and ALL all require generic search.
        if "search" in modes:
            return _CAPS_MODE_TO_REQUEST_TOKEN["search"]
        return None

    def _select_category_ids(self, caps, category: Category) -> tuple[int, ...]:
        if category is Category.ALL:
            return ()
        # Deterministic: caps document order, filtered by the S1 mapping. The
        # mapping already keeps Anime (5070) out of the TV family.
        return tuple(
            cat.id for cat in caps.categories if map_torznab_category(cat.id) is category
        )

    def _page_limit(self, caps) -> int:
        default = caps.default_limit
        limit = default if (default is not None and default > 0) else DEFAULT_PAGE_LIMIT
        if caps.max_limit > 0:
            limit = min(limit, caps.max_limit)
        limit = min(limit, MAX_RESULTS)
        return max(1, limit)

    def _paginate(
        self,
        http: SearchHttp,
        query: str,
        token: str,
        category_ids: tuple[int, ...],
        page_limit: int,
    ) -> list[SearchResult]:
        results: list[SearchResult] = []
        processed_raw = 0
        offset = 0
        previous_signature = None
        for _ in range(MAX_PAGES):
            remaining = RAW_ITEM_BUDGET - processed_raw
            if remaining <= 0:
                break
            limit = min(page_limit, remaining)
            items, raw_count = self._search_page(http, query, token, category_ids, limit, offset)
            if items:
                # Repeated-page detection only over real rows: two consecutive
                # fully-malformed pages both yield an empty tuple and must NOT
                # read as a repeated page, or valid later rows would be dropped.
                signature = _page_signature(items)
                if signature == previous_signature:
                    break
                previous_signature = signature
            for feed_item in items[:remaining]:
                result = self._convert(feed_item)
                if result is not None:
                    results.append(result)
            # Offset advances by the RAW feed item count, never by usable output
            # rows: a filtered or malformed item still consumed its upstream
            # slot, so the parsed tuple length under-counts the page.
            processed_raw += raw_count
            offset += raw_count
            if raw_count < limit:
                break
        return results

    def _search_page(
        self,
        http: SearchHttp,
        query: str,
        token: str,
        category_ids: tuple[int, ...],
        limit: int,
        offset: int,
    ):
        reserved: list[tuple[str, str]] = [("t", token), ("q", query)]
        if category_ids:
            reserved.append(("cat", ",".join(str(cid) for cid in category_ids)))
        reserved.append(("limit", str(limit)))
        reserved.append(("offset", str(offset)))
        raw = self._get_bytes(http, reserved)
        raw_count = _count_feed_items(raw)
        if raw_count > limit:
            # The endpoint returned more rows than we asked for. ``limit`` is
            # advisory on the wire; bound the parser to the page we actually
            # requested so a noncompliant endpoint cannot inflate parsing work
            # past the source's stated per-page ceiling.
            raw = _truncate_feed(raw, limit)
        try:
            items = parse_search_feed(raw)
        except TorznabParseError as error:
            raise SourceError(SourceErrorKind.PARSE, f"Torznab feed is not usable: {error}") from error
        return items, raw_count

    def _convert(self, feed_item) -> SearchResult | None:
        if feed_item.identity is not TorznabIdentity.USABLE_MAGNET_IDENTITY:
            return None
        try:
            return SearchResult(
                info_hash=feed_item.info_hash,
                name=feed_item.title,
                magnet=feed_item.magnet,
                size_bytes=feed_item.size_bytes,
                seeders=feed_item.seeders,
                leechers=feed_item.leechers,
                added=feed_item.published_at,
                source=self.id,
            )
        except ValueError:
            return None

    def _get_bytes(self, http: SearchHttp, reserved: list[tuple[str, str]]) -> bytes:
        params = list(self._preserved_params)
        params.extend(reserved)
        if self.indexer.api_key:
            params.append(("apikey", self.indexer.api_key))
        try:
            return http.get_bytes(self._base_url, params)
        except SourceError as error:
            raise self._sanitize(error) from None

    def _sanitize(self, error: SourceError) -> SourceError:
        """A transport failure without the secret-bearing URL.

        ``requests`` surfaces the full prepared URL - query string included - in
        its error text, and that URL carries the API key. The key must never
        escape through a failure, so every transport error is re-raised with a
        clean message and the same kind.
        """
        if error.kind is SourceErrorKind.HTTP:
            text = str(error)
            code = text.split(None, 1)[0] if text else ""
            if code in ("401", "403"):
                return SourceError(SourceErrorKind.HTTP, "Torznab authentication failed")
            return SourceError(SourceErrorKind.HTTP, "Torznab request failed")
        return SourceError(error.kind, "Torznab request failed")


def _page_signature(items) -> tuple:
    """A small deterministic window signature for repeated-page detection.

    Identical pages (an endpoint ignoring ``offset``) collapse to the same
    signature so the loop stops. This is loop protection only, not dedupe: two
    legitimately distinct pages may still share individual rows.
    """
    return tuple((item.info_hash, item.guid, item.title) for item in items)


def _parse_raw_feed(raw: bytes):
    """Parse a raw feed the source may inspect before ``parse_search_feed``.

    Returns the root, or ``None`` when the document is malformed or carries a
    prohibited DTD/entity declaration. ``parse_search_feed`` runs the same guard
    and rejects both, so this re-parse must not expand an entity first.
    """
    for declaration in _PROHIBITED_DECLARATION_BYTES:
        if declaration in raw:
            return None
    try:
        return ElementTree.fromstring(raw)
    except (ElementTree.ParseError, LookupError):
        return None


def _feed_channel(root):
    """The ``<channel>`` child of a parsed RSS root, or ``None``."""
    for child in root:
        if _local_name(child.tag) == "channel":
            return child
    return None


def _count_feed_items(raw: bytes) -> int:
    """The number of ``<item>`` elements in a raw Torznab feed.

    ``parse_search_feed`` drops malformed items, so its returned tuple length
    under-counts the upstream page. Pagination must advance by the server's own
    slot count (offset, short-page detection, raw-item budget), not by the
    successfully parsed rows. Runs before ``parse_search_feed``, so it stays
    silent on malformed/unsafe input and lets that parser raise the
    authoritative ``TorznabParseError``.
    """
    root = _parse_raw_feed(raw)
    if root is None:
        return 0
    channel = _feed_channel(root)
    if channel is None:
        return 0
    return sum(1 for item in channel if _local_name(item.tag) == "item")


def _truncate_feed(raw: bytes, limit: int) -> bytes:
    """A copy of ``raw`` holding at most ``limit`` ``<item>`` elements.

    ``limit`` on the wire is advisory: a noncompliant endpoint can return more
    rows than requested. The parser must never see more than the page the source
    asked for, so surplus items are dropped before parsing. The returned bytes
    keep the original root and namespace so ``parse_search_feed`` accepts them.
    """
    root = _parse_raw_feed(raw)
    if root is None:
        return raw
    channel = _feed_channel(root)
    if channel is None:
        return raw
    items = [child for child in channel if _local_name(child.tag) == "item"]
    for extra in items[limit:]:
        channel.remove(extra)
    return ElementTree.tostring(root, encoding="utf-8")


def _local_name(tag: str) -> str:
    """The element name without any namespace prefix, mirroring the parser."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag
