"""Task-owned deletion and private aria2 publication, using scratch files only."""
from pathlib import Path
import os

import pytest

from cove import db
from cove.aria2 import Aria2Error
from tests.test_queue import fake_process, queue_env  # noqa: F401 - isolated fixtures


def inline(queue):
    def spawn(fn, *args, on_done=None, on_fail=None, **kwargs):
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            if on_fail:
                on_fail(str(exc))
            else:
                raise
        else:
            if on_done:
                on_done(result)
    queue._spawn = spawn


def hold(queue):
    calls = []
    def spawn(fn, *args, on_done=None, on_fail=None, **kwargs):
        calls.append((fn, args, kwargs, on_done, on_fail))
    queue._spawn = spawn
    return calls


def release(call):
    fn, args, kwargs, done, fail = call
    try:
        value = fn(*args, **kwargs)
    except Exception as exc:
        if fail:
            fail(str(exc))
        else:
            raise
    else:
        if done:
            done(value)


def task(queue, filename="file.zip", backend="aria2", url="https://example.com/file.zip"):
    tid = queue.add_url(url, filename=filename)
    t = queue.tasks[tid]
    t.backend = backend
    return t


def complete(queue, t, path):
    queue._apply_status(t.id, {"status": "complete", "files": [{"path": str(path)}]})


@pytest.mark.parametrize("delete", [False, True])
def test_F2_remove_during_publication_is_durable_and_recovers_without_relaunch(queue_env, delete):
    from tests.test_queue import _persisted_row
    queue, rpc, db_path = queue_env(intelligent_segments=False)
    inline(queue)
    t = task(queue)
    queue._launch(t)
    identity = t.gid
    payload = Path(t.work_dir) / "file.zip"
    payload.write_bytes(b"owned")
    calls = hold(queue)
    complete(queue, t, payload)
    queue.remove(t.id, delete_file=delete)
    row = _persisted_row(db_path, t.id)
    assert row["status"] == "removing", "F2: publication removal intent was not durable"
    assert row["gid"] == identity and not row["output_path"] and t.status != "completed"
    fresh, fresh_rpc, _ = queue_env(intelligent_segments=False)
    assert t.id not in fresh.tasks and fresh._recovered_removals[0][1] == identity
    inline(fresh)
    fresh._scheduler_allows = True
    fresh._maybe_start_next()
    assert fresh_rpc.added == []
    release(calls.pop(0))
    assert t.status == "completed"
    assert _persisted_row(db_path, t.id)["status"] == "removing"
    drain_calls = list(calls)
    calls.clear()
    for call in drain_calls:
        release(call)
    assert t.id not in queue.tasks and _persisted_row(db_path, t.id) is None
    assert (Path(t.out_dir) / "file.zip").exists() is not delete


@pytest.mark.parametrize("outcome", ["success", "error"])
@pytest.mark.parametrize("delete", [False, True])
def test_R3_publication_settlement_never_commits_a_gap_in_removal_intent(queue_env, monkeypatch, outcome, delete):
    import cove.queue as module
    from tests.test_queue import _persisted_row
    queue, rpc, path = queue_env(intelligent_segments=False)
    inline(queue)
    t = task(queue)
    queue._launch(t)
    identity = t.gid
    payload = Path(t.work_dir) / "file.zip"
    payload.write_bytes(b"owned")
    if outcome == "error":
        def fail_publish(*args, **kwargs):
            raise module.OutputPathError("publication failed")
        monkeypatch.setattr(module, "publish_output", fail_publish)
    calls = hold(queue)
    complete(queue, t, payload)
    queue.remove(t.id, delete_file=delete)
    original_persist = queue._persist
    observed = []
    def capture_after_settle_persist(candidate):
        original_persist(candidate)
        if not observed and candidate is t and t.id not in queue._publishing:
            row = _persisted_row(path, t.id)
            fresh, fresh_rpc, _ = queue_env(intelligent_segments=False)
            observed.append((row["status"], row["gid"], fresh, fresh_rpc))
    monkeypatch.setattr(queue, "_persist", capture_after_settle_persist)
    release(calls.pop(0))
    status, gid, fresh, fresh_rpc = observed[0]
    assert status == "removing" and gid == identity, "R3: settlement committed completed/error before removal ownership"
    assert t.id not in fresh.tasks and fresh._recovered_removals[0][1] == identity
    assert fresh_rpc.added == []
    assert t.status == ("completed" if outcome == "success" else "error")
    for call in list(calls):
        release(call)
    assert t.id not in queue.tasks and _persisted_row(path, t.id) is None
    public = Path(t.out_dir) / "file.zip"
    assert public.exists() is (outcome == "success" and not delete)


