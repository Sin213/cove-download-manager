"""Nyaa - an anime index published as RSS.

Nyaa has no JSON API; its search page also serves an RSS feed carrying the
info hash, swarm counts and a human-readable size in a ``nyaa:`` namespace.
Parsing goes through the standard library's ElementTree, and only ever reads
the document as data - no entity resolution, no external parser.
"""
from __future__ import annotations

import re
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

from cove.search.magnet import build_magnet, normalize_info_hash
from cove.search.models import Category, SearchResult, SourceError, SourceErrorKind
from cove.search.sources.base import (
    MAX_RESULTS,
    SearchHttp,
    Source,
    coerce_count,
)

ENDPOINT = "https://nyaa.si/"

_SIZE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)\s*$")
_UNITS = {
    "bytes": 1,
    "byte": 1,
    "b": 1,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
}


def parse_size(text: str | None) -> int | None:
    """Nyaa's "1.4 GiB" into bytes, or None when it is not a size."""
    if not isinstance(text, str):
        return None
    match = _SIZE.match(text)
    if not match:
        return None
    unit = _UNITS.get(match.group(2).lower())
    if unit is None:
        return None
    try:
        return int(float(match.group(1)) * unit)
    except (ValueError, OverflowError):
        # A digit string long enough to overflow a float is not a size.
        return None


def parse_pubdate(text: str | None) -> int | None:
    """An RFC 2822 pubDate as a Unix timestamp, or None when unparseable."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return int(parsedate_to_datetime(text).timestamp())
    except (TypeError, ValueError, OverflowError):
        return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


class NyaaSource(Source):
    id = "nyaa"
    label = "Nyaa"
    categories = (Category.ANIME,)
    homepage = "https://nyaa.si"
    reports_swarm = True

    def search(
        self,
        query: str,
        category: Category,
        http: SearchHttp,
    ) -> list[SearchResult]:
        if not self.serves(category):
            return []
        # c=1_0 is the site's Anime category; f=0 means "no filter".
        params = {"page": "rss", "q": query, "c": "1_0", "f": "0"}
        return self._parse(http.get_bytes(ENDPOINT, params))

    def _parse(self, raw: bytes) -> list[SearchResult]:
        try:
            root = ElementTree.fromstring(raw)
        except ElementTree.ParseError as error:
            raise SourceError(SourceErrorKind.PARSE, f"Nyaa feed is not valid XML: {error}")
        if _local_name(root.tag) != "rss":
            raise SourceError(SourceErrorKind.PARSE, "Nyaa response is not an RSS feed")
        channel = next(
            (child for child in root if _local_name(child.tag) == "channel"), None
        )
        if channel is None:
            raise SourceError(SourceErrorKind.PARSE, "Nyaa feed has no channel")

        results: list[SearchResult] = []
        for item in channel:
            if _local_name(item.tag) != "item":
                continue
            result = self._row(item)
            if result is not None:
                results.append(result)
                if len(results) >= MAX_RESULTS:
                    break
        return results

    def _row(self, item):
        # Element order is not guaranteed and the nyaa: namespace URI has
        # changed before, so fields are looked up by local name.
        fields = {_local_name(child.tag): (child.text or "") for child in item}
        info_hash = normalize_info_hash(fields.get("infoHash"))
        if not info_hash:
            return None
        name = fields.get("title", "").strip()
        if not name:
            return None
        try:
            return SearchResult(
                info_hash=info_hash,
                name=name,
                magnet=build_magnet(info_hash, name),
                size_bytes=parse_size(fields.get("size")),
                seeders=coerce_count(fields.get("seeders")),
                leechers=coerce_count(fields.get("leechers")),
                added=parse_pubdate(fields.get("pubDate")),
                source=self.id,
            )
        except ValueError:
            return None
