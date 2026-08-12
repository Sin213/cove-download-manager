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
from enum import Enum
from typing import Iterable

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal

from cove.search.models import Category, SearchResult, SourceError
from cove.search.registry import SOURCES, sources_for
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

    ``generation`` is carried, never interpreted: the worker was handed it and
    hands it back, so whoever receives the outcome can tell which search asked
    for it without the worker knowing what a search is.
    """

    generation: int
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
        generation: int,
        http_factory=SearchHttp,
    ):
        super().__init__()
        self.setAutoDelete(False)
        self.signals = self._Sig()
        # Opaque here on purpose: the call never compares it to anything and
        # never asks whether the search it belongs to is still wanted.
        self._generation = generation
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
            return _SourceOutcome(self._generation, self._source_id, (), _INTERNAL_ERROR)
        try:
            rows = self._source.search(self._query, self._category, http)
            return _SourceOutcome(self._generation, self._source_id, tuple(rows), None)
        except SourceError as error:
            # The kind is the stable part of a SourceError; its message is
            # written for a human and is not part of this contract.
            return _SourceOutcome(self._generation, self._source_id, (), error.kind.value)
        except Exception:
            # A broken adapter takes itself out of the search and nothing
            # else. The traceback belongs in a log, never in the outcome.
            return _SourceOutcome(self._generation, self._source_id, (), _INTERNAL_ERROR)
        finally:
            # The call owns its HTTP facility for exactly this long, on every
            # path - and a session that refuses to close is not a reason to
            # lose the outcome the source already produced.
            try:
                http.close()
            except Exception:  # pragma: no cover - defensive
                pass


class SourceState(Enum):
    """Where one source of the current search has got to.

    These three are the whole of it. A cancelled or superseded search has no
    state of its own here: it simply stops being reported, so its sources are
    last heard of running rather than announced as anything new. A timed-out
    source is a separate lifecycle and deliberately has no value here yet.
    """

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class SourceFailure:
    """Which source could not answer, and the stable reason it gave.

    The kind comes straight from the worker, which has already reduced a
    SourceError to its kind and any other exception to "internal", so nothing
    a third-party indexer wrote reaches a caller through here.
    """

    source_id: str
    error_kind: str


@dataclass(frozen=True)
class SourceStatus:
    """One source's current state, as the UI needs to show it.

    ``generation`` names the search this is about. A listener needs it because
    a superseded search may already have announced a source as running before
    the newer search announced the same one.
    """

    generation: int
    source_id: str
    state: SourceState
    error_kind: str | None
    result_count: int


@dataclass(frozen=True)
class SearchSummary:
    """What one finished search amounted to.

    Data only: the UI decides how to word "two sources failed", so nothing
    here is a sentence. failures names every source that could not answer,
    which is how a search that returned few rows explains itself.

    ``generation`` names the search that produced it. Only a search that ran
    to its own natural end is summarised at all: one that was superseded or
    cancelled is never summarised, so no field here says so.
    """

    generation: int
    results: tuple[SearchResult, ...]
    dedupe_dropped: int
    failures: tuple[SourceFailure, ...]


class SearchService(QObject):
    """Runs one search across the built-in sources and reports what it finds.

    One search is current at a time, and every search is numbered. Starting
    another replaces the current one and cancel() abandons it; in both cases
    the older search's workers are left to finish on their own and whatever
    they eventually report is discarded, because a superseded or cancelled
    search may no longer speak. Every public event names the search it came
    from, so a listener can always tell which one it is hearing.

    Superseding and cancelling are suppression, never termination: nothing
    here kills a thread, aborts a request or drops a runnable the pool still
    owns. A provider call that never returns therefore stays alive and pinned.
    A bounded overall deadline, and finalising a source that outlives it, are
    a separate concern and are not implemented here.
    """

    # Every one of these names its search. Two carry it inside the payload
    # they already had; the merged result list is a bare tuple with nowhere to
    # put it, so that one sends the number alongside.
    source_status = Signal(object)
    results_updated = Signal(int, object)
    search_finished = Signal(object)

    def __init__(self, parent: QObject | None = None, *, http_factory=SearchHttp):
        super().__init__(parent)
        # The one seam a test needs: the workers build their own HTTP, so the
        # service never touches a session itself.
        self._http_factory = http_factory
        # Every search this service ever runs is numbered, and the number only
        # ever goes up. 0 is the number of the search that has not happened
        # yet, so no real search can ever be mistaken for it.
        self._generation = 0
        self._active = False
        # The runnables still owed an outcome. Qt may reap a QRunnable the
        # moment it finishes, so the service holds each one until its outcome
        # has actually been handled - including the workers of searches that
        # have already been superseded or cancelled, which is why the key is
        # the search as well as the source. The same source may legitimately
        # be running in two generations at once, and the newer call must never
        # evict the older one from its own ownership.
        self._calls: dict[tuple[int, str], _SourceCall] = {}
        self._pending: set[str] = set()
        self._results: dict[str, tuple[SearchResult, ...]] = {}
        self._failures: list[SourceFailure] = []

    @property
    def active(self) -> bool:
        """Whether a search is running."""
        return self._active

    @property
    def generation(self) -> int:
        """The number of the newest search. Read-only, and never goes back."""
        return self._generation

    def start(self, query: str, category: Category = Category.ALL) -> int:
        """Search for the given text across every source serving the category.

        Returns the number given to this search, which is what tells its
        events apart from an older search's.
        """
        generation = self._begin()

        text = query.strip()
        if not text:
            # A blank query is not an error and not a search: it completes
            # immediately so the UI gets one lifecycle rather than none.
            self._finish(generation)
            return generation

        sources = sources_for(category)
        if not sources:
            # A category nothing covers finishes here rather than waiting for
            # callbacks that can never arrive.
            self._finish(generation)
            return generation

        # Active before the first emit: a listener reacting to a running
        # status must already see a search in progress.
        self._active = True
        self._pending.update(source.id for source in sources)

        # Every source is announced running before any of them is submitted,
        # so no worker can report a terminal state for a source the UI has
        # not been told about yet.
        #
        # Emitting hands control to listeners, and a listener may start or
        # cancel a search from here. If it does, this search is over before it
        # ever reached the pool: nothing below may run, or a search the caller
        # has already replaced would go on submitting workers.
        for source in sources:
            self._emit_status(generation, source.id, SourceState.RUNNING, None, 0)
            if generation != self._generation:
                return generation

        # Only this search's own calls are submitted. An older search's
        # workers are already on the pool and are not restarted here.
        pool = _pool()
        for source in sources:
            call = _SourceCall(
                source,
                text,
                category,
                generation=generation,
                http_factory=self._http_factory,
            )
            call.signals.finished.connect(self._on_outcome, Qt.QueuedConnection)
            self._calls[(generation, source.id)] = call
            pool.start(call)
        return generation

    def cancel(self) -> int:
        """Stop accepting the current search's results, and return the number
        that is now current.

        This is suppression, not termination. The provider calls already in
        flight keep running to their own natural end - Cove does not kill a
        thread, abort a request or drop a runnable the pool still owns - and
        what changes is only that this service will ignore whatever they
        eventually report. A worker that never returns therefore stays alive,
        and pinned, until a later slice gives Search a bounded deadline.

        Cancelling when nothing is running is a no-op and spends no
        generation, so an idle UI cannot inflate the numbering.
        """
        if not self._active:
            return self._generation

        self._generation += 1
        self._active = False
        self._pending.clear()
        self._results.clear()
        self._failures.clear()
        # No search_finished: the caller asked for this and already knows.
        # Nothing else may hear from the cancelled search again.
        return self._generation

    def _begin(self) -> int:
        """Number the search about to start, and forget the previous one."""
        self._generation += 1
        self._active = False
        self._pending.clear()
        self._results.clear()
        self._failures.clear()
        return self._generation

    def _emit_status(
        self,
        generation: int,
        source_id: str,
        state: SourceState,
        error_kind: str | None,
        result_count: int,
    ) -> None:
        self.source_status.emit(
            SourceStatus(generation, source_id, state, error_kind, result_count)
        )

    def _on_outcome(self, outcome: _SourceOutcome) -> None:
        """Take one source's terminal outcome, on the service's own thread.

        The connection is queued, so this runs where the service lives and is
        the only place the search's state changes - the pool threads never
        touch it.
        """
        generation = outcome.generation
        source_id = outcome.source_id
        # Releasing the worker comes first and happens whatever else is true:
        # the runnable was pinned only until it reported, and a search nobody
        # wants any more still has to stop owning its threads.
        self._calls.pop((generation, source_id), None)

        if generation != self._generation:
            # A superseded or cancelled search. Its worker has just been let
            # go and that is the whole of what it may do here: the current
            # search's results, failures, pending sources, signals and
            # completion are none of its business.
            return

        if source_id not in self._pending:
            # A worker reports once, so this is only reachable if something
            # ever changes that; still, a second outcome must never be able to
            # finish a search twice.
            return
        self._pending.discard(source_id)

        if outcome.error_kind is None:
            self._results[source_id] = outcome.results
            self._emit_status(
                generation, source_id, SourceState.COMPLETED, None, len(outcome.results)
            )
            if generation != self._generation:
                # A listener replaced or cancelled the search from inside that
                # status. Everything left to do here was this search's, and
                # this search no longer exists.
                return
            # A source that found nothing still succeeded; republishing the
            # same merged view costs nothing and keeps the rule simple.
            self.results_updated.emit(generation, self._merged().results)
            if generation != self._generation:
                return
        else:
            # One source is out. Its peers keep running and whatever they
            # already found stays exactly as it was, so there is no new result
            # view to publish here.
            self._failures.append(SourceFailure(source_id, outcome.error_kind))
            self._emit_status(
                generation, source_id, SourceState.FAILED, outcome.error_kind, 0
            )
            if generation != self._generation:
                return

        if not self._pending:
            self._finish(generation)

    def _merged(self) -> Aggregation:
        """Everything the successful sources have reported so far, merged.

        Aggregation is not reimplemented or incrementally patched here: the
        rows are handed to the one aggregate() the whole feature shares, so a
        partial view obeys exactly the rules the final one does.
        """
        rows: list[SearchResult] = []
        for source_results in self._results.values():
            rows.extend(source_results)
        return aggregate(rows)

    def _finish(self, generation: int) -> None:
        """End the numbered search, exactly once, and go inactive."""
        merged = self._merged()
        summary = SearchSummary(
            generation=generation,
            results=merged.results,
            dedupe_dropped=merged.dedupe_dropped,
            failures=tuple(self._failures),
        )
        self._active = False
        self._pending.clear()
        # Nothing of this search is still pinned - every worker released
        # itself as it reported - and an older search's workers are not this
        # one's to drop, so there is no call bookkeeping to do here.
        #
        # Last statement on purpose: a listener may legitimately start the
        # next search from here, and must not find this one still tidying up.
        self.search_finished.emit(summary)
