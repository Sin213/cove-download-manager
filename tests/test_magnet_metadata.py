"""Magnet metadata resolution, and the precommit ownership it runs inside.

Three properties carry the weight. *Ownership comes first*: the hash is known
from the URI, so the duplicate decision is made and the hash claimed before
anything is written. *The metainfo is untrusted*: peer-supplied bytes are
bounded, contained, parsed by the existing hardened parser and refused unless
they hash to the torrent that was asked for. *Nothing durable exists until
someone commits*: no row, task, probe, payload, chosen files, resolver gid or
temp directory left behind.
"""
import os
import sqlite3
import tempfile

import pytest

from cove import config, debrid, torrent as torrent_mod
from cove.aria2 import Aria2Error, Aria2RPC, Aria2RpcError
from cove.debrid import DebridError
from cove.queue import (
    MAGNET_CANCELLED,
    MAGNET_DUPLICATE,
    MAGNET_ERROR,
    MAGNET_METADATA_POLL_S,
    MAGNET_METADATA_TIMEOUT_S,
    MAGNET_SUCCESS,
    MAGNET_TIMEOUT,
    MAGNET_WORKSPACE_PREFIX,
    SOURCE_TORRENT,
    TORRENT_PREFLIGHT_PENDING,
    TorrentPreflight,
)
from cove.torrent import TorrentError

# Fixture reuse: the real QueueManager environment lives in the queue suite.
from tests.test_queue import queue_env  # noqa: F401
from tests.test_queue import (
    _FakeRpc,
    _local_settings,
    _multi_file_torrent_bytes,
    _rows,
    _sync_spawn,
    _uncached,
)

_RESOLVING = "Cove is already fetching this torrent's file list."
_UNAVAILABLE = "Cove could not read this torrent's file list from the network."
_TIMED_OUT = ("This torrent's file list did not arrive in time. The magnet may "
              "have no peers online right now.")
_MISMATCH = "This magnet's file list is for a different torrent, so it was not used."
_QUEUED = "That torrent is already in Cove's queue."


# --- fixtures --------------------------------------------------------------


def _fixture_bytes(name=b"Resolver Fixture"):
    """A three-file torrent: A.bin, B.bin, Folder/C.bin at indexes 0/1/2."""
    return _multi_file_torrent_bytes(name, (
        (10, (b"A.bin",)), (20, (b"B.bin",)), (30, (b"Folder", b"C.bin")),
    ))


def _other_bytes():
    """A second torrent with a different info hash."""
    return _multi_file_torrent_bytes(b"Other Fixture", (
        (11, (b"D.bin",)), (22, (b"E.bin",)),
    ))


def _magnet(info_hash, *, dn=None, trackers=("http://127.0.0.1:1/announce",)):
    uri = f"magnet:?xt=urn:btih:{info_hash}"
    if dn:
        uri += f"&dn={dn}"
    for tr in trackers:
        uri += f"&tr={tr}"
    return uri


def _write_torrent(tmp_path, raw, name="fixture.torrent"):
    path = tmp_path / name
    path.write_bytes(raw)
    return str(path)


def _env(queue_env, monkeypatch, tmp_path, **settings):
    """A queue with torrent support on, workers inline and no debrid."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    queue, rpc, db_path = queue_env(**_local_settings(**settings))
    _sync_spawn(queue)
    _uncached(queue, monkeypatch)
    return queue, rpc, db_path


def _probe_counter(queue, monkeypatch):
    """Count every provider probe the queue would make."""
    calls = []
    monkeypatch.setattr(
        debrid, "resolve_torrent", lambda *a, **k: (calls.append(a), None)[1])
    return calls


def _workspaces():
    """Every resolver workspace currently on disk under the temp root."""
    root = tempfile.gettempdir()
    return [os.path.join(root, name) for name in os.listdir(root)
            if name.startswith(MAGNET_WORKSPACE_PREFIX)]


# --- the aria2 double ------------------------------------------------------


class _MetadataRpc(_FakeRpc):
    """aria2's metadata-only contract, as measured against 1.37.0."""

    def __init__(self):
        super().__init__()
        self.plans: dict[str, dict] = {}
        self.metadata_added: list[dict] = []
        self.results_removed: list[str] = []
        self.remove_attempts: list[str] = []
        self.result_attempts: list[str] = []
        self.surviving_results: set[str] = set()
        self.purged: set[str] = set()
        self.metadata_status_calls: list[str] = []
        self._jobs: dict[str, dict] = {}
        self._counter = 0

    def plan(self, info_hash, **kwargs):
        self.plans[info_hash] = kwargs
        return self

    def add_magnet_metadata(self, uri, out_dir):
        info_hash = torrent_mod.parse_magnet(uri).info_hash
        plan = self.plans.get(info_hash, {})
        if plan.get("add_error"):
            raise Aria2Error("aria2 said something that quotes the magnet")
        self._counter += 1
        gid = plan.get("gid") or f"gid-meta-{self._counter}"
        self.metadata_added.append(
            {"uri": uri, "out_dir": out_dir, "gid": gid, "info_hash": info_hash})
        self._jobs[gid] = {"info_hash": info_hash, "out_dir": out_dir,
                           "plan": plan, "pending": int(plan.get("pending", 0))}
        return gid

    def tell_status(self, gid):
        self.metadata_status_calls.append(gid)
        if gid in self.purged:
            # Exactly what aria2 1.37.0 answers for an unknown gid: an error
            # object, code 1, "GID <gid> is not found".
            raise Aria2RpcError("aria2.tellStatus", 1, f"GID {gid} is not found")
        job = self._jobs.get(gid)
        if job is None:
            return super().tell_status(gid)
        if job["plan"].get("probe_unreachable") and gid in self.result_attempts:
            # What production raises when the daemon cannot be reached: a
            # plain Aria2Error with no aria2 error object behind it. It says
            # nothing about whether the result entry still exists.
            raise Aria2Error("RPC transport error: connection refused")
        if job["pending"] > 0:
            job["pending"] -= 1
            return {"gid": gid, "status": "active"}
        plan = job["plan"]
        hook = plan.get("on_complete")
        if hook is not None:
            # The seam completion races are arranged through: this happens
            # between aria2 answering and the resolver acting on the answer.
            hook()
        self._write_artifact(job)
        status = {"gid": gid, "status": plan.get("status", "complete")}
        if plan.get("followed_by"):
            status["followedBy"] = list(plan["followed_by"])
        return status

    def remove(self, gid, force=True):
        plan = self._jobs.get(gid, {}).get("plan", {})
        if plan.get("remove_error"):
            raise Aria2Error("could not remove")
        if plan.get("remove_fails_once") and gid not in self.remove_attempts:
            # Transient: the first attempt fails, a retry would succeed.
            self.remove_attempts.append(gid)
            raise Aria2Error("could not remove, this time")
        self.remove_attempts.append(gid)
        self.removed.append(gid)
        if plan.get("purge_absent"):
            # The completed-metadata contract: aria2's own forceRemove ->
            # removeDownloadResult fallback already purged the entry.
            self.purged.add(gid)
        return gid

    def remove_download_result(self, gid):
        plan = self._jobs.get(gid, {}).get("plan", {})
        self.result_attempts.append(gid)
        if plan.get("purge_fails_once") and self.result_attempts.count(gid) == 1:
            # The entry is still there; only the purge call failed.
            self.surviving_results.add(gid)
            raise Aria2Error("could not purge the result, this time")
        if plan.get("purge_absent"):
            # aria2 already purged it inside `remove`; asking again is an error.
            raise Aria2Error("No such download for GID#" + gid)
        self.surviving_results.discard(gid)
        self.purged.add(gid)
        self.results_removed.append(gid)
        return gid

    def _write_artifact(self, job):
        plan, out_dir = job["plan"], job["out_dir"]
        if plan.get("writer") is not None:
            plan["writer"](out_dir, job["info_hash"])
            return
        payload = plan.get("payload")
        if plan.get("no_artifact") or payload is None:
            return
        name = plan.get("artifact_name") or f"{job['info_hash']}.torrent"
        with open(os.path.join(out_dir, name), "wb") as fh:
            fh.write(payload)
        for extra, blob in (plan.get("extra_artifacts") or {}).items():
            with open(os.path.join(out_dir, extra), "wb") as fh:
                fh.write(blob)


