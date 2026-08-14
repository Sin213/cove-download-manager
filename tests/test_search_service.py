"""Deterministic combination of results arriving from several sources.

This layer is pure on purpose. Sources will later answer concurrently, so the
merged list must depend only on the content of the rows, never on which source
happened to finish first - these tests pin that, plus the duplicate-winner,
backfill and ordering rules the UI reads.
"""
import threading
import time
from collections import Counter

import pytest
from PySide6.QtCore import Qt, QCoreApplication, QRunnable, QThread, QThreadPool

from cove.search import service
from cove.search.magnet import build_magnet
from cove.search.models import Category, SearchResult, SourceError, SourceErrorKind
from cove.search.registry import SOURCES
from cove.search.service import aggregate
from cove.search.sources.base import SearchHttp, Source

# Every wait in this module is bounded: a broken implementation must fail the
# suite, not hang it.
_WAIT_SECONDS = 5.0


@pytest.fixture
def _fresh_pool(monkeypatch):
    """Drop the module's lazy pool so a test sees a pool it created itself.

    Pool configuration is a property of creation, so a test that asserts it
    must not inherit whatever an earlier test left in the module global.
    """
    monkeypatch.setattr(service, "_POOL", None)
    yield


def _hash(marker: str) -> str:
    """A canonical 40-hex info hash keyed by a short marker."""
    return (marker * 40)[:40]


A = _hash("a")
B = _hash("b")
C = _hash("c")
D = _hash("d")


def _result(
    info_hash=A,
    name="Example",
    source="yts",
    seeders=5,
    leechers=1,
    size_bytes=1024,
    added=1700000000,
):
    return SearchResult(
        info_hash=info_hash,
        name=name,
        magnet=build_magnet(info_hash, name),
        size_bytes=size_bytes,
        seeders=seeders,
        leechers=leechers,
        added=added,
        source=source,
    )


# --- A. basic dedupe ---------------------------------------------------------


def test_distinct_hashes_are_all_kept():
    rows = [_result(info_hash=A), _result(info_hash=B), _result(info_hash=C)]

    merged = aggregate(rows)

    assert {r.info_hash for r in merged.results} == {A, B, C}


def test_same_hash_collapses_to_one_row():
    rows = [_result(info_hash=A, source="yts"), _result(info_hash=A, source="nyaa")]

    merged = aggregate(rows)

    assert [r.info_hash for r in merged.results] == [A]


def test_empty_input_gives_an_empty_aggregation():
    merged = aggregate([])

    assert merged.results == ()
    assert merged.dedupe_dropped == 0


def test_aggregate_does_not_mutate_its_input():
    rows = [_result(info_hash=A, size_bytes=None), _result(info_hash=A, source="nyaa")]
    before = list(rows)

    aggregate(rows)

    assert rows == before
    assert rows[0].size_bytes is None


# --- B. duplicate winner rules ----------------------------------------------


def test_higher_seeder_row_wins_regardless_of_registry_order():
    rows = [
        _result(info_hash=A, source="yts", seeders=10, name="From YTS"),
        _result(info_hash=A, source="nyaa", seeders=50, name="From Nyaa"),
    ]

    (winner,) = aggregate(rows).results

    assert winner.source == "nyaa"
    assert winner.seeders == 50
    assert winner.name == "From Nyaa"


def test_registry_order_breaks_a_seeder_tie():
    assert [s.id for s in SOURCES] == [
        "yts",
        "piratebay",
        "nyaa",
        "fitgirl",
        "subsplease",
    ]

    rows = [
        _result(info_hash=A, source="piratebay", seeders=7),
        _result(info_hash=A, source="yts", seeders=7),
    ]

    (winner,) = aggregate(rows).results

    assert winner.source == "yts"


def test_nyaa_keeps_its_precedence_over_the_newer_anime_source():
    """Adding SubsPlease must not change which anime row wins a tie."""
    rows = [
        _result(info_hash=A, source="subsplease", seeders=7, name="From SubsPlease"),
        _result(info_hash=A, source="nyaa", seeders=7, name="From Nyaa"),
    ]

    (winner,) = aggregate(rows).results

    assert winner.source == "nyaa"
    assert winner.name == "From Nyaa"


def test_registry_order_breaks_a_seeder_tie_further_down_the_registry():
    rows = [
        _result(info_hash=A, source="nyaa", seeders=7),
        _result(info_hash=A, source="piratebay", seeders=7),
    ]

    (winner,) = aggregate(rows).results

    assert winner.source == "piratebay"


def test_same_source_seeder_tie_keeps_the_first_occurrence():
    rows = [
        _result(info_hash=A, source="yts", seeders=7, name="First seen"),
        _result(info_hash=A, source="yts", seeders=7, name="Second seen"),
    ]

    (winner,) = aggregate(rows).results

    assert winner.name == "First seen"


# --- C. optional metadata backfill ------------------------------------------


def test_missing_optional_fields_are_backfilled_from_a_duplicate():
    rows = [
        _result(
            info_hash=A,
            source="nyaa",
            seeders=50,
            name="Winner",
            size_bytes=None,
            added=None,
            leechers=3,
        ),
        _result(
            info_hash=A,
            source="yts",
            seeders=1,
            name="Loser",
            size_bytes=4096,
            added=1600000000,
        ),
    ]

    (winner,) = aggregate(rows).results

    assert winner.size_bytes == 4096
    assert winner.added == 1600000000
    assert winner.name == "Winner"
    assert winner.magnet == build_magnet(A, "Winner")
    assert winner.seeders == 50
    assert winner.leechers == 3
    assert winner.source == "nyaa"


def test_existing_winner_metadata_is_never_overwritten():
    rows = [
        _result(
            info_hash=A,
            source="nyaa",
            seeders=50,
            size_bytes=1024,
            added=1700000000,
        ),
        _result(
            info_hash=A,
            source="yts",
            seeders=1,
            size_bytes=4096,
            added=1600000000,
        ),
    ]

    (winner,) = aggregate(rows).results

    assert winner.size_bytes == 1024
    assert winner.added == 1700000000


def test_backfill_donor_follows_registry_priority_not_arrival_order():
    winner = _result(
        info_hash=A, source="nyaa", seeders=99, size_bytes=None, added=None
    )
    from_piratebay = _result(
        info_hash=A, source="piratebay", seeders=1, size_bytes=2048, added=1500000000
    )
    from_yts = _result(
        info_hash=A, source="yts", seeders=1, size_bytes=4096, added=1600000000
    )

    for rows in (
        [winner, from_piratebay, from_yts],
        [from_yts, winner, from_piratebay],
        [from_piratebay, from_yts, winner],
    ):
        (merged,) = aggregate(rows).results
        assert merged.source == "nyaa"
        assert merged.size_bytes == 4096
        assert merged.added == 1600000000


def test_backfill_takes_each_field_from_the_first_donor_that_has_it():
    rows = [
        _result(info_hash=A, source="nyaa", seeders=99, size_bytes=None, added=None),
        _result(info_hash=A, source="yts", seeders=1, size_bytes=None, added=1600000000),
        _result(
            info_hash=A, source="piratebay", seeders=1, size_bytes=2048, added=1500000000
        ),
    ]

    (winner,) = aggregate(rows).results

    assert winner.size_bytes == 2048
    assert winner.added == 1600000000


def test_optional_fields_stay_none_when_no_duplicate_has_them():
    rows = [
        _result(info_hash=A, source="nyaa", seeders=50, size_bytes=None, added=None),
        _result(info_hash=A, source="yts", seeders=1, size_bytes=None, added=None),
    ]

    (winner,) = aggregate(rows).results

    assert winner.size_bytes is None
    assert winner.added is None


# --- D. deterministic total ordering ----------------------------------------


def test_results_are_sorted_by_seeders_descending():
    rows = [
        _result(info_hash=A, seeders=1),
        _result(info_hash=B, seeders=9),
        _result(info_hash=C, seeders=5),
    ]

    merged = aggregate(rows)

    assert [r.seeders for r in merged.results] == [9, 5, 1]


def test_equal_seeders_sort_by_added_descending():
    rows = [
        _result(info_hash=A, seeders=5, added=100),
        _result(info_hash=B, seeders=5, added=300),
        _result(info_hash=C, seeders=5, added=200),
    ]

    merged = aggregate(rows)

    assert [r.added for r in merged.results] == [300, 200, 100]


def test_rows_without_an_added_date_sort_last_within_their_seeder_group():
    rows = [
        _result(info_hash=A, seeders=5, added=None),
        _result(info_hash=B, seeders=5, added=100),
        _result(info_hash=C, seeders=9, added=None),
    ]

    merged = aggregate(rows)

    assert [(r.seeders, r.added) for r in merged.results] == [
        (9, None),
        (5, 100),
        (5, None),
    ]


def test_equal_seeders_and_added_sort_by_name_case_insensitively():
    rows = [
        _result(info_hash=A, seeders=5, added=100, name="banana"),
        _result(info_hash=B, seeders=5, added=100, name="Apple"),
        _result(info_hash=C, seeders=5, added=100, name="cherry"),
    ]

    merged = aggregate(rows)

    assert [r.name for r in merged.results] == ["Apple", "banana", "cherry"]


def test_identical_names_fall_back_to_info_hash_ascending():
    rows = [
        _result(info_hash=C, seeders=5, added=100, name="Same"),
        _result(info_hash=A, seeders=5, added=100, name="Same"),
        _result(info_hash=B, seeders=5, added=100, name="Same"),
    ]

    merged = aggregate(rows)

    assert [r.info_hash for r in merged.results] == [A, B, C]


# --- E. arrival-order independence and unknown sources ----------------------


def test_result_is_identical_whatever_order_the_sources_finish_in():
    from_yts = [
        _result(info_hash=A, source="yts", seeders=10, name="Shared"),
        _result(info_hash=B, source="yts", seeders=3, name="Yts only"),
    ]
    from_piratebay = [
        _result(info_hash=A, source="piratebay", seeders=10, name="Shared"),
        _result(info_hash=C, source="piratebay", seeders=3, name="Bay only", added=None),
    ]
    from_nyaa = [
        _result(info_hash=D, source="nyaa", seeders=3, name="Anime only"),
        _result(info_hash=A, source="nyaa", seeders=4, name="Shared"),
    ]

    orders = [
        from_yts + from_piratebay + from_nyaa,
        from_nyaa + from_yts + from_piratebay,
        from_piratebay + from_nyaa + from_yts,
        from_nyaa + from_piratebay + from_yts,
    ]
    merged = [aggregate(rows) for rows in orders]

    assert all(m.results == merged[0].results for m in merged)
    assert all(m.dedupe_dropped == merged[0].dedupe_dropped for m in merged)
    assert [r.source for r in merged[0].results if r.info_hash == A] == ["yts"]


def test_an_unknown_source_id_does_not_crash():
    rows = [_result(info_hash=A, source="somewhere-else")]

    (only,) = aggregate(rows).results

    assert only.source == "somewhere-else"


def test_a_known_source_beats_an_unknown_one_on_a_seeder_tie():
    rows = [
        _result(info_hash=A, source="aaa-unknown", seeders=7),
        _result(info_hash=A, source="nyaa", seeders=7),
    ]

    (winner,) = aggregate(rows).results

    assert winner.source == "nyaa"


def test_two_unknown_sources_break_a_tie_on_source_id():
    rows = [
        _result(info_hash=A, source="zebra"),
        _result(info_hash=A, source="aardvark"),
    ]

    (winner,) = aggregate(rows).results

    assert winner.source == "aardvark"


def test_unknown_source_backfill_donor_order_is_deterministic():
    rows = [
        _result(info_hash=A, source="nyaa", seeders=99, size_bytes=None, added=None),
        _result(info_hash=A, source="zebra", seeders=1, size_bytes=1, added=1),
        _result(info_hash=A, source="aardvark", seeders=1, size_bytes=2, added=2),
    ]

    (winner,) = aggregate(rows).results

    assert winner.size_bytes == 2
    assert winner.added == 2


# --- F. dedupe count ---------------------------------------------------------


def test_dedupe_count_is_zero_when_nothing_is_duplicated():
    rows = [_result(info_hash=A), _result(info_hash=B)]

    assert aggregate(rows).dedupe_dropped == 0


def test_dedupe_count_reports_every_dropped_row():
    rows = [
        _result(info_hash=A, source="yts"),
        _result(info_hash=A, source="piratebay"),
        _result(info_hash=A, source="nyaa"),
        _result(info_hash=B, source="yts"),
        _result(info_hash=C, source="yts"),
    ]

    merged = aggregate(rows)

    assert len(merged.results) == 3
    assert merged.dedupe_dropped == 2


# --- G. the private Search thread pool ---------------------------------------


def test_search_pool_is_a_thread_pool_of_its_own():
    pool = service._pool()

    assert isinstance(pool, QThreadPool)
    assert pool is not QThreadPool.globalInstance()


def test_repeated_pool_calls_return_the_same_private_pool(_fresh_pool):
    assert service._pool() is service._pool()


def test_a_wide_pool_is_clamped_to_the_search_ceiling():
    pool = QThreadPool()
    pool.setMaxThreadCount(32)

    service._configure_pool(pool)

    assert pool.maxThreadCount() == service._MAX_POOL_THREADS


def test_a_pool_just_above_the_ceiling_is_clamped():
    pool = QThreadPool()
    pool.setMaxThreadCount(16)

    service._configure_pool(pool)

    assert pool.maxThreadCount() == 12


def test_a_narrow_pool_is_left_alone(_fresh_pool):
    pool = QThreadPool()
    pool.setMaxThreadCount(4)

    service._configure_pool(pool)

    assert pool.maxThreadCount() == 4


def test_a_pool_at_eight_is_not_widened_towards_the_ceiling():
    pool = QThreadPool()
    pool.setMaxThreadCount(8)

    service._configure_pool(pool)

    assert pool.maxThreadCount() == 8


def test_the_lazy_pool_is_created_within_the_ceiling(_fresh_pool):
    width = service._pool().maxThreadCount()

    assert 0 < width <= service._MAX_POOL_THREADS


def test_configuring_the_search_pool_leaves_the_global_pool_alone(_fresh_pool):
    before = QThreadPool.globalInstance().maxThreadCount()

    service._pool()
    wide = QThreadPool()
    wide.setMaxThreadCount(32)
    service._configure_pool(wide)

    assert QThreadPool.globalInstance().maxThreadCount() == before


# --- H. the ceiling bounds real execution ------------------------------------


def test_no_more_runnables_run_at_once_than_the_pool_allows(_fresh_pool):
    """The cap has to bound running workers, not just a numeric property."""
    pool = service._pool()
    width = pool.maxThreadCount()
    release = threading.Event()
    lock = threading.Lock()
    state = {"active": 0, "peak": 0, "entered": 0}

    class _Blocker(QRunnable):
        def run(self):
            with lock:
                state["entered"] += 1
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            release.wait(_WAIT_SECONDS)
            with lock:
                state["active"] -= 1

    try:
        for _ in range(width + 1):
            pool.start(_Blocker())
        deadline = time.monotonic() + _WAIT_SECONDS
        while time.monotonic() < deadline:
            with lock:
                if state["entered"] >= width:
                    break
            time.sleep(0.01)
        with lock:
            assert state["entered"] == width, "pool never reached its own width"
            assert state["peak"] <= width <= service._MAX_POOL_THREADS
    finally:
        release.set()
        assert pool.waitForDone(int(_WAIT_SECONDS * 1000))

    assert state["peak"] <= width


# --- I. one source call, one terminal outcome --------------------------------


class _FakeHttp:
    """Stands in for SearchHttp, with only what the worker itself touches."""

    def __init__(self):
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class _FakeSource(Source):
    """A real Source whose one search answers however the test wants."""

    id = "fake"
    label = "Fake"
    categories = (Category.MOVIES,)
    homepage = "https://example.invalid"
    reports_swarm = True

    def __init__(self, rows=None, raises=None, *, source_id=None, on_search=None):
        self._rows = rows if rows is not None else []
        self._raises = raises
        self._on_search = on_search
        if source_id is not None:
            self.id = source_id
        self.calls = []

    def search(self, query, category, http):
        self.calls.append((query, category, http))
        if self._on_search is not None:
            # Lets a test hold a source inside search() long enough to observe
            # what the service does while it is still running.
            self._on_search()
        if self._raises is not None:
            raise self._raises
        return list(self._rows)


def _queries(source) -> Counter:
    """How many times `source` was asked each query.

    Two generations can be in flight at once, and each worker appends to
    `calls` from its own pool thread, so which entry lands first is the
    scheduler's choice and carries no meaning. The query a worker was handed
    is the request's own identity, and counting rather than ordering keeps a
    missing call - or a duplicate one, which would be a real regression - just
    as visible as the list comparison it replaces.

    This is not the calls in execution order, and must not be read as such.
    """
    return Counter(call[0] for call in source.calls)