@pytest.mark.parametrize("engine", ["ffmpeg", "yt-dlp", "extractor-fallback"])
def test_g2_media_replacement_after_commit_is_never_recorded_as_owned(
    queue_env, fake_process, tmp_path, monkeypatch, engine
):
    import cove.queue as module
    from tests.test_queue import _start_hls, _start_extractor, _hls_private_path, _extractor_private_path, FINAL_PATH_MARKER
    queue, _, _ = queue_env()
    inline(queue)
    if engine == "ffmpeg":
        t, proc = _start_hls(queue, fake_process, tmp_path)
        source = _hls_private_path(proc)
    else:
        t, proc = _start_extractor(queue, fake_process, tmp_path)
        source = _extractor_private_path(proc)
    source.write_bytes(b"owned")
    foreign = tmp_path / "replacement.bin"
    foreign.write_bytes(b"foreign")
    original = module.publish_output
    def publish(*args, **kwargs):
        result = original(*args, **kwargs)
        public = result[0] if isinstance(result, tuple) else result
        public.rename(tmp_path / "detached.mp4")
        foreign.replace(public)
        return result
    monkeypatch.setattr(module, "publish_output", publish)
    if engine == "yt-dlp":
        proc.emit_output(f"{FINAL_PATH_MARKER}{source}\n")
    proc.finish(0)
    assert t.status == "completed"
    public = Path(t.output_path)
    queue.remove(t.id, delete_file=True)
    assert public.exists(), "G2: media handler captured and deleted the replacement identity"
    assert public.read_bytes() == b"foreign"
    assert (tmp_path / "detached.mp4").read_bytes() == b"owned"


def test_g3_completed_deletion_without_nofollow_uses_guarded_fallback(queue_env, tmp_path, monkeypatch):
    import os
    queue, _, _ = queue_env()
    inline(queue)
    t = task(queue)
    t.status = "completed"
    public = tmp_path / "file.zip"
    public.write_bytes(b"owned")
    info = public.stat()
    t.output_path, t.output_identity = str(public), (info.st_dev, info.st_ino)
    monkeypatch.setattr(os, "O_DIRECTORY", 0, raising=False)
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {os.unlink})
    try:
        queue.remove(t.id, delete_file=True)
    except AttributeError:
        pass
    assert not public.exists(), "G3: unsupported POSIX flags bypassed the guarded fallback"


