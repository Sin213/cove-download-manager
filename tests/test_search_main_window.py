"""The Search page inside the main window.

This suite covers exactly one seam: the wiring between the already-approved
SearchWidget and the already-approved SearchService. Neither of those is
retested here - the widget suite owns rendering, the service suite owns
lifecycle - so every assertion below is about what the main window does
between them.

The interesting cases are all about timing. SearchService may finish a search
synchronously, inside start(), when the query is blank, the category has no
source, or every source answers from cache; a window that prepares its UI
after start() returns would undo the final state it had already been given.
The generation guards matter for the same reason: the service numbers every
search, and a superseded one must never repaint the current page.

No test here reaches the network. Sources are real Source subclasses that
answer from memory, every wait is bounded, and every held source is released
in a finally.
"""
import threading
import time

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QMainWindow, QWidget

import cove.main_window as mw
from cove.search import service as search_service
from cove.search.magnet import build_magnet
from cove.search.models import Category, SearchResult, SourceError, SourceErrorKind
from cove.search.service import SearchService, SearchSummary, SourceFailure
from cove.search.sources.base import Source
from cove.search.widget import SearchWidget

# Every wait in this module is bounded: a broken window must fail the suite,
# not hang it.
_WAIT_SECONDS = 5.0


def _hash(marker: str) -> str:
    return (marker * 40)[:40]


A = _hash("a")
B = _hash("b")


def _result(info_hash=A, name="Example", source="fake", seeders=5):
    return SearchResult(
        info_hash=info_hash,
        name=name,
        magnet=build_magnet(info_hash, name),
        size_bytes=1024,
        seeders=seeders,
        leechers=1,
        added=1_700_000_000,
        source=source,
    )


class _FakeHttp:
    """Stands in for SearchHttp, with only what the worker itself touches."""

    def __init__(self):
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class _FakeSource(Source):
    """A real Source whose one search answers from memory.

    Modelled on the service suite's fake for the same reason: the integration
    tests below run through the real _SourceCall and the real private pool, so
    the thing on the far end has to be a genuine Source that returns genuine
    SearchResults - not a stub that returns None.
    """

    id = "fake"
    label = "Fake"
    categories = (Category.MOVIES,)
    homepage = "https://example.invalid"
    reports_swarm = True

    def __init__(self, rows=None, raises=None, *, source_id=None, hold=None):
        self._rows = rows if rows is not None else []
        self._raises = raises
        self._hold = hold
        if source_id is not None:
            self.id = source_id
        self.calls = []

    def search(self, query, category, http):
        self.calls.append((query, category))
        if self._hold is not None:
            # Bounded: a window that never releases the source still fails.
            assert self._hold.wait(_WAIT_SECONDS), "source was never released"
        if self._raises is not None:
            raise self._raises
        return list(self._rows)


class Host(mw.MainWindow):
    """The real MainWindow methods, without its heavy constructor.

    The same seam tests/test_search_intake.py already uses: the Search page is
    built by its own production method, so a test gets the real composition
    without a queue, a scheduler or a tray.
    """

    def __init__(self):
        QMainWindow.__init__(self)


def _wire(host):
    """Build the host's Search page, and keep the page container alive.

    In the running application the container is owned by the body layout. A
    test that let it go would have Qt delete the stack - and the Search widget
    inside it - out from under the assertions.
    """
    host._page_container = host._build_pages(QWidget())
    return host


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


def _select(monkeypatch, sources):
    """Replace source selection with the given fakes, recording the category."""
    asked = []

    def _sources_for(category=Category.ALL):
        asked.append(category)
        return list(sources)

    monkeypatch.setattr(search_service, "sources_for", _sources_for)
    return asked


def _offline_service(monkeypatch):
    """Make the window's SearchService build fake HTTP instead of real HTTP.

    The service's own http_factory seam, reached the way the window builds its
    service: the symbol the main window imported is replaced, so the window
    still constructs its own single service on its own terms.
    """
    def factory(parent=None):
        return SearchService(parent, http_factory=_FakeHttp)

    monkeypatch.setattr(mw, "SearchService", factory)


def _host(monkeypatch, sources=None, *, offline=True):
    """A host with the real Search page built, and no route to the network."""
    if offline:
        _offline_service(monkeypatch)
    if sources is not None:
        _select(monkeypatch, sources)
    host = Host()
    return _wire(host)


def _rows_shown(host):
    """The names in the result table, top to bottom."""
    table = host.search_widget.table
    return [table.item(row, 0).text() for row in range(table.rowCount())]


