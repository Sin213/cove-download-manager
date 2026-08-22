"""Widget-local column sorting for the current Search results.

The widget reorders only the result set it was already handed. SearchService
still owns membership, dedupe, relevance, the cap and the cache, so nothing in
this file constructs a service, a window, a source or a network call: every
test drives the real SearchWidget, real SearchResult objects and the real
QTableWidget/QHeaderView, and header activation goes through the header's own
sectionClicked signal rather than pixel coordinates or theme artwork.
"""
from PySide6.QtCore import Qt

from cove.search.models import SearchResult
from cove.search.widget import COLUMNS, UNKNOWN, SearchWidget

NAME = COLUMNS.index("Name")
SIZE = COLUMNS.index("Size")
SEEDERS = COLUMNS.index("Seeders")
LEECHERS = COLUMNS.index("Leechers")
ADDED = COLUMNS.index("Added")
SOURCE = COLUMNS.index("Source")

# 2021-01-01 00:00:00 UTC, and two later times on that same calendar day. The
# same-day pair matters: both render as "2021-01-01", so a sort driven by the
# formatted Added text cannot tell them apart while a chronological sort can.
DAY = 1_609_459_200
DAY_06H = DAY + 6 * 3600
DAY_12H = DAY + 12 * 3600


def _result(n, *, name=None, size=None, seeders=0, leechers=0, added=None, source="yts"):
    """A real SearchResult - the model validates, so no fake stands in for it."""
    info_hash = f"{n:040x}"
    return SearchResult(
        info_hash=info_hash,
        name=name if name is not None else f"result {n}",
        magnet=f"magnet:?xt=urn:btih:{info_hash}",
        size_bytes=size,
        seeders=seeders,
        leechers=leechers,
        added=added,
        source=source,
    )


def _names(widget):
    """The visible Name column, top to bottom."""
    return [widget.table.item(r, NAME).text() for r in range(widget.table.rowCount())]


def _rows(widget):
    """The SearchResult object behind each visible row, top to bottom."""
    return [
        widget.table.item(r, NAME).data(Qt.UserRole)
        for r in range(widget.table.rowCount())
    ]


def _click(widget, column):
    """Activate a header section the way the header itself would."""
    widget.table.horizontalHeader().sectionClicked.emit(column)


def _indicator(widget):
    header = widget.table.horizontalHeader()
    return header.sortIndicatorSection(), header.sortIndicatorOrder()


def _record_downloads(widget):
    seen = []
    widget.download_requested.connect(seen.append)
    return seen


# --- group 1: the default display order -------------------------------------
def test_a_fresh_result_snapshot_defaults_to_seeders_high_to_low():
    """No header click anywhere: this proves the default itself."""
    w = SearchWidget()

    w.set_results(
        (
            _result(1, name="mike", seeders=50),
            _result(2, name="zulu", seeders=1),
            _result(3, name="alpha", seeders=999),
        )
    )

    assert _names(w) == ["alpha", "mike", "zulu"]


def test_the_default_order_breaks_equal_seeders_alphabetically():
    w = SearchWidget()

    w.set_results(
        (
            _result(1, name="zulu", seeders=100),
            _result(2, name="alpha", seeders=100),
            _result(3, name="bravo", seeders=100),
            _result(4, name="delta", seeders=50),
        )
    )

    assert _names(w) == ["alpha", "bravo", "zulu", "delta"]


def test_the_fresh_default_shows_a_descending_seeders_indicator():
    w = SearchWidget()

    w.set_results((_result(1, seeders=3), _result(2, seeders=1)))

    assert w.table.horizontalHeader().isSortIndicatorShown() is True
    assert _indicator(w) == (SEEDERS, Qt.DescendingOrder)


# --- group 2: Name ----------------------------------------------------------
def test_the_first_name_click_sorts_a_to_z():
    w = SearchWidget()
    w.set_results(
        (
            _result(1, name="zulu", seeders=3),
            _result(2, name="mike", seeders=2),
            _result(3, name="alpha", seeders=1),
        )
    )

    _click(w, NAME)

    assert _names(w) == ["alpha", "mike", "zulu"]
    assert _indicator(w) == (NAME, Qt.AscendingOrder)


