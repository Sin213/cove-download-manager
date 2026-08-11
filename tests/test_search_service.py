"""Deterministic combination of results arriving from several sources.

This layer is pure on purpose. Sources will later answer concurrently, so the
merged list must depend only on the content of the rows, never on which source
happened to finish first - these tests pin that, plus the duplicate-winner,
backfill and ordering rules the UI reads.
"""
from cove.search.magnet import build_magnet
from cove.search.models import SearchResult
from cove.search.registry import SOURCES
from cove.search.service import aggregate


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
