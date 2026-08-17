"""S5: SearchService dynamic custom-source integration.

These tests prove the orchestration layer only: enabled persisted custom
Torznab indexers become ordinary sources in a normal SearchService generation,
using the same worker pool, deadline, cancellation, supersede, cache and
aggregation machinery as built-ins. The real Torznab transport and endpoint
security are already proven independently by S1/S3/S4, so ``TorznabSource`` is
replaced here by an observable fake and no network is ever touched.
"""

import threading
import uuid

import pytest

from cove.search import service
from cove.search.indexers import CustomTorznabIndexer
from cove.search.models import Category, SourceError, SourceErrorKind
from cove.search.service import SearchService, SourceState, _CacheKey
from cove.search.sources.base import SearchHttp
from tests.test_search_service import (
    A,
    B,
    _FakeSource,
    _Watch,
    _finish,
    _pump,
    _result,
    _select,
)

_ID_A = "custom:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_ID_B = "custom:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_ID_C = "custom:cccccccc-cccc-cccc-cccc-cccccccccccc"

_URL_A = "https://a.example/torznab"
_URL_B = "https://b.example/torznab"


def _rec(
    indexer_id,
    *,
    enabled=True,
    url=_URL_A,
    api_key="",
    name="Custom",
):
    return CustomTorznabIndexer(
        id=indexer_id, enabled=enabled, name=name, url=url, api_key=api_key
    )


def _numbered_id(n):
    return f"custom:{uuid.UUID(int=n)}"


def _hexhash(n):
    """A unique canonical 40-hex info hash for the nth row."""
    return format(n + 1, "040x")


class _CustomHarness:
    """Replaces ``TorznabSource`` with an observable fake source factory.

    ``behavior`` maps a custom id to ``_FakeSource`` keyword arguments (rows,
    raises, on_search). Every construction is recorded so tests can assert which
    records became sources, in what order, and with what snapshotted config.
    """

    def __init__(self, monkeypatch, behavior=None):
        self.snapshots = []  # CustomTorznabIndexer snapshots, construction order
        self.built = []  # source ids, construction order
        self.sources = {}  # id -> most recent fake source
        self.all_sources = []  # every fake source built, across generations
        self._behavior = behavior or {}

        def factory(indexer):
            self.snapshots.append(indexer)
            self.built.append(indexer.id)
            source = _FakeSource(
                source_id=indexer.id, **self._behavior.get(indexer.id, {})
            )
            self.sources[indexer.id] = source
            self.all_sources.append(source)
            return source

        monkeypatch.setattr(service, "TorznabSource", factory)


def _total_queries(harness, source_id):
    """How many times a custom id was actually searched, across generations."""
    return sum(
        1 for source in harness.all_sources if source.id == source_id
        for _ in source.calls
    )


@pytest.fixture
def fresh_pool(monkeypatch):
    monkeypatch.setattr(service, "_POOL", None)
    yield


# --- RED GROUP 1: default empty custom state ---------------------------------


def test_empty_custom_config_builds_no_custom_source(monkeypatch, fresh_pool):
    builtin = _FakeSource([])
    _select(monkeypatch, [builtin])

    def _fail(indexer):
        raise AssertionError("no custom source should be built")

    monkeypatch.setattr(service, "TorznabSource", _fail)
    svc = SearchService()
    watch = _Watch(svc)
    svc.start("dune")
    _finish(watch)
    assert builtin.calls
    assert svc._custom_config == {}


# --- RED GROUP 2: one enabled custom source ---------------------------------


def test_one_enabled_custom_source_becomes_one_source(monkeypatch, fresh_pool):
    builtin = _FakeSource([])
    _select(monkeypatch, [builtin])
    harness = _CustomHarness(
        monkeypatch, {_ID_A: {"rows": [_result(source=_ID_A, info_hash=A)]}}
    )
    svc = SearchService(custom_indexers=[_rec(_ID_A)])
    watch = _Watch(svc)
    svc.start("dune")
    summary = _finish(watch)
    assert harness.built == [_ID_A]
    assert {r.source for r in summary.results} == {_ID_A}
    assert watch.order[:2] == [
        ("fake", SourceState.RUNNING),
        (_ID_A, SourceState.RUNNING),
    ]