def test_the_second_name_click_sorts_z_to_a():
    w = SearchWidget()
    # Deliberately not already in either direction's order, so neither an
    # unsorted table nor a stuck-ascending one can pass by coincidence.
    w.set_results(
        (
            _result(1, name="mike"),
            _result(2, name="alpha"),
            _result(3, name="zulu"),
        )
    )

    _click(w, NAME)
    _click(w, NAME)

    assert _names(w) == ["zulu", "mike", "alpha"]
    assert _indicator(w) == (NAME, Qt.DescendingOrder)


def test_name_sorting_is_case_insensitive():
    """Raw code points would group every capital ahead of every lower case."""
    w = SearchWidget()
    w.set_results(
        (
            _result(1, name="Zebra"),
            _result(2, name="apple"),
            _result(3, name="Banana"),
        )
    )

    _click(w, NAME)

    assert _names(w) == ["apple", "Banana", "Zebra"]


def test_equal_casefolded_names_keep_their_supplied_relative_order():
    w = SearchWidget()
    w.set_results((_result(1, name="alpha"), _result(2, name="ALPHA")))

    _click(w, NAME)
    assert [r.info_hash for r in _rows(w)] == [f"{1:040x}", f"{2:040x}"]

    _click(w, NAME)
    assert [r.info_hash for r in _rows(w)] == [f"{1:040x}", f"{2:040x}"]


# --- group 3: Size ----------------------------------------------------------
#: 20 MB, 900 MB and 1.2 GB. Sorted as the displayed text these read
#: "1.2 GB" < "20 MB" < "900 MB", which is not their true order.
MB_20 = 20 * 1024 * 1024
MB_900 = 900 * 1024 * 1024
GB_1_2 = int(1.2 * 1024 * 1024 * 1024)


def _size_widget():
    w = SearchWidget()
    w.set_results(
        (
            _result(1, name="nine hundred", size=MB_900),
            _result(2, name="one point two", size=GB_1_2),
            _result(3, name="twenty", size=MB_20),
        )
    )
    return w


def test_the_first_size_click_sorts_by_real_byte_count_ascending():
    w = _size_widget()

    _click(w, SIZE)

    assert _names(w) == ["twenty", "nine hundred", "one point two"]
    assert _indicator(w) == (SIZE, Qt.AscendingOrder)


def test_the_second_size_click_sorts_largest_first():
    w = _size_widget()

    _click(w, SIZE)
    _click(w, SIZE)

    assert _names(w) == ["one point two", "nine hundred", "twenty"]
    assert _indicator(w) == (SIZE, Qt.DescendingOrder)


def test_equal_sizes_break_alphabetically_in_both_directions():
    w = SearchWidget()
    w.set_results(
        (
            _result(1, name="zulu", size=MB_20),
            _result(2, name="alpha", size=MB_20),
            _result(3, name="bravo", size=MB_900),
        )
    )

    _click(w, SIZE)
    assert _names(w) == ["alpha", "zulu", "bravo"]

    _click(w, SIZE)
    assert _names(w) == ["bravo", "alpha", "zulu"]


def test_an_unknown_size_sorts_last_ascending():
    w = SearchWidget()
    w.set_results(
        (
            _result(1, name="unknown", size=None),
            _result(2, name="big", size=MB_900),
            _result(3, name="small", size=MB_20),
        )
    )

    _click(w, SIZE)

    assert _names(w) == ["small", "big", "unknown"]
    assert w.table.item(2, SIZE).text() == UNKNOWN


def test_an_unknown_size_sorts_last_descending_too():
    w = SearchWidget()
    w.set_results(
        (
            _result(1, name="unknown", size=None),
            _result(2, name="big", size=MB_900),
            _result(3, name="small", size=MB_20),
        )
    )

    _click(w, SIZE)
    _click(w, SIZE)

    assert _names(w) == ["big", "small", "unknown"]


def test_two_unknown_sizes_break_alphabetically_at_the_bottom():
    w = SearchWidget()
    w.set_results(
        (
            _result(1, name="zulu", size=None),
            _result(2, name="alpha", size=None),
            _result(3, name="known", size=MB_20),
        )
    )

    _click(w, SIZE)

    assert _names(w) == ["known", "alpha", "zulu"]