def _collect(call) -> list:
    """Every outcome `call` reports, in order."""
    seen = []
    call.signals.finished.connect(seen.append)
    return seen


def _run_on_pool(call, seen) -> None:
    """Start `call` on the private pool and pump Qt until its outcome lands."""
    pool = service._pool()
    pool.start(call)
    deadline = time.monotonic() + _WAIT_SECONDS
    while time.monotonic() < deadline and not seen:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    assert pool.waitForDone(int(_WAIT_SECONDS * 1000))
    QCoreApplication.processEvents()


def test_a_source_call_reports_the_rows_the_source_returned():
    rows = [_result(info_hash=A), _result(info_hash=B)]
    call = service._SourceCall(_FakeSource(rows), "dune", Category.MOVIES, generation=1, http_factory=_FakeHttp)
    seen = _collect(call)

    call.run()

    assert len(seen) == 1
    assert seen[0].source_id == "fake"
    assert seen[0].results == tuple(rows)
    assert seen[0].error_kind is None


def test_a_source_call_delivers_its_outcome_through_the_pool(_fresh_pool):
    call = service._SourceCall(
        _FakeSource([_result()]), "dune", Category.MOVIES, generation=1, http_factory=_FakeHttp
    )
    seen = _collect(call)

    _run_on_pool(call, seen)

    assert len(seen) == 1
    assert seen[0].results == (_result(),)


def test_a_source_call_passes_the_query_and_category_to_the_source():
    source = _FakeSource([])
    http = _FakeHttp()
    call = service._SourceCall(
        source, "akira", Category.ANIME, generation=1, http_factory=lambda: http
    )

    call.run()

    assert source.calls == [("akira", Category.ANIME, http)]


def test_an_empty_source_answer_is_a_success():
    call = service._SourceCall(_FakeSource([]), "dune", Category.MOVIES, generation=1, http_factory=_FakeHttp)
    seen = _collect(call)

    call.run()

    assert len(seen) == 1
    assert seen[0].results == ()
    assert seen[0].error_kind is None


def test_a_source_error_becomes_one_failed_outcome_carrying_its_kind():
    source = _FakeSource(raises=SourceError(SourceErrorKind.TIMEOUT, "too slow"))
    call = service._SourceCall(source, "dune", Category.MOVIES, generation=1, http_factory=_FakeHttp)
    seen = _collect(call)

    call.run()

    assert len(seen) == 1
    assert seen[0].error_kind == SourceErrorKind.TIMEOUT.value
    assert seen[0].results == ()


def test_every_source_error_kind_survives_as_the_outcome_kind():
    for kind in SourceErrorKind:
        call = service._SourceCall(
            _FakeSource(raises=SourceError(kind)), "dune", Category.MOVIES, generation=1, http_factory=_FakeHttp
        )
        seen = _collect(call)

        call.run()

        assert [outcome.error_kind for outcome in seen] == [kind.value]


def test_an_unexpected_source_exception_becomes_one_internal_failure():
    source = _FakeSource(raises=RuntimeError("boom"))
    call = service._SourceCall(source, "dune", Category.MOVIES, generation=1, http_factory=_FakeHttp)
    seen = _collect(call)

    call.run()

    assert len(seen) == 1
    assert seen[0].error_kind == service._INTERNAL_ERROR
    assert seen[0].results == ()


def test_an_unexpected_exception_does_not_escape_a_pooled_worker(_fresh_pool):
    call = service._SourceCall(
        _FakeSource(raises=RuntimeError("boom")), "dune", Category.MOVIES, generation=1, http_factory=_FakeHttp
    )
    seen = _collect(call)

    _run_on_pool(call, seen)

    assert [outcome.error_kind for outcome in seen] == [service._INTERNAL_ERROR]


def test_an_outcome_is_immutable():
    call = service._SourceCall(_FakeSource([]), "dune", Category.MOVIES, generation=1, http_factory=_FakeHttp)
    seen = _collect(call)

    call.run()

    with pytest.raises(Exception):
        seen[0].source_id = "other"


# --- J. the worker owns one HTTP facility per call ---------------------------


def test_the_owned_http_is_closed_after_a_successful_call():
    http = _FakeHttp()
    call = service._SourceCall(
        _FakeSource([_result()]), "dune", Category.MOVIES, generation=1, http_factory=lambda: http
    )

    call.run()

    assert http.closed == 1


def test_the_owned_http_is_closed_after_a_source_error():
    http = _FakeHttp()
    call = service._SourceCall(
        _FakeSource(raises=SourceError(SourceErrorKind.NETWORK)),
        "dune",
        Category.MOVIES,
        generation=1,
        http_factory=lambda: http,
    )

    call.run()

    assert http.closed == 1


def test_the_owned_http_is_closed_after_an_unexpected_exception():
    http = _FakeHttp()
    call = service._SourceCall(
        _FakeSource(raises=RuntimeError("boom")),
        "dune",
        Category.MOVIES,
        generation=1,
        http_factory=lambda: http,
    )

    call.run()

    assert http.closed == 1


def test_a_failing_close_still_leaves_exactly_one_outcome():
    class _RudeHttp(_FakeHttp):
        def close(self):
            super().close()
            raise RuntimeError("close failed")

    call = service._SourceCall(_FakeSource([]), "dune", Category.MOVIES, generation=1, http_factory=_RudeHttp)
    seen = _collect(call)

    call.run()

    assert len(seen) == 1


def test_a_call_builds_a_real_search_http_by_default():
    """The production path is the default one, so a test has to walk it.

    SearchHttp opens no session until it is asked for one, so a source that
    never touches it keeps this off the network entirely.
    """
    source = _FakeSource([])
    call = service._SourceCall(source, "dune", Category.MOVIES, generation=1)
    seen = _collect(call)

    call.run()

    assert isinstance(source.calls[0][2], SearchHttp)
    assert [outcome.error_kind for outcome in seen] == [None]


# --- J. lifecycle helpers ----------------------------------------------------


class _Watch:
    """Everything one service told its listeners, in the order it said it."""

    def __init__(self, svc):
        self.statuses = []
        self.results = []
        self.result_generations = []
        self.finished = []
        svc.source_status.connect(self.statuses.append)
        svc.results_updated.connect(self._on_results)
        svc.search_finished.connect(self.finished.append)

    def _on_results(self, generation, results):
        self.result_generations.append(generation)
        self.results.append(results)

    @property
    def order(self):
        return [(status.source_id, status.state) for status in self.statuses]

    def states(self, source_id):
        return [
            status.state for status in self.statuses if status.source_id == source_id
        ]


def _counting_http_factory():
    """An http factory plus the list of facilities it has been asked for."""
    made = []

    def factory():
        http = _FakeHttp()
        made.append(http)
        return http

    return factory, made


def _select(monkeypatch, sources):
    """Replace source selection with the given fakes, recording the category."""
    asked = []

    def _sources_for(category=Category.ALL):
        asked.append(category)
        return list(sources)

    monkeypatch.setattr(service, "sources_for", _sources_for)
    return asked


def _pump(predicate, seconds=_WAIT_SECONDS):
    """Run the Qt event loop until the predicate holds. Always bounded."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    QCoreApplication.processEvents()
    return predicate()


def _finish(watch):
    """Wait for exactly one completion, and return its summary."""
    assert _pump(lambda: bool(watch.finished)), "search never finished"
    assert len(watch.finished) == 1
    return watch.finished[0]


# --- K. searches that never reach a source -----------------------------------


def test_a_whitespace_query_finishes_without_selecting_any_source(monkeypatch):
    asked = _select(monkeypatch, [_FakeSource([_result()])])
    svc = service.SearchService()
    watch = _Watch(svc)

    svc.start("   ")

    summary = _finish(watch)
    assert summary.results == ()
    assert summary.failures == ()
    assert watch.statuses == []
    assert asked == []


def test_a_whitespace_query_leaves_the_service_inactive(monkeypatch):
    _select(monkeypatch, [_FakeSource([_result()])])
    svc = service.SearchService()

    svc.start("\t\n ")

    assert svc.active is False


def test_a_search_no_source_covers_finishes_empty(monkeypatch):
    """A selection that yields nothing must finish, not hang.

    The empty inventory is stated here rather than borrowed from a real
    category: which categories the registry happens to cover changes as
    sources are added, and this lifecycle rule does not.
    """
    _select(monkeypatch, [])
    factory, made = _counting_http_factory()
    svc = service.SearchService(http_factory=factory)
    watch = _Watch(svc)

    svc.start("halo", Category.GAMES)

    summary = _finish(watch)
    assert summary.results == ()
    assert summary.dedupe_dropped == 0
    assert summary.failures == ()
    assert watch.statuses == []
    assert made == [], "a source ran for a category no source covers"
    assert svc.active is False


# --- K2. the shipped registry, reached the ordinary way -----------------------


def test_a_games_search_reaches_the_registered_fitgirl_source(monkeypatch):
    """Registration is what makes a Games search work - nothing source-specific.

    Selection is the real one here: the registry is not replaced, and the
    source asked is the very object the registry ships. Only its search is
    stood in for, so the call is recorded instead of leaving the machine.
    """
    (fitgirl,) = [source for source in SOURCES if source.id == "fitgirl"]
    asked = []

    def _search(query, category, http):
        asked.append((query, category))
        return [_result(info_hash=A, name="Example Repack", source="fitgirl")]

    monkeypatch.setattr(fitgirl, "search", _search)
    # Belt and braces: reaching the seam above is the point, so a real request
    # from any source fails this test rather than travelling.
    monkeypatch.setattr(
        SearchHttp,
        "get_bytes",
        lambda *a, **k: pytest.fail("a source made a real request"),
    )
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("  halo  ", Category.GAMES)

    summary = _finish(watch)
    # The normalised query, the category as asked, exactly once.
    assert asked == [("halo", Category.GAMES)]
    assert [(r.source, r.name) for r in summary.results] == [
        ("fitgirl", "Example Repack")
    ]
    assert summary.failures == ()
    assert watch.states("fitgirl") == [
        service.SourceState.RUNNING,
        service.SourceState.COMPLETED,
    ]
    assert svc.active is False


def test_an_anime_search_reaches_both_registered_anime_sources(monkeypatch):
    """Generic selection hands an Anime search to Nyaa and SubsPlease alike.

    Selection is the real one: the registry is not replaced, and the sources
    asked are the very objects it ships. Only their searches are stood in for.
    """
    anime = {
        source.id: source
        for source in SOURCES
        if source.id in ("nyaa", "subsplease")
    }
    assert sorted(anime) == ["nyaa", "subsplease"]
    asked = []

    def _stub(source_id):
        def _search(query, category, http):
            asked.append((source_id, query, category))
            return [_result(info_hash=A, name=f"From {source_id}", source=source_id)]

        return _search

    for source_id, source in anime.items():
        monkeypatch.setattr(source, "search", _stub(source_id))
    # Belt and braces: reaching the seams above is the point, so a real request
    # from any source fails this test rather than travelling.
    monkeypatch.setattr(
        SearchHttp,
        "get_bytes",
        lambda *a, **k: pytest.fail("a source made a real request"),
    )
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("  bleach  ", Category.ANIME)

    summary = _finish(watch)
    # The normalised query, the category as asked, once per source.
    assert sorted(asked) == [
        ("nyaa", "bleach", Category.ANIME),
        ("subsplease", "bleach", Category.ANIME),
    ]
    # One info hash, so the two rows merge and Nyaa's precedence decides.
    assert [(r.source, r.name) for r in summary.results] == [("nyaa", "From nyaa")]
    assert summary.failures == ()
    for source_id in ("nyaa", "subsplease"):
        assert watch.states(source_id) == [
            service.SourceState.RUNNING,
            service.SourceState.COMPLETED,
        ], source_id
    assert svc.active is False


# --- L. one source, start to finish ------------------------------------------


def test_a_single_source_search_runs_running_then_completed(monkeypatch):
    rows = [_result(info_hash=A, source="alpha")]
    _select(monkeypatch, [_FakeSource(rows, source_id="alpha")])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("dune")

    _finish(watch)
    assert watch.order == [
        ("alpha", service.SourceState.RUNNING),
        ("alpha", service.SourceState.COMPLETED),
    ]
    assert watch.statuses[-1].result_count == 1
    assert watch.statuses[-1].error_kind is None


def test_a_single_source_search_publishes_and_summarises_its_results(monkeypatch):
    rows = [_result(info_hash=A, source="alpha"), _result(info_hash=B, source="alpha")]
    _select(monkeypatch, [_FakeSource(rows, source_id="alpha")])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("dune")

    summary = _finish(watch)
    assert watch.results == [aggregate(rows).results]
    assert summary.results == aggregate(rows).results
    assert summary.failures == ()
    assert svc.active is False


def test_the_default_category_is_every_source(monkeypatch):
    """start() without a category has to mean ALL, all the way to the source."""
    source = _FakeSource([], source_id="alpha")
    asked = _select(monkeypatch, [source])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("  dune  ")

    _finish(watch)
    assert asked == [Category.ALL]
    assert [call[:2] for call in source.calls] == [("dune", Category.ALL)]


def test_an_empty_source_answer_completes_that_source(monkeypatch):
    _select(monkeypatch, [_FakeSource([], source_id="alpha")])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("dune")

    summary = _finish(watch)
    assert watch.states("alpha") == [
        service.SourceState.RUNNING,
        service.SourceState.COMPLETED,
    ]
    assert watch.statuses[-1].result_count == 0
    assert summary.failures == ()


def test_a_finished_search_keeps_no_worker_pinned(monkeypatch):
    _select(monkeypatch, [_FakeSource([_result()], source_id="alpha")])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("dune")

    _finish(watch)
    assert svc._calls == {}
    assert svc._pending == set()


def test_the_source_runs_off_the_owning_thread_and_the_service_answers_on_it(
    monkeypatch,
):
    """The provider must not run on the owning thread, and state must not
    change off it: both halves are asserted through Qt thread identity."""
    ran_on = []
    handled_on = []
    source = _FakeSource(
        [_result()],
        source_id="alpha",
        on_search=lambda: ran_on.append(QThread.currentThread()),
    )
    _select(monkeypatch, [source])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)
    svc.source_status.connect(lambda _s: handled_on.append(QThread.currentThread()))

    svc.start("dune")

    _finish(watch)
    owner = svc.thread()
    assert owner is QThread.currentThread()
    assert ran_on and ran_on[0] is not owner, "the source ran on the owning thread"
    # RUNNING is emitted from start(); the second status can only come from the
    # outcome handler, so its thread is the handler's thread.
    assert handled_on == [owner, owner]


# --- M. real concurrent fan-out ----------------------------------------------


def test_two_sources_are_inside_search_at_the_same_time(monkeypatch):
    """Overlap is proved by a barrier: neither source can leave alone."""
    barrier = threading.Barrier(2)
    sources = [
        _FakeSource(
            [_result(info_hash=A, source="alpha")],
            source_id="alpha",
            on_search=lambda: barrier.wait(_WAIT_SECONDS),
        ),
        _FakeSource(
            [_result(info_hash=B, source="beta")],
            source_id="beta",
            on_search=lambda: barrier.wait(_WAIT_SECONDS),
        ),
    ]
    _select(monkeypatch, sources)
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("dune")

    summary = _finish(watch)
    assert summary.failures == (), "the barrier broke, so the sources never overlapped"
    assert {status.source_id for status in watch.statuses} == {"alpha", "beta"}
    assert len(summary.results) == 2


def test_every_source_is_announced_running_before_any_of_them_finishes(monkeypatch):
    sources = [
        _FakeSource([], source_id="alpha"),
        _FakeSource([], source_id="beta"),
    ]
    _select(monkeypatch, sources)
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("dune")

    _finish(watch)
    states = [state for _, state in watch.order]
    assert states[:2] == [service.SourceState.RUNNING] * 2
    assert set(states[2:]) == {service.SourceState.COMPLETED}


# --- N. incremental aggregation ----------------------------------------------


def test_results_grow_as_each_source_lands_and_always_come_from_aggregate(
    monkeypatch,
):
    """A returns at once; B is held until A's merge has been published."""
    first_landed = threading.Event()
    rows_a = [_result(info_hash=A, source="alpha", seeders=9)]
    rows_b = [_result(info_hash=B, source="beta", seeders=3)]
    sources = [
        _FakeSource(rows_a, source_id="alpha"),
        _FakeSource(
            rows_b,
            source_id="beta",
            on_search=lambda: first_landed.wait(_WAIT_SECONDS),
        ),
    ]
    _select(monkeypatch, sources)
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("dune")

    assert _pump(lambda: bool(watch.results)), "the first source never published"
    assert watch.results[0] == aggregate(rows_a).results
    first_landed.set()

    summary = _finish(watch)
    assert watch.results[-1] == aggregate(rows_a + rows_b).results
    assert summary.results == aggregate(rows_a + rows_b).results


