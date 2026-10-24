"""A7a admission, recovery and duplicate oracles; all IO stays in temporary storage."""
import sqlite3
import types

import pytest
import requests
from PySide6.QtCore import QCoreApplication, QObject
from PySide6.QtWidgets import QMainWindow

from cove import config, db, dedup, main_window as mw, queue as qmod
from cove.aria2 import Aria2RPC
from cove.config import CategoryDirs, Settings
from cove.queue import ENGINE_UNAVAILABLE_TEXT, MAGNET_UNAVAILABLE_TEXT, QueueManager

ENGINE_TEXT = "Cove couldn't start the download engine, so this download did not start. It is saved and will be tried again the next time Cove starts."
MAGNET_TEXT = "Cove's download engine is not ready, so this magnet link was not added. Try again in a moment, or restart Cove if it failed to start."
ACTIVATING = {"aria2.addUri", "aria2.addTorrent", "aria2.unpause", "aria2.unpauseAll"}
URL, URL2 = "https://example.com/dir/f.zip?id=1", "https://example.com/dir/g.zip?id=2"
HASH = "0123456789abcdef0123456789abcdef01234567"
MAGNET = f"magnet:?xt=urn:btih:{HASH}&tr=https://tracker.invalid/PASSKEY"
SAVED = [(URL, "queued"), (URL2, "paused")]


class Recorder(Aria2RPC):
    """Record attempted calls, retaining the real client's unready-secret refusal."""
    def __init__(self, settings):
        self.secret_now, self.calls = None, []
        super().__init__(settings, secret_provider=lambda: self.secret_now)

    def _call(self, method, params=(), *, with_token=True):
        self.calls.append(method)
        if self.secret_now is None:
            return super()._call(method, params, with_token=with_token)
        if method in ACTIVATING:
            return f"{len(self.calls):016x}"
        if method == "aria2.getVersion":
            return {"version": "1.37.0", "enabledFeatures": ["BitTorrent"]}
        return "OK"

    def activations(self):
        return [m for m in self.calls if m in ACTIVATING]


def settle(queue):
    """Drain worker -> callback -> worker chains, requiring two quiet rounds."""
    quiet = 0
    for _ in range(40):
        queue._pool.waitForDone(2000)
        QCoreApplication.processEvents()
        QCoreApplication.sendPostedEvents()
        quiet = 0 if queue._inflight or queue._pool.activeThreadCount() else quiet + 1
        if quiet == 2:
            return
    raise AssertionError("queue did not settle")


def rows(path):
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT id,status,error,finished_at FROM downloads ORDER BY id").fetchall()


@pytest.fixture
def env(tmp_path, monkeypatch):
    path, made, http = tmp_path / "queue.sqlite3", [], []
    init, connect = db.init, db.connect
    monkeypatch.setattr(db, "init", lambda: init(path))
    monkeypatch.setattr(db, "connect", lambda: connect(path))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(Settings, "save", lambda self: None)
    def no_http(self, *a, **k):
        http.append(a)
        raise AssertionError("unexpected HTTP request")
    monkeypatch.setattr(requests.sessions.Session, "request", no_http)
    def build(admission="open", saved=(), **kw):
        settings = Settings(download_dir=str(tmp_path), category_dirs=CategoryDirs(),
                            **{"intelligent_segments": False, "torrent_support_enabled": True, **kw})
        db.init()
        with db.connect() as conn:
            conn.executemany("INSERT INTO downloads (url,out_dir,connections,speed_limit_kbps,status,created_at,category,backend) VALUES (?,?,4,0,?,1,'Other','aria2')",
                             [(url, str(tmp_path), status) for url, status in saved])
        rpc = Recorder(settings)
        queue = QueueManager(settings, rpc, **({} if admission == "open" else {"aria2_admission": admission}))
        queue._poll.stop(); queue._ext_poll.stop()
        made.append(queue)
        return queue, rpc
    build.path, build.http = path, http
    yield build
    for queue in made:
        settle(queue)
    assert http == []


LEGS = {
    "add_url": lambda q: q.add_url("https://example.com/new.zip"),
    "add_urls": lambda q: q.add_urls(["https://example.com/a.zip", "https://example.com/b.zip"]),
    "magnet": lambda q: q.add_url(MAGNET),
    "resume": lambda q: q.resume(2),
    "force": lambda q: q.force_start(1),
    "launch": lambda q: q._launch(q.tasks[1]),
    "restart": lambda q: (q.stop_queue(), q.start_queue()),
    "schedule": lambda q: (q.set_scheduler_allowed(False), q.set_scheduler_allowed(True)),
    "capacity": lambda q: q.set_max_concurrent(3),
}


