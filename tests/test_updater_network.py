"""Updater network behaviour: interface binding, GUI blocking, cancellation.

Three defects with one shape between them - the update flow did network work
on the wrong thread, and the half of it that ran on the right thread ignored
the user's configured interface.
"""
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, Qt, QThread
from PySide6.QtWidgets import QApplication, QProgressDialog

from cove import netiface, updater
from cove.updater import DownloadWorker


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


class _FakeResponse:
    """A response whose body arrives one chunk at a time, on demand."""

    def __init__(self, chunks, total=None, gate=None):
        self._chunks = list(chunks)
        self.headers = {"Content-Length": str(
            total if total is not None else sum(len(c) for c in chunks)
        )}
        self._gate = gate

    def read(self, _size=None):
        if self._gate is not None:
            self._gate.wait(5)
        return self._chunks.pop(0) if self._chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_checksum_fetch_is_bound_to_the_configured_interface(monkeypatch, tmp_path):
    """BUG-009: the manifest went out over the default route.

    A user who binds Cove to a VPN or an isolated interface expects the whole
    update operation on it. The artifact download honoured the setting and the
    manifest request did not, so the two could take different routes - and the
    manifest leaked over the default one.
    """
    seen = []

    def fake_bound_urlopen(req, name="", timeout=None):
        seen.append((req.full_url, name))
        return _FakeResponse([b"digest  Cove.AppImage\n"])

    monkeypatch.setattr(netiface, "bound_urlopen", fake_bound_urlopen)

    worker = DownloadWorker(
        "https://example.invalid/Cove.AppImage",
        tmp_path / "Cove.AppImage",
        "owner/repo",
        iface="tun0",
        checksum_url="https://example.invalid/SHA256SUMS",
        asset_name="Cove.AppImage",
    )
    worker.run()

    assert seen, "the worker never made a request"
    assert [name for _url, name in seen] == ["tun0"] * len(seen)
    assert "SHA256SUMS" in seen[0][0], "the manifest must be fetched first"


def test_the_manifest_is_not_fetched_on_the_calling_thread(monkeypatch, tmp_path):
    """BUG-012: an unreachable checksum host froze the whole interface.

    The fetch is bounded at eight seconds, which is eight seconds of a window
    that does not repaint or accept input.
    """
    threads = []

    def fake_bound_urlopen(req, name="", timeout=None):
        threads.append(threading.current_thread())
        if "SHA256SUMS" in req.full_url:
            return _FakeResponse([b"digest  Cove.AppImage\n"])
        return _FakeResponse([b"body"])

    monkeypatch.setattr(netiface, "bound_urlopen", fake_bound_urlopen)

    worker = DownloadWorker(
        "https://example.invalid/Cove.AppImage",
        tmp_path / "Cove.AppImage",
        "owner/repo",
        checksum_url="https://example.invalid/SHA256SUMS",
        asset_name="Cove.AppImage",
    )

    # The worker owns the fetch, so running it on a worker thread is enough to
    # keep it off the GUI thread - there is no second, GUI-side request left.
    thread = threading.Thread(target=worker.run)
    thread.start()
    thread.join(5)

    assert threads and threads[0] is not threading.current_thread()


def test_a_missing_manifest_is_reported_without_downloading(monkeypatch, tmp_path):
    """An unverifiable release must not transfer the binary at all."""
    downloaded = []

    def fake_bound_urlopen(req, name="", timeout=None):
        if "SHA256SUMS" in req.full_url:
            raise OSError("manifest host unreachable")
        downloaded.append(req)
        return _FakeResponse([b""])

    monkeypatch.setattr(netiface, "bound_urlopen", fake_bound_urlopen)
    reasons = []
    worker = DownloadWorker(
        "https://example.invalid/Cove.AppImage",
        tmp_path / "Cove.AppImage",
        "owner/repo",
        checksum_url="https://example.invalid/SHA256SUMS",
        asset_name="Cove.AppImage",
    )
    worker.manifestFailed.connect(reasons.append)

    worker.run()

    assert reasons == ["unreachable"]
    assert downloaded == []
    assert not (tmp_path / "Cove.AppImage").exists()


def test_a_manifest_without_our_asset_is_reported_without_downloading(
    monkeypatch, tmp_path
):
    downloaded = []

    def fake_bound_urlopen(req, name="", timeout=None):
        if "SHA256SUMS" in req.full_url:
            return _FakeResponse([b"digest  SomethingElse.AppImage\n"])
        downloaded.append(req)
        return _FakeResponse([b""])

    monkeypatch.setattr(netiface, "bound_urlopen", fake_bound_urlopen)
    reasons = []
    worker = DownloadWorker(
        "https://example.invalid/Cove.AppImage",
        tmp_path / "Cove.AppImage",
        "owner/repo",
        checksum_url="https://example.invalid/SHA256SUMS",
        asset_name="Cove.AppImage",
    )
    worker.manifestFailed.connect(reasons.append)

    worker.run()

    assert reasons == ["no_digest"]
    assert downloaded == []