def _metadata_env(queue_env, monkeypatch, tmp_path, **settings):
    """A queue whose RPC models the pinned metadata-only contract."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    queue, _rpc, db_path = queue_env(**_local_settings(**settings))
    rpc = _MetadataRpc()
    queue.rpc = rpc
    _sync_spawn(queue)
    _uncached(queue, monkeypatch)
    return queue, rpc, db_path


def _deferred_spawn(queue):
    """Hold worker calls so a test can complete them in whatever order."""
    pending = []

    def spawn(fn, *args, on_done=None, on_fail=None, **kwargs):
        pending.append((fn, args, kwargs, on_done, on_fail))

    queue._spawn = spawn

    def run(index):
        fn, args, kwargs, on_done, on_fail = pending[index]
        try:
            result = fn(*args, **kwargs)
        except (Aria2Error, DebridError, TorrentError) as exc:
            if on_fail is not None:
                on_fail(str(exc))
        else:
            if on_done is not None:
                on_done(result)

    return pending, run


def _resolve(queue, magnet, **kwargs):
    """Drive one resolution and collect whichever result it delivered."""
    seen = {"calls": 0}

    def resolved(request):
        seen["calls"] += 1
        seen["request"] = request

    def failed(resolution):
        seen["calls"] += 1
        seen["failed"] = resolution

    handle = queue.resolve_magnet_preflight(
        magnet, on_resolved=resolved, on_failed=failed, **kwargs)
    return handle, seen


def _planned(queue, rpc, raw, **plan):
    """Plan a successful resolution for `raw` and return its metadata."""
    meta = torrent_mod.parse_torrent(raw)
    plan.setdefault("payload", raw)
    rpc.plan(meta.info_hash, **plan)
    return meta


def _resolved_request(queue, rpc, tmp_path, raw=None):
    raw = raw or _fixture_bytes()
    meta = _planned(queue, rpc, raw)
    _handle, seen = _resolve(queue, _magnet(meta.info_hash), out_dir=str(tmp_path))
    return seen["request"], meta


# --- Gate 0: a held local `.torrent` preflight owns its info hash -----------
#
# Tab 5 could not establish whether a matching magnet was refused *because*
# the local `.torrent` preflight was held, or merely because of when it ran.
# These two remove the ambiguity: the hold is asserted through the queue's
# own ownership state at the instant the magnet is submitted, from inside
# the precommit callback. They passed before the resolver existed.


def test_matching_magnet_is_refused_while_the_local_preflight_is_actually_held(
    queue_env, monkeypatch, tmp_path
):
    queue, rpc, db_path = _env(queue_env, monkeypatch, tmp_path)
    probes = _probe_counter(queue, monkeypatch)
    raw = _fixture_bytes()
    meta = torrent_mod.parse_torrent(raw)
    source = _write_torrent(tmp_path, raw)
    errors = []
    queue.error.connect(errors.append)
    observed = {}

    def precommit(request):
        # The dialog is "open" here: the request exists, the hold has been
        # taken, and nothing has committed or discarded it. Proved from the
        # queue's ownership state, not from the file on disk.
        observed["held"] = request.metadata.info_hash in queue._preflight_hashes
        observed["live"] = queue._live_torrent(request.metadata.info_hash)
        observed["managed"] = os.path.isfile(request.prepared.torrent_path)
        observed["request"] = request
        observed["magnet_result"] = queue.add_url(_magnet(meta.info_hash))

    queue.add_torrent_file(source, out_dir=str(tmp_path), precommit=precommit)

    assert observed["held"] is True
    assert observed["live"] is True
    assert observed["managed"] is True
    assert observed["magnet_result"] is None
    assert _rows(db_path) == []
    assert rpc.magnets == []
    assert rpc.torrents == []
    assert probes == []
    assert errors == [_QUEUED]

    # The held request is untouched and can still finish either way.
    request = observed["request"]
    assert meta.info_hash in queue._preflight_hashes
    assert os.path.isfile(request.prepared.torrent_path)
    queue.discard_torrent_preflight(request)
    assert meta.info_hash not in queue._preflight_hashes
    assert not os.path.exists(request.prepared.torrent_path)


def test_an_unrelated_magnet_is_not_blocked_by_a_held_preflight(queue_env, monkeypatch, tmp_path):
    """Control for the test above: the hold is per torrent, not global."""
    queue, rpc, db_path = _env(queue_env, monkeypatch, tmp_path)
    held = torrent_mod.parse_torrent(_fixture_bytes())
    other = torrent_mod.parse_torrent(_other_bytes())
    assert held.info_hash != other.info_hash
    source = _write_torrent(tmp_path, _fixture_bytes())
    observed = {}

    def precommit(request):
        observed["held"] = request.metadata.info_hash in queue._preflight_hashes
        observed["other_result"] = queue.add_url(_magnet(other.info_hash))

    queue.add_torrent_file(source, out_dir=str(tmp_path), precommit=precommit)

    assert observed["held"] is True
    # Hash B is admitted on its own merits while hash A is held.
    assert observed["other_result"] is not None
    rows = _rows(db_path)
    assert len(rows) == 1
    assert rows[0]["info_hash"] == other.info_hash


# --- magnet identity: the existing contract, and only that ------------------


def test_the_resolver_uses_coves_existing_canonical_info_hash(queue_env, monkeypatch, tmp_path):
    """Hex and base32 spellings of one torrent are one torrent."""
    import base64

    queue, rpc, _db = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta = _planned(queue, rpc, _fixture_bytes())
    b32 = base64.b32encode(bytes.fromhex(meta.info_hash)).decode("ascii")
    assert len(b32) == 32

    handle, seen = _resolve(queue, _magnet(b32))

    assert handle.info_hash == meta.info_hash
    assert handle.state == MAGNET_SUCCESS
    assert seen["request"].metadata.info_hash == meta.info_hash
    # And the hex spelling of the same torrent is now a duplicate of it.
    second, _seen2 = _resolve(queue, _magnet(meta.info_hash.upper()))
    assert second.state == MAGNET_DUPLICATE
    assert len(rpc.metadata_added) == 1


@pytest.mark.parametrize("uri", [
    "https://example.invalid/not-a-magnet",
    "magnet:?dn=No+Topic+At+All",
    "magnet:?xt=urn:btih:tooshort",
    "magnet:?xt=urn:btih:" + "z" * 40,
    "magnet:?xt=urn:btmh:1220" + "ab" * 32,
    "magnet:?xt=urn:btih:" + "a" * 40 + "&xt.1=urn:btih:" + "b" * 40,
])
def test_a_magnet_cove_does_not_accept_never_reaches_aria2(queue_env, monkeypatch, tmp_path, uri):
    """Refused by the parser Cove already had, before anything exists to clean."""
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    errors = []
    queue.error.connect(errors.append)
    before = set(_workspaces())

    handle, seen = _resolve(queue, uri)

    assert handle.state == MAGNET_ERROR
    assert seen["calls"] == 1
    assert seen["failed"] is handle
    assert len(errors) == 1
    assert rpc.metadata_added == []
    assert rpc.magnets == []
    assert rpc.torrents == []
    assert _rows(db_path) == []
    assert queue._preflight_hashes == set()
    assert queue._magnet_resolutions == {}
    assert set(_workspaces()) == before


def test_a_resolution_nobody_can_receive_is_refused_before_any_work(
    queue_env, monkeypatch, tmp_path
):
    """A held preflight with no owner would leak its hash and managed copy."""
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta = _planned(queue, rpc, _fixture_bytes())
    failures = []
    before = set(_workspaces())

    handle = queue.resolve_magnet_preflight(
        _magnet(meta.info_hash), on_failed=failures.append)

    assert handle.state == MAGNET_ERROR
    assert failures == [handle]
    assert rpc.metadata_added == []
    assert queue._preflight_hashes == set()
    assert queue._magnet_resolutions == {}
    assert set(_workspaces()) == before
    assert _rows(db_path) == []
    # And the torrent is still free for a caller that can own the result.
    later, later_seen = _resolve(queue, _magnet(meta.info_hash))
    assert later.state == MAGNET_SUCCESS
    assert later_seen["request"].metadata.info_hash == meta.info_hash


def test_a_workspace_that_cannot_be_created_still_ends_in_a_terminal_state(
    queue_env, monkeypatch, tmp_path
):
    """`state` is what the failure callback reports, so it can never be empty."""
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta = _planned(queue, rpc, _fixture_bytes())
    errors = []
    queue.error.connect(errors.append)
    monkeypatch.setattr(
        tempfile, "mkdtemp",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no space left on /srv/x")))

    handle, seen = _resolve(queue, _magnet(meta.info_hash))

    assert handle.state == MAGNET_ERROR
    assert seen["failed"] is handle
    assert seen["calls"] == 1
    # One of Cove's own sentences: the OSError text carries a path.
    assert errors == [_UNAVAILABLE]
    assert "/srv/x" not in errors[0]
    assert rpc.metadata_added == []
    assert queue._preflight_hashes == set()
    assert _rows(db_path) == []


@pytest.mark.parametrize("failure", ["store", "prepare"])
def test_a_failure_after_the_metadata_arrives_reports_error_not_success(
    queue_env, monkeypatch, tmp_path, failure
):
    """A failure after the cancel window closes still reports ERROR, not SUCCESS."""
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta = _planned(queue, rpc, _fixture_bytes())
    if failure == "store":
        monkeypatch.setattr(
            torrent_mod, "store_managed_torrent",
            lambda m: (_ for _ in ()).throw(TorrentError("no room for the copy")))
    else:
        monkeypatch.setattr(queue, "prepare_url", lambda *a, **k: None)

    handle, seen = _resolve(queue, _magnet(meta.info_hash))

    assert handle.state == MAGNET_ERROR
    assert handle.state != MAGNET_SUCCESS
    assert seen["calls"] == 1
    assert seen["failed"] is handle
    assert "request" not in seen
    # And it is cleaned up like any other failure.
    assert queue._preflight_hashes == set()
    assert queue._magnet_resolutions == {}
    assert _rows(db_path) == []
    assert queue.tasks == {}
    assert not os.path.exists(rpc.metadata_added[0]["out_dir"])


def test_a_magnet_is_refused_while_bittorrent_support_is_off(queue_env, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    queue, _stub, _db = queue_env(torrent_support_enabled=False)
    rpc = _MetadataRpc()
    queue.rpc = rpc
    _sync_spawn(queue)

    handle, _seen = _resolve(queue, _magnet("a" * 40))

    assert handle.state == MAGNET_ERROR
    assert rpc.metadata_added == []


# --- ownership before publication ------------------------------------------


def test_the_hash_is_claimed_before_any_managed_copy_is_written(queue_env, monkeypatch, tmp_path):
    """The ordering this whole slice exists to get right."""
    queue, rpc, _db = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta = _planned(queue, rpc, _fixture_bytes())
    order = []
    real_store = torrent_mod.store_managed_torrent

    def spy(m):
        order.append(("publish", m.info_hash in queue._preflight_hashes))
        return real_store(m)

    monkeypatch.setattr(torrent_mod, "store_managed_torrent", spy)
    real_add = rpc.add_magnet_metadata

    def add_spy(uri, out_dir):
        order.append(("fetch", torrent_mod.parse_magnet(uri).info_hash
                      in queue._preflight_hashes))
        return real_add(uri, out_dir)

    rpc.add_magnet_metadata = add_spy
    _resolve(queue, _magnet(meta.info_hash))

    # Held before the fetch, still held at publication, publication last.
    assert order == [("fetch", True), ("publish", True)]


def test_a_duplicate_publishes_nothing_and_starts_no_metadata_job(queue_env, monkeypatch, tmp_path):
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta = _planned(queue, rpc, _fixture_bytes())
    published = []
    real_store = torrent_mod.store_managed_torrent
    monkeypatch.setattr(
        torrent_mod, "store_managed_torrent",
        lambda m: (published.append(m.info_hash), real_store(m))[1])
    first, _first_seen = _resolve(queue, _magnet(meta.info_hash))
    assert first.state == MAGNET_SUCCESS
    assert published == [meta.info_hash]

    # Second request for a torrent the first one now holds as a preflight.
    second, second_seen = _resolve(queue, _magnet(meta.info_hash))

    assert second.state == MAGNET_DUPLICATE
    assert second_seen["calls"] == 1
    assert "request" not in second_seen
    assert len(rpc.metadata_added) == 1
    assert published == [meta.info_hash]
    assert _rows(db_path) == []


def test_a_held_local_torrent_preflight_blocks_a_matching_resolver_request(queue_env, monkeypatch, tmp_path):
    """The Gate 0 containment, now from the resolver's side of the door."""
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    raw = _fixture_bytes()
    meta = torrent_mod.parse_torrent(raw)
    source = _write_torrent(tmp_path, raw)
    errors = []
    queue.error.connect(errors.append)
    observed = {}

    def precommit(request):
        observed["held"] = request.metadata.info_hash in queue._preflight_hashes
        observed["handle"], _ = _resolve(queue, _magnet(meta.info_hash))
        observed["request"] = request

    queue.add_torrent_file(source, out_dir=str(tmp_path), precommit=precommit)

    assert observed["held"] is True
    assert observed["handle"].state == MAGNET_DUPLICATE
    # Refused as a held preflight, not as a queued torrent.
    assert errors == [TORRENT_PREFLIGHT_PENDING]
    assert rpc.metadata_added == []
    assert _rows(db_path) == []
    assert os.path.isfile(observed["request"].prepared.torrent_path)