@pytest.mark.parametrize("admission", ["open", "pending", "failed"])
@pytest.mark.parametrize("leg", LEGS)
def test_entry_paths_respect_admission(env, admission, leg):
    queue, rpc = env("open" if admission == "open" else "pending", SAVED)
    if admission == "open":
        rpc.secret_now = "ready"
    if admission == "failed":
        queue.set_aria2_admission("failed")
    LEGS[leg](queue); settle(queue)
    if admission == "open":
        assert rpc.activations()  # positive control: this path really dispatches
    else:
        assert rpc.activations() == []
        assert queue.tasks[1].status == ("queued" if admission == "pending" else "error")
        assert queue.tasks[2].status == ("queued" if admission == "pending" and leg == "resume" else "error" if admission == "failed" and leg == "resume" else "paused")
        if admission == "failed":
            assert all(t.startup_held and t.error == ENGINE_TEXT for t in queue.tasks.values() if t.status == "error")
        assert [r[1] for r in rows(env.path)][:2] == ["queued", "queued" if leg == "resume" else "paused"]


@pytest.mark.parametrize("admission", ["open", "pending", "failed"])
@pytest.mark.parametrize("constraint", ["stopped", "closed", "full", "reentrant"])
def test_failure_sweep_precedes_every_early_return(env, monkeypatch, admission, constraint):
    queue, rpc = env("open" if admission == "open" else "pending", max_concurrent=1)
    rpc.secret_now = "ready"
    if admission == "failed":
        queue.set_aria2_admission("failed")
    busy = qmod.DownloadTask(99, "https://example.com/v.m3u8", "", backend="ffmpeg", status="active")
    queue.tasks[99] = busy
    queue._running, queue._scheduler_allows = constraint != "stopped", constraint != "closed"
    added, snapshot = [], []
    def insert():
        # Insert AFTER failed transition, bypassing _launch and the setter sweep.
        with db.connect() as conn:
            tid = conn.execute("INSERT INTO downloads (url,out_dir,connections,speed_limit_kbps,status,created_at,category,backend) VALUES (?,?,4,0,'queued',1,'Other','aria2')", (URL, queue.settings.download_dir)).lastrowid
        queue.tasks[tid] = qmod.DownloadTask(tid, URL, queue.settings.download_dir)
        added.append(tid); snapshot.extend(rows(env.path))
        queue._maybe_start_next()
    if constraint == "reentrant":
        real = queue._active_count
        def reenter():
            if not added:
                insert()  # _scheduling is true; the next run_pass must sweep
            return real()
        monkeypatch.setattr(queue, "_active_count", reenter)
        queue._maybe_start_next()
    else:
        insert()
    settle(queue)
    t = queue.tasks[added[0]]
    assert busy.status == "active" and rpc.activations() == []
    assert rows(env.path) == snapshot
    assert t.status == ("error" if admission == "failed" else "queued")
    if admission == "failed":
        assert t.startup_held is True and t.error == ENGINE_UNAVAILABLE_TEXT
    if admission == "open":
        busy.status = "completed"
        queue._running = queue._scheduler_allows = True
        queue._maybe_start_next(); settle(queue)
        assert rpc.activations() == ["aria2.addUri"]


@pytest.mark.parametrize("admission", ["open", "pending"])
def test_held_task_does_not_starve_newer_media(env, monkeypatch, admission):
    queue, rpc = env(admission, [(URL, "queued")], max_concurrent=1)
    rpc.secret_now, launched = "ready", []
    monkeypatch.setattr(queue, "_launch_hls", lambda t: launched.append(t.id))
    queue.tasks[99] = qmod.DownloadTask(99, "https://example.com/v.m3u8", "", backend="ffmpeg")
    queue._maybe_start_next(); settle(queue)
    assert launched == ([] if admission == "open" else [99])
    assert queue.tasks[1].status == ("active" if admission == "open" else "queued")
    assert rpc.activations() == (["aria2.addUri"] if admission == "open" else [])


@pytest.mark.parametrize("backend,held", [("aria2", True), ("", True), ("unknown", True), ("ffmpeg", False), ("yt-dlp", False)])
def test_backend_dispatch_fails_closed(env, monkeypatch, backend, held):
    queue, rpc = env("pending", [(URL, "queued")])
    queue.tasks[1].backend, seen = backend, []
    monkeypatch.setattr(queue, "_launch_hls", lambda t: seen.append(t.id))
    monkeypatch.setattr(queue, "_launch_extractor", lambda t: seen.append(t.id))
    queue.force_start(1); settle(queue)
    assert seen == ([] if held else [1]) and rpc.activations() == []
    queue.set_aria2_admission("failed")
    assert queue.tasks[1].status == ("error" if held else "active")


