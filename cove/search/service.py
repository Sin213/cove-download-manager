"""Combining the results several sources returned for one search.

Sources answer independently, and will later answer concurrently, so the merged
list must never depend on which one finished first. Everything here is pure: no
clock, no network, no shared state, and no input is mutated - the same rows in
any order always produce the same :class:`Aggregation`.

Alongside that pure layer this module owns the private thread pool sources
will run on, and the one runnable that calls a single source and reports its
one terminal outcome. Neither knows anything about a search as a whole.

Identity is the info hash and nothing else. Tab 2a already guarantees it is
canonical lower-case 40-hex, so two rows with the same hash are the same
torrent and two rows with different hashes are not; there is no title matching
here and there must never be.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from cove.search.models import Category, SearchResult, SourceError
from cove.search.registry import SOURCES
from cove.search.sources.base import SearchHttp, Source

# Search runs its sources on a pool of its own rather than Qt's global one:
# the global pool is where downloads, RPC calls and hashing already queue, and
# a handful of slow indexers must never take those slots.
#
# 12 is a ceiling, not a target. A machine whose default pool is narrower
# keeps its own width - Search has no business widening a small machine's
# concurrency - so the rule is a clamp, never a max().
_MAX_POOL_THREADS = 12

_POOL: QThreadPool | None = None

# What a source failure is called when the source did not raise SourceError at
# all. A bug in an adapter is not a network problem and must not be reported as
# one, but it is still just one failed source - never a crashed search.
_INTERNAL_ERROR = "internal"

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


def _configure_pool(pool: QThreadPool) -> QThreadPool:
    """Clamp `pool` to Cove's Search ceiling and return it.

    A freshly constructed QThreadPool already defaults to the machine's core
    count, which is commonly above the ceiling, so the clamp has to happen at
    creation - reading the default and calling it configured would let a
    16-thread pool describe itself as capped at 12.
    """
    pool.setMaxThreadCount(min(pool.maxThreadCount(), _MAX_POOL_THREADS))
    return pool


def _pool() -> QThreadPool:
    """The one pool Search executes sources on, created on first use.

    Its width is fixed at creation: nothing here resizes the pool to match how
    many sources a search happens to use. Work beyond the ceiling waits in
    Qt's queue, which is the whole point of having one.
    """
    global _POOL
    if _POOL is None:
        _POOL = _configure_pool(QThreadPool())
    return _POOL


@dataclass(frozen=True)
class _SourceOutcome:
    """Everything one source call ever reports: rows, or why there are none.

    Frozen because it crosses a thread boundary - whoever receives it must be
    reading what the worker sent, not something the worker could still change.
    An empty ``results`` with no ``error_kind`` is a source that legitimately
    found nothing, which is a success and never a failure.
    """

    source_id: str
    results: tuple[SearchResult, ...]
    error_kind: str | None


class _SourceCall(QRunnable):
    """Ask one source for one query, and report exactly one outcome.

    This knows nothing about the search it belongs to: it does not aggregate,
    does not count how many sources are left and touches no shared state, so
    the only thing that can cross back from the pool thread is the outcome
    itself, through a queued signal.

    autoDelete stays off and the caller pins the runnable until its outcome
    lands, matching Cove's existing pool workers: letting the pool reap a
    runnable while the C++ side still references the QObject carrying its
    signal segfaults.
    """

    class _Sig(QObject):
        finished = Signal(object)

    def __init__(
        self,
        source: Source,
        query: str,
        category: Category,
        *,
        http_factory=SearchHttp,
    ):
        super().__init__()
        self.setAutoDelete(False)
        self.signals = self._Sig()
        self._source = source
        # Read once, up front: the outcome must be able to name its source
        # even if the adapter itself is what misbehaved.
        self._source_id = source.id
        self._query = query
        self._category = category
        self._http_factory = http_factory

    def run(self) -> None:
        self.signals.finished.emit(self._outcome())

    def _outcome(self) -> _SourceOutcome:
        """The single terminal outcome, whatever the source did.

        Every path returns from here, so there is exactly one emit above: no
        early success that a later failure could follow, and no way for a
        source to leave the call reporting nothing at all.
        """
        try:
            http = self._http_factory()
        except Exception:  # pragma: no cover - defensive
            return _SourceOutcome(self._source_id, (), _INTERNAL_ERROR)
        try:
            rows = self._source.search(self._query, self._category, http)
            return _SourceOutcome(self._source_id, tuple(rows), None)
        except SourceError as error:
            # The kind is the stable part of a SourceError; its message is
            # written for a human and is not part of this contract.
            return _SourceOutcome(self._source_id, (), error.kind.value)
        except Exception:
            # A broken adapter takes itself out of the search and nothing
            # else. The traceback belongs in a log, never in the outcome.
            return _SourceOutcome(self._source_id, (), _INTERNAL_ERROR)
        finally:
            # The call owns its HTTP facility for exactly this long, on every
            # path - and a session that refuses to close is not a reason to
            # lose the outcome the source already produced.
            try:
                http.close()
            except Exception:  # pragma: no cover - defensive
                pass