def test_one_shot_iterable_custom_indexers_survive_multiple_generations(
    monkeypatch, fresh_pool
):
    # A generator is materialized once at construction, so the first search
    # cannot exhaust it and silently drop the custom source afterwards.
    _select(monkeypatch, [_FakeSource([])])
    harness = _CustomHarness(
        monkeypatch, {_ID_A: {"rows": [_result(source=_ID_A, info_hash=A)]}}
    )
    svc = SearchService(custom_indexers=(r for r in [_rec(_ID_A)]))
    svc.start("dune")
    _finish(_Watch(svc))
    svc.start("other")
    _finish(_Watch(svc))
    assert harness.built == [_ID_A, _ID_A]


# --- RED GROUP 3: disabled source -------------------------------------------


def test_disabled_custom_source_is_not_launched(monkeypatch, fresh_pool):
    _select(monkeypatch, [_FakeSource([])])
    harness = _CustomHarness(
        monkeypatch,
        {
            _ID_A: {"rows": [_result(source=_ID_A, info_hash=A)]},
            _ID_B: {"rows": [_result(source=_ID_B, info_hash=B)]},
        },
    )
    svc = SearchService(
        custom_indexers=[_rec(_ID_A), _rec(_ID_B, enabled=False)]
    )
    watch = _Watch(svc)
    svc.start("dune")
    summary = _finish(watch)
    assert harness.built == [_ID_A]
    assert _ID_B not in harness.sources
    assert all(r.source != _ID_B for r in summary.results)


# --- RED GROUP 4: multiple custom sources / order ---------------------------


def test_custom_sources_follow_builtins_in_persisted_order(
    monkeypatch, fresh_pool
):
    _select(monkeypatch, [_FakeSource([])])
    harness = _CustomHarness(
        monkeypatch,
        {_ID_A: {"rows": []}, _ID_B: {"rows": []}, _ID_C: {"rows": []}},
    )
    svc = SearchService(
        custom_indexers=[_rec(_ID_A), _rec(_ID_B), _rec(_ID_C)]
    )
    watch = _Watch(svc)
    svc.start("dune")
    _finish(watch)
    assert harness.built == [_ID_A, _ID_B, _ID_C]
    assert watch.order[:4] == [
        ("fake", SourceState.RUNNING),
        (_ID_A, SourceState.RUNNING),
        (_ID_B, SourceState.RUNNING),
        (_ID_C, SourceState.RUNNING),
    ]


# --- RED GROUP 5: built-in vs custom tie-break ------------------------------


def test_builtin_precedes_custom_on_a_seeder_tie(monkeypatch, fresh_pool):
    builtin = _FakeSource(
        source_id="yts",
        rows=[_result(source="yts", info_hash=A, seeders=7, name="Builtin")],
    )
    _select(monkeypatch, [builtin])
    harness = _CustomHarness(
        monkeypatch,
        {_ID_A: {"rows": [_result(source=_ID_A, info_hash=A, seeders=7, name="Custom")]}},
    )
    svc = SearchService(custom_indexers=[_rec(_ID_A)])
    watch = _Watch(svc)
    svc.start("dune")
    summary = _finish(watch)
    winners = [r for r in summary.results if r.info_hash == A]
    assert len(winners) == 1
    assert winners[0].source == "yts"


def test_higher_seeder_custom_still_wins(monkeypatch, fresh_pool):
    builtin = _FakeSource(
        source_id="yts", rows=[_result(source="yts", info_hash=A, seeders=7)]
    )
    _select(monkeypatch, [builtin])
    harness = _CustomHarness(
        monkeypatch,
        {_ID_A: {"rows": [_result(source=_ID_A, info_hash=A, seeders=50)]}},
    )
    svc = SearchService(custom_indexers=[_rec(_ID_A)])
    watch = _Watch(svc)
    svc.start("dune")
    summary = _finish(watch)
    winner = next(r for r in summary.results if r.info_hash == A)
    assert winner.source == _ID_A
    assert winner.seeders == 50