def test_a_duplicate_across_two_sources_merges_exactly_as_aggregate_would(
    monkeypatch,
):
    rows_a = [_result(info_hash=A, source="alpha", seeders=2, size_bytes=None)]
    rows_b = [_result(info_hash=A, source="beta", seeders=40, size_bytes=None, added=None)]
    _select(
        monkeypatch,
        [
            _FakeSource(rows_a, source_id="alpha"),
            _FakeSource(rows_b, source_id="beta"),
        ],
    )
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("dune")

    summary = _finish(watch)
    expected = aggregate(rows_a + rows_b)
    assert summary.results == expected.results
    assert summary.dedupe_dropped == expected.dedupe_dropped == 1


# --- O. one source failing takes only itself out ------------------------------


def test_a_source_error_leaves_its_healthy_peer_untouched(monkeypatch):
    rows_b = [_result(info_hash=B, source="beta")]
    _select(
        monkeypatch,
        [
            _FakeSource(
                raises=SourceError(SourceErrorKind.NETWORK, "no route"),
                source_id="alpha",
            ),
            _FakeSource(rows_b, source_id="beta"),
        ],
    )
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("dune")

    summary = _finish(watch)
    assert watch.states("alpha") == [
        service.SourceState.RUNNING,
        service.SourceState.FAILED,
    ]
    assert watch.states("beta") == [
        service.SourceState.RUNNING,
        service.SourceState.COMPLETED,
    ]
    failed = [s for s in watch.statuses if s.state is service.SourceState.FAILED]
    assert [s.error_kind for s in failed] == ["network"]
    assert [s.result_count for s in failed] == [0]
    assert summary.results == aggregate(rows_b).results
    assert summary.failures == (service.SourceFailure("alpha", "network"),)
    assert svc.active is False


def test_an_unexpected_source_exception_leaves_its_healthy_peer_untouched(monkeypatch):
    rows_b = [_result(info_hash=B, source="beta")]
    _select(
        monkeypatch,
        [
            _FakeSource(raises=RuntimeError("adapter bug"), source_id="alpha"),
            _FakeSource(rows_b, source_id="beta"),
        ],
    )
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("dune")

    summary = _finish(watch)
    assert watch.states("alpha")[-1] is service.SourceState.FAILED
    assert watch.states("beta")[-1] is service.SourceState.COMPLETED
    assert summary.failures == (service.SourceFailure("alpha", "internal"),)
    assert summary.results == aggregate(rows_b).results


def test_a_failed_source_does_not_republish_the_same_results(monkeypatch):
    """A failure changes no rows, so it must not look like a result update."""
    rows_a = [_result(info_hash=A, source="alpha")]
    _select(
        monkeypatch,
        [
            _FakeSource(rows_a, source_id="alpha"),
            _FakeSource(
                raises=SourceError(SourceErrorKind.PARSE, "bad xml"), source_id="beta"
            ),
        ],
    )
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("dune")

    _finish(watch)
    assert watch.results == [aggregate(rows_a).results]


def test_a_search_whose_every_source_fails_still_finishes_once(monkeypatch):
    _select(
        monkeypatch,
        [
            _FakeSource(
                raises=SourceError(SourceErrorKind.TIMEOUT, "slow"), source_id="alpha"
            ),
            _FakeSource(raises=RuntimeError("boom"), source_id="beta"),
        ],
    )
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("dune")

    summary = _finish(watch)
    assert summary.results == ()
    assert summary.dedupe_dropped == 0
    assert set(summary.failures) == {
        service.SourceFailure("alpha", "timeout"),
        service.SourceFailure("beta", "internal"),
    }
    assert watch.results == []
    assert svc.active is False
    assert svc._calls == {}


# --- P. one search at a time --------------------------------------------------


def test_a_new_search_may_start_once_the_previous_one_finished(monkeypatch):
    rows = [_result(info_hash=A, source="alpha")]
    _select(monkeypatch, [_FakeSource(rows, source_id="alpha")])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("dune")
    _finish(watch)

    watch.finished.clear()
    svc.start("akira")

    summary = _finish(watch)
    assert summary.results == aggregate(rows).results


# --- Q. every search is numbered ----------------------------------------------


def _held_source(rows, *, source_id, release):
    """A source that stays inside search() until  is set.

    The wait is bounded, and every test that builds one releases it in a
    finally block, so a search this test deliberately abandons can never hold
    the suite open.
    """
    return _FakeSource(
        rows, source_id=source_id, on_search=lambda: release.wait(_WAIT_SECONDS)
    )


def test_a_fresh_service_has_not_numbered_a_search_yet():
    svc = service.SearchService(http_factory=_FakeHttp)

    assert svc.generation == 0


def test_the_first_search_is_given_its_own_generation(monkeypatch):
    _select(monkeypatch, [_FakeSource([], source_id="alpha")])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    generation = svc.start("dune")

    _finish(watch)
    assert generation > 0
    assert svc.generation == generation


def test_a_later_search_is_given_a_larger_generation(monkeypatch):
    _select(monkeypatch, [_FakeSource([], source_id="alpha")])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    first = svc.start("dune")
    _finish(watch)
    watch.finished.clear()
    second = svc.start("akira")
    _finish(watch)

    assert second > first
    assert svc.generation == second


def test_a_whitespace_query_still_takes_a_generation(monkeypatch):
    _select(monkeypatch, [_FakeSource([_result()], source_id="alpha")])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)
    before = svc.generation

    generation = svc.start("   ")

    _finish(watch)
    assert generation > before
    assert svc.generation == generation
    assert svc.active is False


def test_a_search_no_source_covers_still_takes_a_generation(monkeypatch):
    _select(monkeypatch, [])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)
    before = svc.generation

    generation = svc.start("halo", Category.GAMES)

    _finish(watch)
    assert generation > before
    assert svc.generation == generation
    assert svc.active is False


# --- R. a new search supersedes the running one -------------------------------


def _drained(svc) -> bool:
    """Wait until nothing is pinned, so every late outcome has been handled.

    Bounded like every other wait here, and it is what makes "the stale result
    was ignored" a fact rather than a guess about timing.
    """
    return _pump(lambda: not svc._calls)


def test_a_second_start_supersedes_the_running_search(monkeypatch):
    release = threading.Event()
    held = _held_source(
        [_result(info_hash=A, source="alpha")], source_id="alpha", release=release
    )
    other = _FakeSource([_result(info_hash=B, source="beta")], source_id="beta")
    _select(monkeypatch, [held])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    try:
        first = svc.start("dune")
        assert svc.active is True
        _select(monkeypatch, [other])
        second = svc.start("akira")
        assert second > first
        assert svc.active is True
    finally:
        release.set()

    summary = _finish(watch)
    assert _drained(svc), "a worker stayed pinned"
    assert summary.results == aggregate(other._rows).results
    assert [call[0] for call in held.calls] == ["dune"]
    assert [call[0] for call in other.calls] == ["akira"]
    assert len(watch.finished) == 1, "the superseded search finished too"
    assert svc.active is False


def test_an_old_searchs_results_cannot_reach_the_new_one(monkeypatch):
    release = threading.Event()
    stale_rows = [_result(info_hash=A, source="alpha", seeders=99)]
    fresh_rows = [_result(info_hash=B, source="beta", seeders=1)]
    held = _held_source(stale_rows, source_id="alpha", release=release)
    _select(monkeypatch, [held])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    try:
        svc.start("dune")
        _select(monkeypatch, [_FakeSource(fresh_rows, source_id="beta")])
        svc.start("akira")
    finally:
        release.set()

    summary = _finish(watch)
    assert _drained(svc), "a worker stayed pinned"
    assert summary.results == aggregate(fresh_rows).results
    assert watch.results == [aggregate(fresh_rows).results]
    assert watch.states("alpha") == [service.SourceState.RUNNING]
    assert len(watch.finished) == 1


def test_an_old_searchs_failure_cannot_reach_the_new_one(monkeypatch):
    release = threading.Event()
    fresh_rows = [_result(info_hash=B, source="beta")]
    held = _FakeSource(
        raises=SourceError(SourceErrorKind.NETWORK, "no route"),
        source_id="alpha",
        on_search=lambda: release.wait(_WAIT_SECONDS),
    )
    _select(monkeypatch, [held])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    try:
        svc.start("dune")
        _select(monkeypatch, [_FakeSource(fresh_rows, source_id="beta")])
        svc.start("akira")
    finally:
        release.set()

    summary = _finish(watch)
    assert _drained(svc), "a worker stayed pinned"
    assert summary.failures == ()
    assert summary.results == aggregate(fresh_rows).results
    assert watch.states("alpha") == [service.SourceState.RUNNING]
    assert len(watch.finished) == 1


def test_a_whitespace_query_supersedes_the_running_search(monkeypatch):
    release = threading.Event()
    held = _held_source(
        [_result(info_hash=A, source="alpha")], source_id="alpha", release=release
    )
    _select(monkeypatch, [held])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    try:
        first = svc.start("dune")
        second = svc.start("   ")
        assert second > first
        assert svc.active is False
        assert len(watch.finished) == 1, "the blank search did not finish at once"
    finally:
        release.set()

    assert _drained(svc), "a worker stayed pinned"
    assert watch.finished[0].results == ()
    assert len(watch.finished) == 1
    assert watch.results == []
    assert watch.states("alpha") == [service.SourceState.RUNNING]


def test_a_search_no_source_covers_supersedes_the_running_search(monkeypatch):
    release = threading.Event()
    held = _held_source(
        [_result(info_hash=A, source="alpha")], source_id="alpha", release=release
    )
    _select(monkeypatch, [held])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    try:
        first = svc.start("dune")
        # An empty selection for the second search: it supersedes the first
        # without ever reaching a source of its own.
        _select(monkeypatch, [])
        second = svc.start("halo", Category.GAMES)
        assert second > first
        assert svc.active is False
    finally:
        release.set()

    assert _drained(svc), "a worker stayed pinned"
    assert len(watch.finished) == 1
    assert watch.finished[0].results == ()
    assert watch.states("alpha") == [service.SourceState.RUNNING]


# --- S. reentrant lifecycle changes from a public signal ----------------------


def test_starting_from_a_running_status_handler_supersedes_cleanly(monkeypatch):
    """A listener that starts a new search from the first RUNNING status gets a
    clean supersede: the old search must not go on submitting sources."""
    seen_first = []
    fresh_rows = [_result(info_hash=B, source="beta")]
    old_sources = [
        _FakeSource([_result(info_hash=A, source="alpha")], source_id="alpha"),
        _FakeSource([_result(info_hash=A, source="gamma")], source_id="gamma"),
    ]
    fresh = _FakeSource(fresh_rows, source_id="beta")
    _select(monkeypatch, old_sources)
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    def _restart(status):
        if seen_first:
            return
        seen_first.append(status.source_id)
        _select(monkeypatch, [fresh])
        svc.start("akira")

    svc.source_status.connect(_restart)

    first = svc.start("dune")

    summary = _finish(watch)
    assert _drained(svc), "a worker stayed pinned"
    assert seen_first == ["alpha"]
    assert svc.generation > first
    # The restart happened during the very first RUNNING status, so no source
    # of the superseded search may have been submitted at all.
    assert [source.calls for source in old_sources] == [[], []]
    assert summary.results == aggregate(fresh_rows).results
    assert len(watch.finished) == 1
    assert svc.active is False


# --- T. the same source id in two generations ---------------------------------


def test_the_same_source_id_in_two_generations_keeps_two_workers(monkeypatch):
    """Two searches can be running the same source at once.

    Ownership therefore cannot be keyed on the source id alone: the newer call
    would evict the older one from the service's own pins while the pool is
    still running it, and the older outcome would then be taken for the newer
    search's.
    """
    stale_release = threading.Event()
    fresh_release = threading.Event()
    stale_rows = [_result(info_hash=A, source="same", seeders=99)]
    fresh_rows = [_result(info_hash=B, source="same", seeders=1)]
    held = _held_source(stale_rows, source_id="same", release=stale_release)
    fresh = _held_source(fresh_rows, source_id="same", release=fresh_release)
    _select(monkeypatch, [held])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    try:
        svc.start("dune")
        _select(monkeypatch, [fresh])
        svc.start("akira")

        assert _pump(lambda: len(svc._calls) == 2), "the new call evicted the old pin"

        stale_release.set()
        assert _pump(lambda: len(svc._calls) == 1), "the stale worker was never released"

        assert svc._pending == {"same"}, "the stale outcome cleared the live source"
        assert watch.results == [], "the stale outcome published rows"
        assert watch.finished == [], "the stale outcome finished the live search"
        assert svc.active is True
        assert watch.states("same") == [
            service.SourceState.RUNNING,
            service.SourceState.RUNNING,
        ]
    finally:
        stale_release.set()
        fresh_release.set()

    summary = _finish(watch)
    assert _drained(svc), "a worker stayed pinned"
    assert summary.results == aggregate(fresh_rows).results
    assert summary.failures == ()
    assert len(watch.finished) == 1
    assert watch.states("same") == [
        service.SourceState.RUNNING,
        service.SourceState.RUNNING,
        service.SourceState.COMPLETED,
    ]


# --- U. cancelling the current search -----------------------------------------


def test_cancelling_an_active_search_drops_its_late_result(monkeypatch):
    """cancel() is not termination: the provider call is still running, and
    the point is that whatever it eventually says is no longer wanted."""
    release = threading.Event()
    held = _held_source(
        [_result(info_hash=A, source="alpha")], source_id="alpha", release=release
    )
    _select(monkeypatch, [held])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    try:
        first = svc.start("dune")
        cancelled = svc.cancel()
        assert cancelled > first
        assert svc.generation == cancelled
        assert svc.active is False
    finally:
        release.set()

    assert _drained(svc), "the cancelled worker stayed pinned"
    assert held.calls, "the cancelled search never reached its source"
    assert watch.states("alpha") == [service.SourceState.RUNNING]
    assert watch.results == []
    assert watch.finished == [], "a cancelled search finished anyway"


def test_cancelling_an_active_search_drops_its_late_failure(monkeypatch):
    release = threading.Event()
    held = _FakeSource(
        raises=SourceError(SourceErrorKind.NETWORK, "no route"),
        source_id="alpha",
        on_search=lambda: release.wait(_WAIT_SECONDS),
    )
    _select(monkeypatch, [held])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    try:
        svc.start("dune")
        svc.cancel()
    finally:
        release.set()

    assert _drained(svc), "the cancelled worker stayed pinned"
    assert watch.states("alpha") == [service.SourceState.RUNNING]
    assert watch.finished == []
    assert svc.active is False


def test_cancelling_when_nothing_is_running_changes_nothing():
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)
    before = svc.generation

    assert svc.cancel() == before

    assert svc.generation == before, "an idle cancel spent a generation"
    assert svc.active is False
    assert watch.statuses == []
    assert watch.results == []
    assert watch.finished == []


def test_cancelling_a_search_that_already_finished_changes_nothing(monkeypatch):
    _select(monkeypatch, [_FakeSource([_result()], source_id="alpha")])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    generation = svc.start("dune")
    _finish(watch)

    assert svc.cancel() == generation
    assert svc.generation == generation
    assert len(watch.finished) == 1


def test_a_search_started_after_a_cancel_gets_a_later_generation(monkeypatch):
    release = threading.Event()
    held = _held_source(
        [_result(info_hash=A, source="alpha")], source_id="alpha", release=release
    )
    fresh_rows = [_result(info_hash=B, source="beta")]
    _select(monkeypatch, [held])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    try:
        first = svc.start("dune")
        cancelled = svc.cancel()
        _select(monkeypatch, [_FakeSource(fresh_rows, source_id="beta")])
        second = svc.start("akira")
        assert second > cancelled > first
    finally:
        release.set()

    summary = _finish(watch)
    assert _drained(svc), "a worker stayed pinned"
    assert summary.results == aggregate(fresh_rows).results
    assert len(watch.finished) == 1
    assert watch.states("alpha") == [service.SourceState.RUNNING]


# --- V. every public event names the search it belongs to ---------------------


def test_every_event_of_one_search_names_that_search(monkeypatch):
    rows = [_result(info_hash=A, source="alpha")]
    _select(
        monkeypatch,
        [
            _FakeSource(rows, source_id="alpha"),
            _FakeSource(
                raises=SourceError(SourceErrorKind.PARSE, "bad xml"), source_id="beta"
            ),
        ],
    )
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    generation = svc.start("dune")

    summary = _finish(watch)
    assert {status.generation for status in watch.statuses} == {generation}
    assert {status.state for status in watch.statuses} == {
        service.SourceState.RUNNING,
        service.SourceState.COMPLETED,
        service.SourceState.FAILED,
    }
    assert watch.result_generations == [generation]
    assert summary.generation == generation