def test_a_resolved_preflight_blocks_a_matching_local_torrent_and_an_ordinary_magnet(
    queue_env, monkeypatch, tmp_path
):
    """The same guard the other way round."""
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    raw = _fixture_bytes()
    meta = _planned(queue, rpc, raw)
    _handle, seen = _resolve(queue, _magnet(meta.info_hash))
    request = seen["request"]
    managed_before = open(request.prepared.torrent_path, "rb").read()
    errors = []
    queue.error.connect(errors.append)

    committed = []
    queue.add_torrent_file(_write_torrent(tmp_path, raw), out_dir=str(tmp_path),
                           precommit=committed.append)
    assert committed == []
    assert errors[-1] == TORRENT_PREFLIGHT_PENDING

    assert queue.add_url(_magnet(meta.info_hash)) is None
    assert errors[-1] == _QUEUED

    # Nothing was created and the resolved copy is byte for byte intact.
    assert _rows(db_path) == []
    assert rpc.magnets == []
    assert rpc.torrents == []
    assert open(request.prepared.torrent_path, "rb").read() == managed_before
    assert queue._preflight_hashes == {meta.info_hash}


def test_discarding_a_resolved_preflight_frees_the_torrent_for_a_new_request(
    queue_env, monkeypatch, tmp_path
):
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta = _planned(queue, rpc, _fixture_bytes())
    _handle, seen = _resolve(queue, _magnet(meta.info_hash))
    request = seen["request"]

    queue.discard_torrent_preflight(request)

    assert queue._preflight_hashes == set()
    assert queue._magnet_resolutions == {}
    assert not os.path.exists(request.prepared.torrent_path)
    assert _rows(db_path) == []

    # No permanent leak: the same magnet is admissible again.
    again, _again_seen = _resolve(queue, _magnet(meta.info_hash))
    assert again.state == MAGNET_SUCCESS
    assert len(rpc.metadata_added) == 2


def test_a_second_resolver_for_one_hash_never_starts_a_second_metadata_job(queue_env, monkeypatch, tmp_path):
    """Two requests for one torrent while the first is still fetching."""
    queue, rpc, _db = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta = _planned(queue, rpc, _fixture_bytes())
    errors = []
    queue.error.connect(errors.append)
    pending, run = _deferred_spawn(queue)

    first, first_seen = _resolve(queue, _magnet(meta.info_hash))
    second, second_seen = _resolve(queue, _magnet(meta.info_hash))

    # The second never reached a worker, and is told the accurate thing:
    # nobody is waiting to choose files yet, the list is still coming.
    assert len(pending) == 1
    assert second.state == MAGNET_DUPLICATE
    assert second_seen["calls"] == 1
    assert errors == [_RESOLVING]
    assert rpc.metadata_added == []

    run(0)
    assert first.state == MAGNET_SUCCESS
    assert len(rpc.metadata_added) == 1
    assert first_seen["request"].metadata.info_hash == meta.info_hash


def test_two_different_torrents_resolve_independently_in_either_order(queue_env, monkeypatch, tmp_path):
    """No shared "current resolution" slot to cross-assign anything through."""
    queue, rpc, _db = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta_a = _planned(queue, rpc, _fixture_bytes())
    meta_b = _planned(queue, rpc, _other_bytes())
    pending, run = _deferred_spawn(queue)

    handle_a, seen_a = _resolve(queue, _magnet(meta_a.info_hash))
    handle_b, seen_b = _resolve(queue, _magnet(meta_b.info_hash))
    assert len(pending) == 2

    run(1)  # B finishes first.
    run(0)

    assert seen_a["request"].metadata.info_hash == meta_a.info_hash
    assert seen_b["request"].metadata.info_hash == meta_b.info_hash
    assert seen_a["request"].metadata.name == "Resolver Fixture"
    assert seen_b["request"].metadata.name == "Other Fixture"
    assert handle_a.state == handle_b.state == MAGNET_SUCCESS
    assert len({a["gid"] for a in rpc.metadata_added}) == 2
    assert len({a["out_dir"] for a in rpc.metadata_added}) == 2
    assert queue._preflight_hashes == {meta_a.info_hash, meta_b.info_hash}


