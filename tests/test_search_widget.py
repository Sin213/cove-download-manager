"""The standalone Search page widget.

The widget is deliberately passive: it renders whatever it is handed and
reports user intent through one signal. It owns no SearchService, no queue and
no intake path, so these tests construct it directly - there is no window, no
worker pool and no network anywhere in this file.
"""
import ast
import inspect
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractItemView, QComboBox, QLabel, QLineEdit, QPushButton, QTableWidget

import cove.search.widget
from cove.search.models import Category, SearchResult
from cove.search.widget import SearchWidget


def _record(widget):
    """Capture every search_requested emission as (query, category)."""
    seen = []
    widget.search_requested.connect(lambda q, c: seen.append((q, c)))
    return seen


def test_widget_builds_its_controls():
    w = SearchWidget()

    assert isinstance(w.query, QLineEdit)
    assert isinstance(w.category, QComboBox)
    assert isinstance(w.search_button, QPushButton)
    assert isinstance(w.status, QLabel)
    assert isinstance(w.table, QTableWidget)
    assert w.table.rowCount() == 0
    assert w.search_button.text() == "Search"


# --- categories -------------------------------------------------------------


def test_category_labels_are_in_the_fixed_visible_order():
    w = SearchWidget()

    labels = [w.category.itemText(i) for i in range(w.category.count())]

    assert labels == ["All", "Games", "Movies", "TV", "Anime"]


def test_category_items_carry_the_real_enum_values():
    w = SearchWidget()

    data = [w.category.itemData(i) for i in range(w.category.count())]

    # Identity, not the label or the enum's value string: the next slice hands
    # these straight to SearchService.
    assert data == [
        Category.ALL,
        Category.GAMES,
        Category.MOVIES,
        Category.TV,
        Category.ANIME,
    ]


def test_default_category_is_all():
    w = SearchWidget()

    assert w.current_category() is Category.ALL


# --- search_requested -------------------------------------------------------


def test_search_button_emits_the_query_and_category_once():
    w = SearchWidget()
    seen = _record(w)
    w.query.setText("dune")
    w.category.setCurrentIndex(2)

    w.search_button.click()

    assert seen == [("dune", Category.MOVIES)]


def test_return_in_the_query_field_emits_the_same_contract():
    w = SearchWidget()
    seen = _record(w)
    w.query.setText("dune")
    w.category.setCurrentIndex(2)

    QTest.keyClick(w.query, Qt.Key_Return)

    assert seen == [("dune", Category.MOVIES)]


def test_the_widget_emits_the_query_exactly_as_typed():
    """Normalisation belongs to SearchService; the widget must not pre-empt it."""
    w = SearchWidget()
    seen = _record(w)
    w.query.setText("  Dune  ")

    w.search_button.click()
    QTest.keyClick(w.query, Qt.Key_Return)

    assert seen == [("  Dune  ", Category.ALL), ("  Dune  ", Category.ALL)]


def test_typing_alone_does_not_search():
    w = SearchWidget()
    seen = _record(w)

    w.query.setText("dune")

    assert seen == []


def test_changing_the_category_alone_does_not_search():
    w = SearchWidget()
    seen = _record(w)

    w.category.setCurrentIndex(3)

    assert seen == []


# --- result rendering -------------------------------------------------------


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


def _column(widget, col):
    return [widget.table.item(r, col).text() for r in range(widget.table.rowCount())]


def test_the_table_headers_are_the_agreed_columns():
    w = SearchWidget()

    headers = [
        w.table.horizontalHeaderItem(c).text() for c in range(w.table.columnCount())
    ]

    # No info hash, magnet or tracker column: those stay inside the result.
    assert headers == ["Name", "Size", "Seeders", "Leechers", "Added", "Source"]


def test_set_results_renders_one_row_per_result():
    w = SearchWidget()

    w.set_results((_result(1), _result(2), _result(3)))

    assert w.table.rowCount() == 3


def test_set_results_preserves_the_order_it_was_given():
    """SearchService already ranked these; the widget must not rank them again."""
    w = SearchWidget()
    results = (
        _result(1, name="zulu", seeders=1),
        _result(2, name="alpha", seeders=999),
        _result(3, name="mike", seeders=50),
    )

    w.set_results(results)

    assert _column(w, 0) == ["zulu", "alpha", "mike"]


def test_the_table_does_not_sort_itself():
    w = SearchWidget()

    assert w.table.isSortingEnabled() is False


def test_a_populated_result_renders_every_column():
    w = SearchWidget()

    w.set_results(
        (
            _result(
                1,
                name="Dune (2021)",
                size=1536,
                seeders=42,
                leechers=7,
                added=1_600_000_000,
                source="yts",
            ),
        )
    )

    row = [w.table.item(0, c).text() for c in range(w.table.columnCount())]
    assert row == ["Dune (2021)", "1.5 KB", "42", "7", "2020-09-13", "yts"]


def test_larger_sizes_use_larger_units():
    w = SearchWidget()

    w.set_results((_result(1, size=512), _result(2, size=5 * 1024**3)))

    assert _column(w, 1) == ["512 B", "5.0 GB"]


def test_an_unreported_size_renders_the_neutral_placeholder():
    w = SearchWidget()

    w.set_results((_result(1, size=None),))

    assert w.table.item(0, 1).text() == "—"


def test_an_unreported_added_date_renders_the_neutral_placeholder():
    w = SearchWidget()

    w.set_results((_result(1, added=None),))

    assert w.table.item(0, 4).text() == "—"


