"""How wide Search's private pool is made, and when it is made that wide.

Search bounds a whole generation with one deadline, so a source that cannot
start is spending a budget it never got to use. That makes pool capacity a
correctness property rather than a tuning knob: while the production fanout
fits under Cove's ceiling, every source of a generation has to be runnable at
once, whatever the machine's default pool width happens to be.

Every width here is set explicitly rather than read from the machine, so these
tests say the same thing on a four-thread runner as on a sixteen-thread one.
Nothing in this module runs a provider, sleeps, or races a real thread pool.
"""
import pytest
from PySide6.QtCore import QThreadPool

from cove.search import service
from cove.search.models import Category
from cove.search.sources.base import Source


@pytest.fixture
def _fresh_pool(monkeypatch):
    """Drop the module's lazy pool so a test sees a pool it created itself."""
    monkeypatch.setattr(service, "_POOL", None)
    yield


class _StubSource(Source):
    """A registry-shaped source that exists only to be counted."""

    label = "Stub"
    categories = (Category.MOVIES,)
    homepage = "https://example.invalid"
    reports_swarm = True

    def __init__(self, source_id):
        self.id = source_id

    def search(self, query, category, http):  # pragma: no cover - never run
        return []


class _FakeHttp:
    """Stands in for SearchHttp so no worker can reach the network."""

    def close(self):
        pass


class _RecordingPool:
    """A pool that records what the service asks of it, in order.

    It models the two QThreadPool behaviours this defect is about: a starting
    max-thread count that a caller can read back, and a capacity update that
    takes effect. It deliberately never runs a runnable - what is under test is
    the order the service configures and submits in, not what a worker does.
    """

    def __init__(self, width):
        self._max = width
        self.events = []

    def maxThreadCount(self):
        return self._max

    def setMaxThreadCount(self, count):
        self._max = count
        self.events.append(("configure", count))

    def start(self, runnable):
        self.events.append(("start", runnable._source_id))

    @property
    def kinds(self):
        return [kind for kind, _ in self.events]


def _fanout(monkeypatch, count):
    """Make production source selection answer with `count` sources.

    Narrower for any single category, exactly as the registry is: what has to
    fit in the pool is the widest a search can get, and a stub that answered
    every category with the same list would let a fix sized from a narrow
    category look correct.
    """
    sources = [_StubSource(f"stub{index}") for index in range(count)]

    def _sources_for(category=Category.ALL):
        if category is Category.ALL:
            return list(sources)
        return list(sources[:1])

    monkeypatch.setattr(service, "sources_for", _sources_for)
    return sources


def _width(monkeypatch, default_width, fanout):
    """The width a real pool of `default_width` is configured to under `fanout`."""
    _fanout(monkeypatch, fanout)
    pool = QThreadPool()
    pool.setMaxThreadCount(default_width)

    service._configure_pool(pool)

    return pool.maxThreadCount()


def _recording_pool(monkeypatch, default_width):
    """Install a recording pool as the one the service will lazily create."""
    pool = _RecordingPool(default_width)
    monkeypatch.setattr(service, "QThreadPool", lambda: pool)
    return pool


# --- A. the band activation exposes: 5-7 threads, eight sources ---------------


@pytest.mark.parametrize("default_width", [5, 6, 7])
def test_a_pool_narrower_than_the_fanout_is_widened_to_it(monkeypatch, default_width):
    """The whole generation must be able to start, or its deadline is a lie."""
    assert _width(monkeypatch, default_width, 8) == 8


# --- B. the same invariant on today's five-source registry -------------------


def test_a_four_thread_machine_still_starts_all_five_shipped_sources(monkeypatch):
    assert _width(monkeypatch, 4, 5) == 5


# --- C. the capacity is the fanout, never a number from this slice -----------


def test_a_ninth_source_widens_the_pool_again(monkeypatch):
    """Sized from what the registry actually holds, so growth needs no edit."""
    assert _width(monkeypatch, 4, 9) == 9


def test_the_capacity_follows_the_fanout_across_the_whole_range(monkeypatch):
    for fanout in range(1, 13):
        assert _width(monkeypatch, 4, fanout) == max(4, fanout)


# --- D. the ceiling stays authoritative --------------------------------------


def test_the_search_ceiling_is_unchanged():
    assert service._MAX_POOL_THREADS == 12


