"""The Pirate Bay, through its public apibay JSON endpoint.

apibay answers one query with a flat list of rows spanning every category, so
Cove asks once and keeps the rows whose category the user actually searched
for. Filtering locally keeps a search to a single request no matter how many
category ids a Cove category maps to.
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

ENDPOINT = "https://apibay.org/q.php"

# The site's own category ids behind each Cove category.
CATEGORY_IDS: dict[Category, frozenset[str]] = {
    Category.MOVIES: frozenset({"201", "202", "207", "209"}),
    Category.TV: frozenset({"205", "208"}),
}


class PirateBaySource(Source):
    id = "piratebay"
    label = "The Pirate Bay"
    categories = (Category.MOVIES, Category.TV)
    homepage = "https://thepiratebay.org"
    reports_swarm = True

    def search(
        self,
        query: str,
        category: Category,
        http: SearchHttp,
    ) -> list[SearchResult]:
        if not self.serves(category):
            return []
        wanted = self._wanted_ids(category)
        # cat=0 is apibay's "everything"; the category the user picked is
        # applied to the rows that come back.
        payload = http.get_json(ENDPOINT, {"q": query, "cat": "0"})
        if not isinstance(payload, list):
            raise SourceError(SourceErrorKind.PARSE, "apibay response is not a list")

        results: list[SearchResult] = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            if str(row.get("category", "")).strip() not in wanted:
                continue
            result = self._row(row)
            if result is not None:
                results.append(result)
                if len(results) >= MAX_RESULTS:
                    break
        return results

    def _wanted_ids(self, category: Category) -> frozenset[str]:
        if category is Category.ALL:
            return frozenset().union(*CATEGORY_IDS.values())
        return CATEGORY_IDS[category]

    def _row(self, row: dict[str, Any]):
        # apibay answers an empty search with a single placeholder row whose
        # hash is all zeroes; normalisation rejects it like any other unusable
        # hash, so "no results" needs no special case here.
        info_hash = normalize_info_hash(row.get("info_hash"))
        if not info_hash:
            return None
        name = str(row.get("name") or "").strip()
        if not name:
            return None
        try:
            return SearchResult(
                info_hash=info_hash,
                name=name,
                magnet=build_magnet(info_hash, name),
                size_bytes=coerce_size(row.get("size")),
                seeders=coerce_count(row.get("seeders")),
                leechers=coerce_count(row.get("leechers")),
                added=coerce_timestamp(row.get("added")),
                source=self.id,
            )
        except ValueError:
            return None