def _status(host):
    return host.search_widget.status.text()


def _searching(host):
    return not host.search_widget.search_button.isEnabled()


def _seed_cache(host, source_id, category, text, rows):
    """Give the window's own service a cached answer for one source."""
    host.search_service._cache.put(
        search_service._CacheKey(source_id, category, text), rows
    )


# --- Group A: the window owns the Search components --------------------------


def test_the_window_owns_one_search_widget():
    host = Host()

    _wire(host)

    assert isinstance(host.search_widget, SearchWidget)
    assert host.pages.widget(1) is host.search_widget
    assert host.pages.count() == 2


def test_the_window_owns_one_search_service_parented_to_itself():
    """The production default path: no injected factory, no test-only argument."""
    host = Host()

    _wire(host)

    assert isinstance(host.search_service, SearchService)
    assert host.search_service.parent() is host


def test_building_the_search_page_starts_no_search():
    host = Host()

    _wire(host)

    assert host.search_service.generation == 0
    assert host.search_service.active is False


def test_building_the_search_page_calls_no_source(monkeypatch):
    source = _FakeSource([_result()])
    host = _host(monkeypatch, [source])

    assert source.calls == []
    assert host.search_service.generation == 0


def test_the_search_widget_starts_in_its_own_default_state(monkeypatch):
    host = _host(monkeypatch, [])

    assert host.search_widget.query.text() == ""
    assert host.search_widget.current_category() is Category.ALL
    assert host.search_widget.table.rowCount() == 0
    assert _status(host) == ""
    assert _searching(host) is False


# --- Group B: navigation -----------------------------------------------------


def test_the_search_page_is_reachable_and_shows_the_one_widget(monkeypatch):
    host = _host(monkeypatch, [])

    host.nav_search.click()

    assert host.pages.currentWidget() is host.search_widget


def test_the_search_destination_is_labelled_search(monkeypatch):
    host = _host(monkeypatch, [])

    assert host.nav_search.text() == "Search"
    assert host.nav_downloads.text() == "Downloads"


def test_downloads_stays_the_page_the_window_opens_on(monkeypatch):
    host = _host(monkeypatch, [])
    downloads = host.pages.widget(0)

    assert host.pages.currentWidget() is downloads
    assert host.pages.currentWidget() is not host.search_widget


def test_the_nav_button_for_the_visible_page_is_the_highlighted_one(monkeypatch):
    """The stylesheet has no :checked rule, so checked alone shows nothing."""
    host = _host(monkeypatch, [])

    assert host.nav_downloads.property("kind") == "accent"
    assert host.nav_search.property("kind") != "accent"

    host.nav_search.click()

    assert host.nav_search.property("kind") == "accent"
    assert host.nav_downloads.property("kind") != "accent"


def test_opening_the_search_page_starts_no_search(monkeypatch):
    source = _FakeSource([_result()])
    host = _host(monkeypatch, [source])

    host.nav_search.click()
    host.nav_downloads.click()
    host.nav_search.click()

    assert host.search_service.generation == 0
    assert host.search_service.active is False
    assert source.calls == []


def test_navigating_away_and_back_keeps_the_same_search_widget(monkeypatch):
    host = _host(monkeypatch, [])
    widget = host.search_widget
    host.search_widget.set_results((_result(name="Kept"),))
    host.search_widget.set_status("kept")

    host.nav_search.click()
    host.nav_downloads.click()
    host.nav_search.click()

    assert host.search_widget is widget
    assert host.pages.currentWidget() is widget
    assert _rows_shown(host) == ["Kept"]
    assert _status(host) == "kept"


# --- Group C: the request reaches the service unchanged ----------------------


class _SpySearchService(SearchService):
    """The real service, plus a record of what start() saw when it was called."""

    def __init__(self, parent=None):
        super().__init__(parent, http_factory=_FakeHttp)
        self.starts = []
        self.ui_at_start = []
        self.widget = None

    def start(self, query, category=Category.ALL):
        self.starts.append((query, category))
        if self.widget is not None:
            self.ui_at_start.append(
                (
                    self.widget.table.rowCount(),
                    self.widget.status.text(),
                    self.widget.search_button.isEnabled(),
                )
            )
        return super().start(query, category)


def _spy_host(monkeypatch, sources=None):
    monkeypatch.setattr(mw, "SearchService", _SpySearchService)
    if sources is not None:
        _select(monkeypatch, sources)
    host = Host()
    _wire(host)
    host.search_service.widget = host.search_widget
    return host


