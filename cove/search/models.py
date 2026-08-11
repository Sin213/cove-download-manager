"""The normalised Search data model shared by every source adapter.

Search results come from untrusted third-party indexers, so the model is the
validation boundary: an adapter either produces a SearchResult that is safe to
hand to Cove's torrent path, or it drops the row.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from cove.search.magnet import extract_info_hash, normalize_info_hash


class Category(Enum):
    """What the user asked for.

    ALL is a query-side value only: it means "every enabled source", and no
    source ever declares it among the categories it can serve.
    """

    ALL = "all"
    GAMES = "games"
    MOVIES = "movies"
    TV = "tv"
    ANIME = "anime"


class SourceErrorKind(Enum):
    """Why a source failed. Deliberately coarse - the UI shows a reason, not a
    stack trace, and adapters must not leak requests/XML exception types."""

    NETWORK = "network"
    HTTP = "http"
    PARSE = "parse"
    TIMEOUT = "timeout"


class SourceError(Exception):
    """A source could not answer. An empty result list is not an error."""

    def __init__(self, kind: SourceErrorKind, message: str = ""):
        super().__init__(message or kind.value)
        self.kind = kind


@dataclass(frozen=True)
class SearchResult:
    """One torrent, normalised.

    ``info_hash`` is always lower-case 40-character hex and ``magnet`` always
    carries that same hash, so callers never have to re-derive one from the
    other. ``size_bytes`` and ``added`` are None when the source does not
    report them; ``seeders``/``leechers`` are 0 when the source reports zero,
    and a source that cannot report swarm data at all says so through its
    ``reports_swarm`` flag rather than through a fake 0 here.
    """

    info_hash: str
    name: str
    magnet: str
    size_bytes: int | None
    seeders: int
    leechers: int
    added: int | None
    source: str

    def __post_init__(self) -> None:
        if normalize_info_hash(self.info_hash) != self.info_hash:
            raise ValueError(f"info_hash is not normalised: {self.info_hash!r}")
        if not self.name.strip():
            raise ValueError("name must be non-empty")
        if extract_info_hash(self.magnet) != self.info_hash:
            raise ValueError("magnet does not carry info_hash")
        if self.size_bytes is not None and self.size_bytes < 0:
            raise ValueError("size_bytes must not be negative")
        if self.seeders < 0 or self.leechers < 0:
            raise ValueError("swarm counts must not be negative")
        if not self.source:
            raise ValueError("source must be a stable source id")