# --- RED GROUP 6: custom vs custom tie-break (out-of-order completion) ------


def test_custom_tie_uses_persisted_order_not_completion(monkeypatch, fresh_pool):
    _select(monkeypatch, [_FakeSource([])])
    gate_a = threading.Event()
    harness = _CustomHarness(
        monkeypatch,
        {
            _ID_A: {
                "rows": [_result(source=_ID_A, info_hash=A, seeders=7, name="A")],
                "on_search": lambda: gate_a.wait(5.0),
            },
            _ID_B: {
                "rows": [_result(source=_ID_B, info_hash=A, seeders=7, name="B")]
            },
        },
    )
    svc = SearchService(custom_indexers=[_rec(_ID_A), _rec(_ID_B)])
    watch = _Watch(svc)
    svc.start("dune")
    # B completes first while A is still held.
    _pump(
        lambda: any(
            st.source_id == _ID_B and st.state is SourceState.COMPLETED
            for st in watch.statuses
        )
    )
    gate_a.set()
    summary = _finish(watch)
    winner = next(r for r in summary.results if r.info_hash == A)
    assert winner.source == _ID_A
    assert winner.name == "A"


# --- RED GROUP 7: custom aggregate backfill ---------------------------------


def test_custom_winner_backfills_from_builtin_loser(monkeypatch, fresh_pool):
    builtin = _FakeSource(
        source_id="yts",
        rows=[
            _result(
                source="yts",
                info_hash=A,
                seeders=1,
                size_bytes=4096,
                added=1600000000,
            )
        ],
    )
    _select(monkeypatch, [builtin])
    harness = _CustomHarness(
        monkeypatch,
        {
            _ID_A: {
                "rows": [
                    _result(
                        source=_ID_A,
                        info_hash=A,
                        seeders=50,
                        size_bytes=None,
                        added=None,
                    )
                ]
            }
        },
    )
    svc = SearchService(custom_indexers=[_rec(_ID_A)])
    watch = _Watch(svc)
    svc.start("dune")
    summary = _finish(watch)
    winner = next(r for r in summary.results if r.info_hash == A)
    assert winner.source == _ID_A
    assert winner.size_bytes == 4096
    assert winner.added == 1600000000


# --- RED GROUP 8: custom cache hit ------------------------------------------


def test_same_config_custom_source_is_served_from_cache(monkeypatch, fresh_pool):
    _select(monkeypatch, [_FakeSource([])])
    harness = _CustomHarness(
        monkeypatch, {_ID_A: {"rows": [_result(source=_ID_A, info_hash=A)]}}
    )
    svc = SearchService(custom_indexers=[_rec(_ID_A)])
    svc.start("dune")
    _finish(_Watch(svc))
    assert _total_queries(harness, _ID_A) == 1
    watch = _Watch(svc)
    svc.start("dune")
    _finish(watch)
    assert _total_queries(harness, _ID_A) == 1


# --- RED GROUP 8: a name-only edit is not a cache-evicting edit (S8) ---------


def test_name_only_edit_reuses_the_custom_cache(monkeypatch, fresh_pool):
    """S8: display names are presentation; the cache signature is id/url/key."""
    _select(monkeypatch, [_FakeSource([])])
    harness = _CustomHarness(
        monkeypatch, {_ID_A: {"rows": [_result(source=_ID_A, info_hash=A)]}}
    )
    record = _rec(_ID_A, name="Old Name")
    svc = SearchService(custom_indexers=[record])
    svc.start("dune")
    _finish(_Watch(svc))
    assert _total_queries(harness, _ID_A) == 1
    record.name = "New Name"
    watch = _Watch(svc)
    svc.start("dune")
    _finish(watch)
    assert _total_queries(harness, _ID_A) == 1


# --- RED GROUP 9: URL edit invalidates source cache -------------------------