@pytest.mark.parametrize("outcome", ["success", "remove-plain", "remove-delete", "error"])
def test_g1_shutdown_drains_publication_once_and_restart_never_duplicates(queue_env, monkeypatch, outcome):
    import threading
    import cove.queue as module
    from PySide6.QtCore import QCoreApplication
    from tests.test_queue import _persisted_row
    quitting, proceed = threading.Event(), threading.Event()
    app = QCoreApplication.instance()
    app.aboutToQuit.connect(quitting.set)
    queue, rpc, db_path = queue_env(intelligent_segments=False)
    inline(queue)
    t = task(queue)
    queue._launch(t)
    source = Path(t.work_dir) / "file.zip"
    source.write_bytes(b"staged")
    work = t.work_dir
    calls = hold(queue)
    real_publish = module.publish_output
    def publish(*args, **kwargs):
        assert proceed.wait(5)
        if outcome == "error":
            raise module.OutputPathError("publication failed")
        return real_publish(*args, **kwargs)
    monkeypatch.setattr(module, "publish_output", publish)
    complete(queue, t, source)
    fn, args, kwargs, done, fail = calls.pop(0)
    result = {}
    def worker():
        try:
            result["value"] = fn(*args, **kwargs)
        except Exception as exc:
            result["error"] = str(exc)
    thread = threading.Thread(target=worker)
    releaser = threading.Thread(target=lambda: (quitting.wait(5), proceed.set()))
    persisted, main = [], threading.get_ident()
    original_persist = queue._persist
    def persist(task):
        persisted.append(threading.get_ident())
        original_persist(task)
    monkeypatch.setattr(queue, "_persist", persist)
    if outcome.startswith("remove"):
        queue.remove(t.id, delete_file=outcome == "remove-delete")
    thread.start()
    releaser.start()
    try:
        app.aboutToQuit.emit()
    finally:
        proceed.set()
        thread.join(5)
        releaser.join(5)
        app.aboutToQuit.disconnect(quitting.set)
    assert not thread.is_alive()
    row = _persisted_row(db_path, t.id)
    expected = "error" if outcome == "error" else "removing" if outcome.startswith("remove") else "completed"
    assert row["status"] == expected, "G1: quit left the publication result unpersisted"
    assert persisted and set(persisted) == {main}
    if outcome == "error":
        assert row["error"] and row["work_dir"] == work and not row["output_path"]
        assert source.read_bytes() == b"staged"
    else:
        assert row["output_path"] == str(source.parent.parent / "file.zip") and row["work_dir"] == ""
        assert t.output_identity is not None
        assert Path(row["output_path"]).read_bytes() == b"staged"
    count = len(persisted)
    fail(result["error"]) if "error" in result else done(result["value"])
    assert len(persisted) == count, "G1: late callback applied publication twice"
    assert rpc.removed == []  # No backend confirmation was delivered at quit.
    fresh, fresh_rpc, _ = queue_env(intelligent_segments=False)
    assert t.id not in fresh.tasks and fresh_rpc.added == []
    if outcome != "error":
        assert list(Path(t.out_dir).glob("file*.zip")) == [Path(t.output_path)]


def test_r2_provider_filename_is_engine_and_publication_name(queue_env, monkeypatch):
    from cove import debrid
    from tests.test_queue import ALL_DEBRID, NODE_URL, ORIGINAL_URL, Unrestricted, _debrid_settings
    queue, rpc, _ = queue_env(**_debrid_settings(intelligent_segments=False))
    inline(queue)
    monkeypatch.setattr(
        debrid, "resolve",
        lambda *args, **kwargs: Unrestricted(NODE_URL, "provider.mkv", 4096, ALL_DEBRID),
    )
    t = task(queue, filename=None, url=ORIGINAL_URL)
    queue._launch(t)
    assert rpc.added[0]["filename"] == "provider.mkv", "provider filename was dropped before add_uri"
    assert t.aria2_filename == "provider.mkv"
    # Deliberately use a different reported basename: publication must use the
    # provider's admission name, independently of display/backend metadata.
    source = Path(t.work_dir) / "engine-renamed.mkv"
    source.write_bytes(b"download")
    complete(queue, t, source)
    assert Path(t.output_path).name == "provider.mkv"
    assert Path(t.output_path).read_bytes() == b"download"


@pytest.mark.parametrize("delete", [False, True])
def test_d1_never_launched_preserves_requested_public_file(queue_env, tmp_path, delete):
    queue, _, _ = queue_env(intelligent_segments=False)
    inline(queue)
    public = tmp_path / "file.zip"
    public.write_bytes(b"foreign")
    control = tmp_path / "file.zip.aria2"
    control.write_bytes(b"foreign control")
    t = task(queue)
    queue.remove(t.id, delete_file=delete)
    assert public.exists(), "never launched task deleted a foreign public file"
    assert public.read_bytes() == b"foreign"
    assert control.read_bytes() == b"foreign control"


@pytest.mark.parametrize("backend", ["ffmpeg", "yt-dlp"])
def test_d1m_never_started_media_preserves_public_file(queue_env, tmp_path, backend):
    queue, _, _ = queue_env()
    inline(queue)
    public = tmp_path / "file.zip"
    public.write_bytes(b"foreign")
    t = task(queue, backend=backend)
    queue.remove(t.id, delete_file=True)
    assert public.exists(), "never started media task deleted foreign output"
    assert public.read_bytes() == b"foreign"


