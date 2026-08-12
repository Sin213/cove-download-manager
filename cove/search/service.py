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

import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from enum import Enum
from typing import Iterable

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal

from cove import diagnostics
from cove.search.models import Category, SearchResult, SourceError
from cove.search.registry import SOURCES, sources_for
from cove.search.sources.base import SearchHttp, Source

# Every Search event is recorded under this one name, so a support log can be
# read as one lifecycle rather than one per provider. Which source an event is
# about is a field, never part of the component.
_DIAG_COMPONENT = "search"

# Search runs its sources on a pool of its own rather than Qt's global one:
# the global pool is where downloads, RPC calls and hashing already queue, and
# a handful of slow indexers must never take those slots.
#
# 12 is a ceiling, not a target. A machine whose default pool is narrower
# keeps its own width - Search has no business widening a small machine's
# concurrency - so the rule is a clamp, never a max().
_MAX_POOL_THREADS = 12

_POOL: QThreadPool | None = None

# How long Cove waits for a whole search before it stops waiting. This is a
# product promise about the search, not a network setting: SearchHttp already
# bounds each individual request, and this is what makes the search itself
# finish even when a source stops answering without failing.
#
# One deadline covers the search, never one per source: a slow indexer is
# allowed to use the whole window if its peers are quick.
_SEARCH_DEADLINE_MS = 30_000

# What a source failure is called when the source did not raise SourceError at
# all. A bug in an adapter is not a network problem and must not be reported as
# one, but it is still just one failed source - never a crashed search.
_INTERNAL_ERROR = "internal"

# What a source failure is called when the source never said anything at all.
# It is deliberately not "network": the provider may be perfectly healthy and
# simply slower than Cove is willing to wait.
_TIMEOUT_ERROR = "timeout"

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