def test_an_immediate_blank_search_names_its_generation(monkeypatch):
    _select(monkeypatch, [_FakeSource([_result()], source_id="alpha")])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    generation = svc.start("   ")

    assert _finish(watch).generation == generation


def test_an_immediate_uncovered_search_names_its_generation(monkeypatch):
    _select(monkeypatch, [])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    generation = svc.start("halo", Category.GAMES)

    assert _finish(watch).generation == generation


def test_two_searches_events_are_told_apart_by_generation(monkeypatch):
    rows_first = [_result(info_hash=A, source="alpha")]
    rows_second = [_result(info_hash=B, source="beta")]
    _select(monkeypatch, [_FakeSource(rows_first, source_id="alpha")])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    first = svc.start("dune")
    _finish(watch)
    watch.finished.clear()
    _select(monkeypatch, [_FakeSource(rows_second, source_id="beta")])
    second = svc.start("akira")
    _finish(watch)

    by_generation = {}
    for status in watch.statuses:
        by_generation.setdefault(status.generation, set()).add(status.source_id)
    assert by_generation == {first: {"alpha"}, second: {"beta"}}
    assert watch.result_generations == [first, second]
    assert [summary.generation for summary in watch.finished] == [second]


def test_cancelling_from_a_running_status_handler_stops_the_search(monkeypatch):
    """A listener that cancels from the first RUNNING status must be obeyed
    before the search submits anything at all."""
    seen_first = []
    old_sources = [
        _FakeSource([_result(info_hash=A, source="alpha")], source_id="alpha"),
        _FakeSource([_result(info_hash=B, source="gamma")], source_id="gamma"),
    ]
    _select(monkeypatch, old_sources)
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    def _stop(status):
        if seen_first:
            return
        seen_first.append(status.source_id)
        svc.cancel()

    svc.source_status.connect(_stop)

    generation = svc.start("dune")

    assert seen_first == ["alpha"]
    assert svc.generation > generation
    assert svc.active is False
    assert [source.calls for source in old_sources] == [[], []]
    assert svc._calls == {}, "a worker was submitted after the cancel"
    assert watch.results == []
    assert watch.finished == [], "a cancelled search finished anyway"


def test_starting_from_a_completed_status_handler_supersedes_cleanly(monkeypatch):
    """The outcome handler emitted a terminal status and the listener changed
    the search underneath it: everything after that emit belongs to nobody."""
    restarted = []
    fresh_rows = [_result(info_hash=B, source="beta")]
    _select(
        monkeypatch, [_FakeSource([_result(info_hash=A, source="alpha")], source_id="alpha")]
    )
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    def _restart(status):
        if restarted or status.state is not service.SourceState.COMPLETED:
            return
        restarted.append(status.source_id)
        _select(monkeypatch, [_FakeSource(fresh_rows, source_id="beta")])
        svc.start("akira")

    svc.source_status.connect(_restart)

    first = svc.start("dune")

    summary = _finish(watch)
    assert _drained(svc), "a worker stayed pinned"
    assert restarted == ["alpha"]
    assert summary.generation > first
    assert summary.results == aggregate(fresh_rows).results
    assert watch.result_generations == [summary.generation], (
        "the superseded search published its rows after the restart"
    )
    assert len(watch.finished) == 1


def test_starting_from_a_results_updated_handler_supersedes_cleanly(monkeypatch):
    """The replacement search is one that finishes on the spot, so the old
    handler resuming afterwards would visibly finish a second time."""
    restarted = []
    _select(
        monkeypatch, [_FakeSource([_result(info_hash=A, source="alpha")], source_id="alpha")]
    )
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    def _restart(generation, _results):
        if restarted:
            return
        restarted.append(generation)
        svc.start("   ")

    svc.results_updated.connect(_restart)

    first = svc.start("dune")

    assert _drained(svc), "a worker stayed pinned"
    assert restarted == [first]
    assert svc.generation > first
    assert svc.active is False
    assert [summary.generation for summary in watch.finished] == [svc.generation], (
        "the superseded search finished after the restart"
    )
    assert watch.result_generations == [first]


def test_cancelling_from_a_results_updated_handler_stops_the_search(monkeypatch):
    cancelled = []
    _select(
        monkeypatch, [_FakeSource([_result(info_hash=A, source="alpha")], source_id="alpha")]
    )
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    def _stop(generation, _results):
        cancelled.append(generation)
        svc.cancel()

    svc.results_updated.connect(_stop)

    first = svc.start("dune")

    assert _drained(svc), "a worker stayed pinned"
    assert _pump(lambda: cancelled == [first], 1.0), "the source never published"
    assert svc.generation > first
    assert svc.active is False
    assert watch.finished == [], "a cancelled search finished anyway"


# --- W. the worker carries the number, and never reads it ---------------------


def test_a_source_call_hands_back_the_generation_it_was_given():
    call = service._SourceCall(
        _FakeSource([_result()]), "dune", Category.MOVIES, generation=7, http_factory=_FakeHttp
    )
    seen = _collect(call)

    call.run()

    assert [outcome.generation for outcome in seen] == [7]


def test_a_failed_source_call_still_hands_back_its_generation():
    call = service._SourceCall(
        _FakeSource(raises=SourceError(SourceErrorKind.HTTP)),
        "dune",
        Category.MOVIES,
        generation=9,
        http_factory=_FakeHttp,
    )
    seen = _collect(call)

    call.run()

    assert [(o.generation, o.error_kind) for o in seen] == [(9, "http")]


def test_the_generation_only_ever_goes_up(monkeypatch):
    """One service, every lifecycle it has: nothing reuses or rewinds."""
    release = threading.Event()
    held = _held_source(
        [_result(info_hash=A, source="alpha")], source_id="alpha", release=release
    )
    plain = _FakeSource([_result(info_hash=B, source="beta")], source_id="beta")
    _select(monkeypatch, [plain])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)
    fresh = svc.generation

    try:
        completed = svc.start("dune")             # a search that completes
        _finish(watch)
        after_finishing = svc.generation
        _select(monkeypatch, [held])
        abandoned = svc.start("akira")            # a search left running
        _select(monkeypatch, [plain])
        superseding = svc.start("cowboy")         # which this one replaces
        cancelled = svc.cancel()                  # and this one abandons
        idle = svc.cancel()                       # an idle cancel spends none
        blank = svc.start("   ")                  # a blank search
        _select(monkeypatch, [])
        uncovered = svc.start("halo", Category.GAMES)  # a search nothing covers
    finally:
        release.set()

    assert _drained(svc), "a worker stayed pinned"
    # Finishing and cancelling nothing are the only two events that may leave
    # the number where it was. Every start, and the one real cancel, moves it
    # on, and nothing ever hands back a number that has already been used.
    assert after_finishing == completed
    assert idle == cancelled
    numbers = [fresh, completed, abandoned, superseding, cancelled, blank, uncovered]
    assert numbers == sorted(numbers), f"a generation went backwards: {numbers}"
    assert len(set(numbers)) == len(numbers), f"a generation was reused: {numbers}"


# --- X. one global deadline per search ----------------------------------------

# Short enough that a whole suite of deadline tests costs a few seconds, long
# enough that a loaded CI box is not mistaken for a hung provider. Nothing here
# ever waits for the production 30 seconds.
_TEST_DEADLINE_MS = 120

# The stable name a timed-out source is reported under.
_TIMEOUT_KIND = "timeout"


def _short_service(http_factory=_FakeHttp):
    """A service whose product deadline is the short test one."""
    return service.SearchService(http_factory=http_factory, deadline_ms=_TEST_DEADLINE_MS)


def test_the_production_search_deadline_is_thirty_seconds():
    assert service._SEARCH_DEADLINE_MS == 30_000


def test_a_service_uses_the_production_deadline_by_default():
    svc = service.SearchService(http_factory=_FakeHttp)

    assert svc._deadline_ms == 30_000


def test_a_nonsensical_deadline_is_rejected():
    for bad in (0, -1):
        with pytest.raises(ValueError):
            service.SearchService(http_factory=_FakeHttp, deadline_ms=bad)


def test_a_provider_backed_search_arms_one_single_shot_deadline(monkeypatch):
    """The default 30-second service is used here, and never waited on."""
    release = threading.Event()
    held = _held_source([], source_id="alpha", release=release)
    _select(monkeypatch, [held])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    try:
        svc.start("dune")
        assert svc._deadline.isActive() is True
        assert svc._deadline.isSingleShot() is True
    finally:
        release.set()

    _finish(watch)


def test_a_whitespace_query_leaves_no_deadline_armed(monkeypatch):
    _select(monkeypatch, [_FakeSource([_result()], source_id="alpha")])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("   ")

    _finish(watch)
    assert svc._deadline.isActive() is False


def test_a_search_no_source_covers_leaves_no_deadline_armed(monkeypatch):
    _select(monkeypatch, [])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("halo", Category.GAMES)

    _finish(watch)
    assert svc._deadline.isActive() is False


def test_a_normal_completion_disarms_the_deadline(monkeypatch):
    _select(monkeypatch, [_FakeSource([_result()], source_id="alpha")])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("dune")

    _finish(watch)
    assert svc._deadline.isActive() is False


# --- Y. a source that runs out of time ----------------------------------------


def test_a_source_that_never_answers_still_finishes_the_search(monkeypatch):
    """The whole point: one silent provider cannot hold a search open."""
    release = threading.Event()
    held = _held_source([_result()], source_id="alpha", release=release)
    _select(monkeypatch, [held])
    svc = _short_service()
    watch = _Watch(svc)

    started = time.monotonic()
    try:
        svc.start("dune")
        summary = _finish(watch)
        elapsed = time.monotonic() - started
    finally:
        release.set()

    # Bounded on both sides: the deadline is a wait, not an instant giving up,
    # and it is emphatically not the five seconds the provider would have taken.
    assert elapsed >= (_TEST_DEADLINE_MS / 1000) * 0.5, f"finished far too early: {elapsed}"
    assert elapsed < 2.0, f"the deadline did not bound the search: {elapsed}"
    assert watch.states("alpha") == [
        service.SourceState.RUNNING,
        service.SourceState.TIMED_OUT,
    ]
    assert summary.failures == (service.SourceFailure("alpha", "timeout"),)
    assert summary.results == ()
    assert summary.dedupe_dropped == 0
    assert svc.active is False
    assert len(watch.finished) == 1


def test_a_timed_out_source_status_names_the_timeout_and_no_rows(monkeypatch):
    release = threading.Event()
    held = _held_source([_result()], source_id="alpha", release=release)
    _select(monkeypatch, [held])
    svc = _short_service()
    watch = _Watch(svc)

    try:
        generation = svc.start("dune")
        _finish(watch)
    finally:
        release.set()

    timed_out = [
        status
        for status in watch.statuses
        if status.state is service.SourceState.TIMED_OUT
    ]
    assert len(timed_out) == 1
    assert timed_out[0].generation == generation
    assert timed_out[0].source_id == "alpha"
    assert timed_out[0].error_kind == "timeout"
    assert timed_out[0].result_count == 0


def test_a_search_that_timed_out_leaves_no_deadline_armed(monkeypatch):
    release = threading.Event()
    held = _held_source([], source_id="alpha", release=release)
    _select(monkeypatch, [held])
    svc = _short_service()
    watch = _Watch(svc)

    started = time.monotonic()
    try:
        svc.start("dune")
        _finish(watch)
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert elapsed < 2.0, f"the deadline did not bound the search: {elapsed}"
    assert svc._deadline.isActive() is False


def test_a_successful_source_survives_a_peer_timing_out(monkeypatch):
    """A search is not thrown away because one of its sources went quiet."""
    release = threading.Event()
    rows = [_result(info_hash=A, source="beta", seeders=7)]
    quick = _FakeSource(rows, source_id="beta")
    held = _held_source(
        [_result(info_hash=B, source="alpha")], source_id="alpha", release=release
    )
    _select(monkeypatch, [quick, held])
    svc = _short_service()
    watch = _Watch(svc)

    try:
        svc.start("dune")
        summary = _finish(watch)
    finally:
        release.set()

    assert summary.results == aggregate(rows).results
    assert summary.failures == (service.SourceFailure("alpha", _TIMEOUT_KIND),)
    assert watch.states("beta") == [
        service.SourceState.RUNNING,
        service.SourceState.COMPLETED,
    ]
    assert watch.states("alpha") == [
        service.SourceState.RUNNING,
        service.SourceState.TIMED_OUT,
    ]
    assert len(watch.finished) == 1
    assert svc.active is False


def test_the_rows_published_before_a_timeout_are_the_rows_summarised(monkeypatch):
    release = threading.Event()
    rows = [_result(info_hash=A, source="beta"), _result(info_hash=C, source="beta")]
    quick = _FakeSource(rows, source_id="beta")
    held = _held_source([], source_id="alpha", release=release)
    _select(monkeypatch, [quick, held])
    svc = _short_service()
    watch = _Watch(svc)

    try:
        svc.start("dune")
        summary = _finish(watch)
    finally:
        release.set()

    assert watch.results == [aggregate(rows).results]
    assert summary.results == watch.results[-1]


def test_a_search_whose_every_source_goes_quiet_still_finishes_once(monkeypatch):
    release = threading.Event()
    first = _held_source([_result()], source_id="alpha", release=release)
    second = _held_source([_result()], source_id="beta", release=release)
    _select(monkeypatch, [first, second])
    svc = _short_service()
    watch = _Watch(svc)

    try:
        svc.start("dune")
        summary = _finish(watch)
    finally:
        release.set()

    assert summary.results == ()
    assert set(summary.failures) == {
        service.SourceFailure("alpha", _TIMEOUT_KIND),
        service.SourceFailure("beta", _TIMEOUT_KIND),
    }
    assert watch.results == []
    assert len(watch.finished) == 1
    assert svc.active is False


def test_a_normal_failure_and_a_timeout_are_both_reported(monkeypatch):
    """The two are different things and neither may be reported as the other."""
    release = threading.Event()
    broken = _FakeSource(
        raises=SourceError(SourceErrorKind.NETWORK, "down"), source_id="beta"
    )
    held = _held_source([], source_id="alpha", release=release)
    _select(monkeypatch, [broken, held])
    svc = _short_service()
    watch = _Watch(svc)

    try:
        svc.start("dune")
        summary = _finish(watch)
    finally:
        release.set()

    assert set(summary.failures) == {
        service.SourceFailure("beta", SourceErrorKind.NETWORK.value),
        service.SourceFailure("alpha", _TIMEOUT_KIND),
    }
    assert watch.states("beta") == [
        service.SourceState.RUNNING,
        service.SourceState.FAILED,
    ]
    assert watch.states("alpha") == [
        service.SourceState.RUNNING,
        service.SourceState.TIMED_OUT,
    ]


def test_several_sources_time_out_in_a_deterministic_order(monkeypatch):
    """Which order the UI hears about them in cannot come from a set."""
    release = threading.Event()
    ids = ["zulu", "alpha", "mike", "bravo"]
    _select(
        monkeypatch,
        [_held_source([], source_id=name, release=release) for name in ids],
    )
    svc = _short_service()
    watch = _Watch(svc)

    try:
        svc.start("dune")
        summary = _finish(watch)
    finally:
        release.set()

    timed_out = [
        status.source_id
        for status in watch.statuses
        if status.state is service.SourceState.TIMED_OUT
    ]
    assert timed_out == sorted(ids)
    assert [failure.source_id for failure in summary.failures] == sorted(ids)


def test_timed_out_sources_follow_the_registry_order_first(monkeypatch):
    """A known source outranks an unknown one, exactly as ranking does."""
    release = threading.Event()
    known = SOURCES[1].id
    ids = ["zzz", known, SOURCES[0].id]
    _select(
        monkeypatch,
        [_held_source([], source_id=name, release=release) for name in ids],
    )
    svc = _short_service()
    watch = _Watch(svc)

    try:
        svc.start("dune")
        _finish(watch)
    finally:
        release.set()

    timed_out = [
        status.source_id
        for status in watch.statuses
        if status.state is service.SourceState.TIMED_OUT
    ]
    assert timed_out == [SOURCES[0].id, known, "zzz"]


