"""The Search page's Qt widget.

Presentation only. The widget renders whatever results it is handed, in the
order it is handed them, and reports two things back: that the user asked for a
search, and that the user asked to download one result. It never calls a
source, never owns a SearchService and never performs a download - ranking,
normalisation, deadlines and intake all live behind it, so this file imports
nothing from Cove but the Search data model.
"""
from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cove.search.models import Category, SearchResult

#: Visible column order. Deliberately excludes info hash, magnet and trackers:
#: the row keeps the whole SearchResult, so nothing needs to be displayed to be
#: recoverable later.
COLUMNS = ("Name", "Size", "Seeders", "Leechers", "Added", "Source")

#: Label -> Category. The visible order is fixed here rather than derived from
#: the enum so that reordering the enum cannot silently reorder the UI.
CATEGORY_CHOICES = (
    ("All", Category.ALL),
    ("Games", Category.GAMES),
    ("Movies", Category.MOVIES),
    ("TV", Category.TV),
    ("Anime", Category.ANIME),
)

#: Shown wherever a source did not report a value. Sources are allowed to omit
#: size and date, so this is a normal row, not an error.
UNKNOWN = "—"

#: Column -> the SearchResult attribute it orders by. Sorting compares these
#: underlying values, never the formatted cell text: "1.2 GB" sorts before
#: "20 MB" as a string, and two uploads on one day share a rendered date.
#: Name is absent on purpose - it is compared casefolded, and it is also the
#: tie-break every other column falls back to. Source is absent because it is
#: deliberately not sortable.
SORT_FIELDS = {
    COLUMNS.index("Size"): "size_bytes",
    COLUMNS.index("Seeders"): "seeders",
    COLUMNS.index("Leechers"): "leechers",
    COLUMNS.index("Added"): "added",
}

#: Column -> the direction the *first* explicit click on it applies. Sizes read
#: naturally smallest-first; swarm counts and dates are only interesting from
#: the top, so those start descending. A second click on the same column
#: reverses whatever this gave.
FIRST_DESCENDING = {
    COLUMNS.index("Name"): False,
    COLUMNS.index("Size"): False,
    COLUMNS.index("Seeders"): True,
    COLUMNS.index("Leechers"): True,
    COLUMNS.index("Added"): True,
}

#: The order a fresh set of results is shown in, before the user sorts.
DEFAULT_SORT_COLUMN = COLUMNS.index("Seeders")

_UNITS = ("B", "KB", "MB", "GB", "TB")


def _human_size(size_bytes: int | None) -> str:
    """Display-only byte formatting. Mirrors the main window's units without
    importing it - the widget must stay free of application dependencies.

    Any value the model accepts must render: the sizes come from untrusted
    indexers, and one absurd number must not abort a whole refresh, so an
    unrepresentable value degrades to the unknown placeholder.
    """
    if size_bytes is None:
        return UNKNOWN
    try:
        f = float(size_bytes)
    except OverflowError:
        return UNKNOWN
    for unit in _UNITS:
        if f < 1024:
            return f"{int(f)} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} PB"


def _human_count(count: int) -> str:
    """A swarm count as a plain decimal - no `1.2K` abbreviation.

    Guarded for the same reason as the other formatters: the model accepts any
    non-negative int, and Python refuses to stringify one past its digit
    limit, which would otherwise abort the refresh mid-table.
    """
    try:
        return str(count)
    except ValueError:
        return UNKNOWN


