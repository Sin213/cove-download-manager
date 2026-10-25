"""Browser tickets serialize consent, not download queue entries."""
from types import SimpleNamespace
from pathlib import Path
import re
import pytest
from PySide6.QtCore import QObject, Signal, QCoreApplication
from cove import fuckingfast as ff
from cove.browser_fetch import BrowserFetchCoordinator


class Queue(QObject):
    task_added = Signal(int)
    aria2_admission = "open"

    def __init__(self):
        super().__init__()
        self.tasks = {}

    def add(self, url):
        tid = len(self.tasks) + 1
        self.tasks[tid] = SimpleNamespace(url=url)
        self.task_added.emit(tid)


@pytest.fixture
def env(tmp_path):
    app = QCoreApplication.instance() or QCoreApplication([])
    queue, opened, done = Queue(), [], []
    coord = BrowserFetchCoordinator(queue, tmp_path, opened.append)
    coord.finished.connect(done.append)
    yield coord, queue, opened, done, tmp_path
    coord.cancel()
    assert app is not None


def link(i=0):
    return ff.parse_share_url("https://fuckingfast.co/6oavo2x52dq" + str(i) + "#file.zip")


def claim(coord, root, opened, i=-1):
    nonce = opened[i].split("=")[-1]
    assert ff.consume_ticket(root, nonce, link().file_id if i == -1 else link(i).file_id)
    coord._tick()


def test_single_success_and_duplicates(env):
    c, q, opened, done, root = env
    assert c.submit([link(), link()]) == 1
    assert len(opened) == 1
    claim(c, root, opened)
    q.add("https://node.fuckingfast.co/dl/file")
    c._advance()
    assert done == [[]] and not list((root / "browser-fetch").glob("*.json"))


def test_stage_a_timeout_names_possible_causes_without_asserting_one(env):
    c, q, opened, done, root = env
    c.submit([link()])
    c._deadline = 0
    c._tick()
    c._advance()
    assert done == [[{"id": link().file_id, "message": (
        "Firefox did not start this download; the page may be missing or deleted, "
        "the Cove Firefox extension may be disabled or in another browser, or a browser "
        "check needs attention (complete it and press Download yourself).")}]]


@pytest.mark.parametrize("consume_during_revoke", [False, True], ids=["before-deadline", "during-revoke"])
@pytest.mark.parametrize("download_arrives", [False, True], ids=["download-timeout", "download-success"])
def test_claim_consumption_wins_at_stage_a_boundary(env, monkeypatch, consume_during_revoke, download_arrives):
    from cove import browser_fetch
    c, q, opened, done, root = env
    clock = [100.0]
    monkeypatch.setattr(browser_fetch.time, "monotonic", lambda: clock[0])
    c.submit([link(), link(1)])
    nonce = opened[0].split("=")[-1]
    assert c._deadline == 190.0
    if consume_during_revoke:
        original = ff.revoke_ticket
        def consume_then_revoke(data_dir, value):
            if value == nonce and ff.ticket_exists(data_dir, value):
                assert ff.consume_ticket(data_dir, value, link().file_id)
            return original(data_dir, value)
        monkeypatch.setattr(ff, "revoke_ticket", consume_then_revoke)
    else:
        clock[0] = 189.5
        assert ff.consume_ticket(root, nonce, link().file_id)
    clock[0] = 190.0
    c._poll.timeout.emit()
    assert c._stage == "download" and c._deadline == 235.0
    assert not c._failures and not done and len(opened) == 1
    assert not c._pause.isActive()
    if download_arrives:
        clock[0] = 234.5
        q.add("https://dl.fuckingfast.co/dl/file")
        assert c._failures == []
    else:
        clock[0] = 235.0
        c._poll.timeout.emit()
        assert c._failures == [{"id": link().file_id, "message": browser_fetch.NO_DOWNLOAD}]
    assert c._current is None and len(opened) == 1
    assert c._pause.isActive() and c._pause.interval() == 1500
    clock[0] += 1.5
    c._pause.timeout.emit()
    assert len(opened) == 2 and c._current == link(1)


def test_unconsumed_boundary_ticket_is_revoked_and_reported(env, monkeypatch):
    from cove import browser_fetch
    c, _, opened, done, root = env
    clock = [100.0]
    monkeypatch.setattr(browser_fetch.time, "monotonic", lambda: clock[0])
    c.submit([link()])
    nonce = opened[0].split("=")[-1]
    clock[0] = 190.0
    c._poll.timeout.emit()
    assert not ff.ticket_exists(root, nonce)
    assert c._failures == [{"id": link().file_id, "message": browser_fetch.NO_EXTENSION}]
    c._pause.timeout.emit()
    assert done == [[{"id": link().file_id, "message": browser_fetch.NO_EXTENSION}]]