def test_the_query_reaches_the_service_exactly_as_typed(monkeypatch):
    host = _spy_host(monkeypatch, [])
    host.search_widget.query.setText("  Dune  ")

    host.search_widget.search_button.click()

    assert host.search_service.starts == [("  Dune  ", Category.ALL)]


def test_the_chosen_category_reaches_the_service_as_the_enum_itself(monkeypatch):
    host = _spy_host(monkeypatch, [])
    host.search_widget.query.setText("dune")
    host.search_widget.category.setCurrentText("Anime")

    host.search_widget.search_button.click()

    assert host.search_service.starts == [("dune", Category.ANIME)]
    assert host.search_service.starts[0][1] is Category.ANIME


def test_the_category_forwarded_is_the_one_the_request_carried(monkeypatch):
    """The signal's argument, not the combo box read back.

    The two agree in the running application, which is exactly why asserting
    on a matching pair proves nothing: a window that re-derived the category
    from the visible label would pass this suite while ignoring what it was
    actually asked for. Here they disagree on purpose.
    """
    host = _spy_host(monkeypatch, [])
    assert host.search_widget.category.currentText() == "All"

    host.search_widget.search_requested.emit("dune", Category.TV)

    assert host.search_service.starts == [("dune", Category.TV)]
    assert host.search_service.starts[0][1] is Category.TV


def test_the_page_is_prepared_before_the_service_is_asked(monkeypatch):
    """Old rows gone, status set and the button locked, all before start()."""
    host = _spy_host(monkeypatch, [])
    host.search_widget.set_results((_result(name="Stale"),))
    host.search_widget.query.setText("dune")

    host.search_widget.search_button.click()

    assert host.search_service.ui_at_start == [(0, "Searching…", False)]


def test_one_request_asks_the_service_exactly_once(monkeypatch):
    host = _spy_host(monkeypatch, [])
    host.search_widget.query.setText("dune")

    host.search_widget.search_button.click()

    assert len(host.search_service.starts) == 1


# --- Group D: searches that finish inside start() ----------------------------


def test_a_whitespace_search_does_not_leave_the_page_searching(monkeypatch):
    host = _host(monkeypatch, [_FakeSource([_result()])])
    host.search_widget.query.setText("   ")

    host.search_widget.search_button.click()

    assert _searching(host) is False
    assert _rows_shown(host) == []
    assert _status(host) == "No results"
    assert host.search_service.active is False


def test_a_category_no_source_covers_does_not_leave_the_page_searching(monkeypatch):
    """GAMES has no built-in source, and the window must not hang on that."""
    host = _host(monkeypatch)
    host.search_widget.query.setText("dune")
    host.search_widget.category.setCurrentText("Games")

    host.search_widget.search_button.click()

    assert _searching(host) is False
    assert _rows_shown(host) == []
    assert _status(host) == "No results"
    assert host.search_service.active is False


def test_a_search_answered_entirely_from_cache_finishes_the_page(monkeypatch):
    """The whole lifecycle happens inside start(), so nothing may follow it."""
    source = _FakeSource([_result(name="Live")], source_id="alpha")
    host = _host(monkeypatch, [source])
    _seed_cache(
        host, "alpha", Category.ALL, "dune", (_result(name="Cached", source="alpha"),)
    )
    host.search_widget.query.setText("dune")

    host.search_widget.search_button.click()

    assert _rows_shown(host) == ["Cached"]
    assert _searching(host) is False
    assert _status(host) == "1 result"
    assert host.search_service.active is False
    assert host.search_service.generation == 1
    assert source.calls == [], "a cached source was asked anyway"


def test_a_cached_search_of_several_rows_reports_the_plural_count(monkeypatch):
    host = _host(monkeypatch, [_FakeSource([], source_id="alpha")])
    _seed_cache(
        host,
        "alpha",
        Category.ALL,
        "dune",
        (
            _result(info_hash=A, name="One", source="alpha", seeders=9),
            _result(info_hash=B, name="Two", source="alpha", seeders=2),
        ),
    )
    host.search_widget.query.setText("dune")

    host.search_widget.search_button.click()

    assert _rows_shown(host) == ["One", "Two"]
    assert _status(host) == "2 results"
    assert _searching(host) is False


# --- Group E: a search that really runs --------------------------------------