def test_failing_one_resolution_leaves_the_other_untouched(queue_env, monkeypatch, tmp_path):
    queue, rpc, _db = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta_a = _planned(queue, rpc, _fixture_bytes(), gid="gid-A")
    meta_b = _planned(queue, rpc, _other_bytes(), gid="gid-B", pending=1)
    pending, run = _deferred_spawn(queue)
    handle_a, _ = _resolve(queue, _magnet(meta_a.info_hash))
    handle_b, seen_b = _resolve(queue, _magnet(meta_b.info_hash))

    handle_a.cancel()
    run(0)
    after_a = list(rpc.removed)
    run(1)

    assert handle_a.state == MAGNET_CANCELLED
    assert handle_b.state == MAGNET_SUCCESS
    assert seen_b["request"].metadata.info_hash == meta_b.info_hash
    # A's cleanup touched nothing of B's - not its gid, and not its hold.
    assert after_a == []  # A never got as far as adding a gid.
    assert "gid-B" not in after_a
    assert rpc.removed == ["gid-B"]  # B removed only its own, at its own end.
    assert queue._preflight_hashes == {meta_b.info_hash}
    assert queue._magnet_resolutions == {}


# --- the aria2 request: metadata only, per request, in our own directory ----


def test_the_metadata_job_is_added_with_exactly_the_pinned_options(monkeypatch):
    """Measured against aria2 1.37.0, not remembered."""
    calls = []
    monkeypatch.setattr(
        Aria2RPC, "_call",
        lambda self, method, params=(): (calls.append((method, params)), "g")[1])
    wrapper = Aria2RPC.__new__(Aria2RPC)

    wrapper.add_magnet_metadata("magnet:?xt=urn:btih:" + "a" * 40, "/tmp/ws-x")

    assert calls == [("aria2.addUri", [
        ["magnet:?xt=urn:btih:" + "a" * 40],
        {"dir": "/tmp/ws-x", "bt-metadata-only": "true",
         "bt-save-metadata": "true", "seed-time": "0"},
    ])]


def test_the_ordinary_magnet_and_torrent_wrapper_calls_are_unchanged(monkeypatch):
    """Resolver options must not leak into the routes that already existed."""
    calls = []
    monkeypatch.setattr(
        Aria2RPC, "_call",
        lambda self, method, params=(): (calls.append((method, params)), "g")[1])
    wrapper = Aria2RPC.__new__(Aria2RPC)

    wrapper.add_magnet("magnet:?xt=urn:btih:" + "a" * 40, "/downloads")
    wrapper.add_torrent(b"data", "/downloads")

    assert calls[0] == ("aria2.addUri", [
        ["magnet:?xt=urn:btih:" + "a" * 40],
        {"dir": "/downloads", "seed-time": "0"},
    ])
    method, params = calls[1]
    assert method == "aria2.addTorrent"
    assert params[2] == {"dir": "/downloads", "seed-time": "0"}
    for _method, params in calls:
        opts = params[-1]
        assert "bt-metadata-only" not in opts
        assert "bt-save-metadata" not in opts


def test_each_request_gets_its_own_workspace_outside_every_user_directory(queue_env, monkeypatch, tmp_path):
    queue, rpc, _db = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta_a = _planned(queue, rpc, _fixture_bytes())
    meta_b = _planned(queue, rpc, _other_bytes())
    out_dir = tmp_path / "user-download"
    out_dir.mkdir()

    _resolve(queue, _magnet(meta_a.info_hash), out_dir=str(out_dir))
    _resolve(queue, _magnet(meta_b.info_hash), out_dir=str(out_dir))

    dirs = [add["out_dir"] for add in rpc.metadata_added]
    assert len(dirs) == 2
    assert dirs[0] != dirs[1]
    for workspace in dirs:
        assert os.path.basename(workspace).startswith(MAGNET_WORKSPACE_PREFIX)
        # Never the destination, the download directory, or anything under them.
        assert not workspace.startswith(str(out_dir))
        assert not workspace.startswith(str(tmp_path))


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
def test_nothing_a_resolution_does_ever_touches_the_user_destination(
    queue_env, monkeypatch, tmp_path, outcome
):
    """Tab 5 saw aria2 write its saved `.torrent` beside a download and"""
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    out_dir = tmp_path / "user-download"
    out_dir.mkdir()
    keep = out_dir / "KEEP.txt"
    keep.write_text("sentinel")
    raw = _fixture_bytes()
    meta = torrent_mod.parse_torrent(raw)

    if outcome == "success":
        rpc.plan(meta.info_hash, payload=raw)
        _resolve(queue, _magnet(meta.info_hash), out_dir=str(out_dir))
    elif outcome == "failure":
        rpc.plan(meta.info_hash, add_error=True)
        _resolve(queue, _magnet(meta.info_hash), out_dir=str(out_dir))
    else:
        rpc.plan(meta.info_hash, payload=raw, pending=3)
        _pending, run = _deferred_spawn(queue)
        handle, _seen = _resolve(
            queue, _magnet(meta.info_hash), out_dir=str(out_dir))
        handle.cancel()
        run(0)

    assert sorted(os.listdir(out_dir)) == ["KEEP.txt"]
    assert keep.read_text() == "sentinel"
    assert _rows(db_path) == []


# --- the metainfo is untrusted remote input --------------------------------


def _discovery_plan(case, raw):
    """One way the workspace can fail to hold exactly the expected artifact."""
    if case == "missing":
        return {"no_artifact": True}
    if case == "ambiguous":
        # "First" and "newest" are both guesses, and a guess here is a torrent.
        return {"payload": raw,
                "extra_artifacts": {"aaaa-decoy.torrent": _other_bytes()}}
    # Valid metainfo, filed under a name the contract never uses.
    return {"payload": raw, "artifact_name": "downloaded.torrent"}


@pytest.mark.parametrize("case", ["missing", "ambiguous", "wrong_name"])
def test_anything_but_the_one_expected_artifact_fails_closed(queue_env, monkeypatch, tmp_path, case):
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    raw = _fixture_bytes()
    meta = torrent_mod.parse_torrent(raw)
    rpc.plan(meta.info_hash, **_discovery_plan(case, raw))
    errors = []
    queue.error.connect(errors.append)

    handle, seen = _resolve(queue, _magnet(meta.info_hash))

    assert handle.state == MAGNET_ERROR
    assert "request" not in seen
    assert _rows(db_path) == []
    assert queue._preflight_hashes == set()
    assert rpc.removed == [rpc.metadata_added[0]["gid"]]
    assert not os.path.exists(rpc.metadata_added[0]["out_dir"])
    assert len(errors) == 1


def test_a_filename_is_never_proof_of_which_torrent_this_is(queue_env, monkeypatch, tmp_path):
    """Named `<expected hash>.torrent`, containing a different torrent."""
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    wanted = torrent_mod.parse_torrent(_fixture_bytes())
    impostor = _other_bytes()
    assert torrent_mod.parse_torrent(impostor).info_hash != wanted.info_hash
    rpc.plan(wanted.info_hash, payload=impostor)
    published = []
    monkeypatch.setattr(torrent_mod, "store_managed_torrent",
                        lambda m: published.append(m.info_hash))
    errors = []
    queue.error.connect(errors.append)

    handle, seen = _resolve(queue, _magnet(wanted.info_hash))

    assert handle.state == MAGNET_ERROR
    assert "request" not in seen
    assert published == []  # Refused before publication, not after.
    assert _rows(db_path) == []
    assert queue._preflight_hashes == set()
    assert not os.path.exists(rpc.metadata_added[0]["out_dir"])
    assert errors == [_MISMATCH]


@pytest.mark.parametrize("case", ["symlink_out", "symlink_in", "directory"])
def test_the_artifact_must_be_a_contained_regular_file(queue_env, monkeypatch, tmp_path, case):
    """A link is refused whichever way it points, and so is a directory."""
    queue, rpc, _db = _metadata_env(queue_env, monkeypatch, tmp_path)
    raw = _fixture_bytes()
    meta = torrent_mod.parse_torrent(raw)
    outside = tmp_path / "outside.torrent"
    outside.write_bytes(raw)

    def writer(workspace, info_hash):
        link = os.path.join(workspace, f"{info_hash}.torrent")
        if case == "directory":
            os.mkdir(link)
        elif case == "symlink_out":
            os.symlink(str(outside), link)
        else:
            # Valid metainfo, right hash, one indirection away.
            target = os.path.join(workspace, "payload.bin")
            with open(target, "wb") as fh:
                fh.write(raw)
            os.symlink(target, link)

    rpc.plan(meta.info_hash, writer=writer)

    handle, seen = _resolve(queue, _magnet(meta.info_hash))

    assert handle.state == MAGNET_ERROR
    assert "request" not in seen
    assert queue._preflight_hashes == set()
    # Cleanup removes the workspace, so it removes the link - never what the
    # link pointed at, which was not this request's to touch.
    assert outside.exists()
    assert outside.read_bytes() == raw
    assert not os.path.exists(rpc.metadata_added[0]["out_dir"])