def test_admission_state_machine_and_qt_parent(env):
    queue, rpc = env("pending", SAVED)
    changed = []
    queue.task_changed.connect(changed.append)
    queue.set_aria2_admission("bogus"); assert queue.aria2_admission == "pending"
    queue.set_aria2_admission("open")
    assert queue.aria2_admission == "open" and changed == []
    queue.set_aria2_admission("failed"); assert queue.aria2_admission == "open"
    failed, _ = env("pending")
    failed.set_aria2_admission("failed"); failed.set_aria2_admission("open")
    assert failed.aria2_admission == "failed" and failed.tasks[2].status == "paused"
    parent = QObject()
    child = QueueManager(queue.settings, rpc, parent)
    child._poll.stop(); child._ext_poll.stop()
    assert child.parent() is parent and child.aria2_admission == "open"


def test_failure_is_memory_only_and_restart_restores_exactly_once(env):
    queue, rpc = env("pending", SAVED + [("https://example.com/c.zip", "active")])
    before = rows(env.path)
    queue.set_aria2_admission("failed"); settle(queue)
    assert rows(env.path) == before and [r[1] for r in before] == ["queued", "paused", "active"]
    queue.resume(2); settle(queue)
    assert [r[1:] for r in rows(env.path)] == [("queued", None, None), ("queued", None, None), ("active", None, None)]
    restarted, rpc2 = env("pending", max_concurrent=3)
    assert sorted(restarted.tasks) == [1, 2, 3]
    assert all(t.status == "queued" and t.startup_held is False for t in restarted.tasks.values())
    rpc2.secret_now = "ready"
    restarted.set_aria2_admission("open"); restarted.resume_persisted(); settle(restarted)
    restarted._maybe_start_next(); settle(restarted)
    assert rpc.activations() == [] and rpc2.activations() == ["aria2.addUri"] * 3
    assert "startup_held" not in {r[1] for r in sqlite3.connect(env.path).execute("PRAGMA table_info(downloads)")}


@pytest.mark.parametrize("admission", ["open", "pending"])
def test_recovered_removal_is_submitted_before_first_launch(env, monkeypatch, admission):
    queue, rpc = env(admission, [(URL, "queued")])
    queue._recovered_removals = [(9, "deadbeefdeadbeef", False, "", "")]
    order, real = [], queue._spawn
    def spawn(fn, *a, **k):
        order.append(fn.__name__)
        return real(fn, *a, **k)
    monkeypatch.setattr(queue, "_spawn", spawn)
    rpc.secret_now = "ready"
    if admission == "pending":
        queue.set_aria2_admission("open")
    queue.resume_persisted(); settle(queue)
    assert order.count("_cancel_torrent_transfer") == order.count("add_uri") == 1
    assert order.index("_cancel_torrent_transfer") < order.index("add_uri")


def test_settle_drains_three_stage_chain(env):
    queue, _ = env()
    stages = []
    def stage(n):
        stages.append(n)
        if n < 3:
            queue._spawn(lambda: n + 1, on_done=stage)
    queue._spawn(lambda: 1, on_done=stage); settle(queue)
    assert stages == [1, 2, 3] and not queue._inflight


@pytest.mark.parametrize("admission", ["open", "pending"])
@pytest.mark.parametrize("recovery,backend", [(False, "aria2"), (False, "ffmpeg"), (False, "yt-dlp"), (False, "torrent"), (True, "aria2")])
def test_failed_launch_persistence_is_recoverable(env, monkeypatch, recovery, backend, admission):
    queue, rpc = env(admission, [(URL, "queued")])
    if admission == "pending":
        queue.set_aria2_admission("open")
    rpc.secret_now, dispatched, changed = "ready", [], []
    queue.tasks[1].backend = "aria2" if backend == "torrent" else backend
    queue.tasks[1].source_type = "torrent" if backend == "torrent" else ""
    for method in ("_launch_hls", "_launch_extractor", "_launch_torrent"):
        monkeypatch.setattr(queue, method, lambda t, m=method: dispatched.append(m))
    queue.task_changed.connect(changed.append)
    real, spawn = queue._persist, queue._spawn
    def fail(t):
        raise sqlite3.OperationalError("disk full")
    def spy(fn, *a, **k):
        dispatched.append(fn.__name__)
        return spawn(fn, *a, **k)
    monkeypatch.setattr(queue, "_persist", fail)
    monkeypatch.setattr(queue, "_spawn", spy)
    with pytest.raises(sqlite3.OperationalError, match="disk full"):
        queue.resume_persisted()
    settle(queue)
    assert dispatched == [] and rpc.activations() == [] and not queue._pending_launch and not queue._torrent_attempt
    assert queue._scheduling is False and rows(env.path) == [(1, "queued", None, None)]
    if recovery:
        monkeypatch.setattr(queue, "_persist", real)
        queue._maybe_start_next(); settle(queue)
        assert rpc.activations() == ["aria2.addUri"]
        queue._maybe_start_next(); settle(queue)
        assert rpc.activations() == ["aria2.addUri"] and queue.tasks[1].gid
    else:
        assert queue.tasks[1].status == "queued"
        assert queue.tasks[1].gid is None and queue._active_count() == 0 and changed == [1]