def test_url_edit_invalidates_custom_cache(monkeypatch, fresh_pool):
    _select(monkeypatch, [_FakeSource([])])
    harness = _CustomHarness(
        monkeypatch, {_ID_A: {"rows": [_result(source=_ID_A, info_hash=A)]}}
    )
    record = _rec(_ID_A)
    svc = SearchService(custom_indexers=[record])
    svc.start("dune")
    _finish(_Watch(svc))
    assert _total_queries(harness, _ID_A) == 1
    record.url = _URL_B
    watch = _Watch(svc)
    svc.start("dune")
    _finish(watch)
    assert _total_queries(harness, _ID_A) == 2


# --- RED GROUP 10: API-key edit invalidates cache, secret never leaks -------


def test_api_key_edit_invalidates_cache_and_never_leaks(monkeypatch, fresh_pool):
    secret_a = "SUPERSECRETAAA"
    secret_b = "SUPERSECRETBBB"
    _select(monkeypatch, [_FakeSource([])])
    harness = _CustomHarness(
        monkeypatch, {_ID_A: {"rows": [_result(source=_ID_A, info_hash=A)]}}
    )
    record = _rec(_ID_A, api_key=secret_a)
    svc = SearchService(custom_indexers=[record])
    svc.start("dune")
    _finish(_Watch(svc))
    assert _total_queries(harness, _ID_A) == 1
    record.api_key = secret_b
    watch = _Watch(svc)
    svc.start("dune")
    _finish(watch)
    assert _total_queries(harness, _ID_A) == 2
    assert secret_a not in str(svc._cache._entries)
    assert secret_b not in str(svc._cache._entries)
    assert all(secret_a not in repr(k) for k in svc._cache._entries)
    assert all(secret_b not in repr(k) for k in svc._cache._entries)


# --- RED GROUP 11: source-scoped cache eviction -----------------------------


def test_custom_edit_evicts_only_that_source():
    svc = SearchService()
    svc._cache.put(
        _CacheKey("yts", Category.ALL, "dune"), (_result(source="yts", info_hash=A),)
    )
    svc._cache.put(
        _CacheKey(_ID_B, Category.ALL, "dune"),
        (_result(source=_ID_B, info_hash=B),),
    )
    svc._custom_config = {_ID_B: (_URL_A, "")}
    svc._reconcile_custom_cache({_ID_B: (_URL_B, "")})
    assert svc._cache.get(_CacheKey("yts", Category.ALL, "dune")) is not None
    assert svc._cache.get(_CacheKey(_ID_B, Category.ALL, "dune")) is None


def test_removing_a_custom_source_evicts_its_cache():
    svc = SearchService()
    svc._cache.put(
        _CacheKey("yts", Category.ALL, "dune"), (_result(source="yts", info_hash=A),)
    )
    svc._cache.put(
        _CacheKey(_ID_B, Category.ALL, "dune"),
        (_result(source=_ID_B, info_hash=B),),
    )
    svc._custom_config = {_ID_B: (_URL_A, "")}
    svc._reconcile_custom_cache({})
    assert svc._cache.get(_CacheKey("yts", Category.ALL, "dune")) is not None
    assert svc._cache.get(_CacheKey(_ID_B, Category.ALL, "dune")) is None


# --- RED GROUP 12: removed source next generation ---------------------------


def test_removed_custom_source_is_not_launched_next_generation(
    monkeypatch, fresh_pool
):
    _select(monkeypatch, [_FakeSource([])])
    harness = _CustomHarness(
        monkeypatch, {_ID_A: {"rows": [_result(source=_ID_A, info_hash=A)]}}
    )
    records = [_rec(_ID_A)]
    svc = SearchService(custom_indexers=records)
    svc.start("dune")
    _finish(_Watch(svc))
    assert harness.built == [_ID_A]
    records.clear()
    watch = _Watch(svc)
    svc.start("dune")
    _finish(watch)
    assert harness.built == [_ID_A]
    assert svc._cache.get(_CacheKey(_ID_A, Category.ALL, "dune")) is None