# --- group 4: Seeders -------------------------------------------------------
def _seeder_widget():
    w = SearchWidget()
    w.set_results(
        (
            _result(1, name="mike", seeders=50),
            _result(2, name="zulu", seeders=1),
            _result(3, name="alpha", seeders=999),
        )
    )
    return w


def test_seeders_start_descending_without_any_click():
    w = _seeder_widget()

    assert _names(w) == ["alpha", "mike", "zulu"]


def test_the_first_explicit_seeders_click_reapplies_descending():
    """Intentionally no visible reorder: the requested first direction for
    Seeders is the one the default already used. The click still consumes the
    default state, which the next click proves."""
    w = _seeder_widget()

    _click(w, SEEDERS)

    assert _names(w) == ["alpha", "mike", "zulu"]
    assert _indicator(w) == (SEEDERS, Qt.DescendingOrder)
    assert w._sort_is_default is False


def test_the_second_explicit_seeders_click_sorts_low_to_high():
    w = _seeder_widget()

    _click(w, SEEDERS)
    _click(w, SEEDERS)

    assert _names(w) == ["zulu", "mike", "alpha"]
    assert _indicator(w) == (SEEDERS, Qt.AscendingOrder)


def test_equal_seeders_break_alphabetically_in_both_directions():
    w = SearchWidget()
    w.set_results(
        (
            _result(1, name="zulu", seeders=100),
            _result(2, name="alpha", seeders=100),
            _result(3, name="bravo", seeders=5),
        )
    )

    _click(w, SEEDERS)
    assert _names(w) == ["alpha", "zulu", "bravo"]

    _click(w, SEEDERS)
    assert _names(w) == ["bravo", "alpha", "zulu"]


# --- group 5: Leechers ------------------------------------------------------
def _leecher_widget():
    w = SearchWidget()
    w.set_results(
        (
            _result(1, name="mike", leechers=50),
            _result(2, name="zulu", leechers=1),
            _result(3, name="alpha", leechers=999),
        )
    )
    return w


def test_the_first_leechers_click_sorts_high_to_low():
    w = _leecher_widget()

    _click(w, LEECHERS)

    assert _names(w) == ["alpha", "mike", "zulu"]
    assert _indicator(w) == (LEECHERS, Qt.DescendingOrder)


def test_the_second_leechers_click_sorts_low_to_high():
    w = _leecher_widget()

    _click(w, LEECHERS)
    _click(w, LEECHERS)

    assert _names(w) == ["zulu", "mike", "alpha"]
    assert _indicator(w) == (LEECHERS, Qt.AscendingOrder)


def test_equal_leechers_break_alphabetically_in_both_directions():
    w = SearchWidget()
    w.set_results(
        (
            _result(1, name="zulu", leechers=100),
            _result(2, name="alpha", leechers=100),
            _result(3, name="bravo", leechers=5),
        )
    )

    _click(w, LEECHERS)
    assert _names(w) == ["alpha", "zulu", "bravo"]

    _click(w, LEECHERS)
    assert _names(w) == ["bravo", "alpha", "zulu"]


# --- group 6: Added ---------------------------------------------------------
def _added_widget():
    """Both rows land on 2021-01-01, so only the underlying timestamp can
    order them - and the alphabetical tie-break would order them the other
    way round if the formatted date drove the sort."""
    w = SearchWidget()
    w.set_results(
        (
            _result(1, name="alpha", added=DAY_06H),
            _result(2, name="zulu", added=DAY_12H),
            _result(3, name="mike", added=DAY - 86_400),
        )
    )
    return w


def test_the_first_added_click_sorts_most_recent_first():
    w = _added_widget()

    _click(w, ADDED)

    assert _names(w) == ["zulu", "alpha", "mike"]
    assert _indicator(w) == (ADDED, Qt.DescendingOrder)


def test_the_second_added_click_sorts_oldest_first():
    w = _added_widget()

    _click(w, ADDED)
    _click(w, ADDED)

    assert _names(w) == ["mike", "alpha", "zulu"]
    assert _indicator(w) == (ADDED, Qt.AscendingOrder)


