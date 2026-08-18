"""nekoBT - an anime index with a small public JSON API.

A search walks at most `MAX_PAGES` list pages and examines at most
`MAX_RAW_ITEMS` raw rows. The API answers with the matched torrents and their
magnets already inline, so nothing here follows a link, opens a torrent or
talks to a tracker; pagination only issues further requests against the same
list endpoint. Continuation is provider-grounded: the API echoes the current
`search.offset` and effective `search.limit`, and the next request uses
`offset + limit`, but only while `more` is exactly true and both fields
validate.
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

# Hard retrieval bounds, mirroring the other built-ins: `MAX_PAGES` caps the
# list requests one search may issue and `MAX_RAW_ITEMS` caps the raw provider
# rows that may be examined, before any SearchResult conversion. Malformed and
# unusable rows still consume that budget, so bad data cannot buy deeper
# network walking. `MAX_RESULTS` separately caps the normalised results.
MAX_PAGES = 3
MAX_RAW_ITEMS = 200


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
        results: list[SearchResult] = []
        requested_offsets: set[int] = set()
        seen_signatures: set[tuple[tuple[str, str], ...]] = set()
        raw_items = 0
        requested_offset: int | None = None
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {"query": query}
            if requested_offset is not None:
                params["offset"] = requested_offset
            try:
                payload = http.get_json(ENDPOINT, params)
                data = self._data(payload)
                rows = self._rows(data)
            except SourceError:
                # The first page is the search itself: its failures surface
                # exactly as before. A later page is optional depth, so an
                # expected failure there keeps the pages already gathered.
                if requested_offset is None:
                    raise
                break
            if not rows:
                # An empty page is no progress even when the API claims more.
                break
            take = min(len(rows), MAX_RAW_ITEMS - raw_items)
            bounded = rows[:take]
            signature = self._page_signature(bounded)
            if signature in seen_signatures:
                # The same whole page again means the index is not advancing;
                # keep the earlier copy and stop before appending a duplicate.
                break
            seen_signatures.add(signature)
            raw_items += take
            results.extend(self._parse_rows(bounded))
            if len(results) >= MAX_RESULTS or raw_items >= MAX_RAW_ITEMS:
                break
            next_offset = self._next_offset(data, requested_offset)
            if next_offset is None or next_offset in requested_offsets:
                break
            requested_offsets.add(next_offset)
            requested_offset = next_offset
        return results

    def _data(self, payload: Any) -> dict:
        # A search that matched nothing still answers with the full envelope
        # and an empty `results` array, so the envelope is not optional: a
        # payload missing it is schema drift, and reporting that as "no
        # matches" would hide the break behind a normal-looking empty search.
        if not isinstance(payload, dict):
            raise SourceError(SourceErrorKind.PARSE, "nekoBT response is not an object")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise SourceError(SourceErrorKind.PARSE, "nekoBT response has no data object")
        return data

    def _rows(self, data: dict) -> list:
        rows = data.get("results")
        if not isinstance(rows, list):
            raise SourceError(SourceErrorKind.PARSE, "nekoBT result list is not a list")
        return rows

    def _page_signature(self, rows: list) -> tuple[tuple[str, str], ...]:
        # Whole-page identity from fields already present on the row, in page
        # order, without any detail fetch. Rows that lack a usable `id` fall
        # back to their title and finally to a blank marker, so the signature
        # stays deterministic even when the index omits identity fields.
        signature: list[tuple[str, str]] = []
        for row in rows:
            if isinstance(row, dict):
                row_id = row.get("id")
                if isinstance(row_id, str) and row_id:
                    signature.append(("id", row_id))
                    continue
                title = row.get("title")
                if isinstance(title, str) and title:
                    signature.append(("title", title))
                    continue
            signature.append(("blank", ""))
        return tuple(signature)

    def _next_offset(self, data: dict, requested: int | None) -> int | None:
        # Only a literal boolean true advertises another page; generic
        # truthiness would treat "true", 1 or [1] as continuation signals.
        if data.get("more") is not True:
            return None
        search = data.get("search")
        if not isinstance(search, dict):
            return None
        offset = search.get("offset")
        # bool is an int subclass, so each check must exclude it explicitly:
        # True must not count as offset 1 or limit 1.
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            return None
        limit = search.get("limit")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            return None
        # The offset the API echoes must be the one this request asked for.
        # Page 1 sends no offset parameter, which by the provider's default
        # means zero, so a nonzero page-1 echo is just as inconsistent as a
        # later echo that does not match its request.
        if offset != (requested if requested is not None else 0):
            return None
        next_offset = offset + limit
        # A continuation that would not advance past the current request is
        # non-progress; never loop on it.
        if next_offset <= (requested if requested is not None else offset):
            return None
        return next_offset

    def _parse_rows(self, rows: list) -> list[SearchResult]:
        results: list[SearchResult] = []
        for row in rows:
            result = self._row(row)
            if result is not None:
                results.append(result)
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