def test_peer_supplied_metainfo_is_bounded_before_it_is_read(queue_env, monkeypatch, tmp_path):
    """The existing `.torrent` ceiling, applied to the new remote input."""
    queue, rpc, _db = _metadata_env(queue_env, monkeypatch, tmp_path)
    raw = _fixture_bytes()
    meta = torrent_mod.parse_torrent(raw)
    rpc.plan(meta.info_hash, payload=raw)

    # Exactly at the limit: accepted, because it is otherwise valid.
    monkeypatch.setattr(torrent_mod, "MAX_TORRENT_BYTES", len(raw))
    handle, seen = _resolve(queue, _magnet(meta.info_hash))
    assert handle.state == MAGNET_SUCCESS
    assert seen["request"].metadata.info_hash == meta.info_hash
    queue.discard_torrent_preflight(seen["request"])

    # One byte over: refused, and refused without reading the file.
    reads = []
    real_open = open

    def counting_open(path, mode="r", *a, **k):
        # Only reads: the double writes the artifact through open() too.
        if str(path).endswith(".torrent") and "r" in mode:
            reads.append(str(path))
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr(torrent_mod, "MAX_TORRENT_BYTES", len(raw) - 1)
    monkeypatch.setattr("builtins.open", counting_open)
    handle2, seen2 = _resolve(queue, _magnet(meta.info_hash))
    monkeypatch.undo()

    assert handle2.state == MAGNET_ERROR
    assert "request" not in seen2
    assert reads == []  # Never opened, so never read - not truncated.
    assert queue._preflight_hashes == set()


def test_metainfo_that_the_existing_parser_refuses_is_refused_here_too(queue_env, monkeypatch, tmp_path):
    """No fast path, no partial parser, no relaxed limits for aria2 output."""
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta = torrent_mod.parse_torrent(_fixture_bytes())
    hostile = _multi_file_torrent_bytes(
        b"Hostile", ((10, (b"..", b"..", b"etc", b"passwd")),))
    with pytest.raises(TorrentError):
        torrent_mod.parse_torrent(hostile)
    rpc.plan(meta.info_hash, payload=hostile)

    handle, seen = _resolve(queue, _magnet(meta.info_hash))

    assert handle.state == MAGNET_ERROR
    assert "request" not in seen
    assert _rows(db_path) == []
    assert queue._preflight_hashes == set()


def test_corrupt_metainfo_is_refused(queue_env, monkeypatch, tmp_path):
    queue, rpc, _db = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta = torrent_mod.parse_torrent(_fixture_bytes())
    rpc.plan(meta.info_hash, payload=b"not bencode at all")

    handle, seen = _resolve(queue, _magnet(meta.info_hash))

    assert handle.state == MAGNET_ERROR
    assert "request" not in seen
    assert not os.path.exists(rpc.metadata_added[0]["out_dir"])


# --- success: a preflight, and nothing else at all -------------------------


def test_a_resolved_magnet_produces_the_ordinary_torrent_preflight(queue_env, monkeypatch, tmp_path):
    """The same record a local `.torrent` produces, in the same registry."""
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    probes = _probe_counter(queue, monkeypatch)
    meta = _planned(queue, rpc, _fixture_bytes())
    out_dir = tmp_path / "user-download"
    out_dir.mkdir()

    handle, seen = _resolve(
        queue, _magnet(meta.info_hash, dn="Fake+Display+Name"),
        out_dir=str(out_dir), intake="manual")
    request = seen["request"]

    assert handle.state == MAGNET_SUCCESS
    assert seen["calls"] == 1
    assert isinstance(request, TorrentPreflight)
    # The name comes from the metainfo, never from the magnet's `dn`.
    assert request.metadata.name == "Resolver Fixture"
    assert "Fake" not in request.metadata.name
    assert [f.relative_path for f in request.metadata.files] == [
        "A.bin", "B.bin", "Folder/C.bin"]
    assert request.metadata.info_hash == meta.info_hash
    # A prepared request, not a task: no file choice has been made yet.
    assert request.prepared.source_type == SOURCE_TORRENT
    assert request.prepared.selected_files is None
    assert request.prepared.out_dir == str(out_dir)
    assert request.prepared.torrent_name == "Resolver Fixture"
    assert torrent_mod.is_managed_torrent_path(request.prepared.torrent_path)

    # Precommit means precommit.
    assert _rows(db_path) == []
    assert queue.tasks == {}
    assert probes == []
    assert rpc.torrents == []
    assert rpc.magnets == []
    assert rpc.added == []
    # And the hash is held, by the preflight now rather than the resolution.
    assert queue._preflight_hashes == {meta.info_hash}
    assert queue._magnet_resolutions == {}


def test_the_original_magnet_survives_resolution_with_its_trackers(queue_env, monkeypatch, tmp_path):
    """Discovery context is not incidental: the payload route needs it."""
    queue, rpc, _db = _metadata_env(queue_env, monkeypatch, tmp_path)
    trackers = ("http://127.0.0.1:6969/announce", "http://127.0.0.1:7070/announce")
    body = _multi_file_torrent_bytes(b"Tracked Fixture", ((10, (b"A.bin",)),))
    tiers = b"".join(
        b"l" + str(len(t)).encode() + b":" + t.encode() + b"e" for t in trackers)
    # The shape the real 1.37.0 run produced: trackers in announce-list.
    raw = b"d13:announce-listl" + tiers + b"e4:info" + body[len(b"d4:info"):]
    meta = _planned(queue, rpc, raw)
    magnet = _magnet(meta.info_hash, dn="Ignored", trackers=trackers)

    _handle, seen = _resolve(queue, magnet)
    request = seen["request"]

    # 1. The prepared request keeps the magnet exactly as it was accepted.
    assert request.prepared.url == magnet
    for tracker in trackers:
        assert tracker in request.prepared.url
    assert request.prepared.url != torrent_mod.minimal_magnet(meta.info_hash)

    # 2. And the managed copy - the bytes the payload add uses - carries them.
    stored = torrent_mod.read_managed_torrent(
        request.prepared.torrent_path, meta.info_hash)
    root, _span = torrent_mod.bdecode_root(stored)
    announced = [url.decode() for tier in root.get(b"announce-list", [])
                 for url in tier]
    assert announced == list(trackers)


def test_success_leaves_no_resolver_state_and_the_preflight_still_works(queue_env, monkeypatch, tmp_path):
    """Everything temporary is gone *before* success is reported."""
    queue, rpc, _db = _metadata_env(queue_env, monkeypatch, tmp_path)
    raw = _fixture_bytes()
    meta = _planned(queue, rpc, raw, followed_by=["gid-child-1", "gid-child-2"])

    _handle, seen = _resolve(queue, _magnet(meta.info_hash))
    request = seen["request"]

    gid = rpc.metadata_added[0]["gid"]
    workspace = rpc.metadata_added[0]["out_dir"]
    # The root gid, every descendant its own status named, and the result
    # entries - all of them, and only them.
    assert set(rpc.removed) == {gid, "gid-child-1", "gid-child-2"}
    assert set(rpc.results_removed) == {gid, "gid-child-1", "gid-child-2"}
    assert not os.path.exists(workspace)

    # Nothing the preflight needs lived in there.
    assert not request.prepared.torrent_path.startswith(workspace)
    assert os.path.isfile(request.prepared.torrent_path)
    assert torrent_mod.read_managed_torrent(
        request.prepared.torrent_path, meta.info_hash) == raw
    assert request.metadata.raw_bytes == raw


def test_a_resolution_never_removes_an_unrelated_aria2_job(queue_env, monkeypatch, tmp_path):
    queue, rpc, _db = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta = _planned(queue, rpc, _fixture_bytes(), gid="gid-resolver")
    # A download the user started, which the resolver knows nothing about.
    rpc._jobs["gid-user"] = {"info_hash": "", "out_dir": "", "plan": {},
                             "pending": 0}

    _resolve(queue, _magnet(meta.info_hash))

    assert rpc.removed == ["gid-resolver"]
    assert "gid-user" not in rpc.removed
    assert "gid-user" not in rpc.results_removed


def test_a_success_whose_cleanup_leaked_is_not_reported_as_a_success(queue_env, monkeypatch, tmp_path):
    """Valid metainfo plus a stranded aria2 job is not a clean result."""
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta = _planned(queue, rpc, _fixture_bytes(), remove_error=True)
    published = []
    monkeypatch.setattr(torrent_mod, "store_managed_torrent",
                        lambda m: published.append(m.info_hash))

    handle, seen = _resolve(queue, _magnet(meta.info_hash))

    assert handle.state == MAGNET_ERROR
    assert "request" not in seen
    assert published == []
    assert _rows(db_path) == []
    assert queue._preflight_hashes == set()


