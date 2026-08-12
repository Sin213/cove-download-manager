"""The Search page's Qt widget.

Presentation only. The widget renders whatever results it is handed, in the
order it is handed them, and reports one thing back: that the user asked for a
search. It never calls a source, never owns a SearchService and never starts a
download - ranking, normalisation, deadlines and intake all live behind it, so
this file imports nothing from Cove but the Search data model.
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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

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

        outer = QVBoxLayout(self)
        outer.addLayout(controls)
        outer.addWidget(self.status)
        outer.addWidget(self.table, 1)

        # One path for both gestures, so they cannot drift apart.
        self.search_button.clicked.connect(self._emit_search_requested)
        self.query.returnPressed.connect(self._emit_search_requested)

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

    def set_results(self, results) -> None:
        """Render exactly these results, in exactly this order.

        A full replacement, matching the complete snapshots SearchService
        emits: no merging, no dedupe, no filtering and no re-ranking. Any
        earlier selection goes with the rows it belonged to.
        """
        self.table.clearContents()
        self.table.setRowCount(0)
        for row, result in enumerate(results):
            self.table.insertRow(row)
            cells = (
                result.name,
                _human_size(result.size_bytes),
                _human_count(result.seeders),
                _human_count(result.leechers),
                _human_added(result.added),
                result.source,
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
