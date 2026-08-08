"""Qt bridge for the diagnostics logger.

``cove.diagnostics`` stays Qt-free so the native messaging host can use it
without dragging in PySide6. This module is the only place where a diagnostic
record becomes a Qt signal.

It deliberately does no logging of its own: it observes records the logger has
already accepted and sanitized, and forwards them. There is exactly one
persistence path, and it is not this one.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal


class DiagnosticsBridge(QObject):
    """Deliver newly accepted records into the GUI thread.

    ``DiagLogger.emit`` may be called from a worker thread (the queue, the
    aria2 poller, the magnet self-heal daemon). ``_incoming`` is connected to
    ``_republish`` with an explicit queued connection so the public
    ``record_added`` signal always fires on the thread that owns this object.
    """

    record_added = Signal(dict)
    _incoming = Signal(dict)

    def __init__(self, logger, parent=None):
        super().__init__(parent)
        self._logger = logger
        self._attached = False
        self._incoming.connect(self._republish, Qt.QueuedConnection)
        if logger is not None:
            logger.add_observer(self._observe)
            self._attached = True

    def _observe(self, record):
        # Runs on whichever thread emitted. Nothing here may raise.
        try:
            self._incoming.emit(record)
        except Exception:
            pass

    def _republish(self, record):
        try:
            self.record_added.emit(record)
        except Exception:
            pass

    def close(self):
        if self._attached and self._logger is not None:
            try:
                self._logger.remove_observer(self._observe)
            except Exception:
                pass
            self._attached = False