# --- RED GROUP 13: mid-generation disable snapshot --------------------------


def test_mid_generation_disable_keeps_snapshot(monkeypatch, fresh_pool):
    _select(monkeypatch, [_FakeSource([])])
    gate = threading.Event()
    harness = _CustomHarness(
        monkeypatch,
        {
            _ID_A: {
                "rows": [_result(source=_ID_A, info_hash=A)],
                "on_search": lambda: gate.wait(5.0),
            }
        },
    )
    record = _rec(_ID_A)
    svc = SearchService(custom_indexers=[record])
    svc.start("dune")
    _pump(lambda: len(harness.sources[_ID_A].calls) == 1)
    record.enabled = False
    svc.start("dune")
    assert harness.built == [_ID_A]  # A launched once, not again in gen 2
    gate.set()
    _pump(lambda: len(harness.sources[_ID_A].calls) >= 1)


# --- RED GROUP 14: mid-generation URL edit snapshot -------------------------


def test_mid_generation_url_edit_keeps_snapshot(monkeypatch, fresh_pool):
    _select(monkeypatch, [_FakeSource([])])
    gate = threading.Event()
    harness = _CustomHarness(
        monkeypatch,
        {
            _ID_A: {
                "rows": [_result(source=_ID_A, info_hash=A)],
                "on_search": lambda: gate.wait(5.0),
            }
        },
    )
    record = _rec(_ID_A, url=_URL_A)
    svc = SearchService(custom_indexers=[record])
    svc.start("dune")
    _pump(lambda: len(harness.sources[_ID_A].calls) == 1)
    assert harness.snapshots[0].url == _URL_A
    record.url = _URL_B
    svc.start("dune")
    assert len(harness.snapshots) == 2
    assert harness.snapshots[0].url == _URL_A
    assert harness.snapshots[1].url == _URL_B
    gate.set()
    _pump(lambda: len(harness.sources[_ID_A].calls) >= 1)


# --- RED GROUP 15: cancel custom worker -------------------------------------


def test_cancelled_custom_source_does_not_publish(monkeypatch, fresh_pool):
    _select(monkeypatch, [_FakeSource([])])
    gate = threading.Event()
    harness = _CustomHarness(
        monkeypatch,
        {
            _ID_A: {
                "rows": [_result(source=_ID_A, info_hash=A)],
                "on_search": lambda: gate.wait(5.0),
            }
        },
    )
    svc = SearchService(custom_indexers=[_rec(_ID_A)])
    watch = _Watch(svc)
    svc.start("dune")
    _pump(lambda: len(harness.sources[_ID_A].calls) == 1)
    svc.cancel()
    gate.set()
    _pump(lambda: not svc._calls)
    assert not any(
        r.source == _ID_A for results in watch.results for r in results
    )
    assert watch.finished == []


# --- RED GROUP 16: supersede custom generation ------------------------------


def test_superseded_custom_generation_does_not_publish(monkeypatch, fresh_pool):
    _select(monkeypatch, [_FakeSource([])])
    gate = threading.Event()
    harness = _CustomHarness(
        monkeypatch,
        {
            _ID_A: {
                "rows": [_result(source=_ID_A, info_hash=A)],
                "on_search": lambda: gate.wait(5.0),
            }
        },
    )
    records = [_rec(_ID_A)]
    svc = SearchService(custom_indexers=records)
    watch = _Watch(svc)
    gen1 = svc.start("dune")
    _pump(lambda: len(harness.sources[_ID_A].calls) == 1)
    records.clear()
    gen2 = svc.start("dune")
    summary = _finish(watch)
    assert summary.generation == gen2
    gate.set()
    _pump(lambda: not svc._calls)
    assert gen1 != gen2
    assert not any(
        r.source == _ID_A for results in watch.results for r in results
    )
    assert len(watch.finished) == 1


# --- RED GROUP 17: deadline custom source -----------------------------------


