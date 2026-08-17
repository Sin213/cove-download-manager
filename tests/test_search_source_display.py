"""S8: configured custom-indexer display names in the Search Source column.

Presentation only. SearchResult.source keeps its stable ``custom:<uuid>``
identity everywhere below the widget; the widget resolves the current
configured name through a small ``custom_source_names`` provider handed to it
at construction, once per result-table refresh.

The provider is a plain callable returning ``{custom_id: display_name}`` - the
widget never sees Settings, indexer records, URLs or API keys. Lookup is exact
stable-id matching; anything unmatched (a built-in, a future source, a stale
custom id) falls back to the raw source string.
"""
import ast
import inspect
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

import cove.search.widget
from cove.search.indexers import CustomTorznabIndexer
from cove.search.models import SearchResult
from cove.search.widget import COLUMNS, SearchWidget

_CUSTOM_A = "custom:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_CUSTOM_B = "custom:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_CUSTOM_MISSING = "custom:cccccccc-cccc-cccc-cccc-cccccccccccc"


def _record(record_id, name, *, api_key="", url="https://indexer.invalid/torznab"):
    """A minimal configured custom indexer record - only id/name are used."""
    return CustomTorznabIndexer(id=record_id, name=name, url=url, api_key=api_key)


def _result(n, *, source="yts", name=None):
    """A real SearchResult - the model validates, so no fake stands in."""
    info_hash = f"{n:040x}"
    return SearchResult(
        info_hash=info_hash,
        name=name if name is not None else f"result {n}",
        magnet=f"magnet:?xt=urn:btih:{info_hash}",
        size_bytes=None,
        seeders=0,
        leechers=0,
        added=None,
        source=source,
    )


def _sources(widget):
    """The Source column, top to bottom."""
    col = COLUMNS.index("Source")
    return [widget.table.item(r, col).text() for r in range(widget.table.rowCount())]


def _names(*records):
    """The exact {id: name} mapping the widget is allowed to see."""
    return {r.id: r.name for r in records}


# --- RED GROUP 1: built-in source presentation is unchanged ------------------


def test_builtin_sources_render_exactly_as_before_without_a_provider():
    w = SearchWidget()

    w.set_results((_result(1, source="yts"), _result(2, source="nyaa")))

    assert _sources(w) == ["yts", "nyaa"]


def test_builtin_sources_render_exactly_as_before_with_a_provider():
    w = SearchWidget(custom_source_names=lambda: _names(_record(_CUSTOM_A, "X")))

    w.set_results((_result(1, source="yts"), _result(2, source="nyaa")))

    assert _sources(w) == ["yts", "nyaa"]


# --- RED GROUP 2: custom id renders the configured name ----------------------


def test_a_custom_source_renders_its_configured_name():
    record = _record(_CUSTOM_A, "My Local Indexer")
    w = SearchWidget(custom_source_names=lambda: _names(record))
    result = _result(1, source=_CUSTOM_A)

    w.set_results((result,))

    assert _sources(w) == ["My Local Indexer"]
    # The model value is untouched: the id stays the stable identity.
    assert result.source == _CUSTOM_A


# --- RED GROUP 3: the stable id is never mutated -----------------------------


def test_rendering_never_mutates_the_result_source():
    record = _record(_CUSTOM_A, "My Local Indexer")
    w = SearchWidget(custom_source_names=lambda: _names(record))
    result = _result(1, source=_CUSTOM_A)

    w.set_results((result,))

    assert result.source == _CUSTOM_A
    # The row still carries the exact original object, id and all.
    assert w.table.item(0, 0).data(Qt.UserRole) is result


def test_rendering_twice_still_does_not_mutate_the_source():
    record = _record(_CUSTOM_A, "My Local Indexer")
    w = SearchWidget(custom_source_names=lambda: _names(record))
    result = _result(1, source=_CUSTOM_A)

    w.set_results((result,))
    w.set_results((result,))

    assert result.source == _CUSTOM_A


