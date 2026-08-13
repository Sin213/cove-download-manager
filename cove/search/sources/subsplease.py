"""SubsPlease - an anime release group with a small public JSON API.

One search is one request: the API answers with every release it matched and
the magnets already inline, so nothing here follows a link, opens a torrent or
talks to a tracker.
"""
from __future__ import annotations

from email.utils import parsedate_to_datetime

from cove.search.magnet import extract_info_hash
from cove.search.models import Category, SearchResult, SourceError, SourceErrorKind
from cove.search.sources.base import MAX_RESULTS, SearchHttp, Source

ENDPOINT = "https://subsplease.org/api/"

# f picks the API's search mode. tz fixes the timezone its release dates are
# rendered in, so a search does not depend on the machine's local clock.
PARAMS = {"f": "search", "tz": "UTC"}


def parse_release_date(text) -> int | None:
    """The API's RFC 2822 release date as a Unix timestamp, or None.

    The entry also carries a `time` field, but that one is display prose; only
    this field is machine-readable, and a release is still worth showing when
    its date is not.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return int(parsedate_to_datetime(text).timestamp())
    except (TypeError, ValueError, OverflowError):
        return None


class SubsPleaseSource(Source):
    id = "subsplease"
    label = "SubsPlease"
    categories = (Category.ANIME,)
    homepage = "https://subsplease.org"
    # The search API publishes no seeder or leecher counts at all.
    reports_swarm = False

    def search(
        self,
        query: str,
        category: Category,
        http: SearchHttp,
    ) -> list[SearchResult]:
        if not self.serves(category):
            return []
        payload = http.get_json(ENDPOINT, dict(PARAMS, s=query))
        return self._parse(payload)

    def _parse(self, payload) -> list[SearchResult]:
        # The API answers a search that matched nothing with an empty JSON
        # array, and a search that matched with an object keyed by release
        # name. Those are the only two shapes it publishes, so anything else -
        # a populated array, a string, an HTML challenge page - is a broken
        # contract rather than an answer.
        if isinstance(payload, list) and not payload:
            return []
        if not isinstance(payload, dict):
            raise SourceError(
                SourceErrorKind.PARSE, "SubsPlease response is not a release map"
            )

        results: list[SearchResult] = []
        for key, release in payload.items():
            if len(results) >= MAX_RESULTS:
                break
            results.extend(self._release(key, release, MAX_RESULTS - len(results)))
        if not results:
            # The API only returns a release map when it has releases - an
            # empty search answers with the array above - so a map that yields
            # nothing, empty or not, means its shape moved under Cove. Saying
            # "no matches" here would hide that break behind a normal-looking
            # empty search.
            raise SourceError(
                SourceErrorKind.PARSE, "SubsPlease released no usable torrents"
            )
        return results

    def _release(self, key, release, budget: int) -> list[SearchResult]:
        """The usable torrents in one release entry, at most `budget` of them."""
        if not isinstance(release, dict) or not isinstance(key, str):
            return []
        title = key.strip()
        if not title:
            return []
        downloads = release.get("downloads")
        if not isinstance(downloads, list):
            return []
        added = parse_release_date(release.get("release_date"))

        results: list[SearchResult] = []
        for download in downloads:
            result = self._row(download, title, added)
            if result is not None:
                results.append(result)
                if len(results) >= budget:
                    break
        return results

    def _row(self, download, title: str, added: int | None):
        if not isinstance(download, dict):
            return None
        magnet = download.get("magnet")
        # The API hands out a complete magnet - trackers, display name and
        # all - so it is kept verbatim and only its hash is normalised.
        info_hash = extract_info_hash(magnet)
        if not info_hash:
            return None
        resolution = download.get("res")
        # Every resolution of an episode carries the same release name, so
        # without the provider's own resolution the variants are
        # indistinguishable in the results list.
        if isinstance(resolution, str) and resolution.strip():
            name = f"{title} [{resolution.strip()}]"
        else:
            name = title
        try:
            return SearchResult(
                info_hash=info_hash,
                name=name,
                magnet=magnet,
                # Size is only ever stated inside the magnet's own xl
                # parameter, which is not a field the API publishes.
                size_bytes=None,
                seeders=0,
                leechers=0,
                added=added,
                source=self.id,
            )
        except ValueError:
            return None