@pytest.mark.parametrize("transient", [False, True])
def test_duplicate_lifecycle_uses_private_marker(env, transient):
    queue, _ = env("pending" if transient else "open", [(URL, "queued")])
    if transient:
        queue.set_aria2_admission("failed")
    for wording in [ENGINE_UNAVAILABLE_TEXT, "renamed wording"]:
        queue.tasks[1].error = wording
        match = queue.find_duplicate(URL)
        assert (match.category, match.task_id, match.status) == (dedup.LIVE, 1, "queued")
    assert queue.find_duplicate(URL2) is None
    queue.tasks[1].status = "completed"
    assert queue.find_duplicate(URL).category == dedup.COMPLETED
    queue.remove(1); settle(queue)
    assert queue.find_duplicate(URL) is None and rows(env.path) == []
    history, _ = env(saved=[(URL2, "completed")])
    assert history.tasks == {} and history.find_duplicate(URL2).category == dedup.COMPLETED
    assert history.find_duplicate(URL) is None


@pytest.mark.parametrize("wording", ["boom", ENGINE_TEXT])
def test_ordinary_error_with_same_wording_is_not_live(env, wording):
    queue, _ = env(saved=[(URL, "queued")])
    queue.tasks[1].status, queue.tasks[1].error = "error", wording
    assert queue.find_duplicate(URL) is None


@pytest.mark.parametrize("anyway", [False, True], ids=["Already-in-queue", "Download-Anyway"])
def test_duplicate_decision_survives_restart(env, anyway):
    queue, _ = env("pending", [(URL, "queued")])
    queue.set_aria2_admission("failed")
    class Host(mw.MainWindow):
        def __init__(self):
            QMainWindow.__init__(self)
            self.queue, self.prompts = queue, []
        def _confirm_duplicate(self, match, label=""):
            self.prompts.append(match.task_id)
            return anyway
    host = Host()
    assert len(host.add_urls_checked([URL], intake="unknown")) == int(anyway)
    assert host.prompts == [1] and len(rows(env.path)) == 1 + int(anyway)
    restarted, rpc = env("pending", max_concurrent=2)
    assert len(restarted.tasks) == 1 + int(anyway)
    rpc.secret_now = "ready"
    restarted.set_aria2_admission("open"); restarted.resume_persisted(); settle(restarted)
    assert rpc.activations() == ["aria2.addUri"] * (1 + int(anyway))
    host.deleteLater()


@pytest.mark.parametrize("admission", ["open", "pending"])
def test_torrent_consent_respects_readiness(env, monkeypatch, admission):
    queue, rpc = env(admission)
    rpc.secret_now, stages = "ready", []
    def probe(t):
        stages.append(("probe", t.id in queue._torrent_attempt))
        return None, None
    monkeypatch.setattr(queue, "_probe_torrent", probe)
    real = queue._bittorrent_capable
    monkeypatch.setattr(queue, "_bittorrent_capable", lambda: (stages.append("capability"), real())[1])
    tid = queue.add_url(MAGNET); settle(queue)
    assert (tid in queue._awaiting_consent) is (admission == "open")
    queue.torrent_consent_reevaluate(tid); settle(queue)
    queue.torrent_consent(tid, True); settle(queue)
    assert stages == ([("probe", True), "capability"] if admission == "open" else [])
    assert rpc.activations() == (["aria2.addUri"] if admission == "open" else [])
    assert not queue._torrent_attempt