def test_a_resource_whose_removal_failed_stays_owned_so_the_retry_can_free_it(
    queue_env, monkeypatch, tmp_path
):
    """Cleanup gives up ownership per resource, and only once it is really gone.

    Dropping the bookkeeping on the first attempt would make the second pass a
    no-op: the aria2 job would survive with nothing left pointing at it.
    """
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta = _planned(queue, rpc, _fixture_bytes(), remove_fails_once=True)

    handle, seen = _resolve(queue, _magnet(meta.info_hash))
    gid = rpc.metadata_added[0]["gid"]

    # The first pass failed, so this is not reported as a clean success...
    assert handle.state == MAGNET_ERROR
    assert "request" not in seen
    # ...but the gid was still owned, so the retry actually removed it.
    assert rpc.remove_attempts == [gid, gid]
    assert rpc.removed == [gid]
    assert rpc.results_removed == [gid]
    assert not os.path.exists(rpc.metadata_added[0]["out_dir"])
    assert queue._preflight_hashes == set()
    assert _rows(db_path) == []


def test_an_already_purged_result_is_not_mistaken_for_a_failed_purge(
    queue_env, monkeypatch, tmp_path
):
    """The ordinary completed-metadata case: `remove` purged it already.

    Asking a second time is an error, and it is the benign one - aria2 has no
    record of the gid - so cleanup is clean and the resolution succeeds.
    """
    queue, rpc, _db = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta = _planned(queue, rpc, _fixture_bytes(), purge_absent=True)

    handle, seen = _resolve(queue, _magnet(meta.info_hash))
    gid = rpc.metadata_added[0]["gid"]

    assert handle.state == MAGNET_SUCCESS
    assert seen["request"].metadata.info_hash == meta.info_hash
    assert rpc.removed == [gid]
    assert rpc.results_removed == []  # It was already gone.
    assert rpc.surviving_results == set()
    assert not os.path.exists(rpc.metadata_added[0]["out_dir"])


def test_a_result_entry_that_survives_a_failed_purge_stays_owned_for_the_retry(
    queue_env, monkeypatch, tmp_path
):
    """The other reading of the same error, told apart by asking aria2.

    Here the entry really is still there, so releasing the gid would strand
    it with nothing left pointing at it. Ownership is kept and the retry
    pass purges it.
    """
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta = _planned(queue, rpc, _fixture_bytes(), purge_fails_once=True)

    handle, seen = _resolve(queue, _magnet(meta.info_hash))
    gid = rpc.metadata_added[0]["gid"]

    # The first pass could not confirm the entry was gone, so not a success.
    assert handle.state == MAGNET_ERROR
    assert "request" not in seen
    # The gid stayed owned, and the retry actually purged the entry.
    assert rpc.result_attempts == [gid, gid]
    assert rpc.results_removed == [gid]
    assert rpc.surviving_results == set()
    assert gid in rpc.purged
    assert not os.path.exists(rpc.metadata_added[0]["out_dir"])
    assert queue._preflight_hashes == set()
    assert _rows(db_path) == []


def test_a_backend_that_cannot_be_asked_leaves_the_gid_owned(
    queue_env, monkeypatch, tmp_path
):
    """Silence is not proof of absence: an unanswerable probe keeps ownership.

    The probe raises the plain `Aria2Error` a real transport failure produces,
    which carries no statement from aria2 about the download at all.
    """
    queue, rpc, _db = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta = _planned(queue, rpc, _fixture_bytes(),
                    purge_fails_once=True, probe_unreachable=True)

    handle, seen = _resolve(queue, _magnet(meta.info_hash))
    gid = rpc.metadata_added[0]["gid"]

    assert handle.state == MAGNET_ERROR
    assert "request" not in seen
    # The probe could not confirm anything, so the entry is still ours and
    # the retry pass tried it again.
    assert rpc.result_attempts == [gid, gid]
    assert queue._preflight_hashes == set()


def test_aria2_distinguishes_a_missing_gid_from_a_failure_to_reach_it(monkeypatch):
    """The wrapper distinction the whole cleanup decision rests on.

    Pinned against aria2 1.37.0: an unknown gid comes back as a JSON-RPC error
    object, and an unreachable daemon produces no error object at all.
    """
    calls = []

    def fake_post(self, url, json=None, timeout=None):
        calls.append(json["method"])

        class R:
            @staticmethod
            def json():
                return {"error": {"code": 1,
                                  "message": "GID 0000000000000000 is not found"}}
        return R()

    monkeypatch.setattr("requests.Session.post", fake_post)
    wrapper = Aria2RPC.__new__(Aria2RPC)
    wrapper.url = "http://127.0.0.1:1/jsonrpc"
    wrapper.secret = "s"
    wrapper.timeout = 1
    wrapper._local = __import__("threading").local()

    with pytest.raises(Aria2RpcError) as caught:
        wrapper.tell_status("0000000000000000")
    assert caught.value.code == 1
    assert caught.value.gid_not_found() is True
    assert isinstance(caught.value, Aria2Error)  # Existing callers still catch it.

    # A transport failure is an Aria2Error, but never an Aria2RpcError, and
    # so can never be read as "this download is gone".
    def boom(self, url, json=None, timeout=None):
        raise __import__("requests").RequestException("connection refused")

    monkeypatch.setattr("requests.Session.post", boom)
    with pytest.raises(Aria2Error) as transport:
        wrapper.tell_status("0000000000000000")
    assert not isinstance(transport.value, Aria2RpcError)

    # An error body that is not an object at all is still an error, not a
    # crash, and still cannot be read as a statement about the download.
    def odd(self, url, json=None, timeout=None):
        class R:
            @staticmethod
            def json():
                return {"error": "something unstructured"}
        return R()

    monkeypatch.setattr("requests.Session.post", odd)
    with pytest.raises(Aria2Error) as unstructured:
        wrapper.tell_status("0000000000000000")
    assert not isinstance(unstructured.value, Aria2RpcError)


@pytest.mark.parametrize("how", ["timeout", "bad_metadata"])
def test_a_failed_resolution_gets_the_same_cleanup_retry_as_a_successful_one(
    queue_env, monkeypatch, tmp_path, how
):
    """A transient cleanup failure must not strand a job just because the
    resolution itself had already failed."""
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta = torrent_mod.parse_torrent(_fixture_bytes())
    plan = {"remove_fails_once": True}
    if how == "timeout":
        plan["pending"] = 10_000
        kwargs = {"timeout_s": 0.0, "poll_s": 0.0}
    else:
        plan["payload"] = b"not bencode at all"
        kwargs = {}
    rpc.plan(meta.info_hash, **plan)

    handle, seen = _resolve(queue, _magnet(meta.info_hash), **kwargs)
    gid = rpc.metadata_added[0]["gid"]

    assert handle.state == (MAGNET_TIMEOUT if how == "timeout" else MAGNET_ERROR)
    assert "request" not in seen
    # The first removal failed; the retry on the failure path still freed it.
    assert rpc.remove_attempts == [gid, gid]
    assert rpc.removed == [gid]
    assert not os.path.exists(rpc.metadata_added[0]["out_dir"])
    assert queue._preflight_hashes == set()
    assert _rows(db_path) == []


def test_a_failure_whose_cleanup_also_failed_keeps_the_original_failure(queue_env, monkeypatch, tmp_path):
    queue, rpc, _db = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta = torrent_mod.parse_torrent(_fixture_bytes())
    rpc.plan(meta.info_hash, no_artifact=True, remove_error=True)
    errors = []
    queue.error.connect(errors.append)

    handle, seen = _resolve(queue, _magnet(meta.info_hash))

    assert handle.state == MAGNET_ERROR
    assert "request" not in seen
    # The metadata failure is what the user is told about, not the removal.
    assert errors == [_UNAVAILABLE]
    assert queue._preflight_hashes == set()


# --- the preflight the caller gets is one it already knows how to use -------


def test_discarding_a_resolved_preflight_uses_the_existing_cancel_path(queue_env, monkeypatch, tmp_path):
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    request, _meta = _resolved_request(queue, rpc, tmp_path)
    managed = request.prepared.torrent_path
    assert os.path.isfile(managed)

    queue.discard_torrent_preflight(request)

    assert not os.path.exists(managed)
    assert queue._preflight_hashes == set()
    assert _rows(db_path) == []
    assert queue.tasks == {}
    assert rpc.torrents == []