def test_a_live_search_shows_progress_and_then_its_results(monkeypatch):
    hold = threading.Event()
    source = _FakeSource([_result(name="Found")], source_id="alpha", hold=hold)
    host = _host(monkeypatch, [source])
    host.search_widget.query.setText("dune")

    try:
        host.search_widget.search_button.click()

        assert host.search_service.active is True
        assert _searching(host) is True
        assert _status(host) == "Searching…"
        assert _rows_shown(host) == []
    finally:
        hold.set()

    assert _pump(lambda: host.search_service.active is False), "search never finished"
    assert _rows_shown(host) == ["Found"]
    assert _searching(host) is False
    assert _status(host) == "1 result"


def test_a_finished_live_search_paints_the_summary_rows(monkeypatch):
    source = _FakeSource(
        [_result(info_hash=A, name="One", source="alpha", seeders=9)],
        source_id="alpha",
    )
    host = _host(monkeypatch, [source])
    host.search_widget.query.setText("dune")

    host.search_widget.search_button.click()
    assert _pump(lambda: host.search_service.active is False)

    assert _rows_shown(host) == ["One"]
    assert source.calls == [("dune", Category.ALL)]


def test_a_peer_still_running_keeps_the_page_searching(monkeypatch):
    """One source lands while another is held: partial rows, still searching."""
    hold = threading.Event()
    quick = _FakeSource([_result(name="Quick", source="alpha")], source_id="alpha")
    slow = _FakeSource(
        [_result(info_hash=B, name="Slow", source="beta")], source_id="beta", hold=hold
    )
    host = _host(monkeypatch, [quick, slow])
    host.search_widget.query.setText("dune")

    try:
        host.search_widget.search_button.click()
        assert _pump(lambda: _rows_shown(host) == ["Quick"]), "partial rows never shown"
        assert _searching(host) is True
        assert host.search_service.active is True
    finally:
        hold.set()

    assert _pump(lambda: host.search_service.active is False)
    assert sorted(_rows_shown(host)) == ["Quick", "Slow"]
    assert _searching(host) is False
    assert _status(host) == "2 results"


# --- Group F: generation guards ----------------------------------------------


def test_a_stale_result_batch_cannot_repaint_the_page(monkeypatch):
    host = _host(monkeypatch, [])
    host.search_widget.set_results((_result(name="Current"),))
    host.search_widget.set_status("current")
    host.search_widget.set_searching(True)
    host.search_service._generation = 2

    host._on_search_results(1, (_result(info_hash=B, name="Old"),))

    assert _rows_shown(host) == ["Current"]
    assert _status(host) == "current"
    assert _searching(host) is True


def test_a_current_result_batch_paints_the_page(monkeypatch):
    host = _host(monkeypatch, [])
    host.search_service._generation = 2

    host._on_search_results(2, (_result(name="Fresh"),))

    assert _rows_shown(host) == ["Fresh"]


def test_a_stale_summary_cannot_finish_the_current_search(monkeypatch):
    host = _host(monkeypatch, [])
    host.search_widget.set_results((_result(name="Current"),))
    host.search_widget.set_status("current")
    host.search_widget.set_searching(True)
    host.search_service._generation = 2

    host._on_search_finished(
        SearchSummary(generation=1, results=(), dedupe_dropped=0, failures=())
    )

    assert _rows_shown(host) == ["Current"]
    assert _status(host) == "current"
    assert _searching(host) is True


def test_a_current_summary_finishes_the_page(monkeypatch):
    host = _host(monkeypatch, [])
    host.search_widget.set_searching(True)
    host.search_service._generation = 2

    host._on_search_finished(
        SearchSummary(
            generation=2,
            results=(_result(name="Final"),),
            dedupe_dropped=0,
            failures=(),
        )
    )

    assert _rows_shown(host) == ["Final"]
    assert _searching(host) is False
    assert _status(host) == "1 result"


# --- Group G: a newer search owns the page -----------------------------------


def test_a_newer_search_owns_the_page_and_the_older_one_cannot_take_it_back(
    monkeypatch,
):
    hold = threading.Event()
    old = _FakeSource([_result(name="Old", source="alpha")], source_id="alpha", hold=hold)
    new = _FakeSource(
        [_result(info_hash=B, name="New", source="alpha")], source_id="alpha"
    )
    selected = [old]
    _offline_service(monkeypatch)
    monkeypatch.setattr(
        search_service, "sources_for", lambda category=Category.ALL: list(selected)
    )
    host = Host()
    _wire(host)

    try:
        host.search_widget.query.setText("old")
        host.search_widget.search_button.click()
        assert host.search_service.generation == 1

        selected[:] = [new]
        host.search_widget.query.setText("new")
        # The button is locked while a search runs, so the second search comes
        # in the way a superseding one can: through the same signal.
        host.search_widget.search_requested.emit("new", Category.ALL)
        assert host.search_service.generation == 2

        assert _pump(lambda: host.search_service.active is False), "never finished"
        assert _rows_shown(host) == ["New"]
        assert _searching(host) is False
        assert _status(host) == "1 result"
    finally:
        hold.set()
        # The superseded worker is still on the pool; let it drain before the
        # test ends so its outcome cannot land during another test.
        _pump(lambda: False, seconds=0.2)

    assert _rows_shown(host) == ["New"], "the old search repainted the new page"
    assert host.search_service.generation == 2


