"""FitGirl - a curated PC-games repack index published as ordinary HTML.

FitGirl has no API and no feed carrying magnets: its search page lists repack
posts, and the magnets live in the body of each post. A search therefore costs
one search request plus a bounded fan-out over the entries it found. That bound
matters - Search's deadline is logical and does not kill a worker, so a source
that walked every hit could hold a pool thread long after the search it belongs
to has been abandoned.

Everything fetched here stays on the one canonical origin below. Hrefs come out
of untrusted HTML, so a link is resolved and checked against that origin before
it is ever requested; a link that points anywhere else is dropped, not followed.
"""
from __future__ import annotations

from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

from cove.search.magnet import extract_info_hash
from cove.search.models import Category, SearchResult, SourceError, SourceErrorKind
from cove.search.sources.base import SearchHttp, Source

HOST = "fitgirl-repacks.site"
ENDPOINT = f"https://{HOST}/"

# One search page plus at most this many repack pages. Sequential requests are
# the whole cost of this source, so the fan-out is what keeps it bounded.
MAX_DETAIL_PAGES = 8

# The theme puts the outcome of a search in the body class, which is what lets
# an honest "nothing matched" be told apart from a challenge or error page.
_RESULTS_CLASS = "search-results"
_NO_RESULTS_CLASS = "search-no-results"


def _classes(attrs: dict[str, str | None]) -> set[str]:
    return set((attrs.get("class") or "").split())


def _attrs_dict(attrs) -> dict[str, str | None]:
    # Duplicate attributes keep the first value, matching browser behaviour.
    out: dict[str, str | None] = {}
    for name, value in attrs:
        out.setdefault(name.lower(), value)
    return out


def canonical_url(href: str | None) -> str | None:
    """`href` as an absolute URL on the canonical origin, or None.

    A rejected link is never fetched: this runs before the request, not after.
    Anything that is not plain HTTPS on exactly :data:`HOST` - another host, a
    look-alike host, an http downgrade, a javascript/data/file URL, or a URL
    carrying credentials - is not a repack page Cove is willing to open.
    """
    if not isinstance(href, str) or not href.strip():
        return None
    try:
        parts = urlsplit(urljoin(ENDPOINT, href.strip()))
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or hostname != HOST:
        return None
    if parts.username or parts.password:
        return None
    if port not in (None, 443):
        return None
    # The fragment is dropped so the same page is never fetched twice under two
    # spellings; the query is kept because it is part of the address.
    return urlunsplit(("https", HOST, parts.path or "/", parts.query, ""))


def parse_entry_date(value: str | None) -> int | None:
    """A `<time datetime=...>` value as a Unix timestamp, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return int(datetime.fromisoformat(value.strip()).timestamp())
    except (TypeError, ValueError, OverflowError, OSError):
        return None


class _Entry:
    __slots__ = ("url", "title", "added")

    def __init__(self, url: str, title: str, added: int | None):
        self.url = url
        self.title = title
        self.added = added


class _SearchPageParser(HTMLParser):
    """The repack entries on a search page, in document order.

    A fresh parser per page: a Source instance is shared across Search worker
    threads, so no parsing state may outlive a call.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.outcome: str | None = None
        self.entries: list[_Entry] = []
        self._article_depth = 0
        self._in_title = False
        self._href: str | None = None
        self._added: int | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = _attrs_dict(attrs)
        if tag == "body" and self.outcome is None:
            classes = _classes(attributes)
            if _NO_RESULTS_CLASS in classes:
                self.outcome = _NO_RESULTS_CLASS
            elif _RESULTS_CLASS in classes:
                self.outcome = _RESULTS_CLASS
            return
        if tag == "article":
            self._article_depth += 1
            self._href = None
            self._added = None
            return
        if not self._article_depth:
            return
        if tag == "h1" and "entry-title" in _classes(attributes):
            self._in_title = True
            self._text = []
            return
        if tag == "a" and self._in_title and self._href is None:
            self._href = attributes.get("href")
            return
        if tag == "time" and "entry-date" in _classes(attributes):
            if self._added is None:
                self._added = parse_entry_date(attributes.get("datetime"))

    def handle_endtag(self, tag):
        if tag == "h1" and self._in_title:
            self._in_title = False
            return
        if tag == "article" and self._article_depth:
            self._article_depth -= 1
            self._finish()

    def handle_data(self, data):
        if self._in_title:
            self._text.append(data)

    def _finish(self) -> None:
        url = canonical_url(self._href)
        title = " ".join("".join(self._text).split())
        if url and title:
            self.entries.append(_Entry(url, title, self._added))
        self._href = None
        self._added = None
        self._text = []