def test_d2_collision_publishes_and_deletes_only_task_output(queue_env, tmp_path):
    queue, rpc, path = queue_env(intelligent_segments=False)
    inline(queue)
    original = tmp_path / "file.zip"
    original.write_bytes(b"foreign")
    t = task(queue)
    queue._launch(t)
    engine = Path(rpc.added[-1]["out_dir"]) / "file.zip"
    # Baseline aria2 points directly at the pre-existing file; do not overwrite it.
    if engine != original:
        engine.write_bytes(b"download")
    complete(queue, t, engine)
    published = tmp_path / "file (1).zip"
    assert published.exists(), "completion did not publish a separate owned output"
    assert published.read_bytes() == b"download"
    assert original.read_bytes() == b"foreign"
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM downloads WHERE id=?", (t.id,)).fetchone()
    assert row["output_path"] == str(published)
    assert row["work_dir"] == ""
    queue.remove(t.id, delete_file=True)
    assert not published.exists()
    assert original.exists(), "deletion used the requested name instead of the published path"
    assert original.read_bytes() == b"foreign"


@pytest.mark.parametrize("late", [False, True])
def test_d11_refused_backend_removal_deletes_nothing(queue_env, tmp_path, late):
    queue, rpc, _ = queue_env(intelligent_segments=False)
    public = tmp_path / "file.zip"
    public.write_bytes(b"foreign")
    t = task(queue)
    rpc.remove_errors = {"gid-1": Aria2Error("refused")}
    if late:
        calls = hold(queue)
        queue._launch(t)
        queue.remove(t.id, delete_file=True)
        release(calls.pop(0))
        release(calls.pop(0))
    else:
        inline(queue)
        queue._launch(t)
        queue.remove(t.id, delete_file=True)
    assert public.exists(), "refused backend removal deleted a public file"
    assert public.read_bytes() == b"foreign"
    work = getattr(t, "work_dir", "")
    if work:
        assert Path(work).is_dir()


@pytest.mark.parametrize("resume", [False, True])
def test_d3_restart_restores_real_work_and_output_authority(queue_env, resume):
    queue, rpc, path = queue_env(intelligent_segments=False)
    inline(queue)
    t = task(queue)
    queue._launch(t)
    work = Path(t.work_dir)
    (work / "file.zip").write_bytes(b"partial")
    t.output_path = str(Path(t.out_dir) / "committed.zip")
    queue._persist(t)
    restarted, rpc2, _ = queue_env(intelligent_segments=False)
    inline(restarted)
    restored = restarted.tasks[t.id]
    assert restored.work_dir == str(work)
    assert restored.output_path == t.output_path
    if resume:
        restarted._launch(restored)
        assert rpc2.added[-1]["out_dir"] == str(work)
        assert (work / "file.zip").read_bytes() == b"partial"
    else:
        restarted._launch(restored)
        restarted.remove(restored.id)
        assert rpc2.removed == ["gid-1"]
        assert not work.exists()


def test_d4_legacy_row_never_infers_ownership(queue_env, tmp_path):
    queue, _, _ = queue_env()
    inline(queue)
    t = task(queue)
    (tmp_path / "file.zip").write_bytes(b"legacy foreign")
    queue._persist(t)
    fresh, _, _ = queue_env()
    inline(fresh)
    assert fresh.tasks[t.id].work_dir == ""
    assert fresh.tasks[t.id].output_path == ""
    fresh.remove(t.id, delete_file=True)
    assert (tmp_path / "file.zip").exists(), "legacy filename became deletion authority"
    assert (tmp_path / "file.zip").read_bytes() == b"legacy foreign"