def _source_order(source_id: str) -> tuple[int, int, str]:
    """Where one source id sorts when several are reported at once.

    The registry is the authority here too, so a listener hears about sources
    in the order it already ranks them; ids the registry does not know sort
    behind every built-in one and among themselves by the id. This is about
    announcement order only and touches no ranking of results.
    """
    rank = _SOURCE_RANK.get(source_id)
    if rank is None:
        return (1, 0, source_id)
    return (0, rank, "")


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

    These four are the whole of it. A cancelled or superseded search has no
    state of its own here: it simply stops being reported, so its sources are
    last heard of running rather than announced as anything new.

    TIMED_OUT is not FAILED. A failed source answered and said it could not
    help; a timed-out one is still running somewhere and simply did not answer
    inside the search's deadline, which is a statement about Cove's patience
    rather than about the provider being broken.
    """

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


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

    Every search also has a deadline, so one silent source cannot hold a
    search open: when it expires the sources that have not answered are
    reported as timed out, whatever the others found is summarised as usual,
    and the search is over.

    Superseding, cancelling and timing out are all suppression, never
    termination: nothing here kills a thread, aborts a request or drops a
    runnable the pool still owns. A provider call that never returns therefore
    stays alive, keeps one of the private pool's threads, and stays pinned
    until it reports - the deadline bounds this service, not the provider.
    """

    # Every one of these names its search. Two carry it inside the payload
    # they already had; the merged result list is a bare tuple with nowhere to
    # put it, so that one sends the number alongside.
    source_status = Signal(object)
    results_updated = Signal(int, object)
    search_finished = Signal(object)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        http_factory=SearchHttp,
        deadline_ms: int = _SEARCH_DEADLINE_MS,
        cache=None,
    ):
        super().__init__(parent)
        # The one seam a test needs: the workers build their own HTTP, so the
        # service never touches a session itself.
        self._http_factory = http_factory
        # One cache for the service, not one per search: an answer is worth
        # reusing precisely because the search that stored it is over. Nothing
        # here empties it when a search finishes, is superseded or is
        # cancelled - that would leave it with nothing to reuse. The third
        # seam, and private: a test that needs a cache whose clock it controls
        # passes one, and production never does.
        self._cache = _SearchCache() if cache is None else cache
        # The second seam: a test proves what happens at the deadline without
        # waiting half a minute for it. Production never passes this.
        if not isinstance(deadline_ms, int) or isinstance(deadline_ms, bool) or deadline_ms <= 0:
            raise ValueError("deadline_ms must be a positive number of milliseconds")
        self._deadline_ms = deadline_ms
        # Owned by the service and living on its thread, so the deadline fires
        # where every other state change already happens. One timer for the
        # service, armed for whichever search is current - never one per
        # source, and never a thread of its own.
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        # Precise, because the deadline is a promise to the sources as much as
        # to the user: Qt's default coarse timer is allowed to fire up to 5%
        # early, which on 30 seconds is a second and a half a source was told
        # it had. A source that answers inside the window must never be
        # reported as having missed it.
        self._deadline.setTimerType(Qt.PreciseTimer)
        self._deadline.timeout.connect(self._on_deadline)
        # Which search the armed timer belongs to. 0 is no search, so a timer
        # that is not armed can never be mistaken for one that is.
        self._deadline_generation = 0
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
        # What each submitted call would be cached under, remembered for
        # exactly as long as the call itself is. The key is settled when the
        # worker is submitted rather than read back off the service when its
        # outcome lands: by then the query and category the service holds may
        # belong to an entirely different search.
        self._cache_keys: dict[tuple[int, str], _CacheKey] = {}
        self._pending: set[str] = set()
        self._results: dict[str, tuple[SearchResult, ...]] = {}
        self._failures: list[SourceFailure] = []

    # ---- diagnostics -----------------------------------------------------
    #
    # Observation only. Every record below names a transition this service has
    # already committed to, and none of them may influence what a search does:
    # a diagnostics call is not a lifecycle event, cannot reach a listener and
    # is never a point a generation has to be re-checked after.
    #
    # What is recorded is counts and the names the service already publishes.
    # The query itself, the rows a source found, their titles, magnets and info
    # hashes are deliberately absent - a support log is not the place for what
    # a user searched for or what they were shown.

    def _diag(self, event: str, **fields) -> None:
        try:
            diagnostics.emit(_DIAG_COMPONENT, event, **fields)
        except Exception:
            pass

    def _diag_started(
        self, generation: int, category: Category, source_count: int, query_length: int
    ) -> None:
        """One search has begun. The length is of the normalised query, which
        is the text the sources are actually given."""
        self._diag(
            "search_started",
            generation=generation,
            category=getattr(category, "value", None),
            source_count=source_count,
            query_length=query_length,
        )

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
            # immediately so the UI gets one lifecycle rather than none. The
            # record follows that exactly - one start, one finish, no sources -
            # so no search is missing from the log on account of being empty.
            self._diag_started(generation, category, 0, 0)
            self._finish(generation)
            return generation

        sources = sources_for(category)
        # Recorded once the search knows what it is: which sources it will ask,
        # and how long the question was. Never the question itself.
        self._diag_started(generation, category, len(sources), len(text))
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
            # Recorded where the search commits to the source - before the
            # public event, because that event may end this search, and a
            # source it never got to is one the log should not claim it did.
            self._diag(
                "source_started",
                generation=generation,
                source=source.id,
                category=getattr(category, "value", None),
            )
            self._emit_status(generation, source.id, SourceState.RUNNING, None, 0)
            if generation != self._generation:
                return generation

        # What the sources have already answered is settled before any worker
        # is submitted, so a source the cache can serve never reaches the pool
        # at all. The query used here is the normalised text the sources
        # themselves are given, which is what makes "  dune  " and "dune" one
        # answer; nothing else is done to it, so "Dune" stays a question of its
        # own.
        #
        # Each completion hands control to listeners exactly as a live one
        # does, and a listener may start or cancel a search from there. If it
        # does, this search is over: the sources still to be looked up, the
        # workers still to be submitted and the deadline that would have bound
        # them all belonged to a search that no longer exists.
        misses = []
        for source in sources:
            cached = self._cache.get(_CacheKey(source.id, category, text))
            if cached is None:
                # Recorded at the classification itself, so hit and miss are
                # read off the same decision the search acted on.
                self._diag(
                    "source_cache_miss", generation=generation, source=source.id
                )
                misses.append(source)
                continue
            self._diag(
                "source_cache_hit",
                generation=generation,
                source=source.id,
                result_count=len(cached),
            )
            self._pending.discard(source.id)
            if not self._complete_source(generation, source.id, cached):
                return generation

        if not misses:
            # Every source answered from cache, so there is no live work for a
            # deadline to bound and nothing left to wait for. The search ends
            # here, on the thread that asked for it.
            self._finish(generation)
            return generation

        # Only this search's own calls are submitted, and only for the sources
        # the cache could not answer. An older search's workers are already on
        # the pool and are not restarted here.
        pool = _pool()
        for source in misses:
            key = _CacheKey(source.id, category, text)
            call = _SourceCall(
                source,
                text,
                category,
                generation=generation,
                http_factory=self._http_factory,
            )
            call.signals.finished.connect(self._on_outcome, Qt.QueuedConnection)
            self._calls[(generation, source.id)] = call
            self._cache_keys[(generation, source.id)] = key
            pool.start(call)

        # Armed last, once this search is certainly still the current one and
        # its live work is on the pool. Every point above that hands control to
        # a listener returns rather than reaching here, so this can only arm
        # for a search that is still the current one - and a search with no
        # live work at all never arms it, because there is nothing left for a
        # deadline to be about.
        self._arm_deadline(generation)
        return generation

    def cancel(self) -> int:
        """Stop accepting the current search's results, and return the number
        that is now current.

        This is suppression, not termination. The provider calls already in
        flight keep running to their own natural end - Cove does not kill a
        thread, abort a request or drop a runnable the pool still owns - and
        what changes is only that this service will ignore whatever they
        eventually report. A worker that never returns therefore stays alive,
        and pinned, for as long as it goes on running.

        Cancelling when nothing is running is a no-op and spends no
        generation, so an idle UI cannot inflate the numbering.
        """
        if not self._active:
            # Nothing was running, so nothing was cancelled and there is
            # nothing to record: an idle UI cannot fill the log either.
            return self._generation

        cancelled = self._generation
        pending = len(self._pending)
        self._generation += 1
        self._active = False
        self._pending.clear()
        self._results.clear()
        self._failures.clear()
        # Nothing is waiting for this search any more, so nothing may time it
        # out either.
        self._stop_deadline()
        # Recorded as cancelled and never as superseded: the user asking for
        # this search to stop and another search replacing it are two different
        # things, and a log that blurs them cannot tell what the user did. How
        # many sources it was still waiting on is what makes it read as a
        # decision rather than a completion.
        self._diag(
            "search_cancelled", generation=cancelled, pending_source_count=pending
        )
        # No search_finished: the caller asked for this and already knows.
        # Nothing else may hear from the cancelled search again.
        return self._generation

    def _begin(self) -> int:
        """Number the search about to start, and forget the previous one."""
        superseded = self._generation if self._active else 0
        self._generation += 1
        self._active = False
        self._pending.clear()
        self._results.clear()
        self._failures.clear()
        # The previous search is over as far as this service is concerned, so
        # its deadline goes with it: the search about to start gets a fresh one
        # rather than inheriting whatever was left of the old window.
        self._stop_deadline()
        # A search that was still running has been replaced, which is not the
        # same thing as one the user cancelled: the log says which of the two
        # happened, and names the search that took over. A superseded search
        # never finishes, so it is never recorded as having finished either.
        if superseded:
            self._diag(
                "search_superseded",
                generation=superseded,
                superseded_by=self._generation,
            )
        return self._generation

    def _arm_deadline(self, generation: int) -> None:
        """Start the one deadline for the numbered search."""
        self._deadline.stop()
        self._deadline_generation = generation
        self._deadline.start(self._deadline_ms)

    def _stop_deadline(self) -> None:
        """Disarm the deadline, whichever search it belonged to."""
        self._deadline.stop()
        self._deadline_generation = 0

    def _on_deadline(self) -> None:
        """The timer fired: expire whichever search armed it."""
        self._expire(self._deadline_generation)

    def _expire(self, generation: int) -> None:
        """End the numbered search on the sources that never answered.

        Nothing is terminated here. The provider calls still running keep
        running to their own natural end and stay pinned until they report;
        what changes is that this search stops waiting for them, so it can
        tell the user what the sources that did answer found.
        """
        # Being active is not enough: the search that armed this deadline may
        # already have been replaced by another one that is also active, and a
        # window the user never spent must never expire on the search that
        # replaced it.
        if generation != self._generation or not self._active:
            return
        self._deadline_generation = 0

        # Several sources can run out of time in the same instant, and which
        # order they are announced in must not be whatever a set happened to
        # iterate in.
        for source_id in sorted(self._pending, key=_source_order):
            self._pending.discard(source_id)
            self._failures.append(SourceFailure(source_id, _TIMEOUT_ERROR))
            # Running out of time is its own terminal record. The summary
            # stores it as a failure, but recording it as one too would count
            # the same source out twice in a log read for what went wrong.
            self._diag(
                "source_timed_out",
                generation=generation,
                source=source_id,
                deadline_ms=self._deadline_ms,
            )
            self._emit_status(
                generation, source_id, SourceState.TIMED_OUT, _TIMEOUT_ERROR, 0
            )
            if generation != self._generation:
                # A listener replaced or cancelled the search from inside that
                # timeout. The sources still to be expired, and the summary
                # they would have gone into, belonged to a search that no
                # longer exists, so this stops here rather than finishing it.
                return

        # Whatever the sources that did answer found is exactly as valid as it
        # was a millisecond ago, so the search is summarised, never discarded.
        self._finish(generation)

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
        # wants any more still has to stop owning its threads. What it would
        # have been cached under goes with it, whether or not anything is ever
        # stored: a key nobody can use again is not kept.
        self._calls.pop((generation, source_id), None)
        cache_key = self._cache_keys.pop((generation, source_id), None)

        if generation != self._generation:
            # A superseded or cancelled search. Its worker has just been let
            # go and that is the whole of what it may do here: the current
            # search's results, failures, pending sources, signals and
            # completion are none of its business.
            return

        if source_id not in self._pending:
            # Not pending is the whole of "this search is no longer taking
            # answers from you", and it is why the generation alone is not
            # enough: a source the deadline already timed out belongs to the
            # generation that is still current, and its worker is still
            # running. Being dropped from pending is what closed it, here and
            # at the end of every search.
            #
            # Both events arrive on this one thread, so the race at the
            # deadline needs no clock: whichever of the outcome and the
            # deadline is processed first is the one that decides, and the
            # other finds this source already settled.
            return
        self._pending.discard(source_id)

        if outcome.error_kind is None:
            # Cached here and nowhere else: every guard that could reject this
            # outcome has already run, so what is stored is an answer the
            # current search actually accepted. A worker whose search was
            # superseded, cancelled or already timed out has returned above and
            # cannot reach this line, which is what keeps a rejected answer out
            # of the cache. A listener that supersedes the search from one of
            # the emits below cannot make the provider call itself any less of
            # a success, so this comes first and stands.
            if cache_key is not None:
                self._cache.put(cache_key, outcome.results)
            if not self._complete_source(generation, source_id, outcome.results):
                # A listener replaced or cancelled the search from inside one
                # of those events. Everything left to do here was this
                # search's, and this search no longer exists.
                return
        else:
            # One source is out. Its peers keep running and whatever they
            # already found stays exactly as it was, so there is no new result
            # view to publish here.
            self._failures.append(SourceFailure(source_id, outcome.error_kind))
            # The normalised kind the worker already settled on, and nothing
            # else: no exception text, no traceback and no request.
            self._diag(
                "source_failed",
                generation=generation,
                source=source_id,
                error_kind=outcome.error_kind,
            )
            self._emit_status(
                generation, source_id, SourceState.FAILED, outcome.error_kind, 0
            )
            if generation != self._generation:
                return

        if not self._pending:
            self._finish(generation)

    def _complete_source(
        self, generation: int, source_id: str, results: tuple[SearchResult, ...]
    ) -> bool:
        """Record and announce one source's successful rows.

        The one path a source succeeds by, whether a worker just answered or
        the cache answered for it: an answer that was cached must reach a
        listener as exactly the same events, in the same order, carrying the
        same merged view, or the UI would need a second lifecycle to read.

        Returns False when a listener replaced or cancelled the search from
        inside one of the events, which is the caller's signal that everything
        it still had to do belonged to a search that no longer exists.
        """
        self._results[source_id] = results
        # The one terminal record a source succeeding gets, whether a worker
        # just answered or the cache answered for it - exactly as this is the
        # one path a source succeeds by. Which of the two it was is already on
        # record as the hit or miss that preceded it.
        self._diag(
            "source_completed",
            generation=generation,
            source=source_id,
            result_count=len(results),
        )
        self._emit_status(
            generation, source_id, SourceState.COMPLETED, None, len(results)
        )
        if generation != self._generation:
            return False
        # A source that found nothing still succeeded; republishing the same
        # merged view costs nothing and keeps the rule simple.
        self.results_updated.emit(generation, self._merged().results)
        return generation == self._generation

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
        # A search that has finished cannot also run out of time, and the
        # timer is disarmed before the final event rather than after it: a
        # listener may start the next search from there, and must not have its
        # brand new deadline torn down by this one's tidying up.
        self._stop_deadline()
        # Recorded before the public event rather than after it, for the same
        # reason the timer is disarmed first: a listener may start the next
        # search from there, and this one's record must already be complete.
        self._diag(
            "search_finished",
            generation=generation,
            result_count=len(summary.results),
            dedupe_dropped=summary.dedupe_dropped,
            failure_count=len(summary.failures),
        )
        # Nothing of this search is still pinned - every worker released
        # itself as it reported - and an older search's workers are not this
        # one's to drop, so there is no call bookkeeping to do here.
        #
        # Last statement on purpose: a listener may legitimately start the
        # next search from here, and must not find this one still tidying up.
        self.search_finished.emit(summary)


