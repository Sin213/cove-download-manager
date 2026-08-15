"""Rutor - a general-purpose Russian torrent index published as ordinary HTML.

Rutor has no API, but it does not need one here: the search page itself lists a
complete magnet for every hit, alongside the size, the swarm counts and the
date. A search is therefore exactly one request - nothing in here opens a
torrent page, follows the paginator, reads a `.torrent` or talks to a tracker.

The query travels as a path segment rather than a query parameter, which is why
it is escaped explicitly here: `/search/<page>/<category>/<method><in>0/<sort>/
<query>`. Cove sends page 0, category 0 (any), the default search method and
the default sort, which is the same address the site's own search form builds.

Everything is read from the one canonical origin below. There is no mirror
list: SearchHttp does not follow redirects, so a host that moved is a failure
Cove reports rather than a domain it guesses at.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import quote

from cove.search.magnet import extract_info_hash
from cove.search.models import Category, SearchResult, SourceError, SourceErrorKind
from cove.search.sources.base import MAX_RESULTS, SearchHttp, Source, coerce_count

ENDPOINT = "https://rutor.info"

# The classes Rutor puts on the results table: one header row and then the
# alternating result rows.
_HEADER_ROW = "backgr"
_RESULT_ROWS = ("gai", "tum")

# The header row's own labels. Zero result rows is not by itself an empty
# search - an error page, a challenge or a redesign parses to zero rows too -
# so the header is the positive signal that this really is a Rutor search page
# with nothing under it.
_HEADER_LABELS = frozenset(("Добавлен", "Название", "Размер", "Пиры"))

# Rutor labels its sizes GB/MB while dividing by 1024, the way torrent indexes
# of its generation do.
_SIZE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?B)$", re.IGNORECASE)
_SIZE_UNITS = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4}

# "09 Июл 26": a day, an abbreviated Russian month and a two-digit year.
_DATE = re.compile(r"^([0-9]{1,2})\s+([^\W\d_]+)\s+([0-9]{2})$", re.UNICODE)
_MONTHS = {
    "янв": 1,
    "фев": 2,
    "мар": 3,
    "апр": 4,
    "май": 5,
    "июн": 6,
    "июл": 7,
    "авг": 8,
    "сен": 9,
    "окт": 10,
    "ноя": 11,
    "дек": 12,
}


def parse_size(text: str) -> int | None:
    """Rutor's "265.94 GB" into bytes, or None when it is not a size."""
    match = _SIZE.match(text.strip())
    if not match:
        return None
    try:
        return int(float(match.group(1)) * _SIZE_UNITS[match.group(2).lower()])
    except (ValueError, OverflowError):
        # A digit string long enough to overflow a float is not a size.
        return None


def parse_added(text: str) -> int | None:
    """Rutor's "09 Июл 26" as a Unix timestamp, or None.

    The page states a day and nothing finer - no time and no offset - so the
    day is read at UTC midnight rather than dressed up with an hour Rutor never
    published. The year is two digits on a site that has only ever run this
    century, so it is read as 20YY.
    """
    match = _DATE.match(text.strip())
    if not match:
        return None
    month = _MONTHS.get(match.group(2).lower()[:3])
    if month is None:
        return None
    try:
        moment = datetime(
            2000 + int(match.group(3)),
            month,
            int(match.group(1)),
            tzinfo=timezone.utc,
        )
        stamp = int(moment.timestamp())
    except (ValueError, OverflowError, OSError):
        return None
    return stamp if stamp > 0 else None


def _classes(attrs: dict[str, str | None]) -> set[str]:
    return set((attrs.get("class") or "").split())


def _attrs_dict(attrs) -> dict[str, str | None]:
    # Duplicate attributes keep the first value, matching browser behaviour.
    out: dict[str, str | None] = {}
    for name, value in attrs:
        out.setdefault(name.lower(), value)
    return out


def _squeeze(parts: list[str]) -> str:
    # Rutor separates the fields inside a cell with &nbsp;, which str.split
    # treats as whitespace like any other.
    return " ".join("".join(parts).split())


class _Cell:
    __slots__ = ("text", "magnet", "seeders", "leechers")

    def __init__(self) -> None:
        self.text: list[str] = []
        self.magnet: str | None = None
        self.seeders: list[str] | None = None
        self.leechers: list[str] | None = None

    @property
    def has_swarm(self) -> bool:
        return self.seeders is not None or self.leechers is not None