def test_a_timed_out_worker_that_finally_answers_changes_nothing(monkeypatch):
    """The provider was never killed, so its rows do eventually arrive."""
    release = threading.Event()
    held = _held_source(
        [_result(info_hash=A, source="alpha", seeders=99)],
        source_id="alpha",
        release=release,
    )
    _select(monkeypatch, [held])
    svc = _short_service()
    watch = _Watch(svc)

    try:
        svc.start("dune")
        summary = _finish(watch)
        assert svc._calls, "the still-running worker was let go before it reported"
    finally:
        release.set()

    assert _drained(svc), "the late worker stayed pinned"
    assert watch.results == []
    assert len(watch.finished) == 1
    assert watch.states("alpha") == [
        service.SourceState.RUNNING,
        service.SourceState.TIMED_OUT,
    ]
    assert summary.results == ()
    assert svc.active is False
    assert svc._deadline.isActive() is False


def test_a_timed_out_worker_that_finally_fails_changes_nothing(monkeypatch):
    release = threading.Event()
    held = _FakeSource(
        raises=SourceError(SourceErrorKind.NETWORK, "down"),
        source_id="alpha",
        on_search=lambda: release.wait(_WAIT_SECONDS),
    )
    _select(monkeypatch, [held])
    svc = _short_service()
    watch = _Watch(svc)

    try:
        svc.start("dune")
        summary = _finish(watch)
    finally:
        release.set()

    assert _drained(svc), "the late worker stayed pinned"
    assert len(watch.finished) == 1
    assert watch.states("alpha") == [
        service.SourceState.RUNNING,
        service.SourceState.TIMED_OUT,
    ]
    # The timeout stands: a failure that arrived after Cove stopped waiting
    # does not get to rewrite what the search already reported.
    assert summary.failures == (service.SourceFailure("alpha", _TIMEOUT_KIND),)


def test_a_superseding_search_gets_its_own_full_deadline(monkeypatch):
    """Generation two does not inherit what was left of generation one."""
    release = threading.Event()
    first = _held_source([], source_id="alpha", release=release)
    second = _held_source([], source_id="beta", release=release)
    _select(monkeypatch, [first])
    svc = _short_service()
    watch = _Watch(svc)

    try:
        svc.start("dune")
        # Spend most of the first search's window before replacing it.
        _pump(lambda: False, seconds=(_TEST_DEADLINE_MS / 1000) * 0.8)
        _select(monkeypatch, [second])
        started = time.monotonic()
        generation = svc.start("akira")
        summary = _finish(watch)
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert summary.generation == generation
    assert summary.failures == (service.SourceFailure("beta", _TIMEOUT_KIND),)
    assert elapsed >= (_TEST_DEADLINE_MS / 1000) * 0.5, (
        f"the new search inherited the old window: {elapsed}"
    )
    assert elapsed < 2.0
    assert len(watch.finished) == 1


def test_a_stale_deadline_cannot_time_out_the_search_that_replaced_it(monkeypatch):
    """A timer event already in flight when a search is replaced does nothing."""
    release = threading.Event()
    first = _held_source([], source_id="alpha", release=release)
    second = _held_source([], source_id="beta", release=release)
    _select(monkeypatch, [first])
    # The production deadline, so nothing here can expire on its own.
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    try:
        stale = svc.start("dune")
        _select(monkeypatch, [second])
        fresh = svc.start("akira")

        svc._expire(stale)

        assert svc.active is True
        assert svc._pending == {"beta"}
        assert watch.finished == []
        assert watch.states("beta") == [service.SourceState.RUNNING]
        assert svc._deadline.isActive() is True, "the new search lost its deadline"
    finally:
        release.set()

    summary = _finish(watch)
    assert _drained(svc), "a worker stayed pinned"
    assert summary.generation == fresh
    assert summary.failures == ()


def test_cancelling_takes_the_deadline_with_it(monkeypatch):
    release = threading.Event()
    held = _held_source([_result()], source_id="alpha", release=release)
    _select(monkeypatch, [held])
    svc = _short_service()
    watch = _Watch(svc)

    try:
        generation = svc.start("dune")
        assert svc._deadline.isActive() is True
        svc.cancel()
        assert svc._deadline.isActive() is False

        # Well past when the cancelled search would have run out of time.
        _pump(lambda: False, seconds=(_TEST_DEADLINE_MS / 1000) * 3)
        # And a timer event that was already in flight when it was cancelled.
        svc._expire(generation)

        assert watch.finished == []
        assert [status.state for status in watch.statuses] == [
            service.SourceState.RUNNING
        ]
    finally:
        release.set()

    assert _drained(svc), "the abandoned worker stayed pinned"
    assert watch.finished == [], "the cancelled search finished anyway"
    assert svc.active is False


def test_the_same_source_id_survives_a_timeout_and_a_new_search(monkeypatch):
    """A timed-out worker's late answer cannot be taken for its namesake's."""
    stale_release = threading.Event()
    fresh_release = threading.Event()
    stale_rows = [_result(info_hash=A, source="same", seeders=99)]
    fresh_rows = [_result(info_hash=B, source="same", seeders=1)]
    held = _held_source(stale_rows, source_id="same", release=stale_release)
    fresh = _held_source(fresh_rows, source_id="same", release=fresh_release)
    _select(monkeypatch, [held])
    svc = _short_service()
    watch = _Watch(svc)

    try:
        svc.start("dune")
        timed_out = _finish(watch)
        assert timed_out.failures == (service.SourceFailure("same", _TIMEOUT_KIND),)

        _select(monkeypatch, [fresh])
        svc.start("akira")
        assert _pump(lambda: len(svc._calls) == 2), "the new call evicted the old pin"

        stale_release.set()
        assert _pump(lambda: len(svc._calls) == 1), "the stale worker was never released"

        assert svc._pending == {"same"}, "the stale outcome cleared the live source"
        assert svc.active is True
        assert len(watch.finished) == 1
    finally:
        stale_release.set()
        fresh_release.set()

    assert _pump(lambda: len(watch.finished) == 2), "the new search never finished"
    assert _drained(svc), "a worker stayed pinned"
    summary = watch.finished[-1]
    assert summary.results == aggregate(fresh_rows).results
    assert summary.failures == ()
    assert watch.states("same") == [
        service.SourceState.RUNNING,
        service.SourceState.TIMED_OUT,
        service.SourceState.RUNNING,
        service.SourceState.COMPLETED,
    ]


def test_starting_from_a_timed_out_status_handler_supersedes_cleanly(monkeypatch):
    """A listener may replace the search from the first timeout it hears."""
    release = threading.Event()
    _select(
        monkeypatch,
        [
            _held_source([], source_id=name, release=release)
            for name in ("alpha", "bravo")
        ],
    )
    fresh_rows = [_result(info_hash=B, source="beta")]
    fresh = _FakeSource(fresh_rows, source_id="beta")
    svc = _short_service()
    watch = _Watch(svc)
    restarted = []

    def _restart(status):
        if status.state is service.SourceState.TIMED_OUT and not restarted:
            restarted.append(status.generation)
            _select(monkeypatch, [fresh])
            svc.start("akira")

    svc.source_status.connect(_restart)

    try:
        first = svc.start("dune")
        assert _pump(lambda: bool(watch.finished)), "no search ever finished"
        _pump(lambda: False, seconds=(_TEST_DEADLINE_MS / 1000) * 2)
    finally:
        release.set()

    assert _drained(svc), "a worker stayed pinned"
    assert restarted == [first]
    timed_out = [
        status.source_id
        for status in watch.statuses
        if status.state is service.SourceState.TIMED_OUT
    ]
    assert timed_out == ["alpha"], "the replaced search went on timing sources out"
    assert len(watch.finished) == 1, "the replaced search finished as well"
    summary = watch.finished[0]
    assert summary.generation > first
    assert summary.results == aggregate(fresh_rows).results
    assert summary.failures == ()
    assert svc.active is False


def test_cancelling_from_a_timed_out_status_handler_stops_the_search(monkeypatch):
    release = threading.Event()
    _select(
        monkeypatch,
        [
            _held_source([], source_id=name, release=release)
            for name in ("alpha", "bravo")
        ],
    )
    svc = _short_service()
    watch = _Watch(svc)
    stopped = []

    def _stop(status):
        if status.state is service.SourceState.TIMED_OUT and not stopped:
            stopped.append(status.source_id)
            svc.cancel()

    svc.source_status.connect(_stop)

    try:
        svc.start("dune")
        assert _pump(lambda: bool(stopped)), "nothing ever timed out"
        _pump(lambda: False, seconds=(_TEST_DEADLINE_MS / 1000) * 2)
    finally:
        release.set()

    assert _drained(svc), "a worker stayed pinned"
    assert stopped == ["alpha"]
    timed_out = [
        status.source_id
        for status in watch.statuses
        if status.state is service.SourceState.TIMED_OUT
    ]
    assert timed_out == ["alpha"], "the cancelled search went on timing sources out"
    assert watch.finished == [], "the cancelled search finished anyway"
    assert svc.active is False
    assert svc._deadline.isActive() is False


def test_starting_from_a_timed_out_searchs_finish_runs_the_next_search(monkeypatch):
    """The timed-out search is entirely tidied up before it says so."""
    release = threading.Event()
    _select(monkeypatch, [_held_source([], source_id="alpha", release=release)])
    fresh_rows = [_result(info_hash=B, source="beta")]
    fresh = _FakeSource(fresh_rows, source_id="beta")
    svc = _short_service()
    watch = _Watch(svc)
    restarted = []

    def _restart(summary):
        if not restarted:
            restarted.append(summary.generation)
            _select(monkeypatch, [fresh])
            svc.start("akira")

    svc.search_finished.connect(_restart)

    try:
        first = svc.start("dune")
        assert _pump(lambda: len(watch.finished) == 2), "the next search never ran"
    finally:
        release.set()

    assert _drained(svc), "a worker stayed pinned"
    assert restarted == [first]
    assert watch.finished[0].failures == (service.SourceFailure("alpha", _TIMEOUT_KIND),)
    assert watch.finished[1].generation > first
    assert watch.finished[1].results == aggregate(fresh_rows).results
    assert watch.finished[1].failures == ()
    assert svc.active is False
    assert svc._deadline.isActive() is False


def test_the_deadline_reports_on_the_services_own_thread(monkeypatch):
    release = threading.Event()
    _select(monkeypatch, [_held_source([], source_id="alpha", release=release)])
    svc = _short_service()
    watch = _Watch(svc)
    owning = QThread.currentThread()
    seen = []
    svc.source_status.connect(
        lambda status: seen.append((status.state, QThread.currentThread()))
    )
    svc.search_finished.connect(
        lambda summary: seen.append(("finished", QThread.currentThread()))
    )

    try:
        svc.start("dune")
        _finish(watch)
    finally:
        release.set()

    assert svc.thread() is owning
    assert svc._deadline.thread() is owning
    assert [what for what, _ in seen] == [
        service.SourceState.RUNNING,
        service.SourceState.TIMED_OUT,
        "finished",
    ]
    assert all(thread is owning for _, thread in seen)


def test_dropping_a_service_with_an_armed_deadline_is_harmless():
    """Qt owns the timer through the service, so nothing outlives it."""
    svc = _short_service()
    svc._arm_deadline(svc.generation + 1)
    assert svc._deadline.isActive() is True

    svc.deleteLater()
    del svc

    # Well past when that deadline would have fired.
    _pump(lambda: False, seconds=(_TEST_DEADLINE_MS / 1000) * 3)


def test_the_deadline_timer_never_fires_early():
    """A coarse timer may fire up to 5% early, which on the production
    deadline is a second and a half a source was promised and did not get."""
    svc = service.SearchService(http_factory=_FakeHttp)

    assert svc._deadline.timerType() is Qt.PreciseTimer


# --- Z. the bounded Search result cache ---------------------------------------
#
# A pure primitive: no Qt, no threads, no network, no SearchService. Every
# test drives it with a deterministic fake monotonic clock rather than sleeping.


