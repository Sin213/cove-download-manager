"""nekoBT - an anime index with a small public JSON API.

One search is one request: the API answers with the matched torrents and their
magnets already inline, so nothing here follows a link, opens a torrent or
talks to a tracker. The API also publishes a `more` flag and an `offset`
parameter; Cove reads neither, because a second page would double what a search
costs the index for results the cap would mostly discard anyway.
"""
from __future__ import annotations

from typing import Any

from cove.search.magnet import extract_info_hash
from cove.search.models import Category, SearchResult, SourceError, SourceErrorKind
from cove.search.sources.base import (
    MAX_RESULTS,
    SearchHttp,
    Source,
    coerce_count,
    coerce_size,
    coerce_timestamp,
)

ENDPOINT = "https://nekobt.to/api/v1/torrents/search"


def parse_uploaded_at(value: Any) -> int | None:
    """The API's `uploaded_at` as a Unix timestamp in seconds, or None.

    The field is epoch *milliseconds*, so it has to be divided down before it
    can mean anything as a `SearchResult.added`. A torrent is still worth
    showing when its upload date is not usable.
    """
    stamp = coerce_timestamp(value)
    if stamp is None:
        return None
    return (stamp // 1000) or None


class NekoBtSource(Source):
    id = "nekobt"
    label = "nekoBT"
    categories = (Category.ANIME,)
    homepage = "https://nekobt.to"
    # Every row carries its own seeder and leecher counts.
    reports_swarm = True

    def search(
        self,
        query: str,
        category: Category,
        http: SearchHttp,
    ) -> list[SearchResult]:
        if not self.serves(category):
            return []
        payload = http.get_json(ENDPOINT, {"query": query})
        return self._parse(payload)

    def _parse(self, payload: Any) -> list[SearchResult]:
        # A search that matched nothing still answers with the full envelope
        # and an empty `results` array, so the envelope is not optional: a
        # payload missing it is schema drift, and reporting that as "no
        # matches" would hide the break behind a normal-looking empty search.
        if not isinstance(payload, dict):
            raise SourceError(SourceErrorKind.PARSE, "nekoBT response is not an object")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise SourceError(SourceErrorKind.PARSE, "nekoBT response has no data object")
        rows = data.get("results")
        if not isinstance(rows, list):
            raise SourceError(SourceErrorKind.PARSE, "nekoBT result list is not a list")

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
        magnet = row.get("magnet")
        # The API hands out a complete magnet - trackers, display name and
        # all - so it is kept verbatim and only its hash is normalised. The
        # row's own `infohash` field is not consulted: deriving the identity
        # from the magnet Cove will actually hand to the torrent path is what
        # keeps the two from ever disagreeing.
        info_hash = extract_info_hash(magnet)
        if not info_hash:
            return None
        name = str(row.get("title") or "").strip()
        if not name:
            return None
        try:
            return SearchResult(
                info_hash=info_hash,
                name=name,
                magnet=magnet,
                # A decimal string of bytes, or null when the index does not
                # state one.
                size_bytes=coerce_size(row.get("filesize")),
                seeders=coerce_count(row.get("seeders")),
                leechers=coerce_count(row.get("leechers")),
                added=parse_uploaded_at(row.get("uploaded_at")),
                source=self.id,
            )
        except ValueError:
            return None