def test_equal_added_timestamps_break_alphabetically_in_both_directions():
    w = SearchWidget()
    w.set_results(
        (
            _result(1, name="zulu", added=DAY_12H),
            _result(2, name="alpha", added=DAY_12H),
            _result(3, name="bravo", added=DAY - 86_400),
        )
    )

    _click(w, ADDED)
    assert _names(w) == ["alpha", "zulu", "bravo"]

    _click(w, ADDED)
    assert _names(w) == ["bravo", "alpha", "zulu"]


def test_an_unknown_added_date_sorts_last_most_recent_first():
    w = SearchWidget()
    w.set_results(
        (
            _result(1, name="unknown", added=None),
            _result(2, name="old", added=DAY - 86_400),
            _result(3, name="new", added=DAY_12H),
        )
    )

    _click(w, ADDED)

    assert _names(w) == ["new", "old", "unknown"]
    assert w.table.item(2, ADDED).text() == UNKNOWN


def test_an_unknown_added_date_sorts_last_oldest_first_too():
    w = SearchWidget()
    w.set_results(
        (
            _result(1, name="unknown", added=None),
            _result(2, name="old", added=DAY - 86_400),
            _result(3, name="new", added=DAY_12H),
        )
    )

    _click(w, ADDED)
    _click(w, ADDED)

    assert _names(w) == ["old", "new", "unknown"]


def test_two_unknown_added_dates_break_alphabetically_at_the_bottom():
    w = SearchWidget()
    w.set_results(
        (
            _result(1, name="zulu", added=None),
            _result(2, name="alpha", added=None),
            _result(3, name="known", added=DAY_12H),
        )
    )

    _click(w, ADDED)

    assert _names(w) == ["known", "alpha", "zulu"]


# --- unrenderable values are unknown too ------------------------------------
#: Values the model accepts but the formatters cannot render. The cell shows
#: the unknown placeholder, so the sort has to treat them as unknown as well -
#: otherwise a row reading "—" outranks every real value descending.
HUGE_SIZE = 10**400
HUGE_ADDED = 10**30
HUGE_COUNT = 10**5000


def test_a_size_too_large_to_render_sorts_last_in_both_directions():
    w = SearchWidget()
    w.set_results(
        (
            _result(1, name="absurd", size=HUGE_SIZE),
            _result(2, name="big", size=MB_900),
            _result(3, name="small", size=MB_20),
        )
    )

    _click(w, SIZE)
    assert w.table.item(2, SIZE).text() == UNKNOWN
    assert _names(w) == ["small", "big", "absurd"]

    _click(w, SIZE)
    assert _names(w) == ["big", "small", "absurd"]


def test_an_added_date_too_large_to_render_sorts_last_in_both_directions():
    w = SearchWidget()
    w.set_results(
        (
            _result(1, name="absurd", added=HUGE_ADDED),
            _result(2, name="old", added=DAY - 86_400),
            _result(3, name="new", added=DAY_12H),
        )
    )

    _click(w, ADDED)
    assert w.table.item(2, ADDED).text() == UNKNOWN
    assert _names(w) == ["new", "old", "absurd"]

    _click(w, ADDED)
    assert _names(w) == ["old", "new", "absurd"]


def test_a_swarm_count_too_large_to_render_sorts_last_in_both_directions():
    """Seeders and leechers are never None, but they can still be values the
    formatter refuses. The rule is the displayed one: a "—" cell sorts last."""
    w = SearchWidget()
    w.set_results(
        (
            _result(1, name="absurd", seeders=HUGE_COUNT),
            _result(2, name="few", seeders=5),
            _result(3, name="many", seeders=900),
        )
    )

    _click(w, SEEDERS)
    assert w.table.item(2, SEEDERS).text() == UNKNOWN
    assert _names(w) == ["many", "few", "absurd"]

    _click(w, SEEDERS)
    assert _names(w) == ["few", "many", "absurd"]


# --- a sort cannot be chosen against an empty table -------------------------
def test_a_header_click_on_an_empty_table_changes_nothing():
    """SearchService republishes an empty merged view every time a source
    returns no rows, and that is also the widget's new-search reset boundary.
    Refusing to record a sort nobody can see keeps the two from colliding:
    there is no user choice to discard."""
    w = SearchWidget()
    w.set_results(())

    _click(w, SIZE)
    _click(w, NAME)

    assert w.table.rowCount() == 0
    assert w._sort_is_default is True
    assert _indicator(w) == (SEEDERS, Qt.DescendingOrder)