def test_custom_source_past_deadline_is_suppressed(monkeypatch, fresh_pool):
    _select(monkeypatch, [_FakeSource([])])
    gate = threading.Event()
    harness = _CustomHarness(
        monkeypatch,
        {
            _ID_A: {
                "rows": [_result(source=_ID_A, info_hash=A)],
                "on_search": lambda: gate.wait(5.0),
            }
        },
    )
    svc = SearchService(deadline_ms=120, custom_indexers=[_rec(_ID_A)])
    watch = _Watch(svc)
    svc.start("dune")
    summary = _finish(watch)
    assert all(r.source != _ID_A for r in summary.results)
    assert SourceState.TIMED_OUT in watch.states(_ID_A)
    gate.set()
    _pump(lambda: not svc._calls)
    assert not any(
        r.source == _ID_A for results in watch.results for r in results
    )
    assert len(watch.finished) == 1


# --- RED GROUP 18: custom failure isolation ---------------------------------


def test_custom_failure_isolated_from_builtin_and_other_custom(
    monkeypatch, fresh_pool
):
    builtin = _FakeSource(
        source_id="yts", rows=[_result(source="yts", info_hash=A)]
    )
    _select(monkeypatch, [builtin])
    harness = _CustomHarness(
        monkeypatch,
        {
            _ID_A: {"raises": SourceError(SourceErrorKind.NETWORK)},
            _ID_B: {"rows": [_result(source=_ID_B, info_hash=B)]},
        },
    )
    svc = SearchService(custom_indexers=[_rec(_ID_A), _rec(_ID_B)])
    watch = _Watch(svc)
    svc.start("dune")
    summary = _finish(watch)
    sources_in_results = {r.source for r in summary.results}
    assert "yts" in sources_in_results
    assert _ID_B in sources_in_results
    assert _ID_A not in sources_in_results
    assert SourceState.FAILED in watch.states(_ID_A)
    assert any(f.source_id == _ID_A for f in summary.failures)


# --- RED GROUP 19: interface forwarding -------------------------------------


def test_custom_source_receives_the_requested_interface(monkeypatch, fresh_pool):
    _select(monkeypatch, [_FakeSource([])])
    harness = _CustomHarness(monkeypatch, {_ID_A: {"rows": []}})
    svc = SearchService(interface="wg0", custom_indexers=[_rec(_ID_A)])
    watch = _Watch(svc)
    svc.start("dune")
    _finish(watch)
    http = harness.sources[_ID_A].calls[0][2]
    assert isinstance(http, SearchHttp)
    assert http.interface == "wg0"


# --- RED GROUP 20: many custom sources / pool bound -------------------------


def test_many_custom_sources_do_not_resize_the_pool(monkeypatch, fresh_pool):
    _select(monkeypatch, [_FakeSource([])])
    harness = _CustomHarness(monkeypatch)
    records = [_rec(_numbered_id(n)) for n in range(1, 21)]
    svc = SearchService(custom_indexers=records)
    watch = _Watch(svc)
    svc.start("dune")
    _finish(watch)
    assert service._MAX_POOL_THREADS == 12
    assert service._pool().maxThreadCount() <= service._MAX_POOL_THREADS
    assert len(harness.sources) == 20
    assert len(harness.built) == 20


# --- RED GROUP 21: duplicate custom id defense ------------------------------


def test_duplicate_custom_ids_are_rejected_before_launch(monkeypatch, fresh_pool):
    _select(monkeypatch, [_FakeSource([])])
    monkeypatch.setattr(
        service, "TorznabSource", lambda indexer: _FakeSource(source_id=indexer.id)
    )
    svc = SearchService(custom_indexers=[_rec(_ID_A), _rec(_ID_A)])
    with pytest.raises(ValueError):
        svc.start("dune")
    assert svc.generation == 0