class _DetailPageParser(HTMLParser):
    """The magnets inside a repack page's content region, in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.recognised = False
        self.magnets: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        attributes = _attrs_dict(attrs)
        if tag == "div":
            if self._depth:
                self._depth += 1
            elif "entry-content" in _classes(attributes):
                self._depth = 1
                self.recognised = True
            return
        # Only the post body counts: a magnet in a sidebar, a comment or a
        # challenge page is not this repack's download.
        if tag == "a" and self._depth:
            href = attributes.get("href")
            if isinstance(href, str) and href.startswith("magnet:?"):
                self.magnets.append(href.strip())

    def handle_endtag(self, tag):
        if tag == "div" and self._depth:
            self._depth -= 1


class FitGirlSource(Source):
    id = "fitgirl"
    label = "FitGirl"
    categories = (Category.GAMES,)
    homepage = ENDPOINT
    # FitGirl publishes no seeder or leecher counts anywhere Search can see,
    # so it reports none rather than handing the aggregator invented ones.
    reports_swarm = False

    def search(
        self,
        query: str,
        category: Category,
        http: SearchHttp,
    ) -> list[SearchResult]:
        if not self.serves(category):
            return []
        entries = self._entries(http.get_bytes(ENDPOINT, {"s": query}))

        results: list[SearchResult] = []
        failure: SourceError | None = None
        for entry in entries[:MAX_DETAIL_PAGES]:
            try:
                magnets = self._magnets(http.get_bytes(entry.url))
            except SourceError as error:
                # One unreachable or broken repack page must not cost the
                # entries that did load.
                failure = failure or error
                continue
            result = self._result(entry, magnets)
            if result is not None:
                results.append(result)
        if not results and failure is not None:
            # Nothing survived and the reason was the provider, not an honest
            # absence of magnets - so the search failed rather than came up empty.
            raise failure
        return results

    def _entries(self, raw: bytes) -> list[_Entry]:
        parser = _SearchPageParser()
        parser.feed(raw.decode("utf-8", "replace"))
        parser.close()
        if parser.outcome == _NO_RESULTS_CLASS:
            return []
        if parser.outcome != _RESULTS_CLASS:
            raise SourceError(
                SourceErrorKind.PARSE, "FitGirl response is not a search page"
            )
        return parser.entries

    def _magnets(self, raw: bytes) -> list[str]:
        parser = _DetailPageParser()
        parser.feed(raw.decode("utf-8", "replace"))
        parser.close()
        if not parser.recognised:
            raise SourceError(
                SourceErrorKind.PARSE, "FitGirl page has no repack content"
            )
        return parser.magnets

    def _result(self, entry: _Entry, magnets: list[str]):
        for magnet in magnets:
            # The provider's own magnet is kept rather than rebuilt, so its
            # trackers survive; normalisation of the hash stays with the one
            # magnet helper Cove has.
            info_hash = extract_info_hash(magnet)
            if not info_hash:
                continue
            try:
                return SearchResult(
                    info_hash=info_hash,
                    name=entry.title,
                    magnet=magnet,
                    size_bytes=None,
                    seeders=0,
                    leechers=0,
                    added=entry.added,
                    source=self.id,
                )
            except ValueError:
                continue
        return None