def test_an_empty_partial_update_cannot_discard_a_visible_sort():
    w = SearchWidget()
    w.set_results(())
    # The window between a search starting and its first source answering.
    _click(w, SIZE)
    w.set_results(())

    w.set_results(
        (
            _result(1, name="mike", seeders=50),
            _result(2, name="alpha", seeders=999),
        )
    )

    assert _names(w) == ["alpha", "mike"]
    assert _indicator(w) == (SEEDERS, Qt.DescendingOrder)


# --- group 7: result identity -----------------------------------------------
def _identity_widget():
    w = SearchWidget()
    results = (
        _result(1, name="mike", size=MB_900, seeders=50, leechers=7, added=DAY_12H),
        _result(2, name="zulu", size=MB_20, seeders=1, leechers=90, added=None),
        _result(3, name="alpha", size=None, seeders=999, leechers=3, added=DAY_06H),
        _result(4, name="Bravo", size=GB_1_2, seeders=50, leechers=3, added=DAY),
    )
    w.set_results(results)
    return w, results


def test_sorting_never_changes_result_membership_or_row_count():
    w, results = _identity_widget()
    expected = sorted(r.info_hash for r in results)

    for column in (NAME, NAME, SIZE, SIZE, SEEDERS, SEEDERS, LEECHERS, ADDED, ADDED):
        _click(w, column)
        assert w.table.rowCount() == len(results)
        hashes = [r.info_hash for r in _rows(w)]
        assert sorted(hashes) == expected
        assert len(set(hashes)) == len(results)


def test_every_sorted_row_carries_one_of_the_original_result_objects():
    w, results = _identity_widget()

    _click(w, SIZE)

    rendered = _rows(w)
    assert len(rendered) == len(results)
    for shown in rendered:
        assert any(shown is original for original in results)


def test_the_download_button_emits_the_exact_object_of_the_visible_row():
    w, _ = _identity_widget()
    seen = _record_downloads(w)
    _click(w, SIZE)

    row = _names(w).index("mike")
    expected = w.table.item(row, NAME).data(Qt.UserRole)
    w.table.selectRow(row)
    w.download_button.click()

    assert len(seen) == 1
    assert seen[0] is expected
    assert seen[0].name == "mike"


def test_double_clicking_a_sorted_row_emits_that_rows_exact_object():
    w, _ = _identity_widget()
    seen = _record_downloads(w)
    _click(w, SIZE)

    row = _names(w).index("Bravo")
    expected = w.table.item(row, NAME).data(Qt.UserRole)
    w.table.cellDoubleClicked.emit(row, 0)

    assert len(seen) == 1
    assert seen[0] is expected
    assert seen[0].name == "Bravo"


def test_the_selection_follows_the_same_torrent_across_a_header_sort():
    w, _ = _identity_widget()
    start = _names(w).index("zulu")
    w.table.selectRow(start)
    before = w.selected_result()

    _click(w, SIZE)

    after = w.selected_result()
    assert after is before
    assert after.info_hash == before.info_hash
    assert _names(w).index("zulu") == w.table.selectionModel().selectedRows()[0].row()
    assert w.download_button.isEnabled() is True


# --- group 8: a header click costs nothing ----------------------------------
def test_a_header_click_performs_no_search_work():
    calls = []

    def names_provider():
        calls.append(1)
        return {}

    w = SearchWidget(custom_source_names=names_provider)
    searches = []
    w.search_requested.connect(lambda q, c: searches.append((q, c)))
    w.set_results((_result(1, name="zulu"), _result(2, name="alpha")))
    w.query.setText("dune")

    _click(w, NAME)
    _click(w, SIZE)

    assert searches == []
    # The only outward call a sort may make is the widget's own display-name
    # lookup, once per repopulation - never a source, service or network call.
    assert len(calls) == 3


