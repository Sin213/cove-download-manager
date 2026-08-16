"""Torrents-CSV - a public torrent index published as a plain JSON search.

One search is one request. The endpoint answers with a page of rows carrying a
bare info hash, so nothing here opens a detail page, reads a `.torrent` or
talks to a tracker. The index is collaborative and English-titled, it holds
films and television side by side without a category field, and it caps a
response at 25 rows however large a `size` is asked for - so the cap is the
server's, not something Cove has to enforce by walking a paginator.

That missing category field is the one thing worth stating plainly: Movies and
TV are the same query here. Cove asks for both because an index with no
category is still a better answer for either than an empty category, and the
row titles carry the season and episode markers a caller needs to tell them
apart.

This source replaced a 1337x adapter that could not work. 1337x's own domains
answer any non-browser client with a Cloudflare managed challenge, and the
public mirrors that do answer are separate sites whose index matches only the
last word of a query - "Breaking Bad" came back as "Star Wars: The Bad Batch".
Neither problem was fixable from an HTTP client, so the source was removed
rather than shipped with a caveat.
"""
from __future__ import annotations

from typing import Any

from cove.search.magnet import build_magnet, normalize_info_hash
from cove.search.models import Category, SearchResult, SourceError, SourceErrorKind
from cove.search.sources.base import (
    MAX_RESULTS,
    SearchHttp,
    Source,
    coerce_count,
    coerce_size,
    coerce_timestamp,
)

ENDPOINT = "https://torrents-csv.com/service/search"

# What Cove asks for. The service caps its own response well below this, so the
# number is a ceiling Cove states rather than a page size it depends on.
PAGE_SIZE = 100


class TorrentsCsvSource(Source):
    id = "torrents-csv"
    label = "Torrents-CSV"
    # The index carries no category of its own, so one query answers both. See
    # the module docstring: an uncategorised answer beats an empty category.
    categories = (Category.MOVIES, Category.TV)
    homepage = "https://torrents-csv.com"
    reports_swarm = True

    def search(
        self,
        query: str,
        category: Category,
        http: SearchHttp,
    ) -> list[SearchResult]:
        if not self.serves(category):
            return []
        payload = http.get_json(ENDPOINT, {"q": query, "size": PAGE_SIZE})
        return self._parse(payload)

    def _parse(self, payload: Any) -> list[SearchResult]:
        # A search that matched nothing still answers with the full envelope
        # and an empty `torrents` array, so the envelope is not optional: a
        # payload missing it is schema drift, and reporting that as "no
        # matches" would hide the break behind a normal-looking empty search.
        if not isinstance(payload, dict):
            raise SourceError(
                SourceErrorKind.PARSE, "Torrents-CSV response is not an object"
            )
        rows = payload.get("torrents")
        if not isinstance(rows, list):
            raise SourceError(
                SourceErrorKind.PARSE, "Torrents-CSV result list is not a list"
            )

        results: list[SearchResult] = []
        for row in rows:
            result = self._row(row)
            if result is not None:
                results.append(result)
                if len(results) >= MAX_RESULTS:
                    break
        # The service already answers best-seeded first, but that is its
        # choice and not a documented guarantee, so the order Cove shows is one
        # Cove imposed. Sorting is stable, so equal swarms keep the index's own
        # ordering rather than being shuffled.
        results.sort(key=lambda result: result.seeders, reverse=True)
        return results

    def _row(self, row: Any):
        if not isinstance(row, dict):
            return None
        info_hash = normalize_info_hash(row.get("infohash"))
        if not info_hash:
            return None
        name = str(row.get("name") or "").strip()
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
                size_bytes=coerce_size(row.get("size_bytes")),
                seeders=coerce_count(row.get("seeders")),
                leechers=coerce_count(row.get("leechers")),
                # When the torrent was created, not when the index last
                # scraped it: `scraped_date` moves on every crawl and would
                # date every row to roughly now.
                added=coerce_timestamp(row.get("created_unix")),
                source=self.id,
            )
        except ValueError:
            return None