# --- Group H: sources that could not answer ----------------------------------


def test_a_failing_source_is_reported_as_one_source_issue(monkeypatch):
    source = _FakeSource(
        raises=SourceError(SourceErrorKind.NETWORK, "down"), source_id="alpha"
    )
    host = _host(monkeypatch, [source])
    host.search_widget.query.setText("dune")

    host.search_widget.search_button.click()
    assert _pump(lambda: host.search_service.active is False)

    assert _rows_shown(host) == []
    assert _status(host) == "No results · 1 source issue"
    assert _searching(host) is False


def test_two_failing_sources_are_reported_in_the_plural(monkeypatch):
    host = _host(monkeypatch, [])
    host.search_service._generation = 1

    host._on_search_finished(
        SearchSummary(
            generation=1,
            results=(),
            dedupe_dropped=0,
            failures=(
                SourceFailure("alpha", "unavailable"),
                SourceFailure("beta", "timeout"),
            ),
        )
    )

    assert _status(host) == "No results · 2 source issues"


def test_the_rows_a_working_source_found_survive_a_failing_peer(monkeypatch):
    good = _FakeSource([_result(name="Found", source="alpha")], source_id="alpha")
    bad = _FakeSource(
        raises=SourceError(SourceErrorKind.PARSE, "bad"), source_id="beta"
    )
    host = _host(monkeypatch, [good, bad])
    host.search_widget.query.setText("dune")

    host.search_widget.search_button.click()
    assert _pump(lambda: host.search_service.active is False)

    assert _rows_shown(host) == ["Found"]
    assert _status(host) == "1 result · 1 source issue"
    assert _searching(host) is False


# --- Group I: what this slice deliberately does not do -----------------------


def test_the_search_page_never_hands_a_result_to_intake(monkeypatch):
    """Downloading a result is the next slice: nothing here may start one."""
    taken = []
    host = _host(monkeypatch, [_FakeSource([_result(name="Found", source="alpha")], source_id="alpha")])
    monkeypatch.setattr(host, "add_search_result", lambda result: taken.append(result))
    monkeypatch.setattr(
        host, "add_urls_checked", lambda *a, **k: taken.append(a), raising=False
    )
    host.search_widget.query.setText("dune")

    host.search_widget.search_button.click()
    assert _pump(lambda: host.search_service.active is False)
    host.search_widget.table.selectRow(0)
    host.search_widget.table.cellDoubleClicked.emit(0, 0)

    assert taken == []


def test_one_service_event_produces_one_page_update(monkeypatch):
    """Proves the three signals are each connected exactly once."""
    host = _host(monkeypatch, [])
    seen = []
    original = host.search_widget.set_results
    monkeypatch.setattr(
        host.search_widget,
        "set_results",
        lambda results: (seen.append(tuple(results)), original(results))[1],
    )
    host.search_service._generation = 3

    host.search_service.results_updated.emit(3, (_result(name="Once"),))

    assert len(seen) == 1


@pytest.mark.parametrize(
    "results, failures, expected",
    [
        (0, 0, "No results"),
        (1, 0, "1 result"),
        (12, 0, "12 results"),
        (12, 1, "12 results · 1 source issue"),
        (12, 2, "12 results · 2 source issues"),
        (0, 2, "No results · 2 source issues"),
    ],
)
def test_the_final_status_counts_come_from_the_summary(results, failures, expected):
    summary = SearchSummary(
        generation=1,
        results=tuple(
            # From 1: an all-zero info hash is not a usable one.
            _result(info_hash=f"{i + 1:040x}", name=f"r{i}") for i in range(results)
        ),
        dedupe_dropped=0,
        failures=tuple(SourceFailure(f"s{i}", "unavailable") for i in range(failures)),
    )

    assert mw._search_status_text(summary) == expected