def test_batch_strictly_serial_and_late_events_ignored(env):
    c, q, opened, done, root = env
    assert c.submit([link(0), link(1), link(2)]) == 3
    assert len(opened) == 1
    c._deadline = 0
    c._tick()
    assert len(opened) == 1
    c._advance()
    assert len(opened) == 2
    q.add("https://old.fuckingfast.co/late")
    assert len(opened) == 2 and not done
    nonce = opened[-1].split("=")[-1]
    assert ff.consume_ticket(root, nonce, link(1).file_id)
    c._tick()
    c._deadline = 0
    c._tick()
    c._advance()
    assert len(opened) == 3
    nonce = opened[-1].split("=")[-1]
    assert ff.consume_ticket(root, nonce, link(2).file_id)
    c._tick()
    q.add("https://fuckingfast.co/dl/file")
    c._advance()
    assert len(done) == 1 and len(done[0]) == 2
    assert [f["id"] for f in done[0]] == [link(0).file_id, link(1).file_id]
    assert "#cove-fetch" not in repr(done)


@pytest.mark.parametrize("admission", ["pending", "failed"])
def test_engine_gate_opens_nothing(env, admission):
    c, q, opened, done, _ = env
    q.aria2_admission = admission
    assert c.submit([link()]) == 0
    assert not opened and len(done) == 1
    assert len(done[0]) == 1


def test_cancel_revokes_and_ignores_late_download(env):
    c, q, opened, done, root = env
    assert c.submit([link(), link(1)]) == 2
    c.cancel()
    c.cancel()
    q.add("https://node.fuckingfast.co/dl/file")
    QCoreApplication.processEvents()
    assert not done and len(opened) == 1
    assert not list((root / "browser-fetch").glob("*.json"))


def test_opener_failure_is_fixed(env):
    c, q, opened, done, root = env
    c.opener = lambda url: (_ for _ in ()).throw(ValueError("SECRET"))
    assert c.submit([link()]) == 1
    c._advance()
    assert done[0][0]["message"] == "Cove could not open your browser"
    assert "SECRET" not in repr(done)
    assert not list((root / "browser-fetch").glob("*.json"))


def test_real_timer_pump_and_cancel_during_cooldown(env, monkeypatch):
    from PySide6.QtTest import QTest
    from cove import browser_fetch
    c, q, opened, done, root = env
    clock = [10.0]
    monkeypatch.setattr(browser_fetch.time, "monotonic", lambda: clock[0])
    c._poll.setInterval(1)
    c._pause.setInterval(1)
    c.submit([link(), link(1)])
    nonce = opened[0].split("=")[-1]
    assert ff.consume_ticket(root, nonce, link().file_id)
    QTest.qWait(5)
    q.add("https://node.fuckingfast.co/dl/file")
    c.cancel()
    QTest.qWait(5)
    assert len(opened) == 1 and not done
    c.submit([link(2)])
    clock[0] += 91
    for _ in range(20):
        QTest.qWait(5)
        if done:
            break
    assert len(done) == 1 and done[0][0]["id"] == link(2).file_id
    assert not list((root / "browser-fetch").glob("*.json"))


def test_mixed_main_window_intake_never_queues_share_links(monkeypatch):
    import cove.main_window as mw
    from PySide6.QtWidgets import QMainWindow

    class Host(mw.MainWindow):
        def __init__(self):
            QMainWindow.__init__(self)

    host, submitted, queued, duplicate_checks = Host(), [], [], []
    ordinary = ["https://example.com/a.zip?q=One", "https://example.com/b.zip"]
    host.queue = SimpleNamespace(
        find_duplicate=lambda url, **kw: duplicate_checks.append(url),
        add_urls=lambda urls, *a, **kw: queued.append((urls, a, kw)) or [1, 2])
    submitted_dirs = []
    host._submit_browser_fetch = lambda links, out_dir=None: (submitted.extend(links), submitted_dirs.append(out_dir))
    host._add_single_interactive = lambda *a: None
    urls = [link().page_url, ordinary[0], link(1).page_url, ordinary[1]]
    assert host.add_urls_checked(urls, "/destination", "clipboard") == [1, 2]
    assert [s.file_id for s in submitted] == [link().file_id, link(1).file_id]
    assert duplicate_checks == ordinary and queued == [(ordinary, ("/destination",), {"intake": "clipboard"})]
    assert submitted_dirs == ["/destination"]
    assert host.add_urls_checked([link().page_url]) == [] and len(queued) == 1
    ineligible = ["https://user@fuckingfast.co/Ab_4", "https://fuckingfast.co:8443/Other_1",
                  "https://fuckingfast.co/help"]
    assert all(ff.parse_share_url(url) is None for url in ineligible)
    host.add_urls_checked(ineligible, "/destination", "clipboard")
    assert queued[-1] == (ineligible, ("/destination",), {"intake": "clipboard"})
    assert len(submitted) == 3  # Only the earlier strictly eligible links opened the browser.
    host.deleteLater()