def test_a_header_click_does_not_introduce_a_new_result_snapshot():
    w = SearchWidget()
    w.set_results((_result(1, name="zulu"), _result(2, name="alpha")))
    snapshots = []
    original = w.set_results
    w.set_results = lambda results: (snapshots.append(results), original(results))[1]

    _click(w, NAME)
    _click(w, SEEDERS)
    _click(w, SOURCE)

    assert snapshots == []


def test_a_header_click_emits_no_search_request():
    w = SearchWidget()
    seen = []
    w.search_requested.connect(lambda q, c: seen.append((q, c)))
    w.set_results((_result(1), _result(2)))

    for column in range(len(COLUMNS)):
        _click(w, column)

    assert seen == []


# --- group 9: state lifetime ------------------------------------------------
def test_emptying_the_results_resets_the_sort_state_to_the_default():
    w = SearchWidget()
    w.set_results((_result(1, name="zulu"), _result(2, name="alpha")))
    _click(w, NAME)

    w.set_results(())

    assert w.table.rowCount() == 0
    assert _indicator(w) == (SEEDERS, Qt.DescendingOrder)
    assert w._sort_is_default is True


def test_a_later_snapshot_of_the_same_search_keeps_the_chosen_sort():
    w = SearchWidget()
    w.set_results(
        (
            _result(1, name="big", size=MB_900),
            _result(2, name="small", size=MB_20),
        )
    )
    _click(w, SIZE)

    w.set_results(
        (
            _result(1, name="big", size=MB_900),
            _result(2, name="small", size=MB_20),
            _result(3, name="middle", size=MB_900 - 1),
        )
    )

    assert _names(w) == ["small", "middle", "big"]
    assert _indicator(w) == (SIZE, Qt.AscendingOrder)


def test_a_new_search_sequence_returns_to_seeders_descending():
    w = SearchWidget()
    w.set_results((_result(1, name="zulu", seeders=1), _result(2, name="alpha", seeders=9)))
    _click(w, NAME)
    _click(w, NAME)

    w.set_results(())
    w.set_results(
        (
            _result(3, name="mike", seeders=50),
            _result(4, name="yankee", seeders=999),
        )
    )

    assert _names(w) == ["yankee", "mike"]
    assert _indicator(w) == (SEEDERS, Qt.DescendingOrder)


# --- group 10: scope --------------------------------------------------------
def test_clicking_source_changes_neither_the_order_nor_the_indicator():
    w = SearchWidget()
    w.set_results(
        (
            _result(1, name="zulu", seeders=1, source="yts"),
            _result(2, name="alpha", seeders=9, source="rutor"),
        )
    )
    _click(w, NAME)
    before = _names(w)
    # Qt's own header flips the indicator on a real mouse release, so the
    # no-op has to survive the indicator having already moved.
    w.table.horizontalHeader().setSortIndicator(SOURCE, Qt.DescendingOrder)

    _click(w, SOURCE)

    assert _names(w) == before
    assert _indicator(w) == (NAME, Qt.AscendingOrder)


def test_the_table_still_does_not_sort_itself():
    w = SearchWidget()
    w.set_results((_result(1, name="zulu"), _result(2, name="alpha")))

    _click(w, NAME)

    assert w.table.isSortingEnabled() is False


def test_sorting_leaves_the_columns_and_source_display_names_alone():
    w = SearchWidget(custom_source_names=lambda: {"custom:abc": "My Indexer"})
    w.set_results(
        (
            _result(1, name="zulu", source="custom:abc"),
            _result(2, name="alpha", source="yts"),
        )
    )

    _click(w, NAME)

    headers = [
        w.table.horizontalHeaderItem(c).text() for c in range(w.table.columnCount())
    ]
    assert headers == list(COLUMNS)
    assert w.table.item(0, SOURCE).text() == "yts"
    assert w.table.item(1, SOURCE).text() == "My Indexer"


def test_a_sorted_result_still_carries_its_magnet_to_the_download_path():
    w, _ = _identity_widget()
    seen = _record_downloads(w)

    _click(w, ADDED)
    row = _names(w).index("alpha")
    w.table.selectRow(row)
    w.download_button.click()

    assert len(seen) == 1
    assert seen[0].name == "alpha"
    assert seen[0].info_hash in seen[0].magnet