class _Clock:
    """A fake monotonic clock: it only ever moves forward, on demand."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        assert seconds >= 0, "a monotonic clock never goes backwards"
        self.now += seconds


def _key(source_id="yts", category=Category.MOVIES, query="dune"):
    return service._CacheKey(source_id=source_id, category=category, query=query)


def _cache(clock=None, max_entries=3, ttl_seconds=300.0):
    return service._SearchCache(
        max_entries=max_entries,
        ttl_seconds=ttl_seconds,
        clock=clock or _Clock(),
    )


# --- Z.A. basic hit and miss --------------------------------------------------


def test_an_unknown_key_is_a_cache_miss():
    assert _cache().get(_key()) is None


def test_a_stored_key_is_a_cache_hit():
    cache = _cache()
    rows = (_result(),)

    cache.put(_key(), rows)

    assert cache.get(_key()) == rows


def test_a_hit_returns_the_rows_that_were_stored():
    cache = _cache()
    rows = (_result(info_hash=A), _result(info_hash=B, name="Other"))

    cache.put(_key(), rows)

    assert cache.get(_key()) == rows


def test_a_cached_empty_result_is_a_hit_and_not_a_miss():
    """A source that legitimately found nothing is a success, so its empty
    answer is worth caching - and must not read back as "never asked"."""
    cache = _cache()

    cache.put(_key(), ())

    assert cache.get(_key()) == ()
    assert cache.get(_key()) is not None


# --- Z.B. key identity --------------------------------------------------------


def test_two_sources_answering_the_same_query_do_not_share_an_entry():
    cache = _cache()
    rows = (_result(),)

    cache.put(_key(source_id="yts"), rows)

    assert cache.get(_key(source_id="piratebay")) is None
    assert cache.get(_key(source_id="yts")) == rows


def test_two_categories_of_the_same_query_do_not_share_an_entry():
    cache = _cache()
    rows = (_result(),)

    cache.put(_key(category=Category.MOVIES), rows)

    assert cache.get(_key(category=Category.TV)) is None
    assert cache.get(_key(category=Category.MOVIES)) == rows


def test_the_cache_does_not_casefold_the_query():
    """Normalising the query is the caller's job: this primitive must never
    quietly change what a source was asked for."""
    cache = _cache()
    rows = (_result(),)

    cache.put(_key(query="Dune"), rows)

    assert cache.get(_key(query="dune")) is None
    assert cache.get(_key(query="Dune")) == rows


def test_the_cache_does_not_strip_whitespace_from_the_query():
    cache = _cache()
    rows = (_result(),)

    cache.put(_key(query="dune"), rows)

    assert cache.get(_key(query=" dune ")) is None
    assert cache.get(_key(query="dune")) == rows


def test_a_cache_key_is_hashable_and_compares_by_its_three_parts():
    assert _key() == _key()
    assert hash(_key()) == hash(_key())
    assert len({_key(), _key()}) == 1
    assert _key(source_id="piratebay") != _key(source_id="yts")


# --- Z.C. time to live --------------------------------------------------------


def test_an_entry_stored_now_is_valid_now():
    clock = _Clock()
    cache = _cache(clock=clock, ttl_seconds=300.0)
    rows = (_result(),)

    cache.put(_key(), rows)

    assert cache.get(_key()) == rows


def test_an_entry_is_still_valid_a_moment_before_its_ttl():
    clock = _Clock()
    cache = _cache(clock=clock, ttl_seconds=300.0)
    rows = (_result(),)
    cache.put(_key(), rows)

    clock.advance(299.999)

    assert cache.get(_key()) == rows


def test_an_entry_is_expired_exactly_at_its_ttl():
    """The boundary is age < ttl, so the tick the lifetime is reached is
    already too late."""
    clock = _Clock()
    cache = _cache(clock=clock, ttl_seconds=300.0)
    cache.put(_key(), (_result(),))

    clock.advance(300.0)

    assert cache.get(_key()) is None


def test_looking_up_an_expired_entry_drops_it():
    """Expiry is lazy - nothing sweeps in the background - so the lookup that
    finds an entry too old is what removes it."""
    clock = _Clock()
    cache = _cache(clock=clock, ttl_seconds=300.0)
    cache.put(_key(), (_result(),))
    assert len(cache) == 1

    clock.advance(300.0)
    cache.get(_key())

    assert len(cache) == 0


def test_a_cache_hit_does_not_extend_the_lifetime_of_the_entry():
    """Reading an answer does not make it any fresher than it is."""
    clock = _Clock()
    cache = _cache(clock=clock, ttl_seconds=300.0)
    rows = (_result(),)
    cache.put(_key(), rows)

    clock.advance(299.0)
    assert cache.get(_key()) == rows
    clock.advance(1.0)

    assert cache.get(_key()) is None


def test_storing_a_key_again_starts_its_lifetime_over():
    clock = _Clock()
    cache = _cache(clock=clock, ttl_seconds=300.0)
    rows = (_result(),)
    cache.put(_key(), rows)

    clock.advance(250.0)
    cache.put(_key(), rows)
    clock.advance(250.0)

    assert cache.get(_key()) == rows
    clock.advance(50.0)
    assert cache.get(_key()) is None


# --- Z.D. capacity and least-recently-used eviction ---------------------------


def test_the_cache_never_holds_more_entries_than_it_is_allowed():
    cache = _cache(max_entries=3)

    for word in ("a", "b", "c", "d", "e"):
        cache.put(_key(query=word), (_result(),))

    assert len(cache) == 3


def test_the_least_recently_used_entry_is_the_one_evicted():
    cache = _cache(max_entries=3)
    cache.put(_key(query="a"), (_result(),))
    cache.put(_key(query="b"), (_result(),))
    cache.put(_key(query="c"), (_result(),))

    cache.put(_key(query="d"), (_result(),))

    assert cache.get(_key(query="a")) is None
    assert cache.get(_key(query="b")) is not None
    assert cache.get(_key(query="c")) is not None
    assert cache.get(_key(query="d")) is not None


def test_reading_an_entry_saves_it_from_the_next_eviction():
    """A hit is use: the entry read most recently is not the one to drop."""
    cache = _cache(max_entries=3)
    cache.put(_key(query="a"), (_result(),))
    cache.put(_key(query="b"), (_result(),))
    cache.put(_key(query="c"), (_result(),))

    assert cache.get(_key(query="a")) is not None
    cache.put(_key(query="d"), (_result(),))

    assert cache.get(_key(query="a")) is not None
    assert cache.get(_key(query="b")) is None


def test_replacing_an_existing_key_does_not_grow_the_cache():
    cache = _cache(max_entries=3)
    cache.put(_key(query="a"), (_result(),))
    cache.put(_key(query="b"), (_result(),))
    cache.put(_key(query="c"), (_result(),))

    cache.put(_key(query="a"), (_result(name="Newer"),))

    assert len(cache) == 3
    assert cache.get(_key(query="b")) is not None
    assert cache.get(_key(query="c")) is not None


def test_replacing_an_existing_key_returns_the_new_rows():
    cache = _cache(max_entries=3)
    cache.put(_key(), (_result(name="Older"),))

    newer = (_result(name="Newer"),)
    cache.put(_key(), newer)

    assert cache.get(_key()) == newer


def test_replacing_an_existing_key_saves_it_from_the_next_eviction():
    cache = _cache(max_entries=3)
    cache.put(_key(query="a"), (_result(),))
    cache.put(_key(query="b"), (_result(),))
    cache.put(_key(query="c"), (_result(),))

    cache.put(_key(query="a"), (_result(name="Newer"),))
    cache.put(_key(query="d"), (_result(),))

    assert cache.get(_key(query="a")) is not None
    assert cache.get(_key(query="b")) is None


def test_an_expired_lookup_drops_the_entry_instead_of_making_it_recent():
    """An answer too old to use is not use at all - it leaves the cache rather
    than pushing a still-valid neighbour out of it."""
    clock = _Clock()
    cache = _cache(clock=clock, max_entries=3, ttl_seconds=300.0)
    cache.put(_key(query="a"), (_result(),))
    clock.advance(300.0)
    cache.put(_key(query="b"), (_result(),))
    cache.put(_key(query="c"), (_result(),))

    assert cache.get(_key(query="a")) is None
    assert len(cache) == 2

    cache.put(_key(query="d"), (_result(),))

    assert len(cache) == 3
    assert cache.get(_key(query="b")) is not None


# --- Z.E. ownership, emptying and configuration -------------------------------


def test_the_cache_does_not_keep_the_list_the_caller_handed_it():
    """The caller's list is the caller's: what was stored is what was asked
    for, however that list is used afterwards."""
    cache = _cache()
    rows = [_result(info_hash=A)]

    cache.put(_key(), rows)
    rows.append(_result(info_hash=B, name="Other"))

    assert cache.get(_key()) == (_result(info_hash=A),)


def test_a_hit_is_always_a_tuple():
    cache = _cache()

    cache.put(_key(), [_result()])

    assert isinstance(cache.get(_key()), tuple)


def test_clearing_the_cache_empties_it():
    cache = _cache()
    cache.put(_key(query="a"), (_result(),))
    cache.put(_key(query="b"), (_result(),))

    cache.clear()

    assert len(cache) == 0
    assert cache.get(_key(query="a")) is None
    assert cache.get(_key(query="b")) is None


def test_the_default_cache_is_the_production_one():
    """No argument means the shipped policy: five minutes, sixty-four answers,
    and a clock the system time can never move."""
    cache = service._SearchCache()

    assert cache._ttl_seconds == 300.0
    assert cache._max_entries == 64
    assert cache._clock is time.monotonic
    assert service._CACHE_TTL_SECONDS == 300.0
    assert service._CACHE_MAX_ENTRIES == 64


@pytest.mark.parametrize("bad", [0, -1, True, False, 2.5])
def test_a_cache_that_could_hold_nothing_sensible_is_refused(bad):
    with pytest.raises(ValueError):
        service._SearchCache(max_entries=bad)


@pytest.mark.parametrize(
    "bad", [0, 0.0, -1.0, True, False, float("nan"), float("inf"), float("-inf")]
)
def test_a_lifetime_that_is_not_a_real_span_of_time_is_refused(bad):
    with pytest.raises(ValueError):
        service._SearchCache(ttl_seconds=bad)


def test_an_expired_entry_is_dropped_before_a_valid_one_is_evicted():
    """Making room must cost the cache something it could not have used: an
    answer that is merely older than its neighbours is still worth more than
    one that has run out of time, however recently it was read."""
    clock = _Clock()
    cache = _cache(clock=clock, max_entries=3, ttl_seconds=300.0)
    cache.put(_key(query="a"), (_result(),))
    clock.advance(1.0)
    cache.put(_key(query="b"), (_result(),))
    cache.put(_key(query="c"), (_result(),))
    assert cache.get(_key(query="a")) is not None

    # Only "a" has run out of time, and it is the most recently read entry.
    clock.advance(299.0)
    cache.put(_key(query="d"), (_result(),))

    assert cache.get(_key(query="a")) is None
    assert cache.get(_key(query="b")) is not None
    assert cache.get(_key(query="c")) is not None
    assert cache.get(_key(query="d")) is not None


# --- Z.F. cached answers inside the search lifecycle --------------------------

# A cache hit is a source that has already answered. It costs no worker, no
# HTTP facility and no pool thread, but it is still a source of the search that
# asked for it, so it is announced exactly like a live one.


def test_a_second_identical_search_does_not_ask_the_source_again(monkeypatch):
    source = _FakeSource([_result(info_hash=A, source="alpha")], source_id="alpha")
    _select(monkeypatch, [source])
    factory, made = _counting_http_factory()
    svc = service.SearchService(http_factory=factory)
    watch = _Watch(svc)

    svc.start("dune")
    first = _finish(watch)

    watch.finished.clear()
    svc.start("dune")
    second = _finish(watch)

    assert len(source.calls) == 1, "the cached answer was asked for again"
    assert len(made) == 1, "a cache hit built an HTTP facility"
    assert second.results == first.results
    assert second.failures == ()


def test_a_cached_answer_belongs_to_the_search_that_asked_for_it(monkeypatch):
    source = _FakeSource([_result(info_hash=A, source="alpha")], source_id="alpha")
    _select(monkeypatch, [source])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("dune")
    _finish(watch)
    watch.finished.clear()
    watch.statuses.clear()
    watch.results.clear()
    watch.result_generations.clear()

    second = svc.start("dune")
    summary = _finish(watch)

    assert watch.order == [
        ("alpha", service.SourceState.RUNNING),
        ("alpha", service.SourceState.COMPLETED),
    ]
    assert [status.generation for status in watch.statuses] == [second, second]
    assert watch.statuses[-1].result_count == 1
    assert watch.statuses[-1].error_kind is None
    assert watch.result_generations == [second]
    assert summary.generation == second
    assert svc.active is False
    assert len(source.calls) == 1, "the second search was not served from cache"


def _prime(svc, watch, query="dune", category=Category.ALL):
    """Run one live search to the end, and forget everything it said.

    What is left behind is the cache it filled, which is the starting point
    every test below needs: a source that has already answered once.
    """
    svc.start(query, category)
    _finish(watch)
    watch.statuses.clear()
    watch.results.clear()
    watch.result_generations.clear()
    watch.finished.clear()


def test_a_service_owns_one_production_cache_by_default():
    svc = service.SearchService(http_factory=_FakeHttp)

    assert isinstance(svc._cache, service._SearchCache)
    assert svc._cache._max_entries == 64
    assert svc._cache._ttl_seconds == 300.0
    assert svc._cache._clock is time.monotonic


def test_the_cache_outlives_the_searches_that_fill_and_abandon_it(monkeypatch):
    """Emptying it on any of these would leave it with nothing to reuse."""
    source = _FakeSource([_result(info_hash=A, source="alpha")], source_id="alpha")
    _select(monkeypatch, [source])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)
    _prime(svc, watch)

    svc.start("   ")
    _finish(watch)
    watch.finished.clear()
    svc.cancel()
    svc.start("dune")
    _finish(watch)

    assert len(source.calls) == 1


# --- Z.G. a cached answer that found nothing ----------------------------------


def test_a_cached_empty_answer_is_a_hit_and_not_another_call(monkeypatch):
    source = _FakeSource([], source_id="alpha")
    _select(monkeypatch, [source])
    factory, made = _counting_http_factory()
    svc = service.SearchService(http_factory=factory)
    watch = _Watch(svc)
    _prime(svc, watch)

    summary = None
    svc.start("dune")
    summary = _finish(watch)

    assert len(source.calls) == 1, "a cached empty answer was treated as a miss"
    assert len(made) == 1, "a cached empty answer built an HTTP facility"
    assert summary.results == ()
    assert summary.failures == ()
    assert watch.order == [
        ("alpha", service.SourceState.RUNNING),
        ("alpha", service.SourceState.COMPLETED),
    ]
    assert watch.statuses[-1].result_count == 0


# --- Z.H. what makes one cached answer that answer ----------------------------


def test_surrounding_whitespace_reuses_the_same_cached_answer(monkeypatch):
    """The key is the text the source was given, not the text the user typed."""
    source = _FakeSource([_result(info_hash=A, source="alpha")], source_id="alpha")
    _select(monkeypatch, [source])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("  dune  ")
    _finish(watch)
    watch.finished.clear()
    svc.start("dune")
    _finish(watch)
    watch.finished.clear()
    # And from the other direction too: what is stored and what is looked up
    # have to be the same normalisation, not merely two that happen to agree.
    svc.start("\tdune\n")
    _finish(watch)

    assert [call[0] for call in source.calls] == ["dune"]


def test_a_differently_cased_query_is_not_the_same_answer(monkeypatch):
    source = _FakeSource([_result(info_hash=A, source="alpha")], source_id="alpha")
    _select(monkeypatch, [source])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("Dune")
    _finish(watch)
    watch.finished.clear()
    svc.start("dune")
    _finish(watch)

    assert [call[0] for call in source.calls] == ["Dune", "dune"]


def test_two_categories_of_one_query_do_not_share_a_cached_answer(monkeypatch):
    source = _FakeSource([_result(info_hash=A, source="alpha")], source_id="alpha")
    _select(monkeypatch, [source])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("dune", Category.MOVIES)
    _finish(watch)
    watch.finished.clear()
    # The same category again is a hit, so the miss below is about the
    # category and not about the key having missed the category out.
    svc.start("dune", Category.MOVIES)
    _finish(watch)
    watch.finished.clear()
    svc.start("dune", Category.TV)
    _finish(watch)

    assert [call[1] for call in source.calls] == [Category.MOVIES, Category.TV]


def test_one_sources_cached_answer_is_not_another_sources(monkeypatch):
    alpha = _FakeSource([_result(info_hash=A, source="alpha")], source_id="alpha")
    beta = _FakeSource([_result(info_hash=B, source="beta")], source_id="beta")
    _select(monkeypatch, [alpha])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)
    _prime(svc, watch)

    _select(monkeypatch, [alpha, beta])
    svc.start("dune")
    _finish(watch)

    assert len(alpha.calls) == 1, "alpha's own answer was not reused"
    assert len(beta.calls) == 1, "beta was served alpha's answer"


# --- Z.I. a search every source can answer from cache -------------------------


def test_a_wholly_cached_search_runs_no_worker_and_arms_no_deadline(monkeypatch):
    alpha = _FakeSource([_result(info_hash=A, source="alpha")], source_id="alpha")
    beta = _FakeSource([_result(info_hash=B, source="beta")], source_id="beta")
    _select(monkeypatch, [alpha, beta])
    factory, made = _counting_http_factory()
    svc = _short_service(http_factory=factory)
    watch = _Watch(svc)
    _prime(svc, watch)

    generation = svc.start("dune")

    # Nothing was waited for, so it is all already said.
    assert len(watch.finished) == 1
    assert svc._deadline.isActive() is False, "a search with no live work armed a deadline"
    assert svc._calls == {}
    assert svc._cache_keys == {}
    assert svc.active is False
    assert len(alpha.calls) == 1 and len(beta.calls) == 1
    assert len(made) == 2, "a cache hit built an HTTP facility"
    assert watch.order == [
        ("alpha", service.SourceState.RUNNING),
        ("beta", service.SourceState.RUNNING),
        ("alpha", service.SourceState.COMPLETED),
        ("beta", service.SourceState.COMPLETED),
    ]
    summary = watch.finished[0]
    assert summary.generation == generation
    assert summary.failures == ()
    assert summary.results == aggregate(
        [_result(info_hash=A, source="alpha"), _result(info_hash=B, source="beta")]
    ).results

    # Well past the short deadline: nothing may time out a search that is over.
    _pump(lambda: False, seconds=0.3)
    assert len(watch.finished) == 1
    assert [status.state for status in watch.statuses].count(
        service.SourceState.TIMED_OUT
    ) == 0


def test_a_wholly_cached_empty_search_still_finishes_once(monkeypatch):
    alpha = _FakeSource([], source_id="alpha")
    beta = _FakeSource([], source_id="beta")
    _select(monkeypatch, [alpha, beta])
    svc = _short_service()
    watch = _Watch(svc)
    _prime(svc, watch)

    svc.start("dune")

    assert len(watch.finished) == 1
    assert watch.finished[0].results == ()
    assert svc._deadline.isActive() is False
    assert len(alpha.calls) == 1 and len(beta.calls) == 1
    assert [status.result_count for status in watch.statuses] == [0, 0, 0, 0]
    _pump(lambda: False, seconds=0.3)
    assert len(watch.finished) == 1


# --- Z.J. a search that is part cached and part live --------------------------


def test_a_cached_source_answers_before_its_live_peer_does(monkeypatch):
    cached_rows = [_result(info_hash=A, source="alpha")]
    live_rows = [_result(info_hash=B, source="beta")]
    alpha = _FakeSource(cached_rows, source_id="alpha")
    _select(monkeypatch, [alpha])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)
    _prime(svc, watch)

    release = threading.Event()
    beta = _held_source(live_rows, source_id="beta", release=release)
    _select(monkeypatch, [alpha, beta])

    try:
        generation = svc.start("dune")
        # alpha is finished from cache while beta is still inside search().
        assert watch.states("alpha") == [
            service.SourceState.RUNNING,
            service.SourceState.COMPLETED,
        ]
        assert watch.states("beta") == [service.SourceState.RUNNING]
        assert watch.results[-1] == aggregate(cached_rows).results
        assert svc.active is True
        assert svc._deadline.isActive() is True, "live work was left unbounded"
        assert list(svc._calls) == [(generation, "beta")], "a cache hit pinned a worker"
        assert list(svc._cache_keys) == [(generation, "beta")]
    finally:
        release.set()

    summary = _finish(watch)
    assert summary.results == aggregate(cached_rows + live_rows).results
    assert summary.failures == ()
    assert svc._deadline.isActive() is False
    assert svc._calls == {} and svc._cache_keys == {}
    assert len(alpha.calls) == 1


def test_a_cached_row_and_a_live_row_merge_exactly_as_aggregate_would(monkeypatch):
    cached_rows = [
        _result(info_hash=A, name="Dune Cached", source="alpha", seeders=9, size_bytes=None)
    ]
    live_rows = [
        _result(info_hash=A, name="Dune Live", source="beta", seeders=3, size_bytes=4096)
    ]
    alpha = _FakeSource(cached_rows, source_id="alpha")
    _select(monkeypatch, [alpha])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)
    _prime(svc, watch)

    beta = _FakeSource(live_rows, source_id="beta")
    _select(monkeypatch, [alpha, beta])
    svc.start("dune")
    summary = _finish(watch)

    expected = aggregate(cached_rows + live_rows)
    assert summary.results == expected.results
    assert summary.dedupe_dropped == expected.dedupe_dropped == 1
    assert summary.results[0].size_bytes == 4096, "the cached winner was not backfilled"
    assert len(alpha.calls) == 1


def test_a_cached_answer_survives_a_live_peer_failing(monkeypatch):
    cached_rows = [_result(info_hash=A, source="alpha")]
    alpha = _FakeSource(cached_rows, source_id="alpha")
    _select(monkeypatch, [alpha])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)
    _prime(svc, watch)

    beta = _FakeSource(
        raises=SourceError(SourceErrorKind.NETWORK), source_id="beta"
    )
    _select(monkeypatch, [alpha, beta])
    svc.start("dune")
    summary = _finish(watch)

    assert summary.results == aggregate(cached_rows).results
    assert summary.failures == (service.SourceFailure("beta", "network"),)
    assert watch.states("alpha") == [
        service.SourceState.RUNNING,
        service.SourceState.COMPLETED,
    ]
    assert watch.states("beta") == [
        service.SourceState.RUNNING,
        service.SourceState.FAILED,
    ]
    assert len(alpha.calls) == 1


def test_a_cached_answer_survives_a_live_peer_timing_out(monkeypatch):
    cached_rows = [_result(info_hash=A, source="alpha")]
    alpha = _FakeSource(cached_rows, source_id="alpha")
    _select(monkeypatch, [alpha])
    svc = _short_service()
    watch = _Watch(svc)
    _prime(svc, watch)

    release = threading.Event()
    beta = _held_source([], source_id="beta", release=release)
    _select(monkeypatch, [alpha, beta])

    try:
        svc.start("dune")
        summary = _finish(watch)
    finally:
        release.set()

    assert summary.results == aggregate(cached_rows).results
    assert summary.failures == (service.SourceFailure("beta", _TIMEOUT_KIND),)
    assert watch.states("beta") == [
        service.SourceState.RUNNING,
        service.SourceState.TIMED_OUT,
    ]
    assert svc.active is False
    assert svc._deadline.isActive() is False
    assert len(alpha.calls) == 1
    assert _drained(svc)


# --- Z.K. what is never worth keeping -----------------------------------------


def test_a_failed_source_is_asked_again_by_the_next_search(monkeypatch):
    source = _FakeSource(
        raises=SourceError(SourceErrorKind.NETWORK), source_id="alpha"
    )
    _select(monkeypatch, [source])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)
    _prime(svc, watch)

    svc.start("dune")
    summary = _finish(watch)

    assert len(source.calls) == 2, "a failure was cached"
    assert summary.failures == (service.SourceFailure("alpha", "network"),)


def test_a_source_that_broke_is_asked_again_by_the_next_search(monkeypatch):
    source = _FakeSource(raises=RuntimeError("boom"), source_id="alpha")
    _select(monkeypatch, [source])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)
    _prime(svc, watch)

    svc.start("dune")
    summary = _finish(watch)

    assert len(source.calls) == 2, "an internal failure was cached"
    assert summary.failures == (service.SourceFailure("alpha", "internal"),)


def test_a_timed_out_sources_late_answer_is_never_cached(monkeypatch):
    """Its worker succeeds, but the search had already stopped taking answers."""
    rows = [_result(info_hash=A, source="alpha")]
    release = threading.Event()
    held = _held_source(rows, source_id="alpha", release=release)
    _select(monkeypatch, [held])
    svc = _short_service()
    watch = _Watch(svc)

    try:
        svc.start("dune")
        first = _finish(watch)
        assert first.failures == (service.SourceFailure("alpha", _TIMEOUT_KIND),)
    finally:
        release.set()
    assert _drained(svc), "the late answer was never handled"

    watch.finished.clear()
    svc.start("dune")
    second = _finish(watch)

    assert len(held.calls) == 2, "a timed-out search's late answer seeded the cache"
    assert second.results == aggregate(rows).results


def test_a_superseded_searchs_late_answer_is_never_cached(monkeypatch):
    rows = [_result(info_hash=A, source="alpha")]
    release = threading.Event()
    held = _held_source(rows, source_id="alpha", release=release)
    _select(monkeypatch, [held])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    try:
        svc.start("dune")
        svc.start("arrakis")
    finally:
        release.set()
    _finish(watch)
    assert _drained(svc)
    # Both workers have been asked and have finished by now, so what each was
    # asked is settled - only the order they recorded it in is not.
    assert _queries(held) == Counter({"dune": 1, "arrakis": 1}), (
        "the superseded search and the one that replaced it were not both asked"
    )

    watch.finished.clear()
    svc.start("dune")
    _finish(watch)

    assert _queries(held) == Counter({"dune": 2, "arrakis": 1}), (
        "a superseded search's late answer seeded the cache"
    )


def test_a_cancelled_searchs_late_answer_is_never_cached(monkeypatch):
    rows = [_result(info_hash=A, source="alpha")]
    release = threading.Event()
    held = _held_source(rows, source_id="alpha", release=release)
    _select(monkeypatch, [held])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    try:
        svc.start("dune")
        svc.cancel()
    finally:
        release.set()
    assert _drained(svc)
    assert watch.finished == []

    svc.start("dune")
    _finish(watch)

    assert [call[0] for call in held.calls] == ["dune", "dune"], (
        "a cancelled search's late answer seeded the cache"
    )


# --- Z.L. an answer only stays worth reusing for so long ----------------------


def test_a_cached_answer_expires_and_the_source_is_asked_again(monkeypatch):
    clock = _Clock()
    source = _FakeSource([_result(info_hash=A, source="alpha")], source_id="alpha")
    _select(monkeypatch, [source])
    svc = service.SearchService(
        http_factory=_FakeHttp, cache=service._SearchCache(clock=clock)
    )
    watch = _Watch(svc)

    _prime(svc, watch)
    assert len(source.calls) == 1

    clock.advance(service._CACHE_TTL_SECONDS - 1)
    svc.start("dune")
    _finish(watch)
    watch.finished.clear()
    assert len(source.calls) == 1, "a still-valid answer was not reused"

    clock.advance(1)
    svc.start("dune")
    summary = _finish(watch)

    assert len(source.calls) == 2, "an expired answer was reused"
    assert summary.results == aggregate([_result(info_hash=A, source="alpha")]).results


# --- Z.M. a listener that changes the search from inside a cached completion --


def test_starting_from_a_cached_sources_running_status_stops_the_old_search(
    monkeypatch,
):
    alpha = _FakeSource([_result(info_hash=A, source="alpha")], source_id="alpha")
    _select(monkeypatch, [alpha])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)
    _prime(svc, watch)

    later = []

    def restart(status):
        if status.state is service.SourceState.RUNNING and not later:
            later.append(svc.start("arrakis"))

    svc.source_status.connect(restart)
    first = svc.start("dune")
    _finish(watch)

    assert later and later[0] != first
    assert [
        status.generation
        for status in watch.statuses
        if status.state is service.SourceState.COMPLETED
    ] == [later[0]]
    assert watch.result_generations == [later[0]]
    assert watch.finished[0].generation == later[0]


def test_cancelling_from_a_cached_sources_running_status_stops_the_old_search(
    monkeypatch,
):
    alpha = _FakeSource([_result(info_hash=A, source="alpha")], source_id="alpha")
    _select(monkeypatch, [alpha])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)
    _prime(svc, watch)

    def stop(status):
        if status.state is service.SourceState.RUNNING:
            svc.cancel()

    svc.source_status.connect(stop)
    svc.start("dune")

    assert watch.finished == []
    assert watch.results == []
    assert [status.state for status in watch.statuses] == [
        service.SourceState.RUNNING
    ]
    assert svc.active is False
    assert svc._deadline.isActive() is False


def test_starting_from_a_cached_completion_stops_the_old_search(monkeypatch):
    alpha = _FakeSource([_result(info_hash=A, source="alpha")], source_id="alpha")
    beta = _FakeSource([_result(info_hash=B, source="beta")], source_id="beta")
    _select(monkeypatch, [alpha, beta])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)
    _prime(svc, watch)

    later = []

    def restart(status):
        if status.state is service.SourceState.COMPLETED and not later:
            later.append(svc.start("arrakis"))

    svc.source_status.connect(restart)
    first = svc.start("dune")
    _finish(watch)

    completed = [
        (status.source_id, status.generation)
        for status in watch.statuses
        if status.state is service.SourceState.COMPLETED
    ]
    # The old search stopped at alpha: beta was never completed for it.
    assert [pair for pair in completed if pair[1] == first] == [("alpha", first)]
    # The new search completed both of its own, in whichever order its two live
    # sources happened to answer.
    assert sorted(pair for pair in completed if pair[1] == later[0]) == [
        ("alpha", later[0]),
        ("beta", later[0]),
    ]
    assert set(watch.result_generations) == {later[0]}
    assert len(watch.finished) == 1
    assert watch.finished[0].generation == later[0]


def test_cancelling_from_a_cached_results_view_stops_the_old_search(monkeypatch):
    alpha = _FakeSource([_result(info_hash=A, source="alpha")], source_id="alpha")
    beta = _FakeSource([_result(info_hash=B, source="beta")], source_id="beta")
    _select(monkeypatch, [alpha, beta])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)
    _prime(svc, watch)

    svc.results_updated.connect(lambda generation, results: svc.cancel())
    svc.start("dune")

    assert watch.finished == [], "a cancelled search still summarised itself"
    assert len(watch.results) == 1
    assert watch.states("beta") == [service.SourceState.RUNNING]
    assert svc.active is False
    assert svc._deadline.isActive() is False


def test_a_reentrant_search_stops_the_old_one_before_its_live_source_is_submitted(
    monkeypatch,
):
    """The old search's miss must never reach the pool once it is replaced."""
    alpha = _FakeSource([_result(info_hash=A, source="alpha")], source_id="alpha")
    _select(monkeypatch, [alpha])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)
    _prime(svc, watch)

    beta = _FakeSource([_result(info_hash=B, source="beta")], source_id="beta")
    _select(monkeypatch, [alpha, beta])

    later = []

    def restart(status):
        if status.state is service.SourceState.COMPLETED and not later:
            later.append(svc.start("arrakis"))

    svc.source_status.connect(restart)
    svc.start("dune")
    _finish(watch)

    assert [call[0] for call in beta.calls] == ["arrakis"], (
        "the replaced search still submitted its live source"
    )
    assert svc._deadline_generation in (0, later[0])
    assert len(watch.finished) == 1
    assert watch.finished[0].generation == later[0]