class _SearchPageParser(HTMLParser):
    """The result rows on a Rutor search page, in document order.

    A fresh parser per page: a Source instance is shared across Search worker
    threads, so no parsing state may outlive a call.

    Rows are read by what their cells contain rather than by cell number,
    because the number is not fixed: a row with comments has five cells and a
    row without has four, the title cell absorbing the difference with a
    colspan. The comment count sits in a cell shaped exactly like the size one,
    so position alone would mistake one for the other.

    Only the table the header row belongs to is read. `gai` and `tum` are
    generic alternating-row classes rather than result markers, and the page
    renders other tables, so a row from one of those is not a search result no
    matter how much it looks like one.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.header_seen = False
        self.rows: list[list[_Cell]] = []
        self._row: list[_Cell] | None = None
        self._header: list[str] | None = None
        self._cell: _Cell | None = None
        self._span: list[str] | None = None
        self._tables: list[int] = []
        self._next_table = 0
        self._results_table: int | None = None

    def handle_starttag(self, tag, attrs):
        attributes = _attrs_dict(attrs)
        if tag == "table":
            self._finish_row()
            self._next_table += 1
            self._tables.append(self._next_table)
            return
        if tag == "tr":
            # A row that never closed must not swallow the next one.
            self._finish_row()
            classes = _classes(attributes)
            if _HEADER_ROW in classes:
                self._header = []
            elif classes.intersection(_RESULT_ROWS) and self._in_results_table():
                self._row = []
            return
        if tag == "td" and (self._row is not None or self._header is not None):
            self._close_cell()
            self._cell = _Cell()
            return
        if self._cell is None:
            return
        if tag == "a":
            href = attributes.get("href")
            # The row also links a `.torrent` and its own torrent page. Neither
            # is fetched, and neither is an identity: the magnet is.
            if (
                self._cell.magnet is None
                and isinstance(href, str)
                and href.strip().startswith("magnet:?")
            ):
                self._cell.magnet = href.strip()
            return
        if tag == "span":
            classes = _classes(attributes)
            if "green" in classes and self._cell.seeders is None:
                self._cell.seeders = self._span = []
            elif "red" in classes and self._cell.leechers is None:
                self._cell.leechers = self._span = []

    def handle_endtag(self, tag):
        if tag == "span" and self._span is not None:
            self._span = None
            return
        if tag == "td":
            self._close_cell()
            return
        if tag == "tr":
            self._finish_row()
            return
        if tag == "table":
            self._finish_row()
            if self._tables:
                self._tables.pop()

    def handle_data(self, data):
        if self._cell is None:
            return
        self._cell.text.append(data)
        if self._span is not None:
            self._span.append(data)

    def close(self):
        super().close()
        self._finish_row()

    def _in_results_table(self) -> bool:
        return (
            self._results_table is not None
            and bool(self._tables)
            and self._tables[-1] == self._results_table
        )

    def _close_cell(self) -> None:
        if self._cell is None:
            return
        if self._header is not None:
            self._header.append(_squeeze(self._cell.text))
        elif self._row is not None:
            self._row.append(self._cell)
        self._cell = None
        self._span = None

    def _finish_row(self) -> None:
        self._close_cell()
        if self._header is not None:
            if _HEADER_LABELS.issubset(self._header) and self._tables:
                self.header_seen = True
                # The first validated header wins: a later one cannot move the
                # results table out from under the rows already collected.
                if self._results_table is None:
                    self._results_table = self._tables[-1]
            self._header = None
        if self._row:
            self.rows.append(self._row)
        self._row = None


class RutorSource(Source):
    id = "rutor"
    label = "Rutor"
    categories = (Category.MOVIES, Category.TV)
    homepage = ENDPOINT
    # Every row publishes a seeder and a leecher count, so the aggregator gets
    # real swarm data rather than an invented zero.
    reports_swarm = True

    def search(
        self,
        query: str,
        category: Category,
        http: SearchHttp,
    ) -> list[SearchResult]:
        if not self.serves(category):
            return []
        # The query is a path segment, so every character that would otherwise
        # change the shape of the path - a space, a slash, a separator, a
        # percent - is escaped here, once. requests leaves an already-escaped
        # path alone, so nothing re-encodes this afterwards.
        url = f"{ENDPOINT}/search/0/0/000/0/{quote(query, safe='')}"
        return self._parse(http.get_bytes(url))

    def _parse(self, raw: bytes) -> list[SearchResult]:
        parser = _SearchPageParser()
        parser.feed(raw.decode("utf-8", "replace"))
        parser.close()
        if not parser.header_seen:
            # No results header means this is not a Rutor search page, and
            # reporting that as "no matches" would hide a redesign, an error
            # page or a challenge behind a normal-looking empty search.
            raise SourceError(
                SourceErrorKind.PARSE, "Rutor response is not a search page"
            )

        results: list[SearchResult] = []
        for row in parser.rows:
            result = self._row(row)
            if result is not None:
                results.append(result)
                if len(results) >= MAX_RESULTS:
                    break
        return results

    def _row(self, cells: list[_Cell]):
        magnet = next((cell.magnet for cell in cells if cell.magnet), None)
        if not magnet:
            return None
        # The magnet Rutor published is the one Cove hands on, trackers and
        # all, so the hash comes out of it rather than being derived twice.
        info_hash = extract_info_hash(magnet)
        if not info_hash:
            return None
        name = next(
            (_squeeze(cell.text) for cell in cells if cell.magnet == magnet), ""
        )
        if not name:
            return None

        swarm = next((cell for cell in reversed(cells) if cell.has_swarm), None)
        seeders = coerce_count(_squeeze(swarm.seeders or [])) if swarm else 0
        leechers = coerce_count(_squeeze(swarm.leechers or [])) if swarm else 0

        # The size sits in the cell just before the swarm. Which cell that is
        # depends on whether the row has a comment count, so it is located
        # relative to the swarm rather than by a fixed index.
        size_bytes = None
        if swarm is not None:
            index = cells.index(swarm) - 1
            if index >= 0 and cells[index].magnet is None:
                size_bytes = parse_size(_squeeze(cells[index].text))

        try:
            return SearchResult(
                info_hash=info_hash,
                name=name,
                magnet=magnet,
                size_bytes=size_bytes,
                seeders=seeders,
                leechers=leechers,
                added=parse_added(_squeeze(cells[0].text)) if cells else None,
                source=self.id,
            )
        except ValueError:
            return None