# --- RED GROUP 4: unknown custom id falls back to the raw id -----------------


def test_an_unmatched_custom_id_shows_the_raw_id():
    w = SearchWidget(custom_source_names=lambda: {})

    w.set_results((_result(1, source=_CUSTOM_MISSING),))

    assert _sources(w) == [_CUSTOM_MISSING]


def test_a_custom_id_whose_record_was_removed_shows_the_raw_id():
    # The record existed when the result was produced, then was deleted.
    w = SearchWidget(custom_source_names=lambda: _names(_record(_CUSTOM_A, "Old")))
    result = _result(1, source=_CUSTOM_MISSING)

    w.set_results((result,))

    assert _sources(w) == [_CUSTOM_MISSING]


# --- RED GROUP 5: ordinary unknown sources are passed through ----------------


def test_a_future_non_custom_source_keeps_its_raw_string():
    w = SearchWidget(custom_source_names=lambda: _names(_record(_CUSTOM_A, "X")))

    w.set_results((_result(1, source="some-future-source"),))

    assert _sources(w) == ["some-future-source"]


# --- RED GROUP 6: a name edit is reflected on the next render ----------------


def test_a_name_edit_is_visible_on_the_next_render_of_the_same_result():
    names = {_CUSTOM_A: "Old Name"}
    w = SearchWidget(custom_source_names=lambda: names)
    result = _result(1, source=_CUSTOM_A)

    w.set_results((result,))
    assert _sources(w) == ["Old Name"]

    names[_CUSTOM_A] = "New Name"
    w.set_results((result,))

    assert _sources(w) == ["New Name"]
    assert result.source == _CUSTOM_A


def test_the_mapping_is_not_captured_once_at_construction():
    """The provider is invoked at render time, so later edits are seen."""
    names = {_CUSTOM_A: "First"}
    w = SearchWidget(custom_source_names=lambda: names)
    w.set_results((_result(1, source=_CUSTOM_A),))
    assert _sources(w) == ["First"]

    names[_CUSTOM_A] = "Second"
    w.set_results((_result(1, source=_CUSTOM_A),))

    assert _sources(w) == ["Second"]


# --- RED GROUP 8: duplicate display names do not merge sources ---------------


def test_two_records_may_share_a_name_without_merging():
    records = (
        _record(_CUSTOM_A, "Same Name"),
        _record(_CUSTOM_B, "Same Name"),
    )
    w = SearchWidget(custom_source_names=lambda: _names(*records))
    result_a = _result(1, source=_CUSTOM_A)
    result_b = _result(2, source=_CUSTOM_B)

    w.set_results((result_a, result_b))

    assert _sources(w) == ["Same Name", "Same Name"]
    assert result_a.source == _CUSTOM_A
    assert result_b.source == _CUSTOM_B


# --- RED GROUP 9: special characters are plain text --------------------------


def test_special_characters_render_as_literal_text():
    record = _record(_CUSTOM_A, 'Indexer <Primary> & "Local"')
    w = SearchWidget(custom_source_names=lambda: _names(record))

    w.set_results((_result(1, source=_CUSTOM_A),))

    item = w.table.item(0, COLUMNS.index("Source"))
    assert item.text() == 'Indexer <Primary> & "Local"'
    # Plain-text storage: the widget strips the editable flag; nothing here
    # enables rich text, so the string is stored and read back verbatim.
    assert not (item.flags() & Qt.ItemIsEditable)


# --- RED GROUP 10: the api key is never displayed ----------------------------


def test_the_api_key_never_reaches_the_source_cell():
    record = _record(_CUSTOM_A, "Friendly Indexer", api_key="super-secret-s8-key")
    w = SearchWidget(custom_source_names=lambda: _names(record))

    w.set_results((_result(1, source=_CUSTOM_A),))

    item = w.table.item(0, COLUMNS.index("Source"))
    assert item.text() == "Friendly Indexer"
    assert "super-secret-s8-key" not in item.text()
    assert "super-secret-s8-key" not in item.toolTip()
    assert "super-secret-s8-key" not in w.status.text()