def test_cancel_stops_a_download_already_in_progress(monkeypatch, tmp_path):
    """BUG-008: Cancel could not be observed while the transfer was running.

    The worker lives on its own thread and the cancel slot was delivered
    through that thread's event queue, so it sat behind the blocking download
    it was meant to interrupt. Cancellation has to be shared state the running
    transfer reads, not a queued call.
    """
    gate = threading.Event()
    reading = threading.Event()

    class _GatedResponse(_FakeResponse):
        def read(self, _size=None):
            reading.set()
            gate.wait(5)
            return b"x" * 1024

    monkeypatch.setattr(
        netiface, "bound_urlopen",
        lambda req, name="", timeout=None: _GatedResponse([], total=1 << 30),
    )

    dest = tmp_path / "Cove.AppImage"
    worker = DownloadWorker("https://example.invalid/Cove.AppImage", dest, "owner/repo")
    failures = []
    # Direct: this test runs no event loop, so a queued delivery to the main
    # thread would never arrive.
    worker.failed.connect(failures.append, Qt.DirectConnection)

    thread = threading.Thread(target=worker.run)
    thread.start()
    assert reading.wait(5), "the worker never started reading"

    # From another thread entirely, exactly as the GUI thread would.
    worker.cancel()
    gate.set()
    thread.join(5)

    assert not thread.is_alive()
    assert worker.cancelled is True
    assert failures, "a cancelled download must still report a terminal result"
    assert not dest.exists(), "a cancelled download must not leave a partial file"


def test_the_dialogs_cancel_reaches_a_worker_whose_thread_is_busy(app, tmp_path):
    """The controller must not deliver Cancel through the worker's event queue.

    `worker` lives on `thread`, and no event loop is running there - exactly
    the situation during a transfer, when run() owns the thread. A queued
    connection would never deliver, which is what made the Cancel button look
    dead until the download finished on its own.
    """
    worker = DownloadWorker("https://example.invalid/a", tmp_path / "a", "owner/repo")
    thread = QThread()
    worker.moveToThread(thread)
    try:
        dialog = QProgressDialog("Downloading…", "Cancel", 0, 100, None)
        dialog.canceled.connect(worker.cancel, Qt.DirectConnection)

        dialog.canceled.emit()

        assert worker.cancelled is True
    finally:
        worker.moveToThread(QCoreApplication.instance().thread())
        thread.quit()
        thread.wait(1000)


def test_install_wires_cancel_directly_rather_than_through_the_worker_thread(
    app, monkeypatch, tmp_path
):
    """Pins the wiring in the controller itself, not just in a test double."""
    from cove import updater as updater_module

    started = []
    monkeypatch.setattr(QThread, "start", lambda self, *a: started.append(self))
    monkeypatch.setattr(updater_module, "fetch_text", lambda *a, **kw: None)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    controller = updater.UpdateController(
        None, "1.0.0", "owner/repo", "Cove", "cove", iface="tun0",
    )
    info = updater.UpdateInfo(
        latest_version="2.0.0",
        release_url="https://example.invalid/release",
        asset_url="https://example.invalid/Cove.AppImage",
        asset_name="Cove.AppImage",
        asset_size=10,
        checksum_url="https://example.invalid/SHA256SUMS",
        checksum_name="SHA256SUMS",
    )

    controller._install(info)

    assert started, "the download thread was never started"
    worker = controller._download_worker
    assert worker is not None
    # The controller no longer fetches anything itself; the worker carries both
    # the asset and the manifest, on the configured interface.
    assert worker._checksum_url == info.checksum_url
    assert worker._iface == "tun0"

    controller._progress.canceled.emit()
    assert worker.cancelled is True


def test_cancel_interrupts_a_stalled_manifest_fetch(monkeypatch, tmp_path):
    """Cancel must reach every blocking phase, not only the asset transfer.

    Setting the event alone is checked between reads, so a worker blocked
    inside the manifest request ignored Cancel until the socket timeout - the
    dialog and the worker stayed alive for the whole of it.
    """
    reading = threading.Event()
    closed = threading.Event()

    class _StalledResponse(_FakeResponse):
        def read(self, _size=None):
            reading.set()
            # Blocks until something closes us, exactly as a real socket does.
            if not closed.wait(5):
                raise AssertionError("cancel never closed the response")
            raise OSError("closed during read")

        def close(self):
            closed.set()

    monkeypatch.setattr(
        netiface, "bound_urlopen",
        lambda req, name="", timeout=None: _StalledResponse([]),
    )

    worker = DownloadWorker(
        "https://example.invalid/Cove.AppImage",
        tmp_path / "Cove.AppImage",
        "owner/repo",
        checksum_url="https://example.invalid/SHA256SUMS",
        asset_name="Cove.AppImage",
    )
    failures = []
    worker.failed.connect(failures.append, Qt.DirectConnection)

    thread = threading.Thread(target=worker.run)
    thread.start()
    assert reading.wait(5), "the manifest fetch never started"

    worker.cancel()
    thread.join(5)

    assert not thread.is_alive(), "cancel did not interrupt the manifest fetch"
    assert failures == ["cancelled"]
    assert not (tmp_path / "Cove.AppImage").exists()


