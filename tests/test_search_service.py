"""Deterministic combination of results arriving from several sources.

This layer is pure on purpose. Sources will later answer concurrently, so the
merged list must depend only on the content of the rows, never on which source
happened to finish first - these tests pin that, plus the duplicate-winner,
backfill and ordering rules the UI reads.
"""
import threading
import time

import pytest
from PySide6.QtCore import QCoreApplication, QRunnable, QThread, QThreadPool

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
    assert [s.id for s in SOURCES] == ["yts", "piratebay", "nyaa"]

    rows = [
        _result(info_hash=A, source="piratebay", seeders=7),
        _result(info_hash=A, source="yts", seeders=7),
    ]

    (winner,) = aggregate(rows).results

    assert winner.source == "yts"


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


def test_a_category_no_source_covers_finishes_empty():
    """GAMES has no built-in source yet, and must not hang on that account."""
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


def test_a_category_no_source_covers_still_takes_a_generation():
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


def test_a_category_no_source_covers_supersedes_the_running_search(monkeypatch):
    release = threading.Event()
    held = _held_source(
        [_result(info_hash=A, source="alpha")], source_id="alpha", release=release
    )
    real_sources_for = service.sources_for
    _select(monkeypatch, [held])
    svc = service.SearchService(http_factory=_FakeHttp)
    watch = _Watch(svc)

    try:
        first = svc.start("dune")
        # Real selection again, so GAMES genuinely covers no built-in source.
        monkeypatch.setattr(service, "sources_for", real_sources_for)
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


def test_an_immediate_uncovered_category_names_its_generation():
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
    real_sources_for = service.sources_for
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
        monkeypatch.setattr(service, "sources_for", real_sources_for)
        uncovered = svc.start("halo", Category.GAMES)
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