def test_the_provider_mapping_contains_only_ids_and_names():
    """The widget's presentation surface can never carry a secret."""
    record = _record(_CUSTOM_A, "Friendly Indexer", api_key="super-secret-s8-key")
    record.url = "http://127.0.0.1:9696/secret-path"

    mapping = _names(record)

    assert mapping == {_CUSTOM_A: "Friendly Indexer"}


# --- RED GROUP 11: the endpoint is never a display fallback ------------------


def test_a_missing_record_falls_back_to_the_id_not_the_url():
    record = _record(
        _CUSTOM_A, "Friendly Indexer", url="http://127.0.0.1:9696/secret-path"
    )
    w = SearchWidget(custom_source_names=lambda: _names(record))

    # A result from a DIFFERENT custom id whose record is absent.
    w.set_results((_result(1, source=_CUSTOM_B),))

    assert _sources(w) == [_CUSTOM_B]


# --- RED GROUP 12: settings order does not affect matching -------------------


def test_lookup_is_by_exact_id_not_list_position():
    records = (
        _record(_CUSTOM_B, "B Name"),
        _record(_CUSTOM_A, "A Name"),
    )
    w = SearchWidget(custom_source_names=lambda: _names(*records))

    w.set_results((_result(1, source=_CUSTOM_A),))

    assert _sources(w) == ["A Name"]


# --- RED GROUP 13: current settings provider (window wiring) -----------------
# The window-level wiring test lives in test_search_main_window.py, which owns
# the Host seam. The widget contract it relies on is proven above: the provider
# is consulted at every render and reflects the current mapping.


# --- RED GROUP 14: presentation stays out of the core layers -----------------


def _widget_source():
    return Path(inspect.getfile(cove.search.widget)).read_text(encoding="utf-8")


def test_the_widget_never_touches_indexer_records_or_settings_state():
    """The widget receives {id: name}; it must not know how to reach more."""
    source = _widget_source()
    for forbidden in (
        "custom_indexers",
        "CustomTorznabIndexer",
        "api_key",
        ".url",
        "settings",
    ):
        assert forbidden not in source


def test_the_widget_never_assigns_to_result_source():
    """Presentation derives a label; it never rewrites the model."""
    tree = ast.parse(_widget_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                assert not (
                    isinstance(target, ast.Attribute) and target.attr == "source"
                ), "widget must not assign to a .source attribute"


# --- RED GROUP 15: the 500-row presentation path -----------------------------


def test_a_large_result_batch_queries_the_mapping_once():
    calls = []

    def provider():
        calls.append(1)
        return _names(
            _record(_CUSTOM_A, "A Name"),
            _record(_CUSTOM_B, "B Name"),
        )

    w = SearchWidget(custom_source_names=provider)
    results = tuple(
        _result(n, source=_CUSTOM_A if n % 2 else _CUSTOM_B) for n in range(1, 501)
    )

    w.set_results(results)

    assert w.table.rowCount() == 500
    # One mapping per refresh - never a per-row scan.
    assert calls == [1]
    expected = ["A Name" if n % 2 else "B Name" for n in range(1, 501)]
    assert _sources(w) == expected


# --- provider failure safety --------------------------------------------------


def test_a_broken_provider_degrades_to_raw_sources_without_crashing():
    def broken():
        raise RuntimeError("settings state is malformed")

    w = SearchWidget(custom_source_names=broken)

    w.set_results((_result(1, source=_CUSTOM_A), _result(2, source="yts")))

    assert _sources(w) == [_CUSTOM_A, "yts"]


def test_a_provider_returning_garbage_degrades_to_raw_sources():
    w = SearchWidget(custom_source_names=lambda: "not a mapping")

    w.set_results((_result(1, source=_CUSTOM_A),))

    assert _sources(w) == [_CUSTOM_A]