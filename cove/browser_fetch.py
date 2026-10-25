"""Serialized, ticket-authorized browser downloads. No network or worker threads."""
from collections import deque
import time
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, QTimer, Signal
from . import diagnostics, fuckingfast as ff

ENGINE = "Cove's download engine is still starting; try again in a moment"
OPEN = "Cove could not open your browser"
AUTHORIZE = "Cove could not authorize this browser request"
NO_EXTENSION = ("Firefox did not start this download; the page may be missing or deleted, "
                "the Cove Firefox extension may be disabled or in another browser, or a browser "
                "check needs attention (complete it and press Download yourself).")
NO_DOWNLOAD = "Firefox opened the page but no download reached Cove."
_FAILURES = {"engine_not_open": ENGINE, "authorize_failed": AUTHORIZE,
             "open_failed": OPEN, "no_extension": NO_EXTENSION, "no_download": NO_DOWNLOAD}
DOWNLOAD_TIMEOUT_SECONDS = 45


class BrowserFetchCoordinator(QObject):
    finished = Signal(list)

    def __init__(self, queue, data_dir, opener, parent=None):
        super().__init__(parent)
        self.queue, self.data_dir, self.opener = queue, data_dir, opener
        self._pending, self._seen, self._failures = deque(), set(), []
        self._current, self._nonce, self._stage = None, "", ""
        self._out_dir = None
        self._batch = False
        self._poll = QTimer(self)
        self._poll.setInterval(1000)
        self._poll.timeout.connect(self._tick)
        self._pause = QTimer(self)
        self._pause.setSingleShot(True)
        self._pause.setInterval(1500)
        self._pause.timeout.connect(self._advance)
        queue.task_added.connect(self._task_added)

    def submit(self, links, out_dir=None):
        links = list(links)
        if not links:
            return 0
        if self.queue.aria2_admission != "open":
            diagnostics.emit("browser_fetch", "request_ended", reason="engine_not_open")
            failure = {"id": links[0].file_id, "message": _FAILURES["engine_not_open"]}
            if self._batch:
                self._failures.append(failure)
            else:
                self.finished.emit([failure])
            return 0
        accepted = 0
        for link in links:
            if link.file_id not in self._seen:
                self._seen.add(link.file_id)
                self._pending.append((link, out_dir))
                accepted += 1
        if accepted:
            self._batch = True
            if self._current is None and not self._pause.isActive():
                self._advance()
        return accepted

    def _advance(self):
        self._pause.stop()
        if self._current is not None:
            return
        if not self._pending:
            if self._batch:
                failures, self._failures = self._failures, []
                self._seen.clear()
                self._batch = False
                self.finished.emit(failures)
            return
        self._current, self._out_dir = self._pending.popleft()
        diagnostics.emit("browser_fetch", "request_started", queued=len(self._pending))
        if self.queue.aria2_admission != "open":
            self._end("engine_not_open")
            return
        self._nonce = ff.issue_ticket(self.data_dir, self._current, directory=self._out_dir)
        if not self._nonce:
            self._end("authorize_failed")
            return
        self._stage, self._deadline = "claim", time.monotonic() + 90
        self._poll.start()
        try:
            self.opener(ff.fetch_url(self._current, self._nonce))
        except Exception:
            self._end("open_failed")
        else:
            diagnostics.emit("browser_fetch", "browser_opened")

    def _tick(self):
        if self._current is None:
            return
        now = time.monotonic()
        if self._stage == "claim":
            claimed = not ff.ticket_exists(self.data_dir, self._nonce)
            if not claimed and now >= self._deadline:
                # Unlink arbitrates with the consumer: a consumed ticket wins
                # even when it disappears between the exists check and revoke.
                claimed = not ff.revoke_ticket(self.data_dir, self._nonce)
                if not claimed:
                    self._end("no_extension")
                    return
            if claimed:
                self._stage, self._deadline = "download", now + DOWNLOAD_TIMEOUT_SECONDS
        elif now >= self._deadline:
            self._end("no_download")

    def _task_added(self, tid):
        if self._current is None:
            return
        self._tick()
        if self._current is None or self._stage != "download":
            return
        task = self.queue.tasks.get(tid)
        try:
            host = urlsplit(task.url).hostname if task else ""
            if (host and (host == ff.ALLOWED_HOST or host.endswith("." + ff.ALLOWED_HOST))
                    and not ff.parse_share_url(task.url)):
                self._end()
        except (ValueError, AttributeError):
            pass

    def _end(self, reason="success"):
        if self._current is None:
            return
        diagnostics.emit("browser_fetch", "request_ended", reason=reason)
        message = _FAILURES.get(reason)
        if message:
            self._failures.append({"id": self._current.file_id, "message": message})
        ff.revoke_ticket(self.data_dir, self._nonce)
        self._current, self._nonce, self._stage = None, "", ""
        self._out_dir = None
        self._poll.stop()
        self._pause.start()

    def cancel(self):
        if self._current is not None:
            diagnostics.emit("browser_fetch", "request_ended", reason="cancelled")
        ff.revoke_ticket(self.data_dir, self._nonce)
        self._pending.clear()
        self._seen.clear()
        self._failures.clear()
        self._current, self._nonce, self._stage = None, "", ""
        self._out_dir = None
        self._batch = False
        self._poll.stop()
        self._pause.stop()