def test_a_cancel_before_the_request_opens_makes_no_network_call(monkeypatch, tmp_path):
    """Cancellation is checked at the start of every request, not only mid-read.

    A cancel that lands between phases must stop the next one from being
    issued at all.
    """
    opened = []
    monkeypatch.setattr(
        netiface, "bound_urlopen",
        lambda req, name="", timeout=None: opened.append(req.full_url) or _FakeResponse([b""]),
    )

    worker = DownloadWorker(
        "https://example.invalid/Cove.AppImage",
        tmp_path / "Cove.AppImage",
        "owner/repo",
        checksum_url="https://example.invalid/SHA256SUMS",
        asset_name="Cove.AppImage",
    )
    failures = []
    worker.failed.connect(failures.append, Qt.DirectConnection)

    worker.cancel()
    worker.run()

    assert opened == [], "a cancelled worker still opened a connection"
    assert failures, "a cancelled worker must report a terminal result"


def test_cancel_does_not_block_the_calling_thread_on_a_stuck_close(
    monkeypatch, tmp_path
):
    """Cancel is wired direct, so whatever it does happens on the GUI thread.

    `close()` takes the buffered reader's lock, which the worker holds for the
    whole of a read. Calling it here would freeze the interface until the read
    returned or the socket timed out - 8 or 20 seconds of exactly the hang
    Cancel exists to end. The socket shutdown is what breaks the read; the
    close that follows it must not be waited on.
    """
    reading = threading.Event()
    shutdown_called = threading.Event()
    release_close = threading.Event()

    class _StuckCloseResponse(_FakeResponse):
        def read(self, _size=None):
            reading.set()
            if not shutdown_called.wait(5):
                raise AssertionError("the socket was never shut down")
            raise OSError("connection shut down")

        def close(self):
            # Stands in for close() blocking on the read lock.
            release_close.wait(5)

    class _Sock:
        def shutdown(self, _how):
            shutdown_called.set()

    response = _StuckCloseResponse([])
    response.fp = SimpleNamespace(raw=SimpleNamespace(_sock=_Sock()))

    monkeypatch.setattr(
        netiface, "bound_urlopen",
        lambda req, name="", timeout=None: response,
    )

    worker = DownloadWorker(
        "https://example.invalid/Cove.AppImage",
        tmp_path / "Cove.AppImage",
        "owner/repo",
        checksum_url="https://example.invalid/SHA256SUMS",
        asset_name="Cove.AppImage",
    )
    failures = []
    worker.failed.connect(failures.append, Qt.DirectConnection)

    thread = threading.Thread(target=worker.run)
    thread.start()
    assert reading.wait(5), "the manifest fetch never started"

    started = time.monotonic()
    worker.cancel()                        # must return without waiting
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"cancel blocked the caller for {elapsed:.1f}s"
    assert shutdown_called.is_set(), "the read was never actually interrupted"

    release_close.set()
    thread.join(5)
    assert not thread.is_alive()
    assert failures == ["cancelled"]


def test_a_cancel_during_the_final_read_does_not_install_the_update(
    monkeypatch, tmp_path
):
    """The last read returns normally, so there is no exception to ride out.

    Cancellation was only checked before each read. A Cancel arriving during
    the final one found the loop already leaving through its ordinary exit, and
    the worker reported success - after which the controller replaced the
    executable and relaunched, for a download the user had called off.
    """
    at_last_read = threading.Event()
    cancelled = threading.Event()

    class _LastReadResponse(_FakeResponse):
        def read(self, _size=None):
            chunk = super().read(_size)
            if not chunk:
                # End of body: the user hits Cancel exactly here.
                at_last_read.set()
                cancelled.wait(5)
            return chunk

    monkeypatch.setattr(
        netiface, "bound_urlopen",
        lambda req, name="", timeout=None: _LastReadResponse([b"payload"]),
    )

    worker = DownloadWorker(
        "https://example.invalid/Cove.AppImage",
        tmp_path / "Cove.AppImage",
        "owner/repo",
    )
    finished, failures = [], []
    worker.finished.connect(finished.append, Qt.DirectConnection)
    worker.failed.connect(failures.append, Qt.DirectConnection)

    thread = threading.Thread(target=worker.run)
    thread.start()
    assert at_last_read.wait(5), "the download never reached its final read"

    worker.cancel()
    cancelled.set()
    thread.join(5)

    assert not thread.is_alive()
    assert finished == [], "a cancelled download must not report success"
    assert failures == ["cancelled"]
    assert not (tmp_path / "Cove.AppImage").exists()
