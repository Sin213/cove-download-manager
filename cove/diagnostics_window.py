"""The Diagnostics window ("Logs").

A read-only support view over records the logger has already sanitized, plus
a sanitized tail of the native messaging host's own file. It never reads raw
data and it never writes to the diagnostics log itself.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import diagnostics

MAX_VISIBLE = 500
ALL = "All"

_WINDOW = None


def _open_folder(path):
    """Open a directory with the same helper the rest of the app uses.

    Imported lazily: main_window imports this module, so a module-level
    import would be circular.
    """
    try:
        from .main_window import _open_path

        return _open_path(Path(path))
    except Exception:
        return False


class DiagnosticsWindow(QWidget):
    def __init__(self, logger, bridge=None, host_log_dir=None, parent=None):
        super().__init__(parent)
        self._logger = logger
        self._bridge = bridge
        self._host_log_dir = Path(host_log_dir) if host_log_dir else None
        self._host_records = []
        self.skipped_host_records = 0

        self.setWindowTitle("Cove diagnostics")
        self.setMinimumSize(820, 520)
        self._build()
        self._load_host_records()
        self.reload()

        if bridge is not None:
            bridge.record_added.connect(self._on_record)

    # -- construction ------------------------------------------------------

    def _build(self):
        outer = QVBoxLayout(self)

        self.header_label = QLabel(self._header_text())
        self.header_label.setWordWrap(True)
        outer.addWidget(self.header_label)

        filters = QHBoxLayout()
        self.level_combo = QComboBox()
        self.level_combo.addItem(ALL)
        for level in diagnostics.LEVELS:
            self.level_combo.addItem(level)
        self.level_combo.currentIndexChanged.connect(lambda _=0: self.reload())
        filters.addWidget(QLabel("Level"))
        filters.addWidget(self.level_combo)

        self.component_combo = QComboBox()
        self.component_combo.addItem(ALL)
        for component in diagnostics.COMPONENTS:
            self.component_combo.addItem(component)
        self.component_combo.currentIndexChanged.connect(lambda _=0: self.reload())
        filters.addWidget(QLabel("Component"))
        filters.addWidget(self.component_combo)

        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("Task or request id")
        self.id_edit.textChanged.connect(lambda _="": self.reload())
        filters.addWidget(QLabel("Id"))
        filters.addWidget(self.id_edit)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search")
        self.search_edit.textChanged.connect(lambda _="": self.reload())
        filters.addWidget(QLabel("Search"))
        filters.addWidget(self.search_edit, 1)
        outer.addLayout(filters)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        font = QFont("monospace")
        font.setStyleHint(QFont.Monospace)
        self.view.setFont(font)
        self.view.setMaximumBlockCount(MAX_VISIBLE + 32)
        outer.addWidget(self.view, 1)

        actions = QHBoxLayout()
        self.copy_btn = QPushButton("Copy diagnostics")
        self.copy_btn.clicked.connect(self.copy_diagnostics)
        actions.addWidget(self.copy_btn)

        self.save_btn = QPushButton("Save diagnostics")
        self.save_btn.clicked.connect(self.save_diagnostics)
        actions.addWidget(self.save_btn)

        self.folder_btn = QPushButton("Open log folder")
        self.folder_btn.clicked.connect(self.open_log_folder)
        actions.addWidget(self.folder_btn)

        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setToolTip(
            "Clears the view. Log files already written to disk are kept."
        )
        self.clear_btn.clicked.connect(self.clear_view)
        actions.addWidget(self.clear_btn)

        actions.addStretch(1)

        self.debug_check = QCheckBox("Enable debug logging until restart")
        self.debug_check.setChecked(False)
        self.debug_check.toggled.connect(self._on_debug_toggled)
        actions.addWidget(self.debug_check)

        self.debug_status_label = QLabel("Debug logging: off")
        actions.addWidget(self.debug_status_label)
        outer.addLayout(actions)

        self.notice_label = QLabel(diagnostics.SANITIZATION_NOTICE)
        self.notice_label.setWordWrap(True)
        outer.addWidget(self.notice_label)

    def _header_text(self):
        facts = diagnostics.environment_facts()
        session = getattr(self._logger, "session", "unknown")
        return (
            "app version: {}  |  os: {} {}  |  arch: {}  |  install mode: {}"
            "  |  session: {}".format(
                facts["app_version"], facts["os"], facts["os_version"],
                facts["arch"], facts["mode"], session,
            )
        )

    # -- data --------------------------------------------------------------

    def _load_host_records(self):
        self._host_records = []
        self.skipped_host_records = 0
        if self._host_log_dir is None:
            return
        raw, skipped = diagnostics.read_log_tail(
            self._host_log_dir, diagnostics.NATIVE_LOG_NAME, limit=MAX_VISIBLE
        )
        self.skipped_host_records = skipped
        for record in raw:
            clean = diagnostics.sanitize_record(record)
            if clean is not None:
                clean["_source"] = "host"
                self._host_records.append(clean)

    def _all_records(self):
        records = []
        try:
            for record in self._logger.records() if self._logger else []:
                record["_source"] = "app"
                records.append(record)
        except Exception:
            pass
        records.extend(self._host_records)
        records.sort(key=lambda r: str(r.get("ts", "")))
        return records[-MAX_VISIBLE:]

    def visible_records(self):
        return [r for r in self._all_records() if self._matches(r)]

    def _matches(self, record):
        level = self.level_combo.currentText()
        if level != ALL:
            rank = diagnostics.LEVEL_RANK.get(record.get("level"), 0)
            if rank < diagnostics.LEVEL_RANK.get(level, 0):
                return False

        component = self.component_combo.currentText()
        if component != ALL:
            actual = str(record.get("component", ""))
            if actual != component and not actual.startswith(component + "."):
                return False

        wanted_id = self.id_edit.text().strip()
        if wanted_id:
            task = record.get("task")
            request = record.get("request") or ""
            if str(task) != wanted_id and request != wanted_id:
                return False

        search = self.search_edit.text().strip().lower()
        if search and search not in self._render(record).lower():
            return False
        return True

    def _render(self, record):
        return diagnostics.format_record(record, source=record.get("_source"))

    # -- refresh -----------------------------------------------------------

    def reload(self):
        try:
            bar = self.view.verticalScrollBar()
            previous = bar.value()
            # Only follow the tail when the reader is already there. Yanking
            # someone away from an older entry they are reading is worse than
            # missing the newest line.
            at_bottom = previous >= bar.maximum() - 2
            body = "\n".join(self._render(r) for r in self.visible_records())
            self.view.setPlainText(body)
            bar.setValue(bar.maximum() if at_bottom else min(previous, bar.maximum()))
        except Exception:
            pass

    def _on_record(self, _record):
        self.reload()

    def _on_debug_toggled(self, enabled):
        try:
            if self._logger is not None:
                self._logger.set_debug(bool(enabled))
        except Exception:
            pass
        self.debug_status_label.setText(
            "Debug logging: on" if enabled else "Debug logging: off"
        )

    # -- filters (programmatic) -------------------------------------------

    def _select(self, combo, value):
        index = combo.findText(value)
        if index < 0:
            combo.addItem(value)
            index = combo.findText(value)
        combo.setCurrentIndex(index)

    def set_level_filter(self, level):
        self._select(self.level_combo, level)

    def set_component_filter(self, component):
        self._select(self.component_combo, component)

    def set_id_filter(self, value):
        self.id_edit.setText("" if value is None else str(value))

    def set_search(self, value):
        self.search_edit.setText("" if value is None else str(value))

    def _filters_description(self):
        return "level={} component={} id={} search={}".format(
            self.level_combo.currentText(),
            self.component_combo.currentText(),
            self.id_edit.text().strip() or "none",
            self.search_edit.text().strip() or "none",
        )

    # -- support actions ---------------------------------------------------

    def report_text(self):
        header = diagnostics.support_header(
            session=getattr(self._logger, "session", "unknown"),
            filters=self._filters_description(),
        )
        body = "\n".join(self._render(r) for r in self.visible_records())
        return header + "\n\n" + body + "\n"

    def copy_diagnostics(self):
        try:
            QApplication.clipboard().setText(self.report_text())
            return True
        except Exception:
            return False

    def save_to(self, path):
        try:
            Path(path).write_text(self.report_text(), encoding="utf-8")
            return True
        except Exception:
            return False

    def save_diagnostics(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save diagnostics", "cove-diagnostics.txt", "Text files (*.txt)"
        )
        if not path:
            return False
        return self.save_to(path)

    def open_log_folder(self):
        directory = getattr(self._logger, "log_dir", None) or self._host_log_dir
        if directory is None:
            return False
        return _open_folder(directory)

    def clear_view(self):
        """Clear what is on screen. Files on disk are deliberately untouched."""
        try:
            if self._logger is not None:
                self._logger.clear()
        except Exception:
            pass
        self._host_records = []
        self.reload()

    def closeEvent(self, event):
        global _WINDOW
        if _WINDOW is self:
            _WINDOW = None
        if self._bridge is not None:
            try:
                self._bridge.record_added.disconnect(self._on_record)
            except Exception:
                pass
            try:
                self._bridge.close()
            except Exception:
                pass
            self._bridge = None
        super().closeEvent(event)


def show_diagnostics(parent=None, logger=None, bridge=None, host_log_dir=None,
                     task_id=None):
    """Open (or re-raise) the single Diagnostics window."""
    global _WINDOW
    log = logger if logger is not None else diagnostics.get_logger()
    if _WINDOW is None:
        directory = host_log_dir or getattr(log, "log_dir", None)
        if bridge is None and log is not None:
            # Live updates while the window is open. The bridge observes the
            # logger; it never persists anything of its own.
            from .diagnostics_bridge import DiagnosticsBridge

            bridge = DiagnosticsBridge(log)
        _WINDOW = DiagnosticsWindow(log, bridge=bridge, host_log_dir=directory,
                                    parent=parent)
    if task_id is not None:
        _WINDOW.set_id_filter(task_id)
    _WINDOW.show()
    _WINDOW.raise_()
    _WINDOW.activateWindow()
    return _WINDOW


def close_diagnostics():
    global _WINDOW
    window = _WINDOW
    _WINDOW = None
    if window is not None:
        try:
            window.close()
        except Exception:
            pass
