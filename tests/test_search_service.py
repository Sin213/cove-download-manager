"""Deterministic combination of results arriving from several sources.

This layer is pure on purpose. Sources will later answer concurrently, so the
merged list must depend only on the content of the rows, never on which source
happened to finish first - these tests pin that, plus the duplicate-winner,
backfill and ordering rules the UI reads.
"""
import threading
import time

import pytest
from PySide6.QtCore import QCoreApplication, QRunnable, QThreadPool

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

    def __init__(self, rows=None, raises=None):
        self._rows = rows if rows is not None else []
        self._raises = raises
        self.calls = []

    def search(self, query, category, http):
        self.calls.append((query, category, http))
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
    call = service._SourceCall(_FakeSource(rows), "dune", Category.MOVIES, http_factory=_FakeHttp)
    seen = _collect(call)

    call.run()

    assert len(seen) == 1
    assert seen[0].source_id == "fake"
    assert seen[0].results == tuple(rows)
    assert seen[0].error_kind is None


def test_a_source_call_delivers_its_outcome_through_the_pool(_fresh_pool):
    call = service._SourceCall(
        _FakeSource([_result()]), "dune", Category.MOVIES, http_factory=_FakeHttp
    )
    seen = _collect(call)

    _run_on_pool(call, seen)

    assert len(seen) == 1
    assert seen[0].results == (_result(),)


def test_a_source_call_passes_the_query_and_category_to_the_source():
    source = _FakeSource([])
    http = _FakeHttp()
    call = service._SourceCall(source, "akira", Category.ANIME, http_factory=lambda: http)

    call.run()

    assert source.calls == [("akira", Category.ANIME, http)]


def test_an_empty_source_answer_is_a_success():
    call = service._SourceCall(_FakeSource([]), "dune", Category.MOVIES, http_factory=_FakeHttp)
    seen = _collect(call)

    call.run()

    assert len(seen) == 1
    assert seen[0].results == ()
    assert seen[0].error_kind is None


def test_a_source_error_becomes_one_failed_outcome_carrying_its_kind():
    source = _FakeSource(raises=SourceError(SourceErrorKind.TIMEOUT, "too slow"))
    call = service._SourceCall(source, "dune", Category.MOVIES, http_factory=_FakeHttp)
    seen = _collect(call)

    call.run()

    assert len(seen) == 1
    assert seen[0].error_kind == SourceErrorKind.TIMEOUT.value
    assert seen[0].results == ()


def test_every_source_error_kind_survives_as_the_outcome_kind():
    for kind in SourceErrorKind:
        call = service._SourceCall(
            _FakeSource(raises=SourceError(kind)), "dune", Category.MOVIES, http_factory=_FakeHttp
        )
        seen = _collect(call)

        call.run()

        assert [outcome.error_kind for outcome in seen] == [kind.value]


def test_an_unexpected_source_exception_becomes_one_internal_failure():
    source = _FakeSource(raises=RuntimeError("boom"))
    call = service._SourceCall(source, "dune", Category.MOVIES, http_factory=_FakeHttp)
    seen = _collect(call)

    call.run()

    assert len(seen) == 1
    assert seen[0].error_kind == service._INTERNAL_ERROR
    assert seen[0].results == ()


def test_an_unexpected_exception_does_not_escape_a_pooled_worker(_fresh_pool):
    call = service._SourceCall(
        _FakeSource(raises=RuntimeError("boom")), "dune", Category.MOVIES, http_factory=_FakeHttp
    )
    seen = _collect(call)

    _run_on_pool(call, seen)

    assert [outcome.error_kind for outcome in seen] == [service._INTERNAL_ERROR]


def test_an_outcome_is_immutable():
    call = service._SourceCall(_FakeSource([]), "dune", Category.MOVIES, http_factory=_FakeHttp)
    seen = _collect(call)

    call.run()

    with pytest.raises(Exception):
        seen[0].source_id = "other"


# --- J. the worker owns one HTTP facility per call ---------------------------


def test_the_owned_http_is_closed_after_a_successful_call():
    http = _FakeHttp()
    call = service._SourceCall(
        _FakeSource([_result()]), "dune", Category.MOVIES, http_factory=lambda: http
    )

    call.run()

    assert http.closed == 1


def test_the_owned_http_is_closed_after_a_source_error():
    http = _FakeHttp()
    call = service._SourceCall(
        _FakeSource(raises=SourceError(SourceErrorKind.NETWORK)),
        "dune",
        Category.MOVIES,
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
        http_factory=lambda: http,
    )

    call.run()

    assert http.closed == 1


def test_a_failing_close_still_leaves_exactly_one_outcome():
    class _RudeHttp(_FakeHttp):
        def close(self):
            super().close()
            raise RuntimeError("close failed")

    call = service._SourceCall(_FakeSource([]), "dune", Category.MOVIES, http_factory=_RudeHttp)
    seen = _collect(call)

    call.run()

    assert len(seen) == 1


def test_a_call_builds_a_real_search_http_by_default():
    """The production path is the default one, so a test has to walk it.

    SearchHttp opens no session until it is asked for one, so a source that
    never touches it keeps this off the network entirely.
    """
    source = _FakeSource([])
    call = service._SourceCall(source, "dune", Category.MOVIES)
    seen = _collect(call)

    call.run()

    assert isinstance(source.calls[0][2], SearchHttp)
    assert [outcome.error_kind for outcome in seen] == [None]