# How long a source's successful answer stays worth reusing. Five minutes is a
# product judgement about indexers, not a network setting: swarm counts drift
# slowly, and a user refining a query should not re-ask the same source for the
# same words seconds apart.
_CACHE_TTL_SECONDS = 300.0

# How many answers are worth keeping at once. The cache is a small convenience,
# not a store: a hard ceiling is what keeps a long session from holding every
# result set the user ever scrolled past.
_CACHE_MAX_ENTRIES = 64


@dataclass(frozen=True)
class _CacheKey:
    """What makes one cached answer that answer and no other.

    All three parts matter: one source's answer is not another's, one
    category's is not another's, and the query is taken exactly as given.
    The generation is deliberately absent - a still-valid answer is worth
    reusing by a later search, which is the entire point.
    """

    source_id: str
    category: Category
    query: str


class _SearchCache:
    """A small bounded cache of what one source answered, kept in memory.

    It stores one source's successful rows, never a merged search, never a
    failure and never a timeout: the caller decides what is worth keeping,
    and this only decides how long and how many.
    """

    def __init__(
        self,
        max_entries: int = _CACHE_MAX_ENTRIES,
        ttl_seconds: float = _CACHE_TTL_SECONDS,
        clock=time.monotonic,
    ) -> None:
        # A cache that cannot hold a whole answer, or one whose answers are
        # born expired, is a bug rather than a policy - and a bool is a count
        # nobody meant to write.
        if isinstance(max_entries, bool) or not isinstance(max_entries, int):
            raise ValueError(f"max_entries must be an integer: {max_entries!r}")
        if max_entries < 1:
            raise ValueError(f"max_entries must be positive: {max_entries!r}")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)):
            raise ValueError(f"ttl_seconds must be a number: {ttl_seconds!r}")
        # NaN fails every comparison, so it is caught by asking for a real
        # span of time rather than by ruling it out afterwards.
        if not (0 < float(ttl_seconds) < float("inf")):
            raise ValueError(f"ttl_seconds must be a finite span: {ttl_seconds!r}")
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[_CacheKey, tuple[float, tuple[SearchResult, ...]]]
        self._entries = OrderedDict()

    def get(self, key: _CacheKey) -> tuple[SearchResult, ...] | None:
        """The rows stored for the key, or None when there are none.

        None is the one and only miss: a hit carrying no rows is a source that
        legitimately found nothing, and reads back as the empty tuple.

        Entries die where they are read - there is no sweeper - so an answer
        found too old is dropped here and reported as the miss it is.
        """
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, results = entry
        # An answer is worth its full lifetime and not a tick more: reading it
        # tells nothing about how fresh the source's rows still are, so a hit
        # never resets stored_at.
        if self._clock() - stored_at >= self._ttl_seconds:
            del self._entries[key]
            return None
        # Using an answer is what keeps it: this is the only thing a hit
        # changes, and it decides eviction order, never lifetime.
        self._entries.move_to_end(key)
        return results

    def put(self, key: _CacheKey, results: Iterable[SearchResult]) -> None:
        """Store the rows a source answered with for the key.

        Storing a key it already holds replaces the rows and starts the
        lifetime over rather than adding anything, so an update can never cost
        a neighbour its place.
        """
        self._entries[key] = (self._clock(), tuple(results))
        self._entries.move_to_end(key)
        if len(self._entries) > self._max_entries:
            # Room is made out of what nobody could have used anyway. An entry
            # that has run out of time is worth nothing however recently it was
            # read, so it goes before any still-valid neighbour does - which is
            # also the only place a lookup would never reach on its own.
            self._drop_expired()
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def _drop_expired(self) -> None:
        """Forget every entry that has outlived its time."""
        now = self._clock()
        for key in [
            key
            for key, (stored_at, _) in self._entries.items()
            if now - stored_at >= self._ttl_seconds
        ]:
            del self._entries[key]

    def clear(self) -> None:
        """Forget everything, so nothing answered before is reused."""
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