@pytest.mark.parametrize("admission", ["pending", "failed"])
def test_interactive_magnet_is_refused_without_claim_or_consent(env, monkeypatch, admission):
    queue, rpc = env("pending")
    if admission == "failed":
        queue.set_aria2_admission("failed")
    failed, errors, spawned, shown, consent = [], [], [], [], []
    queue.error.connect(errors.append)
    monkeypatch.setattr(queue, "_spawn", lambda *a, **k: spawned.append(a))
    monkeypatch.setattr(mw.QMessageBox, "information", lambda *a: shown.append(a[2]))
    monkeypatch.setattr(mw, "build_p2p_consent_box", lambda *a, **k: consent.append(True))
    assert queue.magnet_preflight_check(MAGNET)[1:] == (qmod.MAGNET_BLOCKED, MAGNET_UNAVAILABLE_TEXT)
    res = queue.resolve_magnet_preflight(MAGNET, on_resolved=lambda r: None, on_failed=failed.append)
    assert errors == [MAGNET_TEXT] and failed == [res]
    owner = types.SimpleNamespace(queue=queue, _preflights_closed=False)
    records, record_type = [], mw._MagnetRequest
    monkeypatch.setattr(mw, "_MagnetRequest", lambda owner: (records.append(record_type(owner)), records[-1])[1])
    assert mw.MainWindow._resolve_magnet_request(owner, MAGNET, None, "manual") is None
    assert len(records) == 1 and records[0].settled
    settle(queue)
    assert res.state == qmod.MAGNET_BLOCKED and failed == [res]
    assert errors == [MAGNET_TEXT] * 2 and shown == [MAGNET_TEXT] and consent == []
    assert spawned == [] and rpc.calls == [] and env.http == [] and rows(env.path) == []
    assert HASH not in queue._preflight_hashes and HASH not in queue._magnet_resolutions


@pytest.mark.parametrize("admission", ["open", "pending"])
def test_ready_magnet_worker_and_cancellation_unchanged(env, monkeypatch, admission):
    queue, _ = env(admission)
    if admission == "pending":
        queue.set_aria2_admission("open")
    started, delivered = [], []
    monkeypatch.setattr(queue, "_resolve_magnet_metadata", lambda rpc, res, *a: started.append(res.info_hash))
    # Keep the real worker/claim, but leave its fake result unsettled for cancellation.
    monkeypatch.setattr(queue, "_on_magnet_resolved", lambda *a: None)
    res = queue.resolve_magnet_preflight(MAGNET, on_resolved=delivered.append, on_failed=delivered.append, p2p_consent_granted=True)
    settle(queue)
    assert started == [HASH] and HASH in queue._preflight_hashes
    assert res.cancel() is True and delivered == []
    queue.cancel_magnet_resolutions(); settle(queue)


@pytest.mark.parametrize("refusal", ["disabled", "invalid", "resolving", "held", "live"])
def test_other_magnet_refusals_keep_precedence(env, refusal):
    queue, _ = env("pending", torrent_support_enabled=refusal != "disabled")
    if refusal == "resolving":
        queue._magnet_resolutions[HASH] = qmod.MagnetResolution(HASH, MAGNET)
    if refusal == "held":
        queue._preflight_hashes.add(HASH)
    if refusal == "live":
        queue.tasks[7] = qmod.DownloadTask(7, MAGNET, "", source_type="torrent", info_hash=HASH)
    _, state, text = queue.magnet_preflight_check("magnet:?xt=urn:btih:zzzz" if refusal == "invalid" else MAGNET)
    assert state == (qmod.MAGNET_ERROR if refusal in ("disabled", "invalid") else qmod.MAGNET_DUPLICATE)
    assert text and text != MAGNET_UNAVAILABLE_TEXT


@pytest.mark.parametrize("admission", ["open", "pending"])
def test_intelligent_segments_uses_probe_only_after_ready(env, monkeypatch, admission):
    queue, rpc = env(admission, [(URL, "queued")], intelligent_segments=True)
    rpc.secret_now, submitted = "ready", []
    response = types.SimpleNamespace(ok=True, headers={"Accept-Ranges": "bytes", "Content-Length": "1024"})
    monkeypatch.setattr(queue, "_bound_session", lambda: types.SimpleNamespace(head=lambda *a, **k: response))
    real = queue._spawn
    def spawn(fn, *a, **k):
        submitted.append(fn.__name__)
        return real(fn, *a, **k)
    monkeypatch.setattr(queue, "_spawn", spawn)
    queue.resume_persisted(); settle(queue)
    assert submitted == (["_probe_and_add"] if admission == "open" else [])
    assert rpc.activations() == (["aria2.addUri"] if admission == "open" else [])