def test_swarm_counts_render_as_plain_decimals():
    w = SearchWidget()

    w.set_results((_result(1, seeders=1200, leechers=0),))

    assert (w.table.item(0, 2).text(), w.table.item(0, 3).text()) == ("1200", "0")


def test_the_table_is_not_editable():
    w = SearchWidget()
    w.set_results((_result(1),))

    assert w.table.editTriggers() == QAbstractItemView.NoEditTriggers
    assert not (w.table.item(0, 0).flags() & Qt.ItemIsEditable)


def test_set_results_replaces_the_previous_rows():
    w = SearchWidget()
    w.set_results((_result(1, name="a"), _result(2, name="b")))

    w.set_results((_result(3, name="c"),))

    assert _column(w, 0) == ["c"]


def test_empty_results_render_an_empty_table():
    w = SearchWidget()
    w.set_results((_result(1),))

    w.set_results(())

    assert w.table.rowCount() == 0
    assert w.selected_result() is None


# --- selection identity -----------------------------------------------------


def test_nothing_is_selected_to_start_with():
    w = SearchWidget()
    w.set_results((_result(1), _result(2)))

    assert w.selected_result() is None


def test_selecting_a_row_yields_the_original_result_object():
    w = SearchWidget()
    results = (_result(1), _result(2), _result(3))
    w.set_results(results)

    w.table.selectRow(1)

    # Identity, not equality: Tab 2d.3 hands this exact object to intake, so a
    # row rebuilt from the displayed strings would be a silent regression.
    assert w.selected_result() is results[1]


def test_the_table_selects_whole_rows_one_at_a_time():
    w = SearchWidget()

    assert w.table.selectionBehavior() == QAbstractItemView.SelectRows
    assert w.table.selectionMode() == QAbstractItemView.SingleSelection


def test_selecting_a_second_row_replaces_the_first_selection():
    w = SearchWidget()
    results = (_result(1), _result(2), _result(3))
    w.set_results(results)
    w.table.selectRow(0)

    w.table.selectRow(2)

    assert len(w.table.selectionModel().selectedRows()) == 1
    assert w.selected_result() is results[2]


def test_replacing_the_results_drops_the_old_selection():
    w = SearchWidget()
    w.set_results((_result(1), _result(2)))
    w.table.selectRow(1)

    w.set_results((_result(3), _result(4)))

    assert w.selected_result() is None


# --- passive state API ------------------------------------------------------


def test_the_status_line_starts_quiet():
    w = SearchWidget()

    assert w.status.text() == ""


def test_set_status_shows_exactly_what_it_is_given():
    w = SearchWidget()

    w.set_status("Searching 3 sources")

    assert w.status.text() == "Searching 3 sources"


def test_set_searching_disables_and_re_enables_only_the_search_button():
    w = SearchWidget()

    w.set_searching(True)

    assert w.search_button.isEnabled() is False
    assert w.query.isEnabled() is True
    assert w.category.isEnabled() is True

    w.set_searching(False)

    assert w.search_button.isEnabled() is True


def test_the_state_setters_never_request_a_search():
    w = SearchWidget()
    seen = _record(w)

    w.set_status("anything")
    w.set_searching(True)
    w.set_searching(False)
    w.set_results((_result(1),))
    w.set_results(())

    assert seen == []


# --- structural boundary ----------------------------------------------------


def _widget_source():
    return Path(inspect.getfile(cove.search.widget)).read_text(encoding="utf-8")


def test_the_widget_module_imports_nothing_but_the_search_model():
    """A presentation component with no application dependencies.

    Read statically rather than through sys.modules: another test module may
    already have imported the service, which would make a runtime snapshot
    depend on collection order.
    """
    tree = ast.parse(_widget_source())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    cove_imports = {name for name in imported if name.split(".")[0] == "cove"}
    assert cove_imports == {"cove.search.models"}
    for forbidden in (
        "cove.search.service",
        "cove.search.registry",
        "cove.main_window",
        "cove.queue",
        "cove.debrid",
        "cove.torrent",
        "cove.aria2",
        "cove.diagnostics",
        "requests",
        "threading",
        "concurrent.futures",
    ):
        assert forbidden not in imported


def test_the_widget_starts_no_download_and_no_work():
    source = _widget_source()

    for forbidden in ("add_search_result", "add_urls_checked", "QTimer", "QThread"):
        assert forbidden not in source


# --- hostile provider metadata ----------------------------------------------


def test_an_absurd_size_falls_back_to_the_placeholder_without_breaking_the_row():
    """The model accepts any non-negative int, so the formatter must too.

    A single unrenderable value from an untrusted indexer must not abort the
    refresh and leave the table half-populated.
    """
    w = SearchWidget()

    w.set_results((_result(1, name="huge", size=10**400), _result(2, name="next"),))

    assert _column(w, 0) == ["huge", "next"]
    assert w.table.item(0, 1).text() == "—"


def test_an_absurd_added_timestamp_falls_back_to_the_placeholder():
    w = SearchWidget()

    w.set_results((_result(1, name="huge", added=10**20), _result(2, name="next"),))

    assert _column(w, 0) == ["huge", "next"]
    assert w.table.item(0, 4).text() == "—"


def test_an_absurd_swarm_count_falls_back_to_the_placeholder():
    """Python refuses to stringify integers past its digit limit."""
    w = SearchWidget()

    w.set_results((_result(1, name="huge", seeders=10**5000), _result(2, name="next"),))

    assert _column(w, 0) == ["huge", "next"]
    assert w.table.item(0, 2).text() == "—"