def test_a_resolved_preflight_commits_every_file_by_the_legacy_route(queue_env, monkeypatch, tmp_path):
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    request, _meta = _resolved_request(queue, rpc, tmp_path)
    queue._running = True
    queue._scheduler_allows = True

    tid = queue.commit_torrent_preflight(request, selected_files=None)
    queue._launch(queue.tasks[tid])

    rows = _rows(db_path)
    assert len(rows) == 1
    assert rows[0]["selected_files"] == ""
    # Byte for byte the call Cove made before selection existed.
    assert rpc.torrents[0]["select_file"] is None
    assert queue._preflight_hashes == set()


def test_a_resolved_preflight_commits_a_subset_through_the_reviewed_conversion(
    queue_env, monkeypatch, tmp_path
):
    """The proof that resolved metainfo is usable, not merely displayable."""
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    request, _meta = _resolved_request(queue, rpc, tmp_path)
    queue._running = True
    queue._scheduler_allows = True

    tid = queue.commit_torrent_preflight(request, selected_files=(0, 2))
    queue._launch(queue.tasks[tid])

    rows = _rows(db_path)
    assert len(rows) == 1
    assert rows[0]["selected_files"] == "0,2"
    assert rpc.torrents[0]["select_file"] == "1,3"


# --- bounded waiting, cancellation and shutdown ----------------------------


def test_metadata_that_never_arrives_times_out_and_cleans_up(queue_env, monkeypatch, tmp_path):
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta = torrent_mod.parse_torrent(_fixture_bytes())
    rpc.plan(meta.info_hash, pending=10_000)  # Never completes.
    errors = []
    queue.error.connect(errors.append)

    # The deadline is injected, so nothing here sleeps for a minute.
    handle, seen = _resolve(
        queue, _magnet(meta.info_hash), timeout_s=0.0, poll_s=0.0)

    assert handle.state == MAGNET_TIMEOUT
    assert seen["calls"] == 1
    assert "request" not in seen
    assert errors == [_TIMED_OUT]
    gid = rpc.metadata_added[0]["gid"]
    assert rpc.removed == [gid]
    assert rpc.results_removed == [gid]
    assert not os.path.exists(rpc.metadata_added[0]["out_dir"])
    assert queue._preflight_hashes == set()
    assert queue._magnet_resolutions == {}
    assert _rows(db_path) == []


def test_the_default_timeout_is_the_named_constant(queue_env, monkeypatch, tmp_path):
    """A resolution called with no deadline still has one."""
    queue, rpc, _db = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta = torrent_mod.parse_torrent(_fixture_bytes())
    rpc.plan(meta.info_hash, no_artifact=True)
    seen_args = []
    real = queue._spawn

    def spy(fn, *args, **kwargs):
        seen_args.append(args)
        return real(fn, *args, **kwargs)

    queue._spawn = spy
    _resolve(queue, _magnet(meta.info_hash))

    assert seen_args[0][2] == MAGNET_METADATA_TIMEOUT_S == 60.0
    assert seen_args[0][3] == MAGNET_METADATA_POLL_S


def test_waiting_for_metadata_is_a_bounded_wait_not_a_spin(queue_env, monkeypatch, tmp_path):
    """One status call and one wait per cycle, and no more."""
    queue, rpc, _db = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta = _planned(queue, rpc, _fixture_bytes(), pending=3)

    handle, _seen = _resolve(queue, _magnet(meta.info_hash), poll_s=0.0)

    assert handle.state == MAGNET_SUCCESS
    # Three "active" answers then the completion: four calls, three waits.
    assert len(rpc.metadata_status_calls) == 4
    assert handle.waits == 3


def test_cancelling_interrupts_the_wait_instead_of_polling_on(queue_env, monkeypatch, tmp_path):
    queue, rpc, _db = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta = torrent_mod.parse_torrent(_fixture_bytes())
    rpc.plan(meta.info_hash, pending=1000)
    handles = {}
    real_tell = rpc.tell_status

    def tell(gid):
        result = real_tell(gid)
        if len(rpc.metadata_status_calls) == 1:
            handles["handle"].cancel()
        return result

    rpc.tell_status = tell
    _pending, run = _deferred_spawn(queue)
    # A poll interval long enough that sleeping through even one of them
    # would take this test half a minute.
    handle, _seen = _resolve(queue, _magnet(meta.info_hash), poll_s=30.0)
    handles["handle"] = handle
    run(0)

    assert handle.state == MAGNET_CANCELLED
    # Stopped at the cancellation rather than polling on, and the 30-second
    # wait returned the moment it was cancelled rather than being slept out.
    assert len(rpc.metadata_status_calls) == 1
    assert handle.waits == 1


def test_cancelling_mid_resolution_delivers_one_cancelled_result_and_cleans_up(
    queue_env, monkeypatch, tmp_path
):
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta = _planned(queue, rpc, _fixture_bytes(), pending=10)
    errors = []
    queue.error.connect(errors.append)
    handles = {}
    real_tell = rpc.tell_status

    def tell(gid):
        result = real_tell(gid)
        if len(rpc.metadata_status_calls) == 1:
            # Cancelled with the gid live and the workspace on disk.
            handles["handle"].cancel()
        return result

    rpc.tell_status = tell
    _pending, run = _deferred_spawn(queue)
    handle, seen = _resolve(queue, _magnet(meta.info_hash), poll_s=0.0)
    handles["handle"] = handle
    run(0)

    assert handle.state == MAGNET_CANCELLED
    assert seen["calls"] == 1
    assert "request" not in seen
    # A cancellation is the caller's own doing; it needs no error sentence.
    assert errors == []
    assert queue._preflight_hashes == set()
    assert queue._magnet_resolutions == {}
    assert _rows(db_path) == []
    gid = rpc.metadata_added[0]["gid"]
    assert rpc.removed == [gid]
    assert rpc.results_removed == [gid]
    assert not os.path.exists(rpc.metadata_added[0]["out_dir"])


def test_cancelling_before_the_add_is_idempotent_and_never_reaches_aria2(queue_env, monkeypatch, tmp_path):
    """The earliest cancellation there is, asked for three times."""
    queue, rpc, _db = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta = _planned(queue, rpc, _fixture_bytes(), pending=2)
    before = set(_workspaces())
    _pending, run = _deferred_spawn(queue)
    handle, seen = _resolve(queue, _magnet(meta.info_hash), poll_s=0.0)

    assert handle.cancel() is True
    assert handle.cancel() is False  # Already settled; no second outcome.
    assert handle.cancel() is False
    run(0)

    assert handle.state == MAGNET_CANCELLED
    assert seen["calls"] == 1
    assert "request" not in seen
    assert rpc.metadata_added == []
    assert rpc.removed == []
    assert set(_workspaces()) == before
    assert queue._preflight_hashes == set()


def test_cancelling_after_the_artifact_arrives_still_publishes_nothing(queue_env, monkeypatch, tmp_path):
    """The race that matters: metainfo in hand, cancellation wins anyway."""
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    raw = _fixture_bytes()
    meta = torrent_mod.parse_torrent(raw)
    handles = {}
    rpc.plan(meta.info_hash, payload=raw,
             on_complete=lambda: handles["handle"].cancel())
    published = []
    monkeypatch.setattr(torrent_mod, "store_managed_torrent",
                        lambda m: published.append(m.info_hash))
    _pending, run = _deferred_spawn(queue)
    handle, seen = _resolve(queue, _magnet(meta.info_hash), poll_s=0.0)
    handles["handle"] = handle
    run(0)

    assert handle.state == MAGNET_CANCELLED
    assert seen["calls"] == 1
    assert "request" not in seen
    assert published == []
    assert _rows(db_path) == []
    assert queue._preflight_hashes == set()
    assert not os.path.exists(rpc.metadata_added[0]["out_dir"])


def test_cancelling_after_success_cannot_undo_the_preflight(queue_env, monkeypatch, tmp_path):
    """Once ownership has transferred, Cancel belongs to the preflight."""
    queue, rpc, _db = _metadata_env(queue_env, monkeypatch, tmp_path)
    request, meta = _resolved_request(queue, rpc, tmp_path)

    # The resolution is long settled and no longer an owner of anything.
    assert queue._magnet_resolutions.get(meta.info_hash) is None
    assert queue._preflight_hashes == {meta.info_hash}
    assert os.path.isfile(request.prepared.torrent_path)

    queue.discard_torrent_preflight(request)
    assert queue._preflight_hashes == set()
    assert not os.path.exists(request.prepared.torrent_path)


