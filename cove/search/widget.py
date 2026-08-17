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
        # Left off deliberately: SearchService already ranked the results, and
        # re-sorting here would silently override that ranking.
        self.table.setSortingEnabled(False)

        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        for col in range(len(COLUMNS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COLUMNS.index("Name"), QHeaderView.Stretch)

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

    def set_results(self, results) -> None:
        """Render exactly these results, in exactly this order.

        A full replacement, matching the complete snapshots SearchService
        emits: no merging, no dedupe, no filtering and no re-ranking. Any
        earlier selection goes with the rows it belonged to.

        The Source cell is the one display-only derivation: a result whose
        source is a configured custom indexer shows that indexer's current
        name; anything else (built-ins, unknown or removed custom ids, future
        sources) shows the raw source string. The result object itself is
        never touched.
        """
        self.table.clearContents()
        self.table.setRowCount(0)
        source_names = self._current_source_names()
        for row, result in enumerate(results):
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
