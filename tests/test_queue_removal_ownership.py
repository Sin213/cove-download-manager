"""S01B: cancellation owns dispatched adds until backend absence is proven."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from cove.aria2 import Aria2Error, Aria2RpcError
from cove.queue import PHASE_METADATA
from tests.test_queue import (  # noqa: F401 - isolated DB/Qt fixture
    MAGNET, _local_settings, _local_torrent_file, _persisted_row, queue_env,
)
from tests.test_queue_output_ownership import complete, hold, inline, release, task


EXPECTED = "123456789abcdef0"
LATE = "abcdef0123456789"
CHILD = "2222222222222222"


def begin_local_add(queue, tid):
    # Ownership tests isolate the add RPC after the pre-add gates have passed.
    attempt = object()
    queue._torrent_attempt[tid] = attempt
    queue._add_local_torrent(tid, attempt)


def dispatched(queue_env, torrent=False, worker=False):
    queue, rpc, path = queue_env(
        **_local_settings(all_debrid_enabled=False, intelligent_segments=worker, max_concurrent=1)
    )
    t = queue.tasks[queue.add_url(MAGNET)] if torrent else task(queue)
    queue._running = queue._scheduler_allows = True
    calls = hold(queue)
    if torrent:
        begin_local_add(queue, t.id)
    else:
        queue._launch(t)
    assert len(calls) == 1
    add = calls.pop()
    return queue, rpc, path, t, calls, add


def drain(calls):
    for _ in range(12):
        if not calls:
            return
        release(calls.pop(0))
    raise AssertionError("cancellation callbacks did not settle")


def absent(gid):
    return Aria2RpcError("aria2.tellStatus", 1, f"GID {gid} is not found")


def private_payload(t):
    payload = Path(t.work_dir) / "file.zip"
    payload.write_bytes(b"owned private bytes")
    return payload


@pytest.mark.parametrize("torrent", [False, True], ids=["C1", "T1"])
def test_remove_retains_durable_identity_until_late_add_cancellation(queue_env, torrent):
    queue, rpc, path, t, calls, add = dispatched(queue_env, torrent)
    removed = []
    queue.task_removed.connect(removed.append)
    queue.remove(t.id)
    row = _persisted_row(path, t.id)
    assert row is not None, "removal dropped durable ownership before add resolved"
    assert row["status"] == "removing" and row["gid"]
    assert row["gid"] == t.launch_gid and t.gid is None
    assert queue.is_pending_removal(t.id) and not removed and not calls
    queue.remove(t.id)
    add[3](LATE)  # Returned identity is authoritative, even if it differs.
    assert _persisted_row(path, t.id)["gid"] == LATE
    assert t.id in queue.tasks and not removed and not rpc.paused
    drain(calls)
    assert rpc.removed == [LATE] and removed == [t.id]
    assert t.id not in queue.tasks and _persisted_row(path, t.id) is None


def r4_retry(queue_env, stage="probe"):
    queue, rpc, path, t, calls, add = dispatched(queue_env, torrent=True)
    queue._bt_capable = True
    add[4]("ordinary add failure")
    queue.resume(t.id)
    probe = calls.pop()
    if stage == "consent":
        queue.settings.torrent_ip_disclosure_shown = False
        probe[3]((None, None))
        assert t.id in queue._awaiting_consent
        continuation = None
    elif stage == "capability":
        queue._bt_capable = None
        probe[3]((None, None))
        continuation = calls.pop()
    else:
        continuation = probe
    return queue, rpc, path, t, calls, continuation


def test_R4b_fresh_launch_supersedes_the_previous_preadd_attempt(queue_env):
    from cove.queue import SOURCE_TORRENT
    from tests.test_queue import _cached
    queue, rpc, path, t, calls, add = dispatched(queue_env, torrent=True)
    add[4]("ordinary add failure")
    identity = t.launch_gid
    queue._bt_capable = True
    queue._launch(t)
    queue._launch(t)
    assert len(calls) == 2, "fresh launch did not supersede the previous pre-add attempt"
    first, second = calls
    calls.clear()
    first[3]((_cached(), None))
    first[4]("obsolete probe failure")
    assert not calls and list(queue.tasks) == [t.id]
    assert t.source_type == SOURCE_TORRENT and t.status == "active" and t.error is None
    assert _persisted_row(path, t.id)["source_type"] == SOURCE_TORRENT
    second[3]((None, None))
    assert len(calls) == 1 and queue._pending_launch[t.id]["gid"] == identity
    received = []
    def add_magnet(*args, gid=None, **kwargs):
        received.append(gid)
        return gid
    rpc.add_magnet = add_magnet
    release(calls.pop())
    assert received == [identity] and t.gid == identity and not calls


def r4_late(queue, t, continuation, stage, outcome):
    from tests.test_queue import _cached
    if stage == "consent":
        if outcome == "reevaluate":
            queue.torrent_consent_reevaluate(t.id)
        else:
            queue.torrent_consent(t.id, outcome == "accept")
    elif outcome == "failure":
        continuation[4]("late stage failure")
    elif stage == "capability":
        continuation[3](outcome == "yes")
    else:
        if outcome == "disabled":
            queue.settings.torrent_fallback_mode = "never"
        continuation[3]((_cached() if outcome == "cached" else None, None))


R4_STAGES = [("probe", v) for v in ("uncached", "cached", "failure", "disabled")]
R4_STAGES += [("consent", v) for v in ("accept", "decline", "reevaluate")]
R4_STAGES += [("capability", v) for v in ("yes", "no", "failure")]


@pytest.mark.parametrize("stage,outcome", R4_STAGES)
@pytest.mark.parametrize("wish", ["pause", "double", "resume", "none"])
def test_R4_1_3_4_7_preadd_removal_never_waits_for_placeholder(
    queue_env, monkeypatch, stage, outcome, wish
):
    queue, rpc, path, t, calls, continuation = r4_retry(queue_env, stage)
    identity, probes = t.launch_gid, []
    monkeypatch.setattr(queue, "_ABSENCE_SETTLE_S", 0)
    def status(gid):
        probes.append(gid)
        raise absent(gid)
    rpc.tell_status = status
    if wish != "none":
        queue.pause(t.id)
        if wish == "double":
            queue.pause(t.id)
        elif wish == "resume":
            queue.resume(t.id)
    queue.remove(t.id)
    assert len(calls) == 1, "R4-1: removal waits for an add that was never dispatched"
    assert _persisted_row(path, t.id)["status"] == "removing"
    assert queue._active_count() == 1
    errors, consent, removed = [], [], []
    queue.error.connect(errors.append)
    queue.torrent_consent_needed.connect(consent.append)
    queue.task_removed.connect(removed.append)
    before = t.status, t.error
    r4_late(queue, t, continuation, stage, outcome)  # During cancellation.
    assert (t.status, t.error) == before and not errors and not consent
    assert len(calls) == 1
    drain(calls)
    r4_late(queue, t, continuation, stage, outcome)  # Duplicate after retirement.
    assert probes == [identity, identity] and rpc.removed == []
    assert not calls and rpc.magnets == rpc.torrents == []
    assert removed == [t.id] and not errors and not consent
    assert t.id not in queue.tasks and _persisted_row(path, t.id) is None
    assert queue._active_count() == 0


@pytest.mark.parametrize("admitted", [False, True])
@pytest.mark.parametrize("stage,outcome", R4_STAGES)
def test_R4_B5_refused_removal_invalidates_every_preadd_continuation(
    queue_env, stage, outcome, admitted
):
    from cove import db
    from cove.queue import SOURCE_TORRENT
    queue, rpc, path, t, calls, continuation = r4_retry(queue_env, stage)
    identity = t.launch_gid
    if admitted:
        rpc.tell_status = lambda gid: {"gid": gid, "status": "active"}
        rpc.remove_errors = {identity: Aria2Error("removal refused")}
    else:
        rpc.tell_status = lambda gid: (_ for _ in ()).throw(Aria2Error("probe transport lost"))
    queue.remove(t.id)
    drain(calls)
    assert not queue.is_pending_removal(t.id)
    assert t.gid == (identity if admitted else None)
    assert (t.id in queue._unreconciled) == (not admitted)
    row, state = dict(_persisted_row(path, t.id)), (t.status, t.error, t.gid, t.launch_gid)
    errors, consent = [], []
    queue.error.connect(errors.append)
    queue.torrent_consent_needed.connect(consent.append)
    r4_late(queue, t, continuation, stage, outcome)
    assert not calls, "B5: stale continuation dispatched another add/stage"
    assert t.source_type == SOURCE_TORRENT and (t.status, t.error, t.gid, t.launch_gid) == state
    assert dict(_persisted_row(path, t.id)) == row and not errors and not consent
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0] == 1
    assert rpc.magnets == rpc.torrents == [] and queue._active_count() == 1


@pytest.mark.parametrize("remove", [False, True])
def test_R4_2_6_7_real_add_keeps_pause_and_removal_ownership(queue_env, remove):
    queue, rpc, path, t, calls, probe = r4_retry(queue_env)
    # Pause/resume before the probe finishes must not dispatch another probe.
    queue.pause(t.id)
    queue.resume(t.id)
    assert not calls
    probe[3]((None, None))
    assert len(calls) == 1
    add = calls.pop()
    identity = t.launch_gid
    queue.pause(t.id)
    queue.pause(t.id)
    assert queue._pending_launch[t.id]["gid"] == identity
    if remove:
        queue.remove(t.id)
        assert not calls and not rpc.removed
    add[3](identity)
    drain(calls)
    if remove:
        assert rpc.removed == [identity] and not rpc.paused
        assert _persisted_row(path, t.id) is None and queue._active_count() == 0
    else:
        assert rpc.paused == [identity] and t.status == "paused" and t.gid == identity
        assert queue._active_count() == 0


@pytest.mark.parametrize("outcome", ["cached", "failure"])
def test_R4_5_provider_refusal_then_remove_releases_slot(queue_env, monkeypatch, outcome):
    queue, rpc, path, t, calls, probe = r4_retry(queue_env)
    r4_late(queue, t, probe, "probe", outcome)
    assert t.status == "error" and queue._active_count() == 0
    monkeypatch.setattr(queue, "_ABSENCE_SETTLE_S", 0)
    rpc.tell_status = lambda gid: (_ for _ in ()).throw(absent(gid))
    queue.remove(t.id)
    drain(calls)
    assert not calls and _persisted_row(path, t.id) is None and queue._active_count() == 0


@pytest.mark.parametrize("stage", ["probe", "consent", "capability"])
def test_R4_8_restart_reconciles_preadd_pause_removal(queue_env, stage):
    queue, rpc, path, t, calls, _ = r4_retry(queue_env, stage)
    queue.pause(t.id)
    queue.remove(t.id)
    identity = t.launch_gid
    fresh, fresh_rpc, _ = queue_env(**_local_settings(intelligent_segments=False))
    assert t.id not in fresh.tasks and fresh._recovered_removals[0][1] == identity
    inline(fresh)
    fresh.resume_persisted()
    assert fresh_rpc.removed == [identity] and not fresh_rpc.magnets
    assert _persisted_row(path, t.id) is None


@pytest.mark.parametrize("torrent", [False, True], ids=["C2-C3", "T5-retry"])
def test_late_cancel_failure_retains_owner_and_retry_retires(queue_env, torrent, tmp_path):
    queue, rpc, path, t, calls, add = dispatched(queue_env, torrent)
    payload = tmp_path / "payload.bin" if torrent else private_payload(t)
    if torrent:
        payload.write_bytes(b"owned torrent bytes")
        rpc.files_result = [{"path": str(payload)}]
    rpc.remove_errors = {LATE: Aria2Error("transport lost")}
    errors, removed = [], []
    queue.error.connect(errors.append)
    queue.task_removed.connect(removed.append)
    queue.remove(t.id, delete_file=True)
    add[3](LATE)
    drain(calls)
    assert t.id in queue.tasks, "failed late cancellation retired its owner"
    assert payload.exists() and not removed and errors
    assert t.gid == LATE and _persisted_row(path, t.id)["gid"] == LATE
    assert _persisted_row(path, t.id)["status"] == "active"
    assert not queue.is_pending_removal(t.id)
    rpc.remove_errors.clear()
    queue.remove(t.id, delete_file=True)
    drain(calls)
    assert removed == [t.id] and not payload.exists()
    assert _persisted_row(path, t.id) is None


@pytest.mark.parametrize("torrent", [False, True], ids=["C4", "T6"])
@pytest.mark.parametrize("confirmed", [False, True], ids=["pre-GID", "known-GID"])
def test_restart_reconciles_removal_without_relaunch(queue_env, torrent, confirmed):
    queue, rpc, path, t, calls, add = dispatched(queue_env, torrent)
    if confirmed:
        add[3](LATE)
        calls.clear()
    queue.remove(t.id)
    row = _persisted_row(path, t.id)
    assert row is not None and row["gid"], "crash loses pre-GID identity"
    identity = row["gid"]
    fresh, fresh_rpc, _ = queue_env(**_local_settings(intelligent_segments=False))
    assert t.id not in fresh.tasks and fresh._recovered_removals
    inline(fresh)
    fresh._running = fresh._scheduler_allows = True
    fresh.resume_persisted()
    fresh._maybe_start_next()
    assert fresh_rpc.removed == [identity]
    assert fresh_rpc.added == fresh_rpc.magnets == fresh_rpc.torrents == []
    assert _persisted_row(path, t.id) is None


@pytest.mark.parametrize("admission", ["present", "absent", "lagged"], ids=["C5", "C6", "lagged-admission"])
@pytest.mark.parametrize("torrent", [False, True], ids=["uri", "magnet"])
def test_ambiguous_add_reconciles_exact_identity_after_terminal_callback(
    queue_env, monkeypatch, admission, torrent
):
    queue, rpc, path, t, calls, add = dispatched(queue_env, torrent)
    monkeypatch.setattr(queue, "_ABSENCE_SETTLE_S", 0, raising=False)
    payload = None if torrent else private_payload(t)
    identity = getattr(t, "launch_gid", EXPECTED)
    probes, events = [], []
    def status(gid):
        probes.append(gid)
        events.append("probe")
        if admission == "absent" or admission == "lagged" and len(probes) == 1:
            raise absent(gid)
        return {"gid": gid, "status": "active"}
    rpc.tell_status = status
    rpc.on_removed = lambda *args: events.append("remove")
    queue.remove(t.id)
    assert not probes and not rpc.removed, "absence queried before add resolved"
    add[4]("add reply lost")
    assert _persisted_row(path, t.id) is not None, "ambiguous add lost ownership"
    drain(calls)
    assert probes and set(probes) == {identity}
    if admission == "absent":
        assert len(probes) == 2 and not rpc.removed
    else:
        assert rpc.removed == [identity] and "remove" in events
        assert events.index("remove") >= (2 if admission == "lagged" else 1)
        if admission == "lagged":
            assert events[:2] == ["probe", "probe"]
    assert t.id not in queue.tasks and _persisted_row(path, t.id) is None
    if payload:
        assert not payload.exists()


@pytest.mark.parametrize("failure", ["transport", "timeout", "rpc", "malformed", "second-probe"])
def test_ambiguous_reconciliation_failure_never_grants_cleanup(queue_env, monkeypatch, failure):
    queue, rpc, path, t, calls, add = dispatched(queue_env)
    monkeypatch.setattr(queue, "_ABSENCE_SETTLE_S", 0, raising=False)
    payload = private_payload(t)
    identity = getattr(t, "launch_gid", EXPECTED)
    probes = []
    def status(gid):
        probes.append(gid)
        if failure == "second-probe" and len(probes) == 1:
            raise absent(gid)
        if failure == "malformed":
            return {}
        if failure == "rpc":
            raise Aria2RpcError("aria2.tellStatus", 1, "permission refused")
        raise Aria2Error("timeout" if failure == "timeout" else "transport lost")
    rpc.tell_status = status
    errors = []
    queue.error.connect(errors.append)
    queue.remove(t.id)
    add[4]("add reply lost")
    drain(calls)
    assert t.id in queue.tasks, "unproved absence retired ownership"
    assert payload.exists() and errors and not rpc.removed
    assert t.gid is None and t.launch_gid == identity
    assert t.status == "error" and t.error == "add reply lost"
    assert _persisted_row(path, t.id)["status"] == "removing", "R1: unproven identity lost durable removal intent"
    assert _persisted_row(path, t.id)["gid"] == identity, "F1: failed reconciliation lost durable identity"
    assert queue._active_count() == 1, "F1: unproven backend identity released its slot"
    assert not queue.is_pending_removal(t.id)
    rpc.tell_status = lambda gid: (_ for _ in ()).throw(absent(gid))
    queue.remove(t.id)
    drain(calls)
    assert _persisted_row(path, t.id) is None and not payload.exists()
    assert queue._active_count() == 0


def test_remove_after_ambiguous_add_failure_reaches_retained_identity(queue_env, monkeypatch):
    queue, rpc, path, t, calls, add = dispatched(queue_env)
    monkeypatch.setattr(queue, "_ABSENCE_SETTLE_S", 0, raising=False)
    payload = private_payload(t)
    add[4]("add reply lost")
    identity = getattr(t, "launch_gid", EXPECTED)
    rpc.tell_status = lambda gid: {"gid": gid, "status": "active"}
    queue.remove(t.id)
    assert _persisted_row(path, t.id) is not None, "failed add identity was forgotten"
    drain(calls)
    assert rpc.removed == [identity] and not payload.exists()


@pytest.mark.parametrize("torrent", [False, True], ids=["C7", "T1-duplicates"])
def test_duplicate_callbacks_do_not_resurrect_or_retire_twice(queue_env, torrent):
    queue, rpc, path, t, calls, add = dispatched(queue_env, torrent)
    removed = []
    queue.task_removed.connect(removed.append)
    queue.remove(t.id)
    add[3](LATE)
    transaction = calls[0]
    add[3](LATE)
    add[4]("stale add failure")
    assert len(calls) == 1 and t.id in queue.tasks, "duplicate add altered cancellation"
    queue._apply_status(t.id, {"status": "complete", "followedBy": [CHILD]})
    drain(calls)
    transaction[3](())
    add[3](LATE)
    add[4]("stale add failure")
    queue._apply_status(t.id, {"status": "active"})
    assert removed == [t.id] and rpc.removed == [LATE] and not calls
    assert t.id not in queue.tasks and _persisted_row(path, t.id) is None


def test_C7_old_add_generation_cannot_overwrite_new_attempt(queue_env):
    queue, rpc, path, t, calls, old = dispatched(queue_env)
    old[4]("not admitted")
    t.status = "queued"
    queue._launch(t)
    new = calls.pop()
    new[3](LATE)
    old[3](EXPECTED)
    old[4]("old failure")
    assert t.gid == LATE and t.status == "active", "old add callback overwrote new attempt"
    assert _persisted_row(path, t.id)["gid"] == LATE and not calls


@pytest.mark.parametrize("torrent", [False, True], ids=["C9", "T1-slot"])
def test_paused_pending_removal_holds_slot_and_releases_it_once(queue_env, torrent):
    queue, rpc, path, t, calls, add = dispatched(queue_env, torrent)
    queue.pause(t.id)
    queue.remove(t.id)
    assert queue._active_count() == 1, "paused cancellation released its live slot"
    other = task(queue, url="https://example.com/other.zip")
    assert other.status == "queued" and not calls
    add[3](LATE)
    assert not rpc.paused and len(calls) == 1
    transaction = calls.pop()
    release(transaction)
    assert t.id not in queue.tasks and other.status == "active" and len(calls) == 1
    transaction[3](())
    assert len(calls) == 1, "duplicate retirement launched another operation"


def test_C9_ordinary_paused_task_does_not_hold_slot(queue_env):
    queue, rpc, path = queue_env(intelligent_segments=False)
    t = task(queue)
    t.status = "paused"
    assert queue._active_count() == 0


def test_C8_completion_after_remove_cannot_publish(queue_env):
    queue, rpc, path, t, calls, add = dispatched(queue_env)
    payload = private_payload(t)
    queue.remove(t.id)
    add[3](LATE)
    complete(queue, t, payload)
    assert t.status != "completed" and not t.output_path
    assert len(calls) == 1, "completion began publishing during cancellation"
    drain(calls)
    assert not (Path(t.out_dir) / "file.zip").exists()


def test_C8_publication_committed_before_remove_uses_recorded_owner(queue_env):
    queue, rpc, path, t, calls, add = dispatched(queue_env)
    payload = private_payload(t)
    add[3](LATE)
    inline(queue)
    complete(queue, t, payload)
    assert t.status == "completed"
    output = Path(t.output_path)
    assert output.exists()
    queue.remove(t.id, delete_file=True)
    assert not output.exists() and _persisted_row(path, t.id) is None


@pytest.mark.parametrize("worker", [False, True], ids=["direct", "probe"])
def test_inflight_identity_cannot_be_adopted_from_external_snapshot(queue_env, worker):
    queue, rpc, path, t, calls, add = dispatched(queue_env, worker=worker)
    identity = getattr(t, "launch_gid", EXPECTED)
    rpc.tell_external_snapshot = lambda: []
    queue._check_external()
    calls.pop()[3]([{"gid": identity, "status": "active", "files": []}])
    assert list(queue.tasks) == [t.id], "external poll adopted Cove's unresolved add"
    assert identity in queue._seen_gids and t.gid is None


@pytest.mark.parametrize("snapshot", ["followedBy", "active", "waiting", "stopped", "late"], ids=["T2", "T2-active", "T2-waiting", "T4", "T3"])
def test_late_metadata_cancellation_walks_payload_children(queue_env, tmp_path, snapshot):
    queue, rpc, path, t, calls, add = dispatched(queue_env, torrent=True)
    payload = tmp_path / "episode.mkv"
    payload.write_bytes(b"owned")
    rpc.files_by_gid = {CHILD: [{"path": str(payload)}]}
    rpc.status_result = {"status": "complete"}
    child = {"gid": CHILD, "following": LATE, "status": "complete" if snapshot == "stopped" else "active"}
    if snapshot == "followedBy":
        rpc.status_result["followedBy"] = [CHILD]
    elif snapshot == "late":
        rpc.on_removed = lambda fake, gid: fake.active_jobs.append(child) if gid == LATE else None
    else:
        getattr(rpc, {"active": "active_jobs", "waiting": "waiting_jobs", "stopped": "stopped_jobs"}[snapshot]).append(child)
    queue.remove(t.id, delete_file=True)
    add[3](LATE)
    drain(calls)
    assert rpc.removed == [LATE, CHILD], "late torrent removal cancelled parent only"
    assert not payload.exists() and _persisted_row(path, t.id) is None


@pytest.mark.parametrize("failure", ["parent", "child", "rescan"])
def test_T5_late_graph_failure_keeps_payload_copy_and_live_anchor(queue_env, monkeypatch, tmp_path, failure):
    queue, rpc, path, tid, raw, _ = _local_torrent_file(queue_env, monkeypatch, tmp_path)
    t = queue.tasks[tid]
    managed = Path(t.torrent_path)
    calls = hold(queue)
    begin_local_add(queue, tid)
    add = calls.pop()
    # Exercise the metadata graph with a managed copy that must survive failure.
    t.phase = PHASE_METADATA
    payload = tmp_path / "episode.mkv"
    payload.write_bytes(b"owned")
    rpc.files_by_gid = {CHILD: [{"path": str(payload)}]}
    probes = []
    def status(gid):
        probes.append(gid)
        if failure == "rescan" and len(probes) == 2:
            raise Aria2Error("rescan lost")
        return {"status": "complete", "followedBy": [CHILD]}
    rpc.tell_status = status
    rpc.remove_errors = {LATE if failure == "parent" else CHILD: Aria2Error("remove lost")}
    errors, removed = [], []
    queue.error.connect(errors.append)
    queue.task_removed.connect(removed.append)
    queue.remove(tid, delete_file=True)
    assert managed.exists(), "pre-GID removal discarded managed torrent"
    add[3](LATE)
    drain(calls)
    assert tid in queue.tasks and managed.exists() and payload.exists()
    assert errors and not removed
    assert t.gid == (LATE if failure == "parent" else CHILD)
    assert _persisted_row(path, tid)["gid"] == t.gid
    if failure != "parent":
        assert t.phase == ""


@pytest.mark.parametrize("torrent", [False, True])
def test_late_gid_pause_is_deferred_until_failed_removal_restores_task(queue_env, torrent):
    queue, rpc, path, t, calls, add = dispatched(queue_env, torrent)
    queue.pause(t.id)
    queue.remove(t.id)
    add[3](LATE)
    assert not rpc.paused and len(calls) == 1
    rpc.remove_errors = {LATE: Aria2Error("cancel lost")}
    drain(calls)
    assert t.id in queue.tasks and t.status == "paused" and rpc.paused == [LATE]


@pytest.mark.parametrize("route", ["direct", "probe", "magnet", "managed"])
def test_dispatch_passes_reserved_gid_to_each_rpc_route(queue_env, monkeypatch, tmp_path, route):
    if route == "managed":
        queue, rpc, path, tid, _, _ = _local_torrent_file(queue_env, monkeypatch, tmp_path)
        t = queue.tasks[tid]
    else:
        queue, rpc, path = queue_env(**_local_settings(
            all_debrid_enabled=False, intelligent_segments=route == "probe"
        ))
        t = queue.tasks[queue.add_url(MAGNET)] if route == "magnet" else task(queue)
        queue._running = queue._scheduler_allows = True
    seen = []
    def add_rpc(*args, gid=None, **kwargs):
        seen.append(gid)
        return gid
    rpc.add_uri = rpc.add_magnet = rpc.add_torrent = add_rpc
    monkeypatch.setattr(queue, "_bound_session", lambda: SimpleNamespace(
        head=lambda *args, **kwargs: SimpleNamespace(ok=False, headers={})
    ))
    calls = hold(queue)
    (begin_local_add(queue, t.id) if route in {"magnet", "managed"} else queue._launch(t))
    identity = t.launch_gid
    assert len(identity) == 16 and int(identity, 16) > 0 and identity == identity.lower()
    assert identity in queue._seen_gids and t.gid is None and not seen
    release(calls.pop())
    assert seen == [identity] and t.gid == identity and not t.launch_gid


def test_recovered_removal_identity_cannot_be_adopted_before_reconciliation(queue_env):
    queue, rpc, path, t, calls, add = dispatched(queue_env)
    queue.remove(t.id)
    identity = _persisted_row(path, t.id)["gid"]
    fresh, fresh_rpc, _ = queue_env(intelligent_segments=False)
    fresh_rpc.tell_external_snapshot = lambda: []
    pending = hold(fresh)
    fresh._check_external()
    pending.pop()[3]([{"gid": identity, "status": "active", "files": []}])
    assert not fresh.tasks, "external snapshot resurrected a durable removing identity"
    assert _persisted_row(path, t.id)["status"] == "removing"


def test_old_cancellation_callbacks_cannot_resolve_new_remove_transaction(queue_env):
    queue, rpc, path, t, calls, add = dispatched(queue_env)
    queue.remove(t.id)
    add[3](LATE)
    old = calls.pop()
    old[4]("cancel lost")
    drain(calls)
    queue.remove(t.id)
    current = calls.pop()
    old[3](())
    old[4]("duplicate old failure")
    assert queue.is_pending_removal(t.id) and _persisted_row(path, t.id)["status"] == "removing"
    release(current)
    assert t.id not in queue.tasks and _persisted_row(path, t.id) is None


@pytest.mark.parametrize("torrent", [False, True])
def test_ambiguous_admitted_gid_is_restored_when_cancellation_fails(queue_env, monkeypatch, torrent):
    queue, rpc, path, t, calls, add = dispatched(queue_env, torrent)
    identity = t.launch_gid
    rpc.tell_status = lambda gid: {"gid": gid, "status": "active"}
    rpc.remove_errors = {identity: Aria2Error("cancel lost")}
    queue.remove(t.id)
    add[4]("add reply lost")
    drain(calls)
    assert t.id in queue.tasks and t.gid == identity and t.status == "active" and t.error is None
    assert _persisted_row(path, t.id)["gid"] == identity
    assert not queue.is_pending_removal(t.id)
    if torrent:
        assert t.phase == PHASE_METADATA, "admitted parent mistaken for payload child"


@pytest.mark.parametrize("refused", [False, True])
def test_ambiguous_absent_metadata_parent_still_owns_payload_child(queue_env, monkeypatch, tmp_path, refused):
    queue, rpc, path, t, calls, add = dispatched(queue_env, torrent=True)
    monkeypatch.setattr(queue, "_ABSENCE_SETTLE_S", 0)
    identity = t.launch_gid
    probes = []
    def status(gid):
        probes.append(gid)
        raise absent(gid)
    rpc.tell_status = status
    rpc.stopped_jobs = [{"gid": CHILD, "following": identity, "status": "complete"}]
    payload = tmp_path / "episode.mkv"
    payload.write_bytes(b"owned")
    rpc.files_by_gid = {CHILD: [{"path": str(payload)}]}
    if refused:
        rpc.remove_errors = {CHILD: Aria2Error("child removal lost")}
    queue.remove(t.id, delete_file=True)
    add[4]("add reply lost")
    drain(calls)
    # Two absence probes of the parent; a removed child is then asked once
    # more, to wait for aria2 to let go of the files before they are deleted.
    assert probes == ([identity, identity] if refused else [identity, identity, CHILD])
    if refused:
        assert t.id in queue.tasks and payload.exists() and t.gid == CHILD and t.phase == ""
        assert _persisted_row(path, t.id)["gid"] == CHILD
    else:
        assert t.id not in queue.tasks and not payload.exists() and rpc.removed == [CHILD]


@pytest.mark.parametrize("torrent", [False, True])
def test_pending_removal_cannot_be_relaunched_or_resumed(queue_env, torrent):
    queue, rpc, path, t, calls, add = dispatched(queue_env, torrent)
    queue.remove(t.id)
    queue._launch(t)
    begin_local_add(queue, t.id)
    t.status = "queued"
    queue.force_start(t.id)
    queue._maybe_start_next()
    assert not calls and _persisted_row(path, t.id)["status"] == "removing"
    add[3](LATE)
    transaction = calls.pop()
    queue.pause(t.id)
    queue.resume(t.id)
    assert not calls and not rpc.paused and not rpc.unpaused
    release(transaction)
    assert t.id not in queue.tasks


def test_F1_retry_reuses_unproven_identity_and_retirement_releases_one_slot(queue_env):
    queue, rpc, path, t, calls, add = dispatched(queue_env)
    identity = t.launch_gid
    rpc.tell_status = lambda gid: (_ for _ in ()).throw(Aria2Error("transport lost"))
    queue.remove(t.id)
    add[4]("add reply lost")
    drain(calls)
    received = []
    def retry_add(*args, gid=None, **kwargs):
        received.append(gid)
        raise Aria2Error("duplicate GID: original transfer is still live")
    rpc.add_uri = retry_add
    queue.resume(t.id)
    assert len(calls) == 1, "F1: retry must use its existing capacity slot"
    release(calls.pop())
    assert received == [identity], "F1: retry abandoned the original backend identity"
    assert t.launch_gid == identity and _persisted_row(path, t.id)["gid"] == identity
    assert queue._active_count() == 1
    other = task(queue, url="https://example.com/next.zip")
    assert other.status == "queued" and not calls
    rpc.tell_status = lambda gid: {"gid": gid, "status": "active"}
    rpc.add_uri = lambda *args, **kwargs: "next-gid"
    queue.remove(t.id)
    transaction = calls.pop()
    release(transaction)
    assert rpc.removed == [identity] and other.status == "active" and len(calls) == 1
    transaction[3](())
    assert len(calls) == 1, "F1: duplicate retirement released capacity twice"


def test_F1_plain_add_failure_does_not_hold_capacity(queue_env):
    queue, rpc, path, t, calls, add = dispatched(queue_env)
    add[4]("explicit add failure")
    assert t.status == "error" and queue._active_count() == 0
    assert _persisted_row(path, t.id)["status"] == "error"


def test_F1_retry_of_same_gid_rejects_old_add_callback_while_retry_is_pending(queue_env):
    queue, rpc, path, t, calls, old = dispatched(queue_env)
    identity = t.launch_gid
    old[4]("add reply lost")
    queue.resume(t.id)
    current = calls.pop()
    assert t.launch_gid == identity, "F1: retry generated a replacement identity"
    old[3](LATE)
    old[4]("duplicate old failure")
    assert t.gid is None and t.id in queue._pending_launch
    current[3](identity)
    assert t.gid == identity and t.id not in queue._pending_launch


@pytest.mark.parametrize("torrent", [False, True])
@pytest.mark.parametrize("result", [None, "", {}], ids=["None", "empty", "object"])
def test_F3_malformed_add_result_reconciles_reserved_identity(queue_env, torrent, result):
    queue, rpc, path, t, calls, add = dispatched(queue_env, torrent)
    identity = t.launch_gid
    rpc.tell_status = lambda gid: (_ for _ in ()).throw(Aria2Error("transport lost"))
    errors = []
    queue.error.connect(errors.append)
    queue.remove(t.id)
    add[3](result)
    row = _persisted_row(path, t.id)
    assert row is not None and row["gid"] == identity, "F3: malformed add lost cancellation identity"
    assert t.gid is None and t.launch_gid == identity and calls
    drain(calls)
    assert t.id in queue.tasks and errors and rpc.removed == []
    assert t.error == "aria2 returned no download identity"
    assert _persisted_row(path, t.id)["gid"] == identity


@pytest.mark.parametrize("torrent", [False, True])
@pytest.mark.parametrize("result", [None, "", {}], ids=["None", "empty", "object"])
def test_F3_malformed_remove_result_refuses_cleanup(queue_env, tmp_path, torrent, result):
    queue, rpc, path, t, calls, add = dispatched(queue_env, torrent)
    payload = tmp_path / "payload.bin" if torrent else private_payload(t)
    if torrent:
        payload.write_bytes(b"owned")
        rpc.files_result = [{"path": str(payload)}]
    add[3](LATE)
    rpc.remove = lambda *args, **kwargs: result
    errors = []
    queue.error.connect(errors.append)
    queue.remove(t.id, delete_file=True)
    drain(calls)
    assert t.id in queue.tasks, "F3: malformed removal response retired ownership"
    assert payload.exists() and _persisted_row(path, t.id)["gid"] == LATE
    assert errors == ["aria2 did not confirm removal"]


@pytest.mark.parametrize("ambiguous", [False, True])
def test_F3_malformed_torrent_child_removal_retains_live_child_and_copy(queue_env, monkeypatch, tmp_path, ambiguous):
    queue, rpc, path, tid, _, _ = _local_torrent_file(queue_env, monkeypatch, tmp_path)
    t = queue.tasks[tid]
    managed = Path(t.torrent_path)
    calls = hold(queue)
    begin_local_add(queue, tid)
    add = calls.pop()
    t.phase = PHASE_METADATA
    parent = t.launch_gid if ambiguous else LATE
    rpc.tell_status = lambda gid: {"gid": gid, "status": "complete", "followedBy": [CHILD]}
    rpc.remove = lambda gid: None if gid == CHILD else gid
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"owned")
    rpc.files_by_gid = {CHILD: [{"path": str(payload)}]}
    errors = []
    queue.error.connect(errors.append)
    queue.remove(tid, delete_file=True)
    add[4]("add reply lost") if ambiguous else add[3](LATE)
    drain(calls)
    assert tid in queue.tasks, "F3: malformed child removal retired graph ownership"
    assert t.gid == CHILD and t.phase == "" and payload.exists() and managed.exists()
    assert _persisted_row(path, tid)["gid"] == CHILD and errors == ["aria2 did not confirm removal"]


def test_F3_ambiguous_uri_removal_needs_backend_confirmation(queue_env):
    queue, rpc, path, t, calls, add = dispatched(queue_env)
    identity = t.launch_gid
    payload = private_payload(t)
    rpc.tell_status = lambda gid: {"gid": gid, "status": "active"}
    rpc.remove = lambda gid: None
    queue.remove(t.id)
    add[4]("add reply lost")
    drain(calls)
    assert t.id in queue.tasks and t.gid == identity and payload.exists()
    assert _persisted_row(path, t.id)["gid"] == identity


def test_F1_found_identity_handoff_restores_ordinary_paused_capacity(queue_env):
    queue, rpc, path, t, calls, add = dispatched(queue_env)
    identity = t.launch_gid
    rpc.tell_status = lambda gid: (_ for _ in ()).throw(Aria2Error("transport lost"))
    queue.remove(t.id)
    add[4]("add reply lost")
    drain(calls)
    assert queue._active_count() == 1
    rpc.tell_status = lambda gid: {"gid": gid, "status": "active"}
    rpc.remove_errors = {identity: Aria2Error("cancel lost")}
    queue.remove(t.id)
    drain(calls)
    assert t.gid == identity and not t.launch_gid
    queue.pause(t.id)
    drain(calls)
    assert t.status == "paused" and queue._active_count() == 0, "resolved identity retained an unproven slot"


@pytest.mark.parametrize("torrent", [False, True])
def test_R1_failed_reconciliation_recovers_exact_identity_after_restart(queue_env, tmp_path, torrent):
    queue, rpc, path, t, calls, add = dispatched(queue_env, torrent)
    identity = t.launch_gid
    payload = tmp_path / "torrent-partial.bin" if torrent else private_payload(t)
    if torrent:
        payload.write_bytes(b"owned")
    rpc.tell_status = lambda gid: (_ for _ in ()).throw(Aria2Error("transport lost"))
    queue.remove(t.id)
    add[4]("add reply lost")
    drain(calls)
    assert t.status == "error" and t.id in queue._unreconciled and queue._active_count() == 1
    row = _persisted_row(path, t.id)
    assert row["status"] == "removing" and row["gid"] == identity, "R1: crash loses unreconciled ownership"
    fresh, fresh_rpc, _ = queue_env(intelligent_segments=False)
    assert t.id not in fresh.tasks and fresh._recovered_removals[0][1] == identity
    inline(fresh)
    fresh.resume_persisted()
    assert fresh_rpc.removed == [identity] and fresh_rpc.added == []
    assert _persisted_row(path, t.id) is None and payload.exists(), "recovery must keep files"


def test_R1_http_retry_dispatch_clears_durable_unreconciled_marker(queue_env):
    queue, rpc, path, t, calls, add = dispatched(queue_env)
    identity = t.launch_gid
    rpc.tell_status = lambda gid: (_ for _ in ()).throw(Aria2Error("transport lost"))
    queue.remove(t.id)
    add[4]("add reply lost")
    drain(calls)
    queue.resume(t.id)
    assert len(calls) == 1 and t.id not in queue._unreconciled
    assert t.launch_gid == identity and t.status == "active"
    row = _persisted_row(path, t.id)
    assert row["status"] == "active" and row["gid"] == identity, "R1: retry dispatch left a stale removal marker"


@pytest.mark.parametrize("entry", ["resume", "scheduler", "force", "launch"])
def test_R2_unreconciled_torrent_retry_cannot_reroute_owned_graph(queue_env, tmp_path, entry):
    from cove.queue import SOURCE_TORRENT
    queue, rpc, path, t, calls, add = dispatched(queue_env, torrent=True)
    identity = t.launch_gid
    rpc.tell_status = lambda gid: (_ for _ in ()).throw(Aria2Error("transport lost"))
    queue.remove(t.id)
    add[4]("add reply lost")
    drain(calls)
    errors = []
    queue.error.connect(errors.append)
    if entry == "resume":
        queue.resume(t.id)
    elif entry == "scheduler":
        t.status = "queued"
        queue._maybe_start_next()
    elif entry == "force":
        t.status = "queued"
        queue.force_start(t.id)
    else:
        queue._launch(t)
    assert not calls, "R2: unresolved torrent was sent through normal provider/add routing"
    assert t.status == "error" and t.id in queue._unreconciled and queue._active_count() == 1
    assert errors == ["Remove this download first; Cove could not confirm the previous attempt stopped."]
    assert t.source_type == SOURCE_TORRENT and t.launch_gid == identity
    payload = tmp_path / "episode.mkv"
    payload.write_bytes(b"owned")
    rpc.tell_status = lambda gid: {"gid": gid, "status": "complete", "followedBy": [CHILD]}
    rpc.files_by_gid = {CHILD: [{"path": str(payload)}]}
    queue.remove(t.id, delete_file=True)
    drain(calls)
    assert rpc.removed == [identity, CHILD], "R2: Remove lost the metadata-to-payload graph"
    assert not payload.exists() and t.id not in queue.tasks and _persisted_row(path, t.id) is None


@pytest.mark.parametrize("failed_reconciliation", [False, True])
def test_R2_cached_provider_cannot_materialize_over_reserved_torrent_identity(queue_env, failed_reconciliation):
    from cove.queue import SOURCE_TORRENT
    from tests.test_queue import _cached
    queue, rpc, path, t, calls, add = dispatched(queue_env, torrent=True)
    identity = t.launch_gid
    if failed_reconciliation:
        rpc.tell_status = lambda gid: (_ for _ in ()).throw(Aria2Error("transport lost"))
        queue.remove(t.id)
        add[4]("add reply lost")
        drain(calls)
    cached = _cached()
    queue._materialize_cached_torrent(t, cached, cached.files)
    assert t.source_type == SOURCE_TORRENT, "R2: cached-provider materialization abandoned torrent graph ownership"
    assert t.launch_gid == identity and list(queue.tasks) == [t.id] and not calls
    assert _persisted_row(path, t.id)["source_type"] == SOURCE_TORRENT
    if not failed_reconciliation:
        assert queue._pending_launch[t.id]["gid"] == identity


@pytest.mark.parametrize("entry", ["resume", "scheduler", "force", "launch"])
def test_repair3_cached_retry_after_plain_add_failure_settles_without_losing_identity(
    queue_env, monkeypatch, tmp_path, entry
):
    from cove import db, debrid
    from cove.queue import SOURCE_TORRENT
    from tests.test_queue import _cached
    queue, rpc, path, t, calls, add = dispatched(queue_env, torrent=True)
    identity = t.launch_gid
    add[4]("local add rejected")  # No Remove/reconciliation was attempted.
    assert t.status == "error" and t.id not in queue._unreconciled
    assert t.launch_gid == identity and queue._active_count() == 0
    probes, errors, changed = [], [], []
    def cached_provider(*args, **kwargs):
        probes.append(args[0])
        return _cached()
    monkeypatch.setattr(debrid, "resolve_torrent", cached_provider)
    queue.error.connect(errors.append)
    queue.task_changed.connect(changed.append)
    if entry == "resume":
        queue.resume(t.id)
    elif entry == "scheduler":
        t.status = "queued"
        queue._maybe_start_next()
    elif entry == "force":
        t.status = "queued"
        queue.force_start(t.id)
    else:
        queue._launch(t)
    assert len(calls) == 1 and calls[0][0] == queue._probe_torrent
    release(calls.pop())
    assert t.status == "error", "cached retry refusal left an ownerless active task"
    row = _persisted_row(path, t.id)
    assert row["status"] == "error" and row["gid"] == identity
    assert queue._active_count() == 0 and t.id not in queue._pending_launch
    assert t.gid is None and t.launch_gid == identity and t.source_type == SOURCE_TORRENT
    assert t.error == errors[-1] == queue._UNRECONCILED_TORRENT_ERROR
    assert changed[-1] == t.id and probes == [t.info_hash]
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0] == 1
    assert not calls and not rpc.added and not rpc.magnets and not rpc.torrents
    payload = tmp_path / "episode.mkv"
    payload.write_bytes(b"owned")
    rpc.tell_status = lambda gid: {"gid": gid, "status": "complete", "followedBy": [CHILD]}
    rpc.files_by_gid = {CHILD: [{"path": str(payload)}]}
    queue.remove(t.id, delete_file=True)
    drain(calls)
    assert rpc.removed == [identity, CHILD] and not payload.exists()
    assert t.id not in queue.tasks and _persisted_row(path, t.id) is None


# ---- Cleanup waits for aria2 to let go (Windows delete-pending race) -------
# aria2.forceRemove answers before aria2 has closed the file. On Windows a
# file deleted while aria2 still holds it stays "delete pending", so cleanup's
# emptiness check fails with error 5 and the work directory is left behind.


def test_removal_cleans_up_only_after_aria2_reports_the_transfer_stopped(queue_env, monkeypatch):
    queue, rpc, path, t, calls, add = dispatched(queue_env)
    add[3](EXPECTED)
    payload = private_payload(t)
    monkeypatch.setattr(queue, "_REMOVAL_POLL_S", 0)
    statuses = iter(["active", "active", "removed"])
    seen = []
    rpc.tell_status = lambda gid: seen.append(payload.exists()) or {"gid": gid, "status": next(statuses)}

    queue.remove(t.id, delete_file=True)
    drain(calls)

    assert rpc.removed == [EXPECTED]
    assert seen == [True, True, True], "cleanup ran before aria2 reported the transfer stopped"
    assert not payload.exists() and t.id not in queue.tasks


@pytest.mark.parametrize("answer", ["stuck", "error", "gone"])
def test_removal_still_completes_when_aria2_never_reports_stopped(queue_env, monkeypatch, answer):
    queue, rpc, path, t, calls, add = dispatched(queue_env)
    add[3](EXPECTED)
    payload = private_payload(t)
    monkeypatch.setattr(queue, "_REMOVAL_SETTLE_S", 0.05)
    monkeypatch.setattr(queue, "_REMOVAL_POLL_S", 0.01)
    def status(gid):
        if answer == "error":
            raise Aria2Error("transport lost")
        if answer == "gone":
            raise absent(gid)
        return {"gid": gid, "status": "active"}
    rpc.tell_status = status

    queue.remove(t.id, delete_file=True)
    drain(calls)

    assert rpc.removed == [EXPECTED]
    assert not payload.exists() and t.id not in queue.tasks
    assert _persisted_row(path, t.id) is None


def test_torrent_removal_deletes_files_only_after_aria2_reports_stopped(queue_env, monkeypatch, tmp_path):
    queue, rpc, path, t, calls, add = dispatched(queue_env, torrent=True)
    add[3](EXPECTED)
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"owned torrent bytes")
    rpc.files_result = [{"path": str(payload)}]
    monkeypatch.setattr(queue, "_REMOVAL_POLL_S", 0)
    events = []
    remove = rpc.remove
    rpc.remove = lambda gid, force=True: events.append("remove") or remove(gid, force)
    def status(gid):
        # Still stopping on the first look after the removal, gone after that.
        answer = "removed" if events.count("active-after-remove") else "active"
        if "remove" in events and answer == "active":
            events.append("active-after-remove")
        events.append(answer)
        return {"gid": gid, "status": answer, "followedBy": []}
    rpc.tell_status = status
    delete = queue._delete_torrent_files
    monkeypatch.setattr(queue, "_delete_torrent_files",
                        lambda paths, base: events.append("delete") or delete(paths, base))

    queue.remove(t.id, delete_file=True)
    drain(calls)

    assert "removed" in events[:events.index("delete")], "files deleted before aria2 reported the transfer stopped"
    assert not payload.exists() and t.id not in queue.tasks