def test_cancelling_from_a_cached_completion_leaves_no_live_work_behind(monkeypatch):
    alpha = _FakeSource([_result(info_hash=A, source="alpha")], source_id="alpha")
    _select(monkeypatch, [alpha])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)
    _prime(svc, watch)

    beta = _FakeSource([_result(info_hash=B, source="beta")], source_id="beta")
    _select(monkeypatch, [alpha, beta])

    def stop(status):
        if status.state is service.SourceState.COMPLETED:
            svc.cancel()

    svc.source_status.connect(stop)
    svc.start("dune")

    assert beta.calls == [], "a cancelled search still submitted its live source"
    assert svc._deadline.isActive() is False, "a cancelled search armed a deadline"
    assert svc._calls == {} and svc._cache_keys == {}
    assert svc.active is False
    assert watch.finished == []


# --- Z.N. what a search records about itself ----------------------------------
#
# Diagnostics is observation only: every assertion below is about what was
# *recorded*, and every existing assertion about what the service *does* stays
# exactly as it was. The search term itself, the rows it found, their magnets
# and their info hashes are none of the log's business - only counts and the
# names the service already publishes.

import json  # noqa: E402

from cove import diagnostics as diag_module  # noqa: E402

# The one component every Search event is recorded under.
_DIAG_COMPONENT = "search"


@pytest.fixture
def diag(tmp_path):
    """The real app logger, writing where the test can read it back."""
    diag_module.shutdown_logger()
    log = diag_module.init_app_logger(tmp_path / "diag")
    yield log
    diag_module.shutdown_logger()


def _events(log, event=None):
    """Every Search record, optionally only the ones named ."""
    out = []
    for record in log.records():
        if record["component"] != _DIAG_COMPONENT:
            continue
        if event is not None and record["event"] != event:
            continue
        out.append(record)
    return out


def _one_event(log, event):
    found = _events(log, event)
    assert len(found) == 1, "expected one {}, got {}".format(event, len(found))
    return found[0]


def _fields(record):
    return record.get("fields", {})


def _names(log):
    return [record["event"] for record in _events(log)]


def _trail(log, source_id):
    """The events one source went through, in order."""
    return [
        record["event"]
        for record in _events(log)
        if _fields(record).get("source") == source_id
    ]


def _recorded_text(log):
    """Everything every Search event recorded, as one searchable blob."""
    return json.dumps([_fields(record) for record in _events(log)])


# --- Z.N.A. a search names itself at both ends --------------------------------


def test_a_search_records_one_start_and_one_finish(monkeypatch, diag):
    _select(monkeypatch, [_FakeSource([_result(source="alpha")], source_id="alpha")])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("dune")

    _finish(watch)
    assert len(_events(diag, "search_started")) == 1
    assert len(_events(diag, "search_finished")) == 1


def test_the_start_record_names_the_search_its_category_and_its_sources(
    monkeypatch, diag
):
    _select(monkeypatch, [
        _FakeSource([], source_id="alpha"),
        _FakeSource([], source_id="beta"),
    ])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    generation = svc.start("dune", Category.MOVIES)

    _finish(watch)
    fields = _fields(_one_event(diag, "search_started"))
    assert fields["generation"] == generation
    assert fields["category"] == "movies"
    assert fields["source_count"] == 2


def test_the_start_record_carries_the_length_of_the_normalised_query(
    monkeypatch, diag
):
    _select(monkeypatch, [_FakeSource([], source_id="alpha")])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("   dune   ")

    _finish(watch)
    fields = _fields(_one_event(diag, "search_started"))
    assert fields["query_length"] == 4


def test_no_search_record_carries_the_query_text(monkeypatch, diag):
    """The UI knows what was asked. The log must not keep it."""
    _select(monkeypatch, [_FakeSource([_result(source="alpha")], source_id="alpha")])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("SECRET_SEARCH_TERM_123")

    _finish(watch)
    assert _events(diag), "the search recorded nothing at all"
    assert "SECRET_SEARCH_TERM_123" not in _recorded_text(diag)


def test_the_finish_record_carries_the_summary_counts(monkeypatch, diag):
    rows = [_result(info_hash=A, source="alpha"), _result(info_hash=A, source="alpha")]
    _select(monkeypatch, [_FakeSource(rows, source_id="alpha")])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    generation = svc.start("dune")

    summary = _finish(watch)
    fields = _fields(_one_event(diag, "search_finished"))
    assert fields["generation"] == generation
    assert fields["result_count"] == len(summary.results)
    assert fields["dedupe_dropped"] == summary.dedupe_dropped
    assert fields["failure_count"] == len(summary.failures)


# --- Z.N.B. a source the cache could not answer -------------------------------


def test_a_live_source_is_recorded_started_missed_then_completed(monkeypatch, diag):
    rows = [_result(info_hash=A, source="alpha"), _result(info_hash=B, source="alpha")]
    _select(monkeypatch, [_FakeSource(rows, source_id="alpha")])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("dune")

    _finish(watch)
    assert _trail(diag, "alpha") == [
        "source_started",
        "source_cache_miss",
        "source_completed",
    ]


def test_a_live_source_record_names_the_search_and_what_it_found(monkeypatch, diag):
    rows = [_result(info_hash=A, source="alpha"), _result(info_hash=B, source="alpha")]
    _select(monkeypatch, [_FakeSource(rows, source_id="alpha")])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    generation = svc.start("dune", Category.MOVIES)

    _finish(watch)
    started = _fields(_one_event(diag, "source_started"))
    assert started == {
        "generation": generation,
        "source": "alpha",
        "category": "movies",
    }
    completed = _fields(_one_event(diag, "source_completed"))
    assert completed["generation"] == generation
    assert completed["source"] == "alpha"
    assert completed["result_count"] == 2


def test_a_live_source_records_no_cache_hit(monkeypatch, diag):
    _select(monkeypatch, [_FakeSource([_result(source="alpha")], source_id="alpha")])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("dune")

    _finish(watch)
    assert _events(diag, "source_cache_hit") == []


def test_the_search_start_is_recorded_before_any_source(monkeypatch, diag):
    _select(monkeypatch, [
        _FakeSource([], source_id="alpha"),
        _FakeSource([], source_id="beta"),
    ])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("dune")

    _finish(watch)
    names = _names(diag)
    assert names[0] == "search_started"
    assert names[-1] == "search_finished"


