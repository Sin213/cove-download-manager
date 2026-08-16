"""The Add/Edit dialog for one custom Torznab indexer, plus its async probe.

S6 slice. One small editor surfaced from the Settings "Custom Torznab indexers"
section. Validation delegates to the committed S2 model via a
:func:`~cove.search.indexers.parse_custom_indexers` round-trip, and the Test
Connection button reuses the committed S3 :class:`TorznabSource` caps path (and
therefore the S4 endpoint policy) off the GUI thread, without persisting
anything.
"""
from __future__ import annotations

import dataclasses

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from cove.search.indexers import CustomTorznabIndexer, parse_custom_indexers
from cove.search.models import SourceError
from cove.search.sources.torznab import TorznabSource

# Pinned for the same reason dialogs pins account tests: a runnable still in
# flight survives the editor closing, and the pool must not reap a runnable
# whose signal carrier the C++ side still references.
_INFLIGHT_INDEXER_PROBES: set = set()


class _IndexerProbe(QRunnable):
    """Run one Torznab caps probe off the GUI thread.

    Same shape as dialogs._AccountTest: autoDelete(False), a signal carrier that
    outlives the dialog, and bound-method connections that Qt drops when the
    editor is destroyed mid-flight.
    """

    class _Sig(QObject):
        done = Signal(object)    # parsed TorznabCaps
        failed = Signal(str)     # displayable, secret-safe message
        finished = Signal()

    def __init__(self, fn):
        super().__init__()
        self.setAutoDelete(False)
        self.signals = self._Sig()
        self._fn = fn

    def run(self):
        try:
            caps = self._fn()
        except SourceError as error:
            # The source already sanitizes its outward messages (no
            # secret-bearing URL, no raw response).
            self.signals.failed.emit(str(error))
        except Exception:
            # Never surface a raw exception: it may quote the request, and the
            # request carries the API key.
            self.signals.failed.emit("The connection test could not be completed.")
        else:
            self.signals.done.emit(caps)
        self.signals.finished.emit()


class IndexerEditorDialog(QDialog):
    """Add or edit one custom indexer; expose an optional non-blocking caps test.

    ``indexer`` is the draft record to edit. For a new indexer the caller passes
    a freshly created record (S2 mints its ``custom:<uuid>`` id exactly once);
    for an existing indexer the caller passes the current record, so its stable
    id survives. The dialog never regenerates the id itself.
    """

    def __init__(self, indexer: CustomTorznabIndexer, *, interface: str = "", is_new: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add custom indexer" if is_new else "Edit custom indexer")
        self.setMinimumWidth(460)
        self._indexer_id = indexer.id
        self._interface = interface
        self._test_inflight = False
        self._result: CustomTorznabIndexer | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        form = QFormLayout()
        form.setSpacing(10)

        self.name_edit = QLineEdit(indexer.name)
        self.name_edit.setPlaceholderText("My private indexer")
        form.addRow("Name", self.name_edit)

        self.url_edit = QLineEdit(indexer.url)
        self.url_edit.setPlaceholderText("http://127.0.0.1:9696/torznab/api")
        form.addRow("Endpoint URL", self.url_edit)

        self.api_key_edit = QLineEdit(indexer.api_key)
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setPlaceholderText("Optional")
        self.test_button = QPushButton("Test Connection")
        self.test_button.clicked.connect(self._on_test)
        key_row = QHBoxLayout()
        key_row.addWidget(self.api_key_edit, 1)
        key_row.addWidget(self.test_button)
        form.addRow("API key", key_row)

        self.enabled_check = QCheckBox("Enabled")
        self.enabled_check.setChecked(indexer.enabled)
        form.addRow("", self.enabled_check)

        layout.addLayout(form)

        self.validation_label = QLabel("")
        self.validation_label.setProperty("role", "error")
        self.validation_label.setWordWrap(True)
        self.validation_label.setVisible(False)
        layout.addWidget(self.validation_label)

        self.result_label = QLabel("")
        self.result_label.setProperty("role", "muted")
        self.result_label.setWordWrap(True)
        self.result_label.setTextFormat(Qt.PlainText)
        layout.addWidget(self.result_label)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok = bb.button(QDialogButtonBox.Ok)
        ok.setText("Save")
        ok.setProperty("kind", "accent")
        bb.accepted.connect(self._on_accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

    def result(self) -> CustomTorznabIndexer | None:
        """The validated record after Save, or None when the editor was cancelled."""
        return self._result

    # -- validation ---------------------------------------------------------

    def _build_candidate(self) -> CustomTorznabIndexer | None:
        """The current field values as a validated S2 record, or None.

        Validation is the committed S2 parse round-trip: nothing here re-defines
        the name/url/key bounds or id rules. The returned record carries the
        trimmed values exactly as S2 would persist them.
        """
        record = CustomTorznabIndexer(
            id=self._indexer_id,
            enabled=self.enabled_check.isChecked(),
            name=self.name_edit.text(),
            url=self.url_edit.text(),
            api_key=self.api_key_edit.text(),
        )
        parsed = parse_custom_indexers([dataclasses.asdict(record)])
        return parsed[0] if parsed else None

    def _validation_message(self) -> str | None:
        if not self.name_edit.text().strip():
            return "Name is required."
        if not self.url_edit.text().strip():
            return "Endpoint URL is required."
        if self._build_candidate() is None:
            return "Enter a valid name, endpoint URL, and API key."
        return None

    def _on_accept(self) -> None:
        message = self._validation_message()
        if message is not None:
            self.validation_label.setText(message)
            self.validation_label.setVisible(True)
            return
        self.validation_label.setVisible(False)
        self._result = self._build_candidate()
        self.accept()

    # -- Test Connection ----------------------------------------------------

    def _on_test(self) -> None:
        if self._test_inflight:
            return
        message = self._validation_message()
        if message is not None:
            self.validation_label.setText(message)
            self.validation_label.setVisible(True)
            return
        self.validation_label.setVisible(False)
        # Capture an immutable snapshot now: the worker must never reread the
        # mutable text fields, so the result always belongs to the values that
        # were tested.
        candidate = self._build_candidate()
        self._test_inflight = True
        self.test_button.setEnabled(False)
        self.result_label.setText("Testing\u2026")
        self._launch_probe(candidate)

    def _launch_probe(self, candidate: CustomTorznabIndexer) -> None:
        interface = self._interface
        call = _IndexerProbe(lambda: TorznabSource(candidate).probe_caps(interface))
        _INFLIGHT_INDEXER_PROBES.add(call)
        call.signals.done.connect(self._on_probe_done)
        call.signals.failed.connect(self._on_probe_failed)
        call.signals.finished.connect(
            lambda c=call: _INFLIGHT_INDEXER_PROBES.discard(c)
        )
        QThreadPool.globalInstance().start(call)

    def _on_probe_done(self, _caps) -> None:
        self._test_inflight = False
        self.test_button.setEnabled(True)
        self.result_label.setText("Connection successful.")

    def _on_probe_failed(self, message: str) -> None:
        self._test_inflight = False
        self.test_button.setEnabled(True)
        self.result_label.setText(message)