def _human_added(added: int | None) -> str:
    """A source's upload timestamp as a fixed UTC date.

    UTC and date-only on purpose: the value is a Unix timestamp, and anything
    relative ("3 days ago") would need a clock and a timer to stay true.

    A timestamp outside the representable range is an indexer's problem, not
    a reason to abandon the refresh: it degrades to the unknown placeholder.
    """
    if added is None:
        return UNKNOWN
    try:
        stamp = datetime.fromtimestamp(added, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return UNKNOWN
    return stamp.strftime("%Y-%m-%d")


#: Sort field -> the formatter that decides whether its cell can be rendered.
#: Consulted as a validity test only; the ordering itself always compares the
#: underlying value, never formatter output.
_UNKNOWN_RENDERERS = {
    "size_bytes": _human_size,
    "seeders": _human_count,
    "leechers": _human_count,
    "added": _human_added,
}


def _sorts_last(field: str, value) -> bool:
    """True when a value has no place on the ordered scale.

    None is the ordinary case: the source did not report it. The other case is
    a value the model accepted but the formatters refuse - an absurd size,
    swarm count or timestamp from an untrusted indexer - which renders as the
    unknown placeholder just as a missing one does. Sorting has to agree with
    what the row actually shows: a cell reading "—" that still carried its raw
    number would outrank every real value in a descending sort.
    """
    if value is None:
        return True
    return _UNKNOWN_RENDERERS[field](value) == UNKNOWN


class SearchWidget(QWidget):
    """Query controls, a status line and a read-only result table."""

    #: The user asked for a search: (raw query text, chosen Category). Emitted
    #: only by the button and by Return in the query field - never by editing.
    search_requested = Signal(str, object)

    #: The user asked to download one result: the SearchResult object itself,
    #: exactly as the source produced it. Never a magnet, a hash or a row
    #: index - what to do with the result is the window's business, and
    #: anything less than the whole object would have to be rebuilt there.
    download_requested = Signal(object)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        custom_source_names=None,
    ) -> None:
        """Build the widget.

        ``custom_source_names`` is an optional callable returning the current
        ``{custom_id: display_name}`` mapping for custom Torznab indexers. It
        is consulted once per result-table refresh, and only for the Source
        column: results keep their stable ``custom:<uuid>`` identity, and the
        label is derived at render time so a name-only edit is visible on the
        next refresh without touching SearchService or the cache. A callable
        that fails, or returns anything but a mapping, degrades to raw source
        strings - a display nicety must never abort a refresh.
        """
        super().__init__(parent)
        self._custom_source_names = custom_source_names

        #: The current results in the order the caller supplied them. A render
        #: and sort buffer only: every sort starts from this order, so repeated
        #: clicks are deterministic rather than cumulative. It is never a
        #: row -> download lookup - see set_results.
        self._results: tuple = ()
        self._reset_sort()

        self.query = QLineEdit(self)
        self.query.setPlaceholderText("Search torrents…")

        self.category = QComboBox(self)
        for label, value in CATEGORY_CHOICES:
            self.category.addItem(label, value)

        self.search_button = QPushButton("Search", self)

        controls = QHBoxLayout()
        controls.addWidget(self.query, 1)
        controls.addWidget(self.category)
        controls.addWidget(self.search_button)

        self.status = QLabel("", self)

        self.table = QTableWidget(0, len(COLUMNS), self)
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        # Left off deliberately. Native sorting would compare the formatted
        # cell text and would reorder rows while they are being filled in,
        # which can hand a row the wrong result. The widget sorts the
        # SearchResult objects itself instead and repopulates from that order.
        self.table.setSortingEnabled(False)

        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        for col in range(len(COLUMNS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COLUMNS.index("Name"), QHeaderView.Stretch)
        # The indicator is ours to place: with native sorting off nothing else
        # moves it, and the widget always has a defined sort state to show.
        header.setSortIndicatorShown(True)
        self._show_sort_indicator()
        header.sectionClicked.connect(self._sort_by_column)

        self.download_button = QPushButton("Download", self)
        # Nothing is selected yet, so there is nothing to download. Selection
        # is the only thing that ever lifts this.
        self.download_button.setEnabled(False)

        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(self.download_button)

        outer = QVBoxLayout(self)
        outer.addLayout(controls)
        outer.addWidget(self.status)
        outer.addWidget(self.table, 1)
        outer.addLayout(actions)

        # One path for both gestures, so they cannot drift apart.
        self.search_button.clicked.connect(self._emit_search_requested)
        self.query.returnPressed.connect(self._emit_search_requested)

        # Selection only ever changes what the button offers; it never asks
        # for a download of its own accord.
        self.table.itemSelectionChanged.connect(self._sync_download_enabled)
        self.table.cellDoubleClicked.connect(self._request_download_for_row)
        self.download_button.clicked.connect(self._request_download)

    def current_category(self) -> Category:
        """The Category the user selected, as the enum member itself."""
        return self.category.currentData()

    def set_status(self, text: str) -> None:
        """Show a line of status. Rendering only - the caller decides wording."""
        self.status.setText(text)

    def set_searching(self, searching: bool) -> None:
        """Render the 'a search is running' state.

        Only the button is locked: the user stays free to type the next query
        and change the category while results arrive.
        """
        self.search_button.setEnabled(not searching)

    def _current_source_names(self) -> dict:
        """The custom id -> display-name mapping for this refresh, or {}.

        Queried once per set_results call, so every row in one batch agrees
        and a later refresh re-queries. The widget only ever handles a plain
        id -> name mapping - never Settings, records, URLs or API keys - and
        a broken provider degrades to raw source strings rather than
        aborting the refresh.
        """
        provider = self._custom_source_names
        if provider is None:
            return {}
        try:
            names = provider()
        except Exception:
            return {}
        if not isinstance(names, dict):
            return {}
        return names

    def _reset_sort(self) -> None:
        """Return to the order a new search starts in: seeders, high to low."""
        self._sort_column = DEFAULT_SORT_COLUMN
        self._sort_descending = True
        # True until the user clicks a header. While it holds, the next click
        # applies that column's first direction rather than reversing the
        # default - so the first Seeders click confirms descending instead of
        # flipping to ascending.
        self._sort_is_default = True

    def _show_sort_indicator(self) -> None:
        order = Qt.DescendingOrder if self._sort_descending else Qt.AscendingOrder
        self.table.horizontalHeader().setSortIndicator(self._sort_column, order)

    def _sorted_results(self) -> list:
        """The current results in the current sort order.

        Always derived from the supplied order, so the result is a function of
        the sort state alone. Two deliberate properties:

        - equal primary values keep Name A->Z whichever way the primary field
          points, because the alphabetical pass runs first and Python's sort
          is stable - reversing the primary key does not reverse rows it
          considers equal;
        - a value the source did not report is appended after the ordered
          rows rather than given a stand-in number, so it is last ascending
          and last descending both.
        """
        results = list(self._results)
        results.sort(key=lambda result: result.name.casefold())
        field = SORT_FIELDS.get(self._sort_column)
        if field is None:
            # Name: the alphabetical pass is the sort. Reversing it leaves
            # equal-casefolded rows in their supplied order.
            if self._sort_descending:
                results.sort(key=lambda result: result.name.casefold(), reverse=True)
            return results
        known = [r for r in results if not _sorts_last(field, getattr(r, field))]
        unknown = [r for r in results if _sorts_last(field, getattr(r, field))]
        known.sort(key=lambda result: getattr(result, field), reverse=self._sort_descending)
        return known + unknown

    def _sort_by_column(self, column: int) -> None:
        """A clicked header section. Reorders the results already on screen.

        Widget-local and synchronous: no source, no service, no cache and no
        new snapshot. Membership is whatever the last set_results supplied.
        """
        if self.table.rowCount() == 0:
            # Nothing to order, so nothing to record. This also keeps the one
            # reset boundary honest: SearchService republishes an empty merged
            # view every time a source returns no rows, and until the first
            # rows arrive that is indistinguishable from a new search. A sort
            # chosen against an empty table could therefore be dropped by the
            # next empty update - so no sort may be chosen there.
            self._show_sort_indicator()
            return
        if column not in FIRST_DESCENDING:
            # Source is not sortable. Qt's own header flips the indicator on
            # a real mouse release before this runs, so put it back rather
            # than leaving it pointing at a column that sorts nothing.
            self._show_sort_indicator()
            return
        if self._sort_is_default or column != self._sort_column:
            descending = FIRST_DESCENDING[column]
        else:
            descending = not self._sort_descending
        self._sort_column = column
        self._sort_descending = descending
        self._sort_is_default = False
        # The user is looking at a row they picked; it must stay picked even
        # though its row number changed underneath them.
        self._render(keep_selection=True)

    def set_results(self, results) -> None:
        """Render exactly these results, sorted by the current sort state.

        A full replacement, matching the complete snapshots SearchService
        emits: no merging, no dedupe, no filtering and no re-ranking. Only the
        display order is the widget's: membership, ranking and the cap remain
        the service's. Any earlier selection goes with the rows it belonged to.

        An empty snapshot is the start of a new search, and is the one place
        the sort state resets to the default. A later non-empty snapshot for
        the same search keeps whatever the user chose, so a sort does not come
        undone every time another source answers.

        The Source cell is the one display-only derivation: a result whose
        source is a configured custom indexer shows that indexer's current
        name; anything else (built-ins, unknown or removed custom ids, future
        sources) shows the raw source string. The result object itself is
        never touched.
        """
        self._results = tuple(results)
        if not self._results:
            self._reset_sort()
        self._render(keep_selection=False)

    def _render(self, *, keep_selection: bool) -> None:
        """Repopulate the table from the sorted results.

        Native sorting stays off throughout, so no row moves while its cells
        are being filled in and no cell can end up on another result's row.
        """
        keep = self.selected_result() if keep_selection else None
        keep_hash = keep.info_hash if keep is not None else None
        self.table.clearContents()
        self.table.setRowCount(0)
        source_names = self._current_source_names()
        for row, result in enumerate(self._sorted_results()):
            self.table.insertRow(row)
            cells = (
                result.name,
                _human_size(result.size_bytes),
                _human_count(result.seeders),
                _human_count(result.leechers),
                _human_added(result.added),
                source_names.get(result.source, result.source),
            )
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                # Belt and braces with NoEditTriggers: the cells are a view of
                # a validated result, never an input.
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(row, col, item)
            # The whole result rides along on the first cell, so the download
            # action never has to rebuild one from the displayed strings.
            self.table.item(row, 0).setData(Qt.UserRole, result)
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        # Nothing re-enables the download action here, and nothing has to
        # disable it either: the rows the old selection belonged to are gone,
        # and dropping that selection takes the action away with them.
        self._show_sort_indicator()
        if keep_hash is not None:
            self._select_by_info_hash(keep_hash)

    def _select_by_info_hash(self, info_hash: str) -> None:
        """Re-select the row holding this torrent, if it is still on screen.

        Identity is the info hash rather than the row number: the row number
        is exactly what a sort changes, and the hash is unique across one
        result set because SearchService dedupes on it.
        """
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            result = item.data(Qt.UserRole) if item is not None else None
            if result is not None and result.info_hash == info_hash:
                self.table.selectRow(row)
                return

    def selected_result(self) -> SearchResult | None:
        """The SearchResult object behind the selected row, or None."""
        rows = self.table.selectionModel().selectedRows()
        if len(rows) != 1:
            return None
        item = self.table.item(rows[0].row(), 0)
        return item.data(Qt.UserRole) if item is not None else None

    def _emit_search_requested(self) -> None:
        # text() verbatim: trimming and casefolding are SearchService's job.
        self.search_requested.emit(self.query.text(), self.current_category())

    def _sync_download_enabled(self) -> None:
        """Offer the download action exactly when a result is selected.

        Deliberately not tied to whether a search is running: SearchService
        publishes partial snapshots, and a result already on screen is worth
        downloading while the remaining sources are still answering.
        """
        self.download_button.setEnabled(self.selected_result() is not None)

    def _request_download(self) -> None:
        """The Download button. Reports intent for the selected result."""
        result = self.selected_result()
        # The button is disabled without a selection, so this is belt and
        # braces - but a request carrying nothing is worse than no request.
        if result is None:
            return
        self.download_requested.emit(result)

    def _request_download_for_row(self, row: int, _column: int) -> None:
        """A double-clicked row. The clicked row is the authoritative one.

        Read from the row Qt reports rather than from the current selection:
        the two agree in practice, and relying on that would make the gesture
        depend on selection ordering the widget does not control.
        """
        item = self.table.item(row, 0)
        if item is None:
            return
        result = item.data(Qt.UserRole)
        if result is None:
            return
        self.download_requested.emit(result)
