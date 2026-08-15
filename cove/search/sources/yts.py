"""YTS - a JSON movie index.

YTS publishes one entry per film with a torrent per quality, and it answers on
several mirror domains that go down independently, so a search walks the known
mirrors in order until one of them gives a usable answer.
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

# Mirrors, tried in this order. A fixed, short list: failover is a fallback,
# not a retry loop. Both were verified to answer the API directly, on their
# own hostname, with no redirect; the second is the successor base URL the API
# itself announces. Hosts that stop answering directly come out of this tuple
# rather than staying on as a fallback that only spends the timeout budget -
# yts.mx (gone from DNS), yts.am (redirects cross-host, which Search refuses by
# design), and yts.rs (500s out of the API) were retired for exactly that.
HOSTS = ("yts.gg", "movies-api.accel.li")

# The API's own per-page maximum.
_PAGE_LIMIT = 50


class YtsSource(Source):
    id = "yts"
    label = "YTS"
    categories = (Category.MOVIES,)
    homepage = "https://yts.gg"
    reports_swarm = True

    def search(
        self,
        query: str,
        category: Category,
        http: SearchHttp,
    ) -> list[SearchResult]:
        if not self.serves(category):
            return []
        params = {"query_term": query, "limit": _PAGE_LIMIT, "sort_by": "seeds"}
        last: SourceError | None = None
        for host in HOSTS:
            url = f"https://{host}/api/v2/list_movies.json"
            try:
                return self._parse(http.get_json(url, params))
            except SourceError as error:
                last = error
        assert last is not None
        raise last

    def _parse(self, payload: Any) -> list[SearchResult]:
        if not isinstance(payload, dict):
            raise SourceError(SourceErrorKind.PARSE, "YTS response is not an object")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise SourceError(SourceErrorKind.PARSE, "YTS response has no data object")
        movies = data.get("movies", [])
        if movies is None:
            movies = []
        if not isinstance(movies, list):
            raise SourceError(SourceErrorKind.PARSE, "YTS movie list is not a list")

        results: list[SearchResult] = []
        for movie in movies:
            if not isinstance(movie, dict):
                continue
            title = str(movie.get("title_long") or movie.get("title") or "").strip()
            if not title:
                continue
            torrents = movie.get("torrents")
            if not isinstance(torrents, list):
                continue
            fallback_date = coerce_timestamp(movie.get("date_uploaded_unix"))
            for torrent in torrents:
                result = self._row(torrent, title, fallback_date)
                if result is not None:
                    results.append(result)
                    if len(results) >= MAX_RESULTS:
                        return results
        return results

    def _row(self, torrent: Any, title: str, fallback_date: int | None):
        if not isinstance(torrent, dict):
            return None
        info_hash = normalize_info_hash(torrent.get("hash"))
        if not info_hash:
            return None
        quality = str(torrent.get("quality") or "").strip()
        # Quality is what tells one YTS entry for a film from the next, so it
        # belongs in the name the user picks from.
        name = f"{title} [{quality}]" if quality else title
        try:
            return SearchResult(
                info_hash=info_hash,
                name=name,
                magnet=build_magnet(info_hash, name),
                size_bytes=coerce_size(torrent.get("size_bytes")),
                seeders=coerce_count(torrent.get("seeds")),
                leechers=coerce_count(torrent.get("peers")),
                added=coerce_timestamp(torrent.get("date_uploaded_unix")) or fallback_date,
                source=self.id,
            )
        except ValueError:
            return None
