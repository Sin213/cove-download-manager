"""Combining the results several sources returned for one search.

Sources answer independently, and will later answer concurrently, so the merged
list must never depend on which one finished first. Everything here is pure: no
clock, no network, no shared state, and no input is mutated - the same rows in
any order always produce the same :class:`Aggregation`.

Identity is the info hash and nothing else. Tab 2a already guarantees it is
canonical lower-case 40-hex, so two rows with the same hash are the same
torrent and two rows with different hashes are not; there is no title matching
here and there must never be.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable

from cove.search.models import SearchResult
from cove.search.registry import SOURCES

# The registry is the one ranking authority: a duplicate from an earlier source
# wins a tie, rather than a second hand-written source order drifting from it.
_SOURCE_RANK: dict[str, int] = {source.id: index for index, source in enumerate(SOURCES)}

# Optional fields a duplicate may fill in for the winner. The rest of the row -
# name, magnet, hash, swarm counts and source - stays the winner's, because
# mixing them would describe a torrent no source actually offered.
_BACKFILL_FIELDS = ("size_bytes", "added")


@dataclass(frozen=True)
class Aggregation:
    """The merged rows, plus how many duplicates were dropped to get them."""

    results: tuple[SearchResult, ...]
    dedupe_dropped: int


def _priority_key(row: SearchResult, arrival: int) -> tuple[int, int, str, int]:
    """Source priority for `row`, first occurrence breaking a remaining tie.

    Registry sources come first, in registry order. A source id the registry
    does not know still has to sort somewhere, so it ranks behind every
    built-in one and ties with its peers on the id itself.
    """
    rank = _SOURCE_RANK.get(row.source)
    if rank is None:
        return (1, 0, row.source, arrival)
    return (0, rank, "", arrival)


def _winner_key(row: SearchResult, arrival: int) -> tuple:
    """Which of two rows for the same hash Cove keeps.

    Most seeders first, then the earlier source in the registry, then whichever
    was seen first - so the choice never depends on completion order.
    """
    return (-row.seeders,) + _priority_key(row, arrival)


def _sort_key(row: SearchResult) -> tuple:
    """The total order the UI shows: seeders, then recency, then name.

    Rows without an ``added`` date sort last inside their seeder group rather
    than pretending to be ancient, and ``casefold`` keeps the name order
    intuitive without being locale-sensitive. The hash is a final tie break, so
    the order is total: no two distinct rows can compare equal.
    """
    added_missing = row.added is None
    return (
        -row.seeders,
        added_missing,
        -(row.added if row.added is not None else 0),
        row.name.casefold(),
        row.name,
        row.info_hash,
    )


def _backfilled(winner: SearchResult, group: list[tuple[SearchResult, int]]) -> SearchResult:
    """`winner` with any optional field it lacks taken from a duplicate.

    Donors are considered in source-priority order, so which duplicate supplies
    a value is fixed by the registry rather than by arrival. A field the winner
    already has is never touched.
    """
    missing = [field for field in _BACKFILL_FIELDS if getattr(winner, field) is None]
    if not missing:
        return winner

    donors = sorted(group, key=lambda pair: _priority_key(*pair))
    found = {}
    for field in missing:
        for donor, _ in donors:
            value = getattr(donor, field)
            if value is not None:
                found[field] = value
                break

    if not found:
        return winner
    return replace(winner, **found)


def aggregate(results: Iterable[SearchResult]) -> Aggregation:
    """Merge `results` from any number of sources into one ordered list.

    Duplicate info hashes collapse to a single row, that row picks up any
    optional metadata only its duplicates reported, and the output is sorted
    into a total order. The input is left untouched.
    """
    groups: dict[str, list[tuple[SearchResult, int]]] = {}
    total = 0
    for arrival, row in enumerate(results):
        total += 1
        groups.setdefault(row.info_hash, []).append((row, arrival))

    merged = []
    for group in groups.values():
        winner, _ = min(group, key=lambda pair: _winner_key(*pair))
        merged.append(_backfilled(winner, group))

    merged.sort(key=_sort_key)
    return Aggregation(results=tuple(merged), dedupe_dropped=total - len(merged))