def test_duplicate_id_enabled_and_disabled_is_rejected(monkeypatch, fresh_pool):
    # A disabled record sharing an enabled record's id must not silently
    # overwrite the enabled source's runtime signature (which would leave a
    # later URL/API-key edit unable to evict stale cache entries).
    _select(monkeypatch, [_FakeSource([])])
    monkeypatch.setattr(
        service, "TorznabSource", lambda indexer: _FakeSource(source_id=indexer.id)
    )
    svc = SearchService(
        custom_indexers=[
            _rec(_ID_A, enabled=True, url=_URL_A),
            _rec(_ID_A, enabled=False, url=_URL_B),
        ]
    )
    with pytest.raises(ValueError):
        svc.start("dune")
    assert svc.generation == 0


# --- RED GROUP 22/23: global unique output cap ------------------------------


def test_aggregate_caps_unique_results_after_sort():
    rows = [
        _result(info_hash=_hexhash(i), name=f"N{i:03d}", seeders=i)
        for i in range(600)
    ]
    merged = service.aggregate(rows, limit=500)
    assert len(merged.results) == 500
    assert merged.dedupe_dropped == 0
    assert merged.results[0].seeders == 599
    assert merged.results[-1].seeders == 100


def test_service_publishes_at_most_the_global_cap(monkeypatch, fresh_pool):
    rows = [
        _result(info_hash=_hexhash(i), name=f"N{i:03d}", seeders=i)
        for i in range(600)
    ]
    _select(monkeypatch, [_FakeSource(rows)])
    svc = SearchService()
    watch = _Watch(svc)
    svc.start("dune")
    summary = _finish(watch)
    assert len(summary.results) == 500


# --- RED GROUP 24: normal app wiring ----------------------------------------


def test_main_window_wires_live_custom_config():
    from tests.test_search_main_window import Host, _wire

    host = Host()
    host.settings.custom_indexers = [_rec(_ID_A)]
    _wire(host)
    provided = list(host.search_service._custom_provider())
    assert provided == [_rec(_ID_A)]
    assert [r.id for r in provided] == [_ID_A]


def test_main_window_empty_settings_stay_backward_compatible():
    from tests.test_search_main_window import Host, _wire

    host = Host()
    _wire(host)
    assert list(host.search_service._custom_provider()) == []


# ============================================================================
# S9: custom indexer order is the S5 priority tie-break
#
# The Settings Move Up / Move Down controls reorder the persisted list. S5
# already proves ties resolve by persisted order; these tests prove the S9
# surface: a reordered list flips an equal tie on the next generation, a
# reorder alone never evicts the per-source cache, built-ins keep winning an
# equal tie no matter where the customs sit, and a mid-flight reorder only
# reaches the next generation.
# ============================================================================


def _tie_winner(monkeypatch, records, completes_first, expected):
    """Run one generation with both customs gated; release one, then the other."""
    _select(monkeypatch, [_FakeSource([])])
    gates = {_ID_A: threading.Event(), _ID_B: threading.Event()}
    harness = _CustomHarness(
        monkeypatch,
        {
            _ID_A: {
                "rows": [_result(source=_ID_A, info_hash=A, seeders=7, name="A")],
                "on_search": lambda: gates[_ID_A].wait(5.0),
            },
            _ID_B: {
                "rows": [_result(source=_ID_B, info_hash=A, seeders=7, name="B")],
                "on_search": lambda: gates[_ID_B].wait(5.0),
            },
        },
    )
    svc = SearchService(custom_indexers=list(records))
    watch = _Watch(svc)
    svc.start("dune")
    gates[completes_first].set()
    _pump(
        lambda: any(
            st.source_id == completes_first
            and st.state is SourceState.COMPLETED
            for st in watch.statuses
        )
    )
    other = _ID_A if completes_first == _ID_B else _ID_B
    gates[other].set()
    summary = _finish(watch)
    winner = next(r for r in summary.results if r.info_hash == A)
    assert winner.source == expected


def test_reorder_flips_the_equal_tie_on_the_next_generation(monkeypatch, fresh_pool):
    """Order [A,B] with B completing first -> A wins; [B,A] with A first -> B."""
    _tie_winner(
        monkeypatch,
        [_rec(_ID_A, name="A"), _rec(_ID_B, name="B")],
        completes_first=_ID_B,
        expected=_ID_A,
    )
    # The Settings Move Down on A produced this persisted order.
    _tie_winner(
        monkeypatch,
        [_rec(_ID_B, name="B"), _rec(_ID_A, name="A")],
        completes_first=_ID_A,
        expected=_ID_B,
    )


