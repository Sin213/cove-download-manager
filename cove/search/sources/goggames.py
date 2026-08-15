"""GOG Games - a DRM-free GOG release index with a public JSON search.

One search is one request. The API answers with a page of catalogue entries and
each entry's bare info hash, so nothing here opens a detail page, reads a
`.torrent` or talks to a tracker. The response also carries a paginator - a
`links.next` and a `meta.last_page` in the hundreds - and every row carries a
`slug` that resolves to a release page; Cove reads none of them, because a
second page or a per-row fetch would multiply what a search costs the index for
results the cap would mostly discard anyway.

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
        payload = http.get_json(ENDPOINT, {"search": query, "page": 1})
        return self._parse(payload)

    def _parse(self, payload: Any) -> list[SearchResult]:
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

        results: list[SearchResult] = []
        for row in rows:
            result = self._row(row)
            if result is not None:
                results.append(result)
                if len(results) >= MAX_RESULTS:
                    break
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
