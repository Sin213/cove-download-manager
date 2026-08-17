"""GOG Games - a DRM-free GOG release index with a public JSON search.

A search walks the advertised result pages, at most `MAX_PAGES` of them and at
most `MAX_RAW_ITEMS` raw rows in total. Each page answers with catalogue entries
and each entry's bare info hash, so nothing here opens a detail page, reads a
`.torrent` or talks to a tracker. Every row also carries a `slug` that resolves
to a release page; that is never followed either - pagination is a further list
request built from the same endpoint, not a fan-out into detail pages.

The paginator advertises `links.next` and a `meta.last_page` in the hundreds,
but the deepest pages would cost a broad search most of its request budget for
rows a real search would not reach. `links.next` is used only as an availability
signal; the next request is always the trusted endpoint with the next page
number, so a malformed or foreign `links.next` can never turn into a request.

The filtering parameter is `search`. `query` is accepted and silently ignored,
and the paginator echoes `query` back in the URLs it builds, so sending it
returns the whole catalogue in reverse-alphabetical order while still looking
like a successful search. This is a specialised index of GOG releases, not a
general games index.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from cove.search.magnet import build_magnet, normalize_info_hash
from cove.search.models import Category, SearchResult, SourceError, SourceErrorKind
from cove.search.sources.base import MAX_RESULTS, SearchHttp, Source

ENDPOINT = "https://gog-games.to/search"

# Depth policy: a single search costs at most three list requests and inspects
# at most 200 raw rows, including rows that turn out to be unusable. The API
# advertises hundreds of pages; these fixed budgets keep a broad query from
# becoming an unbounded crawl and fit the existing search deadline unchanged.
# A raw item is a provider row before SearchResult conversion; MAX_RAW_ITEMS is
# a retrieval budget, while MAX_RESULTS caps how many normalised results a
# source may contribute - they coincide by value but answer different questions.
MAX_PAGES = 3
MAX_RAW_ITEMS = 200


def parse_last_update(value: Any) -> int | None:
    """The API's `last_update` as a Unix timestamp, or None.

    The field is an ISO 8601 instant with a `Z` offset, which only parses
    directly on Python 3.11 and newer, so the suffix is spelled out first. A
    release is still worth showing when its update date is not usable.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        stamp = int(moment.timestamp())
    except (OverflowError, OSError, ValueError):
        return None
    return stamp if stamp > 0 else None


class GogGamesSource(Source):
    id = "goggames"
    label = "GOG Games"
    categories = (Category.GAMES,)
    homepage = "https://gog-games.to"
    # The API publishes no seeder or leecher counts anywhere in a row.
    reports_swarm = False

    def search(
        self,
        query: str,
        category: Category,
        http: SearchHttp,
    ) -> list[SearchResult]:
        if not self.serves(category):
            return []
        results: list[SearchResult] = []
        seen: list[tuple[str, str]] = []
        raw_items = 0
        # A fixed range, not a follow-until-exhaustion loop: page 1, then page 2
        # and page 3 if the paginator advertises them, then stop. No fourth
        # page exists in this slice.
        for page in range(1, MAX_PAGES + 1):
            try:
                payload = http.get_json(ENDPOINT, {"search": query, "page": page})
                rows = self._rows(payload)
            except SourceError:
                # Page 1 has always been mandatory: the search cannot stand
                # without it, so its failures keep their pre-pagination
                # semantics. A later page is optional enrichment, so an
                # expected source error there (network, timeout, HTTP or a
                # parse violation) keeps the pages already parsed instead of
                # throwing away a valid page 1 because page 2 failed.
                if page == 1:
                    raise
                break

            # The raw budget counts every provider row examined, usable or not,
            # and it is capped before conversion: a page that would overshoot
            # it contributes only the remaining allowance and no later page is
            # requested.
            take = min(len(rows), MAX_RAW_ITEMS - raw_items)
            page_results = self._parse_rows(rows[:take])

            # A provider that serves the same page twice is not making
            # progress. Stop before appending the repeat, and do not ask for
            # the page after it.
            signature = tuple((r.info_hash, r.name) for r in page_results)
            if page > 1 and signature in seen:
                break
            seen.append(signature)

            raw_items += take
            results.extend(page_results)
            if len(results) >= MAX_RESULTS or raw_items >= MAX_RAW_ITEMS:
                break
            if not self._advertises_more(payload, page):
                break
        return results

    def _advertises_more(self, payload: Any, page: int) -> bool:
        """Whether the committed paginator agrees another page exists.

        Both signals must line up: `meta.last_page` must be a sensible number
        past the current page and `links.next` must be a non-empty string.
        Contradictory, missing or malformed metadata stops pagination
        conservatively while the rows already parsed still stand. `links.next`
        is evidence only; it is never used as a request URL.
        """
        meta = payload.get("meta")
        links = payload.get("links")
        if not isinstance(meta, dict) or not isinstance(links, dict):
            return False
        last_page = meta.get("last_page")
        if not isinstance(last_page, int) or isinstance(last_page, bool):
            return False
        if page >= last_page:
            return False
        next_link = links.get("next")
        return isinstance(next_link, str) and bool(next_link)

    def _rows(self, payload: Any) -> list[Any]:
        # A search that matched nothing still answers with the full envelope
        # and an empty `data` array, so the envelope is not optional: a payload
        # missing it is schema drift, and reporting that as "no matches" would
        # hide the break behind a normal-looking empty search.
        if not isinstance(payload, dict):
            raise SourceError(
                SourceErrorKind.PARSE, "GOG Games response is not an object"
            )
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise SourceError(
                SourceErrorKind.PARSE, "GOG Games result list is not a list"
            )
        return rows

    def _parse_rows(self, rows: list[Any]) -> list[SearchResult]:
        results: list[SearchResult] = []
        for row in rows:
            result = self._row(row)
            if result is not None:
                results.append(result)
        return results

    def _row(self, row: Any):
        if not isinstance(row, dict):
            return None
        # A catalogue entry with no release behind it yet carries a null hash,
        # which is the ordinary case rather than a broken row. Either way there
        # is no torrent to hand to Cove, so the entry is not a search result.
        info_hash = normalize_info_hash(row.get("infohash"))
        if not info_hash:
            return None
        name = str(row.get("title") or "").strip()
        if not name:
            return None
        try:
            return SearchResult(
                info_hash=info_hash,
                name=name,
                # No magnet is published, so Cove builds one from the hash it
                # just validated - the same way it does for every other
                # hash-only source, with Cove's own fixed tracker list.
                magnet=build_magnet(info_hash, name),
                # The index states neither, and neither is worth a second
                # request. None and reports_swarm=False keep "not reported"
                # distinguishable from a real zero.
                size_bytes=None,
                seeders=0,
                leechers=0,
                # `last_update` is when the release itself last changed.
                # `release_timestamp` is the game's original store date, which
                # would date a recent repack by a decade-old release, so it is
                # not read - not even as a fallback.
                added=parse_last_update(row.get("last_update")),
                source=self.id,
            )
        except ValueError:
            return None