def test_reorder_alone_reuses_cached_source_answers(monkeypatch, fresh_pool):
    _select(monkeypatch, [_FakeSource([])])
    harness = _CustomHarness(
        monkeypatch,
        {
            _ID_A: {"rows": [_result(source=_ID_A, info_hash=A, seeders=7, name="A")]},
            _ID_B: {"rows": [_result(source=_ID_B, info_hash=A, seeders=7, name="B")]},
        },
    )
    # Same list object the Settings dialog would hand the live provider: the
    # S5 provider reads it live each generation, so an in-place reorder is
    # visible without re-instantiating the service.
    records = [_rec(_ID_A, name="A"), _rec(_ID_B, name="B")]
    svc = SearchService(custom_indexers=records)
    svc.start("dune")
    summary = _finish(_Watch(svc))
    assert next(r for r in summary.results if r.info_hash == A).source == _ID_A
    records.reverse()  # the S9 Move Up/Down writes the list in place
    watch = _Watch(svc)
    svc.start("dune")
    summary = _finish(watch)
    # A reorder is not a config change: same id/url/api_key signatures mean
    # the per-source cache is served, not re-fetched.
    assert _total_queries(harness, _ID_A) == 1
    assert _total_queries(harness, _ID_B) == 1
    winner = next(r for r in summary.results if r.info_hash == A)
    assert winner.source == _ID_B


def test_custom_at_top_of_list_still_loses_to_builtin_tie(monkeypatch, fresh_pool):
    builtin = _FakeSource(
        source_id="yts", rows=[_result(source="yts", info_hash=A, seeders=7)]
    )
    _select(monkeypatch, [builtin])
    harness = _CustomHarness(
        monkeypatch,
        {
            _ID_B: {"rows": [_result(source=_ID_B, info_hash=A, seeders=7)]},
            _ID_A: {"rows": [_result(source=_ID_A, info_hash=A, seeders=7)]},
        },
    )
    # B moved above A, but built-ins still rank ahead of every custom.
    svc = SearchService(custom_indexers=[_rec(_ID_B), _rec(_ID_A)])
    watch = _Watch(svc)
    svc.start("dune")
    summary = _finish(watch)
    winner = next(r for r in summary.results if r.info_hash == A)
    assert winner.source == "yts"


def test_reorder_mid_generation_applies_to_next_generation_only(
    monkeypatch, fresh_pool
):
    _select(monkeypatch, [_FakeSource([])])
    gate_a = threading.Event()
    harness = _CustomHarness(
        monkeypatch,
        {
            _ID_A: {
                "rows": [_result(source=_ID_A, info_hash=A, seeders=7, name="A")],
                "on_search": lambda: gate_a.wait(5.0),
            },
            _ID_B: {
                "rows": [_result(source=_ID_B, info_hash=A, seeders=7, name="B")]
            },
        },
    )
    records = [_rec(_ID_A, name="A"), _rec(_ID_B, name="B")]
    svc = SearchService(custom_indexers=records)
    watch = _Watch(svc)
    gen1 = svc.start("dune")
    _pump(lambda: len(harness.sources[_ID_A].calls) == 1)
    # Generation 1 snapshotted A,B. Now the Settings dialog reorders.
    assert harness.built == [_ID_A, _ID_B]
    records.reverse()
    watch2 = _Watch(svc)
    gen2 = svc.start("dune")  # supersedes generation 1
    # Generation 2 snapshotted the reordered B,A.
    assert harness.built == [_ID_A, _ID_B, _ID_B, _ID_A]
    gate_a.set()
    summary = _finish(watch2)
    assert summary.generation == gen2
    assert gen1 != gen2
    winner = next(r for r in summary.results if r.info_hash == A)
    assert winner.source == _ID_B