def test_a_completion_arriving_after_cancellation_is_ignored(queue_env, monkeypatch, tmp_path):
    """A worker that finished anyway cannot resurrect a cancelled request."""
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    raw = _fixture_bytes()
    meta = _planned(queue, rpc, raw)
    published = []
    real_store = torrent_mod.store_managed_torrent
    monkeypatch.setattr(
        torrent_mod, "store_managed_torrent",
        lambda m: (published.append(m.info_hash), real_store(m))[1])
    pending, _run = _deferred_spawn(queue)
    handle, seen = _resolve(queue, _magnet(meta.info_hash))
    _fn, _args, _kwargs, on_done, on_fail = pending[0]

    handle.cancel()
    # A success lands late, as it would if the worker had got all the way to
    # the end before the cancellation was seen.
    on_done(torrent_mod.parse_torrent(raw))

    assert handle.state == MAGNET_CANCELLED
    assert "request" not in seen
    assert published == []  # No managed copy, so nothing to clean up.
    assert _rows(db_path) == []
    assert queue.tasks == {}
    # Still held: this request has not delivered its own result yet.
    assert queue._preflight_hashes == {meta.info_hash}

    # The worker's real result arrives and releases the hold - exactly once.
    on_fail("ignored")
    assert seen["calls"] == 1
    assert seen["failed"] is handle
    assert queue._preflight_hashes == set()
    assert queue._magnet_resolutions == {}
    # A second late delivery changes nothing.
    on_fail("ignored")
    on_done(torrent_mod.parse_torrent(raw))
    assert seen["calls"] == 1
    assert published == []
    assert _rows(db_path) == []


def test_a_completion_arriving_after_a_timeout_is_ignored(queue_env, monkeypatch, tmp_path):
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    raw = _fixture_bytes()
    meta = _planned(queue, rpc, raw, pending=10_000)
    handle, seen = _resolve(
        queue, _magnet(meta.info_hash), timeout_s=0.0, poll_s=0.0)
    assert handle.state == MAGNET_TIMEOUT

    queue._on_magnet_resolved(
        handle, torrent_mod.parse_torrent(raw), str(tmp_path), "manual",
        lambda r: seen.__setitem__("request", r), None)

    assert "request" not in seen
    assert _rows(db_path) == []
    assert queue._preflight_hashes == set()


def test_shutting_down_stops_every_resolution_and_releases_every_hold(queue_env, monkeypatch, tmp_path):
    """Nothing in flight can publish into a queue that is going away."""
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta_a = _planned(queue, rpc, _fixture_bytes())
    meta_b = _planned(queue, rpc, _other_bytes())
    _pending, run = _deferred_spawn(queue)
    handle_a, seen_a = _resolve(queue, _magnet(meta_a.info_hash))
    handle_b, seen_b = _resolve(queue, _magnet(meta_b.info_hash))
    assert queue._preflight_hashes == {meta_a.info_hash, meta_b.info_hash}

    queue.cancel_magnet_resolutions()

    assert handle_a.state == handle_b.state == MAGNET_CANCELLED
    assert queue._preflight_hashes == set()
    assert queue._magnet_resolutions == {}

    # The workers still run, but nothing is published and nothing is
    # delivered: there is no longer anyone to hand a preflight to.
    run(0)
    run(1)
    assert "request" not in seen_a
    assert "request" not in seen_b
    assert seen_a["calls"] == seen_b["calls"] == 0
    assert _rows(db_path) == []
    assert queue.tasks == {}
    assert queue._preflight_hashes == set()


def test_shutdown_beats_a_success_that_has_already_passed_the_cancel_point(
    queue_env, monkeypatch, tmp_path
):
    """The one race `cancel` deliberately loses, and shutdown must not.

    The worker has finished and closed the cancellation window, but its
    completion has not reached the GUI thread yet. Releasing the hash without
    claiming that pending success would leave the torrent unowned with a
    publication still in flight - and then publish it.
    """
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta = _planned(queue, rpc, _fixture_bytes())
    published = []
    real_store = torrent_mod.store_managed_torrent
    monkeypatch.setattr(
        torrent_mod, "store_managed_torrent",
        lambda m: (published.append(m.info_hash), real_store(m))[1])
    pending, _run = _deferred_spawn(queue)
    handle, seen = _resolve(queue, _magnet(meta.info_hash))

    # Run the worker, but hold its completion back.
    fn, args, kwargs, on_done, _on_fail = pending[0]
    result = fn(*args, **kwargs)
    assert handle.pending_success is True
    assert handle.cancel() is False  # Past the point an ordinary cancel wins.

    queue.cancel_magnet_resolutions()

    assert handle.state == MAGNET_CANCELLED
    assert handle.pending_success is False
    assert queue._preflight_hashes == set()
    assert queue._magnet_resolutions == {}

    # The completion finally lands, and publishes nothing.
    on_done(result)

    assert "request" not in seen
    assert seen["calls"] == 0
    assert published == []
    assert _rows(db_path) == []
    assert queue.tasks == {}
    assert queue._preflight_hashes == set()
    # And the torrent is free again for a later run of the app.
    assert queue._live_torrent(meta.info_hash) is False


def test_a_backend_failure_ends_the_resolution_cleanly(queue_env, monkeypatch, tmp_path):
    """aria2's own words never reach the user: they may quote the magnet."""
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta = torrent_mod.parse_torrent(_fixture_bytes())
    rpc.plan(meta.info_hash, add_error=True)
    errors = []
    queue.error.connect(errors.append)

    handle, _seen = _resolve(queue, _magnet(meta.info_hash))

    assert handle.state == MAGNET_ERROR
    assert errors == [_UNAVAILABLE]
    assert meta.info_hash not in errors[0]
    assert queue._preflight_hashes == set()
    assert _rows(db_path) == []


def test_an_aria2_error_status_ends_the_resolution_cleanly(queue_env, monkeypatch, tmp_path):
    queue, rpc, _db = _metadata_env(queue_env, monkeypatch, tmp_path)
    meta = torrent_mod.parse_torrent(_fixture_bytes())
    rpc.plan(meta.info_hash, status="error")

    handle, seen = _resolve(queue, _magnet(meta.info_hash))

    assert handle.state == MAGNET_ERROR
    assert "request" not in seen
    assert rpc.removed == [rpc.metadata_added[0]["gid"]]
    assert not os.path.exists(rpc.metadata_added[0]["out_dir"])
    assert queue._preflight_hashes == set()


# --- everything that already worked, still working -------------------------


def test_the_ordinary_production_magnet_path_does_not_use_the_resolver(queue_env, monkeypatch, tmp_path):
    """This slice adds a capability; it changes no existing route."""
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(queue, "resolve_magnet_preflight",
                        lambda *a, **k: calls.append(a))
    intakes = {"manual": "a" * 40, "search": "b" * 40,
               "extension": "c" * 40, "api": "d" * 40}

    for intake, info_hash in intakes.items():
        assert queue.add_url(_magnet(info_hash), intake=intake) is not None

    assert calls == []
    assert len(_rows(db_path)) == 4
    assert all(row["source_type"] == SOURCE_TORRENT for row in _rows(db_path))

    # Launching one takes the legacy metadata route, not the resolver.
    queue._running = True
    queue._scheduler_allows = True
    queue._launch(queue.tasks[_rows(db_path)[0]["id"]])
    assert len(rpc.magnets) == 1
    assert rpc.metadata_added == []


def test_a_local_torrent_preflight_is_untouched_by_this_slice(queue_env, monkeypatch, tmp_path):
    """The packaged local `.torrent` MVP keeps its exact shape."""
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    raw = _fixture_bytes()
    meta = torrent_mod.parse_torrent(raw)
    seen = {}

    queue.add_torrent_file(
        _write_torrent(tmp_path, raw), out_dir=str(tmp_path),
        precommit=lambda r: seen.__setitem__("request", r))
    request = seen["request"]

    assert request.metadata.info_hash == meta.info_hash
    assert request.prepared.selected_files is None
    assert request.prepared.url == torrent_mod.minimal_magnet(meta.info_hash)
    assert _rows(db_path) == []
    assert rpc.metadata_added == []
    assert queue._preflight_hashes == {meta.info_hash}

    queue._running = True
    queue._scheduler_allows = True
    tid = queue.commit_torrent_preflight(request, selected_files=(0, 2))
    queue._launch(queue.tasks[tid])
    assert rpc.torrents[0]["select_file"] == "1,3"


def test_the_resolver_adds_no_database_schema(queue_env, monkeypatch, tmp_path):
    """Precommit means nothing durable, which means nothing to store."""
    queue, rpc, db_path = _metadata_env(queue_env, monkeypatch, tmp_path)
    _resolved_request(queue, rpc, tmp_path)

    conn = sqlite3.connect(db_path)
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()

    assert not any("magnet" in name or "resolver" in name or "metadata" in name
                   for name in tables)
    assert _rows(db_path) == []