@pytest.mark.parametrize("kind", ["outside", pytest.param("symlink", marks=pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")), "prefix", "nested", "file"])
def test_d5_invalid_work_directory_never_deleted(queue_env, tmp_path, kind):
    queue, _, _ = queue_env()
    inline(queue)
    destination = tmp_path / "public"
    destination.mkdir()
    external = tmp_path / ".cove-work-external"
    external.mkdir()
    protected = external / "precious"
    protected.write_bytes(b"foreign")
    bad = destination / ".cove-work-invalid"
    if kind == "outside":
        bad = external
    elif kind == "symlink":
        bad.symlink_to(external, target_is_directory=True)
    elif kind == "prefix":
        bad = destination / "foreign"
        bad.mkdir()
    elif kind == "nested":
        parent = destination / "nested"
        parent.mkdir()
        bad = parent / ".cove-work-child"
        bad.mkdir()
    else:
        bad.write_bytes(b"foreign")
    t = task(queue)
    t.out_dir = str(destination)
    t.work_dir = str(bad)
    queue.remove(t.id, delete_file=True)
    assert bad.exists()
    assert protected.read_bytes() == b"foreign"


@pytest.mark.parametrize("kind", ["outside", *[pytest.param(kind, marks=pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")) for kind in ("symlink", "parent-symlink")]])
def test_d5_recorded_output_must_be_contained_regular_file(queue_env, tmp_path, kind):
    queue, _, _ = queue_env()
    inline(queue)
    dest = tmp_path / "public"
    dest.mkdir()
    foreign = tmp_path / "foreign.zip"
    foreign.write_bytes(b"foreign")
    output = foreign
    if kind == "symlink":
        output = dest / "link.zip"
        output.symlink_to(foreign)
    elif kind == "parent-symlink":
        (dest / "escape").symlink_to(tmp_path, target_is_directory=True)
        output = dest / "escape" / foreign.name
    t = task(queue)
    t.out_dir, t.output_path, t.status = str(dest), str(output), "completed"
    info = foreign.stat()
    t.output_identity = (info.st_dev, info.st_ino)
    queue.remove(t.id, delete_file=True)
    assert foreign.exists(), "out-of-destination recorded path was deleted"
    assert foreign.read_bytes() == b"foreign"
    assert output.exists()


def test_d6_same_name_tasks_publish_and_remove_independently(queue_env):
    queue, rpc, _ = queue_env(intelligent_segments=False)
    inline(queue)
    tasks = [task(queue, url=f"https://example.com/{n}") for n in (1, 2)]
    for n, t in enumerate(tasks):
        rpc.add_result = f"gid-{n}"
        queue._launch(t)
        source = Path(t.work_dir) / "file.zip"
        source.write_bytes(bytes([n]))
        complete(queue, t, source)
    assert rpc.added[0]["out_dir"] != rpc.added[1]["out_dir"]
    first, second = [Path(t.output_path) for t in tasks]
    assert first != second
    queue.remove(tasks[0].id, delete_file=True)
    assert not first.exists()
    assert second.read_bytes() == b"\x01"
    queue.remove(tasks[1].id, delete_file=True)
    assert not second.exists()


@pytest.mark.parametrize("identity", [True, False])
def test_d7_recorded_completed_output_is_the_only_explicit_delete_target(queue_env, tmp_path, identity):
    queue, _, _ = queue_env()
    inline(queue)
    requested = tmp_path / "file.zip"
    requested.write_bytes(b"foreign requested name")
    published = tmp_path / "recorded-output.zip"
    published.write_bytes(b"owned publication")
    sidecar = published.with_name(published.name + ".aria2")
    sidecar.write_bytes(b"foreign sidecar")
    t = task(queue)
    t.status = "completed"
    t.output_path = str(published)
    info = published.stat()
    t.output_identity = (info.st_dev, info.st_ino) if identity else None
    queue._persist(t)
    queue.remove(t.id, delete_file=True)
    assert published.exists() is not identity
    assert requested.read_bytes() == b"foreign requested name"
    assert sidecar.read_bytes() == b"foreign sidecar"


@pytest.mark.parametrize("valid", [True, False])
def test_r3_probe_worker_only_adopts_already_persisted_staging(queue_env, monkeypatch, valid):
    import threading
    from types import SimpleNamespace
    import cove.queue as module
    queue, rpc, _ = queue_env(intelligent_segments=True)
    calls = hold(queue)
    t = task(queue)
    queue._launch(t)
    persisted = t.work_dir
    with db.connect() as conn:
        assert conn.execute("SELECT work_dir FROM downloads WHERE id=?", (t.id,)).fetchone()[0] == persisted
    fn, args, kwargs, done, _ = calls.pop(0)
    assert fn.__name__ == "_probe_and_add"
    monkeypatch.setattr(queue, "_bound_session", lambda: SimpleNamespace(head=lambda *a, **k: SimpleNamespace(ok=True, headers={})))
    if not valid:
        t.work_dir = str(Path(persisted).parent / "wrong-prefix")
    main = threading.get_ident()
    outcomes, thread_ids = [], []
    def forbidden(*args, **kwargs):
        pytest.fail("probe worker attempted directory creation or DB persistence")
    def run():
        thread_ids.append(threading.get_ident())
        try:
            outcomes.append(fn(*args, **kwargs))
        except BaseException as exc:
            outcomes.append(exc)
    with monkeypatch.context() as patch:
        patch.setattr(module, "create_work_directory", forbidden)
        patch.setattr(queue, "_prepare_aria2_work", forbidden)
        patch.setattr(queue, "_persist", forbidden)
        patch.setattr(db, "connect", forbidden)
        worker = threading.Thread(target=run)
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()
    assert thread_ids[0] != main
    assert len(outcomes) == 1
    if valid:
        assert outcomes == ["gid-1"]
        assert rpc.added[0]["out_dir"] == persisted
        inline(queue)
        done(outcomes[0])
    else:
        assert isinstance(outcomes[0], module.OutputPathError)
        assert rpc.added == []
    assert Path(persisted).is_dir()


@pytest.mark.parametrize("keep", [False, True])
def test_d8_d9_incomplete_cleanup_respects_keep_contract(queue_env, keep):
    queue, _, _ = queue_env(intelligent_segments=False)
    inline(queue)
    t = task(queue)
    queue._launch(t)
    work = Path(t.work_dir)
    (work / "file.zip").write_bytes(b"partial")
    (work / "file.zip.aria2").write_bytes(b"control")
    queue.remove(t.id, keep_incomplete=keep)
    assert work.exists() is keep


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("refused", [False, True])
def test_d10_d11_late_gid_owns_only_private_staging(queue_env, tmp_path, refused, paused):
    queue, rpc, _ = queue_env(intelligent_segments=False)
    calls = hold(queue)
    t = task(queue)
    queue._launch(t)
    work = Path(t.work_dir)
    partial = work / "file.zip"
    partial.write_bytes(b"partial")
    public = tmp_path / "file.zip"
    public.write_bytes(b"foreign")
    if refused:
        rpc.remove_errors = {"gid-1": Aria2Error("ambiguous timeout")}
    if paused:
        queue.pause(t.id)
    queue.remove(t.id)
    assert partial.exists(), "F1: paused admission was cleaned before backend cancellation"
    assert partial.read_bytes() == b"partial"
    release(calls.pop(0))
    assert partial.exists()  # Backend cancellation has not yet confirmed.
    release(calls.pop(0))
    assert work.exists() is refused
    assert public.read_bytes() == b"foreign"


def test_finding_f2_reused_public_name_never_authorizes_another_tasks_output(queue_env, tmp_path):
    queue, rpc, _ = queue_env(intelligent_segments=False)
    inline(queue)
    owners = [task(queue, url=f"https://example.com/{n}") for n in (1, 2)]
    for n, t in enumerate(owners):
        rpc.add_result = f"gid-{n}"
        queue._launch(t)
        source = Path(t.work_dir) / "file.zip"
        source.write_bytes(bytes([n]))
        complete(queue, t, source)
        if n == 0:
            Path(t.output_path).rename(tmp_path / "moved.zip")
    reused = Path(owners[1].output_path)
    assert reused.name == "file.zip"
    queue.remove(owners[0].id, delete_file=True)
    assert reused.exists(), "F2: task A deleted task B's reused pathname"
    assert reused.read_bytes() == b"\x01"
    assert (tmp_path / "moved.zip").read_bytes() == b"\x00"


@pytest.mark.skipif(os.name == "nt", reason="POSIX dir_fd and symlink semantics")
def test_finding_f4_parent_swap_cannot_redirect_completed_deletion(queue_env, tmp_path, monkeypatch):
    import os
    queue, _, _ = queue_env(intelligent_segments=False)
    inline(queue)
    root, outside = tmp_path / "public", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    victim = outside / "file.zip"
    victim.write_bytes(b"foreign")
    t = task(queue)
    t.out_dir = str(root)
    queue._launch(t)
    source = Path(t.work_dir) / "file.zip"
    source.write_bytes(b"owned")
    complete(queue, t, source)
    real_unlink = os.unlink
    def unlink(path, *args, **kwargs):
        if Path(path).name == "file.zip":
            root.rename(tmp_path / "moved-public")
            root.symlink_to(outside, target_is_directory=True)
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {unlink})
    queue.remove(t.id, delete_file=True)
    assert victim.exists(), "F4: destination swap redirected unlink to an outside file"
    assert victim.read_bytes() == b"foreign"


@pytest.mark.parametrize("delete", [False, True])
def test_finding_f5_committed_publication_survives_unpause_failure(queue_env, delete):
    queue, _, _ = queue_env(intelligent_segments=False)
    inline(queue)
    t = task(queue)
    queue._launch(t)
    source = Path(t.work_dir) / "file.zip"
    source.write_bytes(b"owned")
    calls = hold(queue)
    complete(queue, t, source)
    queue._on_unpause_failed(t.id, "unpause refused")
    assert t.gid is None and t.status == "queued"
    if delete:
        queue.remove(t.id, delete_file=True)
    release(calls.pop(0))
    assert t.output_path, "F5: committed publication was discarded after gid changed"
    assert t.status == "completed"
    assert (t.id not in queue.tasks) is delete
    assert Path(t.output_path).exists() is not delete


@pytest.mark.parametrize("kind", ["outside", pytest.param("symlink", marks=pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")), "empty", "other-task"])
def test_p4_p5_p6_reported_path_never_grants_ownership(queue_env, tmp_path, kind):
    queue, _, _ = queue_env(intelligent_segments=False)
    inline(queue)
    t = task(queue, filename=None)
    queue._launch(t)
    work = Path(t.work_dir)
    foreign = tmp_path / "foreign.zip"
    foreign.write_bytes(b"foreign")
    reported = foreign
    if kind == "symlink":
        reported = work / "link.zip"
        reported.symlink_to(foreign)
    elif kind == "empty":
        reported = ""
    elif kind == "other-task":
        other = task(queue, url="https://example.com/other")
        queue._launch(other)
        reported = Path(other.work_dir) / "other.zip"
        reported.write_bytes(b"other output")
    complete(queue, t, reported)
    assert t.status == "error"
    assert t.output_path == ""
    assert work.exists()
    assert foreign.read_bytes() == b"foreign"


@pytest.mark.parametrize("name", ["Content Disposition.zip", *[pytest.param(name, marks=pytest.mark.skipif(os.name == "nt", reason="POSIX permits Windows-invalid source names")) for name in ("trailing.", "CON")]])
def test_f6_backend_chosen_name_validated_and_published(queue_env, name):
    queue, rpc, _ = queue_env(intelligent_segments=False)
    inline(queue)
    t = task(queue, filename=None)
    queue._launch(t)
    assert rpc.added[-1]["filename"] is None
    source = Path(t.work_dir) / name
    source.write_bytes(b"download")
    complete(queue, t, source)
    if name == "Content Disposition.zip":
        assert t.status == "completed"
        assert Path(t.output_path).read_bytes() == b"download"
    else:
        assert t.status == "error"
        assert t.output_path == ""
        assert source.read_bytes() == b"download"


def test_p6_publication_commit_is_the_only_authority(queue_env):
    queue, _, _ = queue_env(intelligent_segments=False)
    inline(queue)
    t = task(queue)
    queue._launch(t)
    source = Path(t.work_dir) / "file.zip"
    source.write_bytes(b"download")
    calls = hold(queue)
    complete(queue, t, source)
    assert t.output_path == ""
    complete(queue, t, source)
    assert len(calls) == 1  # Poll/completion cannot double publish.
    queue.remove(t.id, delete_file=True)
    assert t.id in queue.tasks
    assert source.exists()
    release(calls.pop(0))  # Publishes, records authority, then schedules remove.
    assert Path(t.output_path).exists()
    release(calls.pop(0))
    assert t.id not in queue.tasks
    assert not Path(t.output_path).exists()


def test_publication_runs_on_worker_and_commits_on_main_thread(queue_env, monkeypatch):
    import threading
    import cove.queue as module
    from tests.test_queue import _drain
    queue, _, _ = queue_env(intelligent_segments=False)
    inline(queue)
    t = task(queue)
    queue._launch(t)
    source = Path(t.work_dir) / "file.zip"
    source.write_bytes(b"download")
    main = threading.get_ident()
    worker_ids, callback_ids = [], []
    original = module.publish_output
    def publish(*args, **kwargs):
        worker_ids.append(threading.get_ident())
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "publish_output", publish)
    queue.task_changed.connect(lambda _: callback_ids.append(threading.get_ident()))
    queue._spawn = module.QueueManager._spawn.__get__(queue)
    complete(queue, t, source)
    _drain()
    assert worker_ids and worker_ids[0] != main
    assert callback_ids and set(callback_ids) == {main}
    assert t.status == "completed"


@pytest.mark.skipif(os.name == "nt", reason="POSIX copy worker")
def test_remove_while_copy_worker_is_writing_defers_cleanup(queue_env, monkeypatch):
    import errno
    import os
    import threading
    import cove.output_paths as paths
    import cove.queue as module
    from tests.test_queue import _drain
    queue, rpc, _ = queue_env(intelligent_segments=False)
    inline(queue)
    t = task(queue)
    queue._launch(t)
    work = Path(t.work_dir)
    source = work / "file.zip"
    source.write_bytes(b"download")
    writing, proceed = threading.Event(), threading.Event()
    real_write = os.write
    def unsupported(*args):
        raise OSError(errno.EOPNOTSUPP, "no hardlinks")
    def write(fd, chunk):
        writing.set()
        if not proceed.wait(timeout=5):
            raise OSError(errno.EIO, "test barrier timed out")
        return real_write(fd, chunk)
    monkeypatch.setattr(paths, "_link_pinned_fd", unsupported)
    monkeypatch.setattr(os, "write", write)
    queue._spawn = module.QueueManager._spawn.__get__(queue)
    complete(queue, t, source)
    try:
        assert writing.wait(timeout=5), "publication did not reach the copy worker"
        complete(queue, t, source)
        queue.remove(t.id, delete_file=True)
        assert source.exists()
        assert t.output_path == ""
        assert t.id in queue.tasks
        assert rpc.removed == []
    finally:
        proceed.set()
        _drain()
        _drain()  # Publication callback schedules a second worker for remove.
    assert t.id not in queue.tasks
    assert rpc.removed == ["gid-1"]
    assert not work.exists()
    assert not Path(t.output_path).exists()


@pytest.mark.parametrize("failure", [pytest.param("copy", marks=pytest.mark.skipif(os.name == "nt", reason="POSIX copy fallback")), "admission", pytest.param("copy-unavailable", marks=pytest.mark.skipif(os.name == "nt", reason="POSIX copy fallback"))])
def test_f3_f5_p7_failure_has_no_output_authority_and_restart_retries(
    queue_env, monkeypatch, failure
):
    import errno
    import os
    import cove.output_paths as paths
    queue, rpc, _ = queue_env(intelligent_segments=False)
    inline(queue)
    t = task(queue)
    if failure == "admission":
        monkeypatch.setattr(rpc, "add_uri", lambda *args: (_ for _ in ()).throw(Aria2Error("admission failed")))
        queue._launch(t)
        assert t.status == "error"
        assert t.output_path == ""
        assert Path(t.work_dir).is_dir()
        return
    queue._launch(t)
    work = Path(t.work_dir)
    source = work / "file.zip"
    source.write_bytes(b"staged complete output")
    def unsupported(*args):
        raise OSError(errno.EOPNOTSUPP, "hardlinks unsupported")
    with monkeypatch.context() as patch:
        patch.setattr(paths, "_link_pinned_fd", unsupported)
        if failure == "copy":
            patch.setattr(os, "write", lambda *args: (_ for _ in ()).throw(OSError(errno.EIO, "write failed")))
        else:
            patch.setattr(paths, "_copy_pinned_fd", lambda *args: (_ for _ in ()).throw(paths.OutputPathError("safe copy unavailable")))
        complete(queue, t, source)
    assert t.status == "error"
    assert t.output_path == ""
    assert t.work_dir == str(work)
    assert source.read_bytes() == b"staged complete output"
    # Startup loads queued/active/paused only. A persisted retry intent is queued.
    t.status = "queued"
    t.gid = None
    queue._persist(t)
    fresh, new_rpc, _ = queue_env(intelligent_segments=False)
    inline(fresh)
    restored = fresh.tasks[t.id]
    fresh._launch(restored)
    assert new_rpc.added[-1]["out_dir"] == str(work)
    complete(fresh, restored, source)
    assert restored.status == "completed"
    assert Path(restored.output_path).read_bytes() == b"staged complete output"