def test_a_fanout_beyond_the_ceiling_queues_rather_than_widening(monkeypatch):
    assert _width(monkeypatch, 4, 13) == 12


def test_a_wide_machine_is_still_clamped_to_the_ceiling(monkeypatch):
    assert _width(monkeypatch, 16, 8) == 12


def test_no_pairing_of_machine_and_fanout_escapes_the_ceiling(monkeypatch):
    for default_width in (1, 4, 8, 12, 16, 32):
        for fanout in (1, 5, 8, 13, 40):
            assert _width(monkeypatch, default_width, fanout) <= 12


# --- E. capacity is the minimum needed, not the ceiling ----------------------


def test_a_machine_that_already_fits_the_fanout_is_left_alone(monkeypatch):
    assert _width(monkeypatch, 8, 8) == 8


def test_a_machine_wider_than_the_fanout_keeps_its_own_width(monkeypatch):
    """Search widens a pool to what it needs and never past it."""
    assert _width(monkeypatch, 10, 8) == 10


# --- F. capacity is configured before any worker is submitted ----------------


def test_the_pool_is_configured_before_the_first_worker_is_submitted(
    monkeypatch, _fresh_pool
):
    """Resizing after submission would leave the queued sources queued."""
    pool = _recording_pool(monkeypatch, 5)
    _fanout(monkeypatch, 8)
    svc = service.SearchService(http_factory=_FakeHttp)

    svc.start("dune")

    assert pool.kinds.index("configure") < pool.kinds.index("start")
    assert pool.events[0] == ("configure", 8)
    svc.cancel()


# --- G. an eight-source generation on a five-thread machine ------------------


def test_every_source_of_an_eight_source_search_can_start_at_once(
    monkeypatch, _fresh_pool
):
    """The activation case: eight submissions, eight runnable slots."""
    pool = _recording_pool(monkeypatch, 5)
    _fanout(monkeypatch, 8)
    svc = service.SearchService(http_factory=_FakeHttp)

    svc.start("dune")

    assert pool.kinds.count("start") == 8
    assert pool.maxThreadCount() >= 8
    svc.cancel()


# --- H. the lazy pool reads the registry when it is built, not at import -----


def test_the_lazy_pool_sizes_itself_from_the_registry_it_finds(
    monkeypatch, _fresh_pool
):
    """A registry that grows after import must still get the width it needs."""
    _recording_pool(monkeypatch, 5)
    _fanout(monkeypatch, 8)

    assert service._pool().maxThreadCount() == 8


def test_the_lazy_pool_is_still_one_pool(monkeypatch, _fresh_pool):
    _recording_pool(monkeypatch, 5)
    _fanout(monkeypatch, 8)

    assert service._pool() is service._pool()


# --- I. characterization: what the widening does not buy ---------------------


def test_a_second_search_gets_no_pool_of_its_own(monkeypatch, _fresh_pool):
    """Characterization of a pre-existing limit, deliberately left alone.

    Superseding suppresses a search without terminating it (SearchService),
    so the previous search's workers still hold their threads and the new
    one can still queue behind them. The pool is sized for one search on an
    idle pool, and is not resized per generation: fixing the overlap means
    abandoning a runnable Qt still owns or paying for every live search at
    once, both of which are changes to cancellation, not to capacity.
    """
    pool = _recording_pool(monkeypatch, 5)
    _fanout(monkeypatch, 8)
    svc = service.SearchService(http_factory=_FakeHttp)

    svc.start("dune")
    svc.start("arrakis")

    assert [count for kind, count in pool.events if kind == "configure"] == [8]
    assert pool.kinds.count("start") == 16
    svc.cancel()


# --- J. characterization: nothing about the deadline moves -------------------


def test_the_search_deadline_is_what_it_was(_fresh_pool):
    """Characterization. Widening the pool is not a change to Cove's patience."""
    assert service._SEARCH_DEADLINE_MS == 30_000


def test_sizing_the_search_pool_leaves_the_global_pool_alone(monkeypatch, _fresh_pool):
    """Characterization. The widening is Search's own pool, never Qt's."""
    before = QThreadPool.globalInstance().maxThreadCount()

    _width(monkeypatch, 4, 12)

    assert QThreadPool.globalInstance().maxThreadCount() == before