def test_main_window_reuses_coordinator_and_shutdown_cancels(tmp_path, monkeypatch):
    import cove.main_window as mw
    from cove import config
    from PySide6.QtWidgets import QMainWindow

    class Host(mw.MainWindow):
        def __init__(self):
            QMainWindow.__init__(self)

    host, opened, warnings = Host(), [], []
    host.queue = Queue()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mw, "open_url", opened.append)
    monkeypatch.setattr(mw.QMessageBox, "warning", lambda *a: warnings.append(a))
    assert host._submit_browser_fetch([link()]) == 1
    coordinator = host._browser_fetch_coordinator
    assert host._submit_browser_fetch([link(1)]) == 1
    assert host._browser_fetch_coordinator is coordinator and len(opened) == 1
    host.discard_magnet_requests = lambda: None
    host.discard_torrent_preflights = lambda: None
    host._torrent_contents_dialog = None
    host.close_interactive_preflights()
    assert not list((tmp_path / "browser-fetch").glob("*.json"))
    assert not coordinator._poll.isActive() and not coordinator._pause.isActive()
    host._browser_fetch_finished([])
    host._browser_fetch_finished([{"id": "Ab_4", "message": "Fixed failure"}])
    assert len(warnings) == 1 and warnings[0][2] == "Ab_4: Fixed failure"
    host.deleteLater()


@pytest.mark.parametrize("reason", ["success", "no_extension", "no_download", "open_failed",
                                  "authorize_failed", "engine_not_open", "cancelled"])
def test_diagnostics_lifecycle_contains_fixed_fields_only(env, monkeypatch, reason):
    from cove import diagnostics
    c, q, opened, done, root = env
    records = []
    monkeypatch.setattr(diagnostics, "emit", lambda component, event, **fields:
                        records.append((component, event, fields)))
    c.cancel()
    assert records == []
    if reason == "engine_not_open":
        q.aria2_admission = "pending"
    if reason == "authorize_failed":
        monkeypatch.setattr(ff, "issue_ticket", lambda *a, **k: "")
    if reason == "open_failed":
        c.opener = lambda url: (_ for _ in ()).throw(ValueError("SECRET " + url))
    c.submit([link(), link(1)])
    nonce = opened[0].split("=")[-1] if opened else ""
    if reason == "success":
        claim(c, root, opened)
        q.add("https://node.fuckingfast.co/dl/DELIVERY_SECRET")
    elif reason in ("no_extension", "no_download"):
        if reason == "no_download":
            claim(c, root, opened)
        c._deadline = 0
        c._tick()
    elif reason == "cancelled":
        c.cancel()
        c.cancel()
    expected = [] if reason == "engine_not_open" else [
        ("browser_fetch", "request_started", {"queued": 1})]
    if reason not in ("engine_not_open", "authorize_failed", "open_failed"):
        expected.append(("browser_fetch", "browser_opened", {}))
    expected.append(("browser_fetch", "request_ended", {"reason": reason}))
    assert records == expected
    for secret in (nonce, link().file_id, link().filename_hint, "https://", "DELIVERY_SECRET", "SECRET"):
        if secret:
            assert secret not in repr(records)


def test_diagnostics_real_logger_accepts_browser_fetch_records(env, monkeypatch):
    from cove import diagnostics
    c, q, opened, done, root = env
    logger = diagnostics.DiagLogger()
    monkeypatch.setattr(diagnostics, "_LOGGER", logger)
    try:
        c.submit([link()])
        nonce = opened[0].split("=")[-1]
        c.cancel()
        rows = [r for r in logger.records() if r["component"] == "browser_fetch"]
        assert [r["event"] for r in rows] == ["request_started", "browser_opened", "request_ended"]
        assert rows[0]["fields"] == {"queued": 0}
        assert rows[-1]["fields"] == {"reason": "cancelled"}
        assert diagnostics.sanitize_record(rows[-1])["component"] == "browser_fetch"
        assert all(value not in repr(rows) for value in (nonce, link().file_id, link().filename_hint, "https://"))
    finally:
        c.cancel()
        logger.close()


def test_claim_lifetime_has_margin_before_coordinator_timeout():
    from cove import browser_fetch
    text = (Path(__file__).resolve().parents[1] / "extension/fuckingfast-fetch.js").read_text()
    named = re.search(r"\bCLAIM_TTL_MS\s*=\s*(\d+)", text)
    assert named is not None
    assert int(named[1]) / 1000 + 5 <= browser_fetch.DOWNLOAD_TIMEOUT_SECONDS


def test_coordinator_retains_directory_per_link_and_dedupes_by_id(env, monkeypatch):
    from cove import diagnostics
    c, q, opened, done, root = env
    records = []
    monkeypatch.setattr(diagnostics, "emit", lambda *a, **k: records.append((a, k)))
    def submit(links, directory):
        return c.submit(links, out_dir=directory)
    assert submit([link(), link(1)], "/first") == 2
    assert submit([link(2)], "/second") == 1
    assert submit([link(1)], "/ignored") == 0
    for i, directory in enumerate(("/first", "/first", "/second")):
        nonce = opened[-1].split("=")[-1]
        assert ff.consume_ticket(root, nonce, link(i).file_id) == {"hint": "file.zip", "dir": directory}
        c._tick()
        q.add("https://node.fuckingfast.co/dl/file" + str(i))
        c._advance()
    assert done == [[]]
    assert all(directory not in repr(records) for directory in ("/first", "/second", "/ignored"))