def test_no_search_record_carries_a_result_title_magnet_or_info_hash(
    monkeypatch, diag
):
    """Counts only: what a source found is the UI's business, not the log's."""
    row = _result(info_hash=_hash("f"), name="SECRET_TITLE_123", source="alpha")
    _select(monkeypatch, [_FakeSource([row], source_id="alpha")])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("dune")

    _finish(watch)
    recorded = _recorded_text(diag)
    assert _events(diag), "the search recorded nothing at all"
    assert "SECRET_TITLE_123" not in recorded
    assert row.info_hash not in recorded
    assert "magnet:" not in recorded


# --- Z.N.C. a source the cache could answer -----------------------------------


def test_a_cached_source_is_recorded_started_hit_then_completed(monkeypatch, diag):
    source = _FakeSource([_result(info_hash=A, source="alpha")], source_id="alpha")
    _select(monkeypatch, [source])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)
    _prime(svc, watch)
    diag.clear()

    generation = svc.start("dune")

    _finish(watch)
    assert _trail(diag, "alpha") == [
        "source_started",
        "source_cache_hit",
        "source_completed",
    ]
    assert _events(diag, "source_cache_miss") == []
    hit = _fields(_one_event(diag, "source_cache_hit"))
    assert hit["generation"] == generation
    assert hit["result_count"] == 1
    assert _fields(_one_event(diag, "source_completed"))["generation"] == generation


def test_a_cached_answer_that_found_nothing_is_recorded_as_none(monkeypatch, diag):
    source = _FakeSource([], source_id="alpha")
    _select(monkeypatch, [source])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)
    _prime(svc, watch)
    diag.clear()

    svc.start("dune")

    _finish(watch)
    assert _fields(_one_event(diag, "source_cache_hit"))["result_count"] == 0
    assert _fields(_one_event(diag, "source_completed"))["result_count"] == 0
    assert len(source.calls) == 1, "the empty cached answer was asked for again"


def test_a_part_cached_search_records_a_hit_and_a_miss(monkeypatch, diag):
    alpha = _FakeSource([_result(info_hash=A, source="alpha")], source_id="alpha")
    _select(monkeypatch, [alpha])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)
    _prime(svc, watch)

    beta = _FakeSource([_result(info_hash=B, source="beta")], source_id="beta")
    _select(monkeypatch, [alpha, beta])
    diag.clear()

    svc.start("dune")

    _finish(watch)
    assert _trail(diag, "alpha") == [
        "source_started",
        "source_cache_hit",
        "source_completed",
    ]
    assert _trail(diag, "beta") == [
        "source_started",
        "source_cache_miss",
        "source_completed",
    ]
    assert len(_events(diag, "search_finished")) == 1


def test_a_search_every_source_answers_from_cache_records_no_miss(monkeypatch, diag):
    alpha = _FakeSource([_result(info_hash=A, source="alpha")], source_id="alpha")
    beta = _FakeSource([_result(info_hash=B, source="beta")], source_id="beta")
    _select(monkeypatch, [alpha, beta])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)
    _prime(svc, watch)
    diag.clear()

    svc.start("dune")

    _finish(watch)
    assert _events(diag, "source_cache_miss") == []
    assert len(_events(diag, "source_cache_hit")) == 2
    assert len(_events(diag, "source_completed")) == 2
    assert len(_events(diag, "search_finished")) == 1


# --- Z.N.D. a source that fails -----------------------------------------------


def test_a_source_error_is_recorded_once_under_its_own_kind(monkeypatch, diag):
    failing = _FakeSource(raises=SourceError(SourceErrorKind.NETWORK), source_id="alpha")
    _select(monkeypatch, [failing])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    generation = svc.start("dune")

    summary = _finish(watch)
    failed = _fields(_one_event(diag, "source_failed"))
    assert failed["generation"] == generation
    assert failed["source"] == "alpha"
    assert failed["error_kind"] == summary.failures[0].error_kind
    assert failed["error_kind"] == SourceErrorKind.NETWORK.value


def test_a_source_that_breaks_is_recorded_as_an_internal_failure(monkeypatch, diag):
    failing = _FakeSource(raises=RuntimeError("boom"), source_id="alpha")
    _select(monkeypatch, [failing])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("dune")

    _finish(watch)
    assert _fields(_one_event(diag, "source_failed"))["error_kind"] == "internal"


def test_a_failing_source_gets_no_other_terminal_record(monkeypatch, diag):
    failing = _FakeSource(raises=SourceError(SourceErrorKind.PARSE), source_id="alpha")
    _select(monkeypatch, [failing])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("dune")

    _finish(watch)
    assert _trail(diag, "alpha") == [
        "source_started",
        "source_cache_miss",
        "source_failed",
    ]


def test_a_failure_records_nothing_of_the_exception_itself(monkeypatch, diag):
    failing = _FakeSource(
        raises=RuntimeError("SECRET_EXCEPTION_TEXT_123"), source_id="alpha"
    )
    _select(monkeypatch, [failing])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("dune")

    _finish(watch)
    assert "SECRET_EXCEPTION_TEXT_123" not in json.dumps(_events(diag))


def test_a_partly_failing_search_records_both_sources_and_one_finish(
    monkeypatch, diag
):
    good = _FakeSource([_result(info_hash=A, source="alpha")], source_id="alpha")
    bad = _FakeSource(raises=SourceError(SourceErrorKind.HTTP), source_id="beta")
    _select(monkeypatch, [good, bad])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("dune")

    summary = _finish(watch)
    assert _trail(diag, "alpha")[-1] == "source_completed"
    assert _trail(diag, "beta")[-1] == "source_failed"
    finished = _fields(_one_event(diag, "search_finished"))
    assert finished["failure_count"] == len(summary.failures) == 1
    assert finished["result_count"] == len(summary.results) == 1


# --- Z.N.E. a source that runs out of time ------------------------------------


def test_a_timed_out_source_is_recorded_once_as_timed_out(monkeypatch, diag):
    release = threading.Event()
    held = _held_source([_result()], source_id="alpha", release=release)
    _select(monkeypatch, [held])
    svc = _short_service()
    watch = _Watch(svc)

    try:
        generation = svc.start("dune")
        _finish(watch)
    finally:
        release.set()
    assert _drained(svc)

    timed_out = _fields(_one_event(diag, "source_timed_out"))
    assert timed_out["generation"] == generation
    assert timed_out["source"] == "alpha"
    assert timed_out["deadline_ms"] == _TEST_DEADLINE_MS


def test_a_timed_out_source_is_never_also_recorded_as_failed(monkeypatch, diag):
    """One terminal record per source, or the log double-counts the failure."""
    release = threading.Event()
    held = _held_source([_result()], source_id="alpha", release=release)
    _select(monkeypatch, [held])
    svc = _short_service()
    watch = _Watch(svc)

    try:
        svc.start("dune")
        summary = _finish(watch)
    finally:
        release.set()
    assert _drained(svc)

    assert _events(diag, "source_failed") == []
    assert _trail(diag, "alpha") == [
        "source_started",
        "source_cache_miss",
        "source_timed_out",
    ]
    finished = _fields(_one_event(diag, "search_finished"))
    assert finished["failure_count"] == len(summary.failures) == 1


def test_every_source_that_runs_out_of_time_is_recorded_in_the_stated_order(
    monkeypatch, diag
):
    release = threading.Event()
    held = [
        _held_source([], source_id=source_id, release=release)
        for source_id in ("nyaa", "yts")
    ]
    _select(monkeypatch, held)
    svc = _short_service()
    watch = _Watch(svc)

    try:
        svc.start("dune")
        _finish(watch)
    finally:
        release.set()
    assert _drained(svc)

    recorded = [
        _fields(record)["source"] for record in _events(diag, "source_timed_out")
    ]
    announced = [
        status.source_id
        for status in watch.statuses
        if status.state is service.SourceState.TIMED_OUT
    ]
    assert recorded == announced
    assert len(_events(diag, "search_finished")) == 1


# --- Z.N.F. superseding is not cancelling -------------------------------------


def test_a_superseded_search_is_recorded_as_superseded_by_the_new_one(
    monkeypatch, diag
):
    release = threading.Event()
    held = _held_source([], source_id="alpha", release=release)
    _select(monkeypatch, [held])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    try:
        first = svc.start("dune")
        _select(monkeypatch, [_FakeSource([], source_id="beta")])
        second = svc.start("akira")
    finally:
        release.set()
    _finish(watch)
    assert _drained(svc)

    superseded = _fields(_one_event(diag, "search_superseded"))
    assert superseded["generation"] == first
    assert superseded["superseded_by"] == second
    assert _events(diag, "search_cancelled") == []


def test_a_superseded_search_is_never_recorded_as_finished(monkeypatch, diag):
    release = threading.Event()
    held = _held_source([], source_id="alpha", release=release)
    _select(monkeypatch, [held])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    try:
        first = svc.start("dune")
        _select(monkeypatch, [_FakeSource([], source_id="beta")])
        svc.start("akira")
    finally:
        release.set()
    _finish(watch)
    assert _drained(svc)

    finished = [_fields(record)["generation"] for record in _events(diag, "search_finished")]
    assert first not in finished


def test_a_search_that_follows_a_finished_one_supersedes_nothing(monkeypatch, diag):
    _select(monkeypatch, [_FakeSource([], source_id="alpha")])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("dune")
    _finish(watch)
    watch.finished.clear()
    svc.start("akira")
    _finish(watch)

    assert _events(diag, "search_superseded") == []


def test_a_cancelled_search_is_recorded_as_cancelled(monkeypatch, diag):
    release = threading.Event()
    held = _held_source([], source_id="alpha", release=release)
    _select(monkeypatch, [held])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    try:
        generation = svc.start("dune")
        svc.cancel()
    finally:
        release.set()
    assert _drained(svc)

    cancelled = _fields(_one_event(diag, "search_cancelled"))
    assert cancelled["generation"] == generation
    assert cancelled["pending_source_count"] == 1
    assert _events(diag, "search_superseded") == []
    assert _events(diag, "search_finished") == []


def test_cancelling_an_idle_service_records_nothing(diag):
    svc = service.SearchService(http_factory=_FakeHttp)

    svc.cancel()

    assert _events(diag) == []


def test_cancelling_after_a_search_finished_records_nothing_more(monkeypatch, diag):
    _select(monkeypatch, [_FakeSource([], source_id="alpha")])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    svc.start("dune")
    _finish(watch)
    svc.cancel()

    assert _events(diag, "search_cancelled") == []


# --- Z.N.G. a late answer nobody is waiting for -------------------------------


def test_a_superseded_searchs_late_answer_records_no_terminal_event(
    monkeypatch, diag
):
    release = threading.Event()
    held = _held_source([_result(info_hash=A, source="alpha")], source_id="alpha",
                        release=release)
    _select(monkeypatch, [held])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    try:
        first = svc.start("dune")
        _select(monkeypatch, [_FakeSource([], source_id="beta")])
        svc.start("akira")
    finally:
        release.set()
    _finish(watch)
    assert _drained(svc)

    assert _trail(diag, "alpha") == ["source_started", "source_cache_miss"]
    for record in _events(diag, "source_completed") + _events(diag, "source_failed"):
        assert _fields(record)["generation"] != first


def test_a_cancelled_searchs_late_answer_records_no_terminal_event(
    monkeypatch, diag
):
    release = threading.Event()
    held = _held_source([_result(info_hash=A, source="alpha")], source_id="alpha",
                        release=release)
    _select(monkeypatch, [held])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    try:
        svc.start("dune")
        svc.cancel()
    finally:
        release.set()
    assert _drained(svc)

    assert _trail(diag, "alpha") == ["source_started", "source_cache_miss"]
    assert _events(diag, "source_completed") == []
    assert _events(diag, "search_finished") == []


def test_a_timed_out_sources_late_answer_records_no_second_terminal_event(
    monkeypatch, diag
):
    release = threading.Event()
    held = _held_source([_result(info_hash=A, source="alpha")], source_id="alpha",
                        release=release)
    _select(monkeypatch, [held])
    svc = _short_service()
    watch = _Watch(svc)

    try:
        svc.start("dune")
        _finish(watch)
    finally:
        release.set()
    assert _drained(svc)

    assert _trail(diag, "alpha") == [
        "source_started",
        "source_cache_miss",
        "source_timed_out",
    ]
    assert len(_events(diag, "search_finished")) == 1


# --- Z.N.H. a search that never reaches a source ------------------------------


def test_a_whitespace_query_still_records_a_whole_lifecycle(monkeypatch, diag):
    _select(monkeypatch, [_FakeSource([_result()], source_id="alpha")])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    generation = svc.start("   ")

    _finish(watch)
    assert _names(diag) == ["search_started", "search_finished"]
    started = _fields(_one_event(diag, "search_started"))
    assert started["generation"] == generation
    assert started["source_count"] == 0
    assert started["query_length"] == 0
    finished = _fields(_one_event(diag, "search_finished"))
    assert finished["generation"] == generation
    assert finished["result_count"] == 0
    assert finished["dedupe_dropped"] == 0
    assert finished["failure_count"] == 0


def test_a_search_no_source_covers_still_records_a_whole_lifecycle(diag, monkeypatch):
    _select(monkeypatch, [])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    generation = svc.start("halo", Category.GAMES)

    _finish(watch)
    assert _names(diag) == ["search_started", "search_finished"]
    started = _fields(_one_event(diag, "search_started"))
    assert started["generation"] == generation
    assert started["category"] == "games"
    assert started["source_count"] == 0
    assert started["query_length"] == 4
    assert _fields(_one_event(diag, "search_finished"))["result_count"] == 0


# --- Z. the configured network interface -------------------------------------
#
# Cove binds its traffic to the interface chosen in Settings, and Search is not
# an exception: a user who picked a VPN adapter did not agree to let indexer
# requests leave over the default route instead. The service is the only place
# that knows the choice, so these pin that every source's HTTP facility is
# built with it - and that no choice still means no binding.


def _interface_recorder(monkeypatch):
    """Record the interface every SearchHttp the service builds is given."""
    seen = []

    class _Recorder(_FakeHttp):
        def __init__(self, interface="", *, session=None):
            super().__init__()
            seen.append(interface)

    monkeypatch.setattr(service, "SearchHttp", _Recorder)
    return seen


def test_the_configured_interface_reaches_the_source_transport(monkeypatch):
    _select(monkeypatch, [_FakeSource([_result()])])
    seen = _interface_recorder(monkeypatch)
    svc = service.SearchService(interface="cove-test0")
    watch = _Watch(svc)

    svc.start("dune")

    _finish(watch)
    assert seen == ["cove-test0"]


def test_every_source_in_one_search_gets_the_same_configured_interface(monkeypatch):
    """The binding belongs to the service, not to any one adapter.

    Two sources are enough: the transport is built per call, so a search whose
    sources shared nothing would show it here.
    """
    _select(
        monkeypatch,
        [
            _FakeSource([_result(info_hash=A)], source_id="alpha"),
            _FakeSource([_result(info_hash=B)], source_id="beta"),
        ],
    )
    seen = _interface_recorder(monkeypatch)
    svc = service.SearchService(interface="cove-test0")
    watch = _Watch(svc)

    svc.start("dune")

    _finish(watch)
    assert seen == ["cove-test0", "cove-test0"]


def test_no_configured_interface_leaves_the_transport_unbound(monkeypatch):
    """Characterisation: the default route stays the default behaviour.

    An empty setting is the shipped state, and it must reach SearchHttp as the
    empty interface it already treats as "do not bind" - not as some stand-in
    address the service invented.
    """
    _select(monkeypatch, [_FakeSource([_result()])])
    seen = _interface_recorder(monkeypatch)
    svc = service.SearchService()
    watch = _Watch(svc)

    svc.start("dune")

    _finish(watch)
    assert seen == [""]


def test_an_injected_factory_still_owns_the_whole_transport_decision(monkeypatch):
    """Characterisation: the test seam is unchanged, and takes no arguments.

    Every existing caller passes a zero-argument factory. The interface is the
    service's business only when it is the one building the facility.
    """
    _select(monkeypatch, [_FakeSource([_result()])])
    factory, made = _counting_http_factory()
    svc = service.SearchService(interface="cove-test0", http_factory=factory)
    watch = _Watch(svc)

    svc.start("dune")

    _finish(watch)
    assert len(made) == 1


def test_search_http_binds_its_session_to_the_interface_it_was_given(monkeypatch):
    """Characterisation: the far end of the wiring already works.

    SearchHttp defers to the same netiface helper every other direct HTTP call
    in Cove uses, so the service only has to supply the name - there is no
    Search-specific binding to write.
    """
    from cove.search.sources import base

    asked = []

    def _bound(name):
        asked.append(name)
        return object()

    monkeypatch.setattr("cove.netiface.bound_requests_session", _bound)

    session = base.SearchHttp("cove-test0").session()

    assert asked == ["cove-test0"]
    assert session is not None
