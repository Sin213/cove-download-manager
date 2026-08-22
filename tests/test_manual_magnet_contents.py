"""Manual magnet -> metadata resolver -> Torrent Contents, in the GUI.

The reviewed resolver already fetches a magnet's metainfo without committing
anything; the reviewed Torrent Contents dialog already turns a manifest into
a file choice. This module is the coordinator between them, and only for the
one caller that is allowed it: a single magnet a person typed or pasted into
Add Download.

Three properties carry the weight.

*Nothing durable exists before the choice*: while the metadata is loading and
while Torrent Contents is open there is no row, no task, no provider probe and
no payload, and every way the resolution can fail leaves it that way. A
failure never quietly starts the whole magnet, because that would download
precisely the files the user was about to deselect.

*One request owns one of everything*: its own magnet, resolver handle,
preflight, destination, loading dialog and selection. Two magnets can never be
given each other's metadata, and one ending never strands the next.

*The resolver is consumed, not rebuilt*: the window starts one resolution per
request, hands the preflight it gets back to the existing dialog, and commits
that same preflight. It never parses metainfo, stores a second managed copy,
resolves twice or reconstructs a magnet.
"""
import os
import time
from collections import deque

import pytest
from PySide6.QtWidgets import QMainWindow

import cove.main_window as mw
from cove import debrid
from cove import torrent as torrent_mod
from cove.debrid import ALL_DEBRID, CachedTorrent, CachedTorrentFile, Unrestricted
from cove.dialogs import MagnetMetadataDialog, TorrentContentsDialog
from cove.queue import (
    SOURCE_TORRENT,
    SOURCE_TORRENT_FILE,
    MagnetResolution,
    TorrentPreflight,
)

# Fixture reuse: the real QueueManager environment lives in the queue suite,
# and the reviewed resolver's aria2 double in the resolver suite. Nothing
# about either is re-implemented here.
from tests.test_queue import queue_env  # noqa: F401
from tests.test_queue import _local_settings, _rows
from tests.test_magnet_metadata import _MetadataRpc, _fixture_bytes, _magnet

_RESOLVING = "Cove is already fetching this torrent's file list."
_QUEUED = "That torrent is already in Cove's queue."


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _Pill:
    def set_state(self, *a):
        pass


class Host(mw.MainWindow):
    """The real MainWindow methods, without its heavy constructor."""

    def __init__(self, queue, settings):
        QMainWindow.__init__(self)
        self.queue = queue
        self.settings = settings
        self._items = {}
        self.status_pill = _Pill()
        self._torrent_preflights = deque()
        self._torrent_preflight_open = False
        self._preflights_closed = False
        self._magnet_requests = deque()
        self._magnet_preflight_open = False
        self._magnet_loading = []
        self.duplicate_prompts = []
        self._tray = None
        self._force_quit = False
        self._torrent_contents_dialog = None

    def _refresh_status_pill(self):
        pass

    def _confirm_duplicate(self, match, label):
        # The duplicate prompt is the existing intake gate and is answered
        # elsewhere; recording it keeps a modal out of these tests while
        # still proving the magnet reaches the resolver's own refusal.
        self.duplicate_prompts.append(label)
        return True


class _Signal:
    """The one signal the loading dialog exposes, without needing Qt."""

    def __init__(self):
        self._slots = []

    def connect(self, fn):
        self._slots.append(fn)

    def emit(self, *args):
        for slot in list(self._slots):
            slot(*args)


class _Loading:
    """Stands in for MagnetMetadataDialog and records every construction.

    `exec` runs the next queued hook, which is how a test arranges for the
    resolver's answer (or the user's Cancel) to land *while the modal is up* --
    the only place it can land in production.
    """

    def __init__(self, monkeypatch, hooks=(), default=None):
        outer = self
        self.calls = []
        self.hooks = deque(hooks)
        self.default = default
        self.open_now = 0
        self.max_open = 0
        self.depth_at_finish = []

        class _Fake:
            def __init__(self, parent=None):
                self.parent = parent
                self.cancelled = _Signal()
                self.finished = False
                outer.calls.append(self)

            def exec(self):
                outer.open_now += 1
                outer.max_open = max(outer.max_open, outer.open_now)
                try:
                    hook = outer.hooks.popleft() if outer.hooks else outer.default
                    if hook is not None:
                        hook(self)
                finally:
                    outer.open_now -= 1
                return 0

            def finish(self):
                self.finished = True
                outer.depth_at_finish.append(outer.open_now)

        monkeypatch.setattr(mw, "MagnetMetadataDialog", _Fake)


class _Contents:
    """Stands in for TorrentContentsDialog and records every construction."""

    def __init__(self, monkeypatch, decide):
        outer = self
        self.calls = []
        self._decide = decide
        self.open_now = False
        self.during_exec = None
        self.rejected = []

        class _Fake:
            Accepted = TorrentContentsDialog.Accepted

            def __init__(self, metadata, save_to, parent=None):
                self.metadata = metadata
                self.save_to = save_to
                self._rejected_externally = False
                outer.calls.append(self)
                self._accepted, self._selection = outer._decide(metadata)

            def exec(self):
                if outer.open_now:
                    raise AssertionError("a second Torrent Contents dialog opened")
                outer.open_now = True
                try:
                    hook, outer.during_exec = outer.during_exec, None
                    if hook is not None:
                        hook(self)
                    if self._rejected_externally:
                        return TorrentContentsDialog.Rejected
                    return (
                        TorrentContentsDialog.Accepted if self._accepted
                        else TorrentContentsDialog.Rejected
                    )
                finally:
                    outer.open_now = False

            def reject(self):
                # The real dialog ends its own event loop here; recording the
                # ask is what a test can observe.
                self._rejected_externally = True
                outer.rejected.append(self)

            def result_selection(self):
                return self._selection

        monkeypatch.setattr(mw, "TorrentContentsDialog", _Fake)


def _deferred(queue):
    """Hold every worker call so a test decides when metadata arrives."""
    pending = deque()

    def spawn(fn, *args, on_done=None, on_fail=None, **kwargs):
        pending.append((fn, args, kwargs, on_done, on_fail))

    queue._spawn = spawn

    def deliver(_dlg=None):
        """Finish the most recently started resolution.

        Most recent, not oldest: `exec` is running for the request that was
        just spawned, so that is the worker whose answer the modal on screen
        is waiting for. With one request in flight the two are the same.
        """
        from cove.aria2 import Aria2Error
        from cove.debrid import DebridError
        from cove.torrent import TorrentError

        fn, args, kwargs, on_done, on_fail = pending.pop()
        try:
            result = fn(*args, **kwargs)
        except (Aria2Error, DebridError, TorrentError) as exc:
            if on_fail is not None:
                on_fail(str(exc))
        else:
            if on_done is not None:
                on_done(result)

    return pending, deliver


def _accept(selection):
    return lambda metadata: (True, selection)


_REJECT = lambda metadata: (False, None)  # noqa: E731


def _env(queue_env, monkeypatch, tmp_path, decide=None, hooks=None, **settings):
    """A Host over a real queue whose RPC models the metadata-only contract."""
    from cove import config as config_mod

    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path / "data")
    queue, _plain, db_path = queue_env(**_local_settings(**settings))
    rpc = _MetadataRpc()
    queue.rpc = rpc
    monkeypatch.setattr(debrid, "resolve_torrent", lambda *a, **k: None)
    pending, deliver = _deferred(queue)
    host = Host(queue, queue.settings)
    contents = _Contents(monkeypatch, decide or _accept(None))
    loading = _Loading(
        monkeypatch, () if hooks is None else hooks,
        default=deliver if hooks is None else None,
    )
    return host, queue, rpc, db_path, contents, loading, pending, deliver


def _inline(queue):
    """Restore inline workers, for the launch a route test drives afterwards."""
    from cove.aria2 import Aria2Error
    from cove.debrid import DebridError
    from cove.torrent import TorrentError

    def spawn(fn, *args, on_done=None, on_fail=None, **kwargs):
        try:
            result = fn(*args, **kwargs)
        except (Aria2Error, DebridError, TorrentError) as exc:
            if on_fail is not None:
                on_fail(str(exc))
        else:
            if on_done is not None:
                on_done(result)

    queue._spawn = spawn


def _plan(rpc, raw=None, **plan):
    """Plan a successful resolution and return the metadata it will produce."""
    raw = raw or _fixture_bytes()
    meta = torrent_mod.parse_torrent(raw)
    plan.setdefault("payload", raw)
    rpc.plan(meta.info_hash, **plan)
    return meta


def _errors(queue):
    seen = []
    queue.error.connect(seen.append)
    return seen


def _running(queue):
    queue._running = True
    queue._scheduler_allows = True


# ---------------------------------------------------------------------------
# The loading dialog, in isolation
# ---------------------------------------------------------------------------


def test_the_loading_dialog_says_what_it_is_doing():
    dlg = MagnetMetadataDialog()
    try:
        assert "Loading torrent metadata" in dlg.heading.text()
        # Metadata, never "downloading": no payload exists yet.
        assert "ownload" not in dlg.heading.text()
    finally:
        dlg.deleteLater()


def test_the_loading_dialog_is_indeterminate():
    """No percentage, no ETA: nothing here knows how far along the swarm is."""
    dlg = MagnetMetadataDialog()
    try:
        assert (dlg.bar.minimum(), dlg.bar.maximum()) == (0, 0)
        assert dlg.bar.isTextVisible() is False
    finally:
        dlg.deleteLater()


def test_the_loading_dialog_offers_cancel():
    dlg = MagnetMetadataDialog()
    try:
        assert dlg.cancel_btn.text() == "Cancel"
        assert dlg.cancel_btn.isEnabled()
    finally:
        dlg.deleteLater()


@pytest.mark.parametrize("act", ["button", "reject"])
def test_cancelling_asks_but_does_not_close_the_loading_dialog(act):
    """The request's real ending closes this dialog, so Cancel can only ask.

    Closing here would leave the window past a modal that a success already
    in flight is still going to answer.
    """
    dlg = MagnetMetadataDialog()
    try:
        asked = []
        dlg.cancelled.connect(lambda: asked.append(True))

        if act == "button":
            dlg.cancel_btn.click()
        else:
            dlg.reject()

        assert asked == [True]
        assert dlg.result() == 0
    finally:
        dlg.deleteLater()


def test_closing_the_loading_window_is_the_same_as_cancel():
    dlg = MagnetMetadataDialog()
    try:
        asked = []
        dlg.cancelled.connect(lambda: asked.append(True))

        dlg.close()

        assert asked == [True]
        assert dlg.isVisible() is False or dlg.result() == 0
    finally:
        dlg.deleteLater()


def test_the_loading_dialog_closes_when_the_request_ends():
    dlg = MagnetMetadataDialog()
    try:
        dlg.finish()
        assert dlg.result() != 0 or not dlg.isVisible()
    finally:
        dlg.deleteLater()


# ---------------------------------------------------------------------------
# RED 1-6: which callers reach the resolver
# ---------------------------------------------------------------------------


def test_a_manual_magnet_is_resolved_before_anything_is_committed(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert len(rpc.metadata_added) == 1
    assert loading.calls != []
    assert len(contents.calls) == 1
    assert len(_rows(db_path)) == 1  # committed only after the choice


def test_a_manual_http_url_never_reaches_the_magnet_resolver(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    monkeypatch.setattr(host, "_preflight_download_info", lambda p: p)

    ids = host.add_urls_checked(["https://example.test/file.zip"])

    assert rpc.metadata_added == []
    assert loading.calls == []
    assert contents.calls == []
    # And it still downloads: diverting it into the resolver would refuse it
    # outright, which is a silent regression the counts above cannot see.
    assert len(ids) == 1
    assert [r["url"] for r in _rows(db_path)] == ["https://example.test/file.zip"]


def test_a_local_torrent_file_never_reaches_the_magnet_resolver(
    queue_env, monkeypatch, tmp_path
):
    """The local `.torrent` route already has its manifest; it needs no swarm."""
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path
    )
    source = tmp_path / "picked.torrent"
    source.write_bytes(_fixture_bytes())

    queue.add_torrent_file(
        str(source), str(tmp_path), precommit=host._torrent_preflight
    )
    deliver()

    assert rpc.metadata_added == []
    assert loading.calls == []
    assert len(contents.calls) == 1


def test_a_search_magnet_now_shares_this_coordinator(
    queue_env, monkeypatch, tmp_path
):
    """One chosen Search result is the second caller this path ever gained.

    Only the fact of the sharing is asserted here, and only so that a change
    to the coordinator has to account for both origins; everything about what
    Search then does with it belongs to tests/test_search_magnet_contents.py.
    """
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)

    host.add_urls_checked([_magnet(meta.info_hash)], intake="search")

    assert len(rpc.metadata_added) == 1
    assert len(loading.calls) == 1
    assert len(contents.calls) == 1
    assert len(_rows(db_path)) == 1


def test_a_non_interactive_magnet_gets_no_dialog(
    queue_env, monkeypatch, tmp_path
):
    """The API bridge and native messaging add through the queue directly."""
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)

    queue.add_url(_magnet(meta.info_hash), intake="api")

    assert rpc.metadata_added == []
    assert loading.calls == []
    assert contents.calls == []
    assert len(_rows(db_path)) == 1


def test_a_manual_batch_keeps_its_existing_route(
    queue_env, monkeypatch, tmp_path
):
    """Known limit: the interactive preflight is the single-add feature the
    Download File Info preflight already established, and a batch keeps the
    exact route it had."""
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)

    host.add_urls_checked(
        [_magnet(meta.info_hash), "https://example.test/file.zip"]
    )

    assert rpc.metadata_added == []
    assert loading.calls == []
    assert contents.calls == []


def test_a_malformed_magnet_is_refused_before_any_backend_work(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    errors = _errors(queue)

    host.add_urls_checked(["magnet:?xt=urn:btih:nonsense"])

    assert rpc.metadata_added == []
    assert contents.calls == []
    assert _rows(db_path) == []
    assert errors != []


# ---------------------------------------------------------------------------
# RED 7-15: the loading phase, and the handoff out of it
# ---------------------------------------------------------------------------


def test_the_loading_dialog_is_up_while_the_metadata_is_pending(
    queue_env, monkeypatch, tmp_path
):
    seen = {}
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path,
        hooks=[lambda dlg: (
            seen.update(contents=len(contents.calls), rows=len(_rows(db_path))),
            deliver(dlg),
        )],
    )
    meta = _plan(rpc)

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert seen == {"contents": 0, "rows": 0}


def test_the_metadata_is_awaited_inside_the_event_loop_not_a_blocking_wait(
    queue_env, monkeypatch, tmp_path
):
    """The answer arrives while the modal is running, which is the whole point.

    A coordinator that waited for the resolver before showing anything would
    never reach `exec`, and would hold the GUI thread for up to a minute.
    """
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert loading.depth_at_finish == [1]


def test_the_resolution_starts_on_a_worker(queue_env, monkeypatch, tmp_path):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, hooks=[lambda dlg: None]
    )
    meta = _plan(rpc)

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert len(pending) == 1
    assert pending[0][0].__name__ == "_resolve_magnet_metadata"


def test_success_opens_torrent_contents_exactly_once(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert len(contents.calls) == 1
    assert loading.calls[0].finished is True


def test_the_existing_torrent_contents_dialog_is_reused(
    queue_env, monkeypatch, tmp_path
):
    """Structural: the window's only contents dialog is the reviewed class."""
    assert mw.TorrentContentsDialog is TorrentContentsDialog


def test_the_resolved_name_wins_over_the_magnets_display_hint(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)

    host.add_urls_checked([_magnet(meta.info_hash, dn="Totally+Fake+Name")])

    assert contents.calls[0].metadata.name == meta.name
    assert contents.calls[0].metadata.name != "Totally Fake Name"


def test_the_resolved_manifest_is_handed_over_directly(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)

    host.add_urls_checked([_magnet(meta.info_hash)])

    shown = contents.calls[0].metadata
    assert shown == meta
    assert [f.index for f in shown.files] == [0, 1, 2]


def test_the_dialog_is_shown_the_prepared_destination(
    queue_env, monkeypatch, tmp_path
):
    dest = tmp_path / "chosen"
    dest.mkdir()
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    queue.settings.download_dir = str(tmp_path / "global")
    meta = _plan(rpc)

    host.add_urls_checked([_magnet(meta.info_hash)], out_dir=str(dest))

    assert contents.calls[0].save_to == str(dest)


def test_the_preflight_handed_over_is_the_reviewed_record(
    queue_env, monkeypatch, tmp_path
):
    """Ownership transfer, not a copy: one held hash, one managed copy."""
    captured = []
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)
    original = host._torrent_preflight
    monkeypatch.setattr(
        host, "_torrent_preflight",
        lambda request: (captured.append(request), original(request))[1],
    )

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert len(captured) == 1
    assert isinstance(captured[0], TorrentPreflight)
    assert captured[0].prepared.url == _magnet(meta.info_hash)
    assert captured[0].prepared.info_hash == meta.info_hash


def test_the_window_never_stores_a_second_managed_copy(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)
    stores = []
    real_store = torrent_mod.store_managed_torrent
    monkeypatch.setattr(
        torrent_mod, "store_managed_torrent",
        lambda m: (stores.append(m.info_hash), real_store(m))[1],
    )

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert stores == [meta.info_hash]


def test_the_window_never_parses_metainfo_itself(
    queue_env, monkeypatch, tmp_path
):
    """Structural: no second parser, and no bencode in the coordinator."""
    import inspect

    source = inspect.getsource(mw.MainWindow._resolve_magnet_request)
    source += inspect.getsource(mw.MainWindow._magnet_preflight)
    for forbidden in (
        "parse_torrent", "bencode", "bt-metadata-only", "bt-save-metadata",
        "followedBy", "minimal_magnet", "store_managed_torrent",
        "normalize_info_hash", "remove_download_result",
    ):
        assert forbidden not in source


# ---------------------------------------------------------------------------
# RED 16-20: the selection domain, unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("selection", [None])
def test_every_file_commits_the_legacy_whole_torrent_selection(
    queue_env, monkeypatch, tmp_path, selection
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, decide=_accept(selection)
    )
    meta = _plan(rpc)

    host.add_urls_checked([_magnet(meta.info_hash)])

    row = _rows(db_path)[0]
    assert row["selected_files"] == ""
    assert row["source_type"] == SOURCE_TORRENT


def test_a_subset_commits_canonical_zero_based_indexes(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, decide=_accept((2, 0))
    )
    meta = _plan(rpc)

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert _rows(db_path)[0]["selected_files"] == "0,2"


def test_an_empty_selection_commits_nothing(queue_env, monkeypatch, tmp_path):
    """Zero chosen files can never become `None`, which means every file."""
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, decide=_accept(())
    )
    meta = _plan(rpc)

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert _rows(db_path) == []


def test_a_committed_magnet_reports_its_task_id_to_the_caller(
    queue_env, monkeypatch, tmp_path
):
    """`dropEvent` accepts or rejects a drop on this list being non-empty.

    Returning nothing for a magnet that really did create a task makes a
    successful drag-and-drop read as a refusal to the drag source.
    """
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, decide=_accept((0,))
    )
    meta = _plan(rpc)

    ids = host.add_urls_checked([_magnet(meta.info_hash)], out_dir=str(tmp_path))

    assert ids == [_rows(db_path)[0]["id"]]


def test_a_preflight_reports_its_own_task_not_a_later_ones(
    queue_env, monkeypatch, tmp_path
):
    """One drain can commit several, and the caller asked about exactly one.

    A drop that reported the task belonging to some other request as its own
    would be accepted for the wrong reason.
    """
    from tests.test_magnet_metadata import _other_bytes

    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, decide=_accept(None)
    )
    first = torrent_mod.parse_torrent(_fixture_bytes())
    second = torrent_mod.parse_torrent(_other_bytes())
    # A second preflight lands while the first's dialog is up, so the drain
    # commits both and the later one finishes last.
    contents.during_exec = lambda dlg: host._torrent_preflight(
        _held_preflight(queue, second, tmp_path))

    tid = host._torrent_preflight(_held_preflight(queue, first, tmp_path))

    rows = {r["info_hash"]: r["id"] for r in _rows(db_path)}
    assert len(rows) == 2
    assert tid == rows[first.info_hash]
    assert tid != rows[second.info_hash]


def test_a_rejected_magnet_reports_no_task_id(queue_env, monkeypatch, tmp_path):
    """The other half: a drop that created nothing must still read as empty."""
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, decide=_REJECT
    )
    meta = _plan(rpc)

    ids = host.add_urls_checked([_magnet(meta.info_hash)], out_dir=str(tmp_path))

    assert ids == []
    assert _rows(db_path) == []


def test_the_download_commits_exactly_one_task(queue_env, monkeypatch, tmp_path):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, decide=_accept((1,))
    )
    meta = _plan(rpc)

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert len(_rows(db_path)) == 1
    assert len(rpc.metadata_added) == 1


def test_the_committed_task_keeps_the_original_magnet_and_its_trackers(
    queue_env, monkeypatch, tmp_path
):
    """No infohash-only reconstruction: the trackers the user gave survive."""
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)
    magnet = _magnet(meta.info_hash, trackers=("http://127.0.0.1:6969/announce",))

    host.add_urls_checked([magnet])

    row = _rows(db_path)[0]
    assert row["url"] == magnet
    assert "tr=" in row["url"]


# ---------------------------------------------------------------------------
# RED 21-28: cancel, failure and timeout all fail closed
# ---------------------------------------------------------------------------


def _cancel_then_deliver(deliver):
    def hook(dlg):
        dlg.cancelled.emit()
        deliver(dlg)
    return hook


def test_cancelling_while_loading_cancels_the_resolution(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, hooks=[None]
    )
    loading.hooks = deque([_cancel_then_deliver(deliver)])
    meta = _plan(rpc, pending=3)
    cancels = []
    real_cancel = MagnetResolution.cancel
    monkeypatch.setattr(
        MagnetResolution, "cancel",
        lambda self: (cancels.append(self), real_cancel(self))[1],
    )

    host.add_urls_checked([_magnet(meta.info_hash)])

    # Exactly once: a Cancel that asked twice would be a second claim on a
    # request that has already ended.
    assert len(cancels) == 1
    assert contents.calls == []
    assert _rows(db_path) == []
    assert rpc.magnets == []  # no payload torrent was ever added


def test_cancelling_while_loading_shows_no_error(
    queue_env, monkeypatch, tmp_path
):
    """A cancellation is the user's own doing and needs no sentence."""
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, hooks=[None]
    )
    loading.hooks = deque([_cancel_then_deliver(deliver)])
    errors = _errors(queue)
    meta = _plan(rpc, pending=3)

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert errors == []


def test_cancelling_while_loading_releases_the_torrent(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, hooks=[None]
    )
    loading.hooks = deque([_cancel_then_deliver(deliver)])
    meta = _plan(rpc, pending=3)

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert meta.info_hash not in queue._preflight_hashes


@pytest.mark.parametrize("plan", [{"add_error": True}, {"no_artifact": True}])
def test_a_failed_resolution_creates_nothing_at_all(
    queue_env, monkeypatch, tmp_path, plan
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    errors = _errors(queue)
    meta = _plan(rpc, **plan)

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert contents.calls == []
    assert _rows(db_path) == []
    assert rpc.magnets == []
    assert len(errors) == 1
    assert loading.calls[0].finished is True


def test_a_failed_resolution_never_falls_back_to_the_whole_magnet(
    queue_env, monkeypatch, tmp_path
):
    """Downloading everything is exactly what the user was about to prevent."""
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    magnet = _magnet(_plan(rpc, add_error=True).info_hash)

    host.add_urls_checked([magnet])

    assert [t["url"] for t in _rows(db_path)] == []


def test_a_timed_out_resolution_creates_nothing(
    queue_env, monkeypatch, tmp_path
):
    import cove.queue as queue_module

    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    monkeypatch.setattr(queue_module, "MAGNET_METADATA_TIMEOUT_S", 0.0)
    monkeypatch.setattr(queue_module, "MAGNET_METADATA_POLL_S", 0.0)
    errors = _errors(queue)
    meta = _plan(rpc, pending=10_000)

    started = time.monotonic()
    host.add_urls_checked([_magnet(meta.info_hash)])
    elapsed = time.monotonic() - started

    # The deadline is what ends this, not exhaustion: `pending=10_000` would
    # answer eventually, and a coordinator that waited for it would sit here
    # for the full committed 60s. Constrain the cost, not just the outcome.
    assert elapsed < 5.0, elapsed
    assert rpc.metadata_status_calls, "the resolver never polled at all"
    assert contents.calls == []
    assert _rows(db_path) == []
    assert rpc.magnets == []
    assert len(errors) == 1


def test_a_hash_mismatch_creates_nothing(queue_env, monkeypatch, tmp_path):
    from tests.test_magnet_metadata import _other_bytes

    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = torrent_mod.parse_torrent(_fixture_bytes())
    rpc.plan(meta.info_hash, payload=_other_bytes())

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert contents.calls == []
    assert _rows(db_path) == []


def test_a_duplicate_refusal_opens_no_dialog(queue_env, monkeypatch, tmp_path):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)
    errors = _errors(queue)
    queue._preflight_hashes.add(meta.info_hash)

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert rpc.metadata_added == []
    assert loading.calls == []
    assert contents.calls == []
    assert _rows(db_path) == []
    assert errors != []


# ---------------------------------------------------------------------------
# RED 29-36: ownership after the handoff, and the races around it
# ---------------------------------------------------------------------------


def test_rejecting_torrent_contents_discards_the_preflight(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, decide=_REJECT
    )
    meta = _plan(rpc)

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert _rows(db_path) == []
    assert meta.info_hash not in queue._preflight_hashes
    import os

    assert not os.path.exists(torrent_mod.managed_torrent_path(meta.info_hash))


def test_rejecting_torrent_contents_does_not_cancel_the_finished_resolution(
    queue_env, monkeypatch, tmp_path
):
    """The resolution stopped owning this torrent when the preflight began."""
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, decide=_REJECT
    )
    meta = _plan(rpc)
    cancels = []
    monkeypatch.setattr(
        mw._MagnetRequest, "cancel",
        lambda self: cancels.append(self), raising=False,
    )

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert cancels == []


def test_a_cancel_that_wins_keeps_torrent_contents_shut(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, hooks=[None]
    )
    loading.hooks = deque([_cancel_then_deliver(deliver)])
    meta = _plan(rpc, pending=2)

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert contents.calls == []
    assert _rows(db_path) == []


def test_a_stale_cancel_cannot_destroy_a_transferred_preflight(
    queue_env, monkeypatch, tmp_path
):
    """Cancel arriving after the success is committed to loses, deterministically."""
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, hooks=[None]
    )

    def late_cancel(dlg):
        # The metainfo is already verified and the success already claimed
        # when the user's click lands.
        deliver(dlg)
        dlg.cancelled.emit()

    loading.hooks = deque([late_cancel])
    meta = _plan(rpc)

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert len(contents.calls) == 1
    assert len(_rows(db_path)) == 1


def test_a_late_success_after_a_cancel_opens_nothing(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, hooks=[lambda dlg: dlg.cancelled.emit()]
    )
    meta = _plan(rpc, pending=2)

    host.add_urls_checked([_magnet(meta.info_hash)])
    # The worker only reports afterwards, long past the closed modal.
    deliver()

    assert contents.calls == []
    assert _rows(db_path) == []


def test_a_second_success_delivery_opens_one_dialog_and_is_cleaned_up(
    queue_env, monkeypatch, tmp_path
):
    """Defence: the reviewed resolver delivers once, so this cannot happen.

    If it ever did, a second preflight must neither open a second dialog nor
    be dropped on the floor -- dropping it would leak the managed copy and
    hold its info hash for the rest of the session.
    """
    import os

    from tests.test_magnet_metadata import _other_bytes

    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path
    )
    first = _plan(rpc)
    second = torrent_mod.parse_torrent(_other_bytes())
    monkeypatch.setattr(
        queue, "resolve_magnet_preflight",
        lambda url, **kw: _twice(queue, kw["on_resolved"], first, second, tmp_path),
    )

    host.add_urls_checked([_magnet(first.info_hash)])

    assert [c.metadata.name for c in contents.calls] == [first.name]
    assert [r["info_hash"] for r in _rows(db_path)] == [first.info_hash]
    assert second.info_hash not in queue._preflight_hashes
    assert not os.path.exists(torrent_mod.managed_torrent_path(second.info_hash))


def _held_preflight(queue, meta, tmp_path):
    """A real held TorrentPreflight, as the resolver would have produced."""
    managed = torrent_mod.store_managed_torrent(meta)
    prepared = queue.prepare_url(
        _magnet(meta.info_hash), out_dir=str(tmp_path),
        source_type=SOURCE_TORRENT, info_hash=meta.info_hash,
        torrent_name=meta.name, torrent_path=managed,
    )
    request = TorrentPreflight(metadata=meta, prepared=prepared)
    queue.hold_torrent_preflight(request)
    return request


def _twice(queue, on_resolved, first, second, tmp_path):
    """Deliver two successes to one request, which the resolver never does."""
    from cove.queue import MagnetResolution

    for meta in (first, second):
        managed = torrent_mod.store_managed_torrent(meta)
        prepared = queue.prepare_url(
            _magnet(meta.info_hash), out_dir=str(tmp_path),
            source_type=SOURCE_TORRENT, info_hash=meta.info_hash,
            torrent_name=meta.name, torrent_path=managed,
        )
        request = TorrentPreflight(metadata=meta, prepared=prepared)
        queue.hold_torrent_preflight(request)
        on_resolved(request)
    return MagnetResolution(first.info_hash, _magnet(first.info_hash))


def test_shutdown_during_loading_leaves_nothing_behind(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path,
        hooks=[lambda dlg: host.discard_magnet_requests()],
    )
    meta = _plan(rpc, pending=5)

    host.add_urls_checked([_magnet(meta.info_hash)])
    deliver()

    assert contents.calls == []
    assert _rows(db_path) == []
    assert meta.info_hash not in queue._preflight_hashes
    assert loading.calls[0].finished is True


def test_explicit_quit_while_loading_ends_the_modal_before_quitting(
    queue_env, monkeypatch, tmp_path
):
    """Quit has to escape the modal, and `QApplication.quit()` cannot.

    A modal runs its own event loop, which `quit()` does not unwind (measured
    on Qt 6.11.1, and true of a stock QDialog too). So a Quit chosen while a
    magnet's metadata is loading would exit no loop at all: Cove would stay
    up with its tray icon already hidden, and on a dead magnet that window
    lasts the full 60 second timeout.
    """
    seen = {}
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, hooks=[None]
    )
    # Recorded at the instant quit is asked for, which is the only place the
    # ordering can be observed: by the time `request_quit` returns, both
    # steps have happened whichever order they ran in.
    monkeypatch.setattr(
        mw.QApplication, "quit",
        staticmethod(lambda: seen.update(
            quit_called=True, modal_finished=loading.calls[0].finished)),
    )
    loading.hooks = deque([lambda dlg: host.request_quit()])
    meta = _plan(rpc, pending=10_000)

    host.add_urls_checked([_magnet(meta.info_hash)])

    # The modal is closed first, so `exec` returns and the quit is reachable.
    assert seen == {"quit_called": True, "modal_finished": True}, seen
    assert contents.calls == []
    assert _rows(db_path) == []
    assert meta.info_hash not in queue._preflight_hashes


def test_explicit_quit_while_torrent_contents_is_open_rejects_it(
    queue_env, monkeypatch, tmp_path
):
    """Both phases of this workflow are modal, so quit has to unwind both.

    Fixing only the loading modal would leave the trap intact for the half
    of the flow the user actually spends time in.
    """
    seen = {}
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, decide=_accept(None)
    )
    monkeypatch.setattr(
        mw.QApplication, "quit",
        staticmethod(lambda: seen.update(
            quit_called=True, rejected=len(contents.rejected))),
    )
    contents.during_exec = lambda dlg: host.request_quit()
    meta = _plan(rpc)

    host.add_urls_checked([_magnet(meta.info_hash)], out_dir=str(tmp_path))

    # Rejected before the quit is asked for, so the modal's loop can end.
    assert seen == {"quit_called": True, "rejected": 1}, seen
    # Rejecting runs the ordinary discard: nothing committed, nothing held.
    assert _rows(db_path) == []
    assert meta.info_hash not in queue._preflight_hashes
    assert not os.path.exists(torrent_mod.managed_torrent_path(meta.info_hash))


def test_a_loading_dialog_that_raises_fails_closed(
    queue_env, monkeypatch, tmp_path
):
    """An exception here must not leave a preflight nobody owns.

    The resolution is already in flight, so a dialog that cannot be built or
    run would otherwise strand its managed copy and hold its info hash for
    the rest of the session.
    """
    from tests.test_magnet_metadata import _other_bytes

    host = None
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path
    )
    working = mw.MagnetMetadataDialog  # the ordinary fake, for B
    a = _plan(rpc)
    b = _plan(rpc, raw=_other_bytes())
    errors = _errors(queue)
    built = []

    def boom(parent=None):
        built.append(parent)
        if len(built) == 1:
            # Queue a second request first: whether the drain survives A's
            # exception is exactly what is under test.
            host.add_urls_checked([_magnet(b.info_hash)], out_dir=str(tmp_path))
            raise RuntimeError("no dialog today")
        return working(parent)

    monkeypatch.setattr(mw, "MagnetMetadataDialog", boom)

    host.add_urls_checked([_magnet(a.info_hash)], out_dir=str(tmp_path))
    # A's worker still runs; being cancelled, it reports failure and gives up
    # the hold, exactly as it would in production.
    while pending:
        deliver()

    # A never opened contents and left nothing held.
    assert contents.calls and a.info_hash not in queue._preflight_hashes
    assert not os.path.exists(torrent_mod.managed_torrent_path(a.info_hash))
    assert errors != []
    # B was not stranded by A's exception.
    assert [c.metadata.name for c in contents.calls] == [b.name]
    assert [r["info_hash"] for r in _rows(db_path)] == [b.info_hash]


def test_a_success_arriving_after_a_dialog_failure_is_cleaned_up(
    queue_env, monkeypatch, tmp_path
):
    """Cancelling can lose, so failing the dialog is not enough on its own.

    `MagnetResolution.cancel` politely declines to interrupt a success whose
    last steps are already running. If the modal blew up in that window, the
    success still lands afterwards -- and with nobody left to hand it to, the
    managed copy and the held info hash would sit there for the session.
    """
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)
    captured = {}
    real = queue.resolve_magnet_preflight
    monkeypatch.setattr(
        queue, "resolve_magnet_preflight",
        lambda url, **kw: (captured.update(kw), real(url, **kw))[1],
    )
    monkeypatch.setattr(
        mw, "MagnetMetadataDialog",
        lambda parent=None: (_ for _ in ()).throw(RuntimeError("no dialog")),
    )

    host.add_urls_checked([_magnet(meta.info_hash)], out_dir=str(tmp_path))
    # The success the resolution was already committed to, arriving late.
    captured["on_resolved"](_held_preflight(queue, meta, tmp_path))

    assert contents.calls == []
    assert _rows(db_path) == []
    assert meta.info_hash not in queue._preflight_hashes
    assert not os.path.exists(torrent_mod.managed_torrent_path(meta.info_hash))


def test_explicit_quit_does_not_open_the_next_queued_contents_dialog(
    queue_env, monkeypatch, tmp_path
):
    """Rejecting the open modal must not just let the drain show the next one.

    The contents queue is FIFO, so unwinding one dialog during shutdown would
    otherwise immediately open the one behind it and block the quit again --
    with the tray icon already hidden.
    """
    from tests.test_magnet_metadata import _other_bytes

    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, decide=_accept(None)
    )
    monkeypatch.setattr(mw.QApplication, "quit", staticmethod(lambda: None))
    a = _plan(rpc)
    queued = torrent_mod.parse_torrent(_other_bytes())

    def during(dlg):
        # A second preflight lands while the first modal is up: it joins the
        # queue rather than stacking, exactly as a second `.torrent` would.
        host._torrent_preflight(_held_preflight(queue, queued, tmp_path))
        host.request_quit()

    contents.during_exec = during

    host.add_urls_checked([_magnet(a.info_hash)], out_dir=str(tmp_path))

    assert len(contents.calls) == 1, [c.metadata.name for c in contents.calls]
    assert _rows(db_path) == []
    for meta in (a, queued):
        assert meta.info_hash not in queue._preflight_hashes
        assert not os.path.exists(
            torrent_mod.managed_torrent_path(meta.info_hash))


def test_an_update_restart_ends_the_preflight_modal_before_quitting(
    queue_env, monkeypatch, tmp_path
):
    """Every exit has to unwind the modal, including the one that relaunches.

    The updater swaps the AppImage, relaunches and quits. If the old process
    is sitting in a preflight modal's event loop, `app.quit()` does not
    unwind it, so the old process lingers and the replacement can lose the
    single-instance race and exit -- an update that silently does not happen.
    """
    from cove import updater as updater_mod

    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path
    )
    seen = {}
    controller = updater_mod.UpdateController(
        host, "1.0.0", "owner/repo", "Cove", "cove-updates"
    )
    # Get past the integrity gate honestly: a real file and its real digest.
    asset = tmp_path / "Cove.AppImage"
    asset.write_bytes(b"new appimage payload")
    controller._expected_digest = updater_mod.sha256_file(asset)
    monkeypatch.setattr(updater_mod, "swap_in_appimage", lambda p: str(p))
    monkeypatch.setattr(updater_mod, "relaunch", lambda p: None)
    # A dialog here would mean the flow bailed before the quit; fail loudly
    # rather than block on a modal.
    for level in ("warning", "critical"):
        monkeypatch.setattr(
            updater_mod.QMessageBox, level,
            staticmethod(lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("the update flow bailed before quitting"))),
        )
    monkeypatch.setattr(
        updater_mod.QApplication, "quit",
        lambda *a: seen.update(quit_called=True, sweeps=sweeps[0]),
    )
    sweeps = [0]
    real_sweep = host.close_interactive_preflights
    monkeypatch.setattr(
        host, "close_interactive_preflights",
        lambda: (sweeps.__setitem__(0, sweeps[0] + 1), real_sweep())[1],
    )

    controller._on_downloaded(str(asset))

    # The sweep ran, and it ran before the quit.
    assert seen == {"quit_called": True, "sweeps": 1}, seen


def test_shutdown_does_not_reopen_a_preflight_resolved_under_an_inner_dialog(
    queue_env, monkeypatch, tmp_path
):
    """The sweep has to take the preflight too, not just close the dialog.

    A local `.torrent` parse can land while a magnet's loading modal is up,
    putting a contents dialog inside its event loop. The magnet can then
    resolve while that inner dialog is still open: the loading dialog closes
    but its `exec` cannot return yet, so the record sits in the sweep's list
    holding a delivered preflight. Closing the dialog alone would leave that
    preflight to be dispatched once the inner dialog unwinds -- opening a new
    modal after shutdown has already begun.
    """
    from tests.test_magnet_metadata import _other_bytes

    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, decide=_REJECT, hooks=[None]
    )
    a = _plan(rpc)
    inner = torrent_mod.parse_torrent(_other_bytes())

    def during_inner(dlg):
        deliver()                      # A resolves while this dialog is open
        host.discard_magnet_requests()  # the shutdown sweep

    def during_loading(dlg):
        contents.during_exec = during_inner
        host._torrent_preflight(_held_preflight(queue, inner, tmp_path))

    loading.hooks = deque([during_loading])

    host.add_urls_checked([_magnet(a.info_hash)], out_dir=str(tmp_path))

    # Only the inner local `.torrent` dialog ever opened.
    assert [c.metadata.name for c in contents.calls] == [inner.name]
    assert _rows(db_path) == []
    for meta in (a, inner):
        assert meta.info_hash not in queue._preflight_hashes
        assert not os.path.exists(
            torrent_mod.managed_torrent_path(meta.info_hash))


def test_no_interactive_preflight_opens_once_shutdown_has_begun(
    queue_env, monkeypatch, tmp_path
):
    """The class, not the instance: after the sweep, nothing opens a modal.

    Four review rounds found four different unwind orders that each let one
    more dialog through. Rather than close them one at a time, the sweep
    latches, and every interactive preflight refuses to start while it is
    latched -- so no nesting order can produce a modal after shutdown.
    """
    from tests.test_magnet_metadata import _other_bytes

    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path
    )
    later = torrent_mod.parse_torrent(_other_bytes())

    host.close_interactive_preflights()

    # A local `.torrent` preflight arriving after the sweep, by any route.
    host._torrent_preflight(_held_preflight(queue, later, tmp_path))
    # And a manual magnet arriving after the sweep.
    host.add_urls_checked(
        [_magnet(_plan(rpc).info_hash)], out_dir=str(tmp_path))

    assert contents.calls == []
    assert loading.calls == []
    assert rpc.metadata_added == []
    assert _rows(db_path) == []
    assert later.info_hash not in queue._preflight_hashes
    assert not os.path.exists(torrent_mod.managed_torrent_path(later.info_hash))


def test_shutdown_discards_magnet_requests_that_never_started(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    host._magnet_requests.append(("magnet:?xt=urn:btih:" + "ab" * 20, None))

    host.discard_magnet_requests()

    assert list(host._magnet_requests) == []
    assert rpc.metadata_added == []


# ---------------------------------------------------------------------------
# RED 37-49: duplicates, request isolation and the interactive FIFO
# ---------------------------------------------------------------------------


def test_a_held_local_preflight_blocks_the_matching_manual_magnet(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)
    queue._preflight_hashes.add(meta.info_hash)

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert rpc.metadata_added == []
    assert contents.calls == []


def test_a_live_torrent_blocks_the_matching_manual_magnet(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)
    errors = _errors(queue)
    queue.add_url(
        _magnet(meta.info_hash), out_dir=str(tmp_path), intake="api"
    )

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert rpc.metadata_added == []
    assert contents.calls == []
    assert len(_rows(db_path)) == 1
    assert _QUEUED in errors


def test_an_unrelated_magnet_is_not_blocked_by_a_held_hash(
    queue_env, monkeypatch, tmp_path
):
    from tests.test_magnet_metadata import _other_bytes

    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    held = torrent_mod.parse_torrent(_fixture_bytes())
    queue._preflight_hashes.add(held.info_hash)
    other = _plan(rpc, raw=_other_bytes())

    host.add_urls_checked([_magnet(other.info_hash)])

    assert len(contents.calls) == 1
    assert contents.calls[0].metadata.info_hash == other.info_hash


def test_two_requests_never_cross_their_metadata_or_destinations(
    queue_env, monkeypatch, tmp_path
):
    from tests.test_magnet_metadata import _other_bytes

    picked = {}
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path,
        decide=lambda meta: (True, picked[meta.name]),
    )
    a = _plan(rpc)
    b = _plan(rpc, raw=_other_bytes())
    picked = {a.name: (0, 2), b.name: (1,)}
    dir_a, dir_b = tmp_path / "a", tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()

    host.add_urls_checked([_magnet(a.info_hash)], out_dir=str(dir_a))
    host.add_urls_checked([_magnet(b.info_hash)], out_dir=str(dir_b))

    shown = {c.metadata.name: c.save_to for c in contents.calls}
    assert shown == {a.name: str(dir_a), b.name: str(dir_b)}
    rows = {r["info_hash"]: r["selected_files"] for r in _rows(db_path)}
    assert rows == {a.info_hash: "0,2", b.info_hash: "1"}


def test_a_second_request_arriving_mid_flight_waits_its_turn(
    queue_env, monkeypatch, tmp_path
):
    """One interactive torrent preflight at a time, and neither is lost."""
    from tests.test_magnet_metadata import _other_bytes

    host = queue = None

    def first(dlg):
        # B is submitted while A's modal is up, exactly as a second-instance
        # magnet would arrive.
        host.add_urls_checked([_magnet(b.info_hash)], out_dir=str(tmp_path))
        deliver(dlg)

    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, hooks=[None, None]
    )
    a = _plan(rpc)
    b = _plan(rpc, raw=_other_bytes())
    loading.hooks = deque([first, deliver])

    host.add_urls_checked([_magnet(a.info_hash)], out_dir=str(tmp_path))

    assert loading.max_open == 1
    assert [c.metadata.name for c in contents.calls] == [a.name, b.name]
    assert len(_rows(db_path)) == 2


@pytest.mark.parametrize("plan", [{"add_error": True}, {"no_artifact": True}])
def test_a_failed_request_does_not_strand_the_one_behind_it(
    queue_env, monkeypatch, tmp_path, plan
):
    from tests.test_magnet_metadata import _other_bytes

    host = None

    def first(dlg):
        host.add_urls_checked([_magnet(b.info_hash)], out_dir=str(tmp_path))
        deliver(dlg)

    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, hooks=[None, None]
    )
    a = _plan(rpc, **plan)
    b = _plan(rpc, raw=_other_bytes())
    loading.hooks = deque([first, deliver])

    host.add_urls_checked([_magnet(a.info_hash)], out_dir=str(tmp_path))

    assert [c.metadata.name for c in contents.calls] == [b.name]
    assert len(_rows(db_path)) == 1


def test_a_rejected_contents_dialog_does_not_strand_the_one_behind_it(
    queue_env, monkeypatch, tmp_path
):
    from tests.test_magnet_metadata import _other_bytes

    host = None

    def first(dlg):
        host.add_urls_checked([_magnet(b.info_hash)], out_dir=str(tmp_path))
        deliver(dlg)

    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path,
        decide=lambda meta: (meta.name == b.name, None), hooks=[None, None],
    )
    a = _plan(rpc)
    b = _plan(rpc, raw=_other_bytes())
    loading.hooks = deque([first, deliver])

    host.add_urls_checked([_magnet(a.info_hash)], out_dir=str(tmp_path))

    assert [c.metadata.name for c in contents.calls] == [a.name, b.name]
    assert [r["info_hash"] for r in _rows(db_path)] == [b.info_hash]


def test_the_same_magnet_twice_never_starts_a_second_resolution(
    queue_env, monkeypatch, tmp_path
):
    """The FIFO holds the repeat back, and the queue's ownership refuses it.

    Which of the queue's three refusals it earns depends on how far the first
    request has got by then; that it earns one, and that no second metadata
    job, dialog or task exists, does not.
    """
    host = None

    def first(dlg):
        host.add_urls_checked([_magnet(a.info_hash)], out_dir=str(tmp_path))
        deliver(dlg)

    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, hooks=[None, None]
    )
    a = _plan(rpc)
    loading.hooks = deque([first, deliver])
    errors = _errors(queue)

    host.add_urls_checked([_magnet(a.info_hash)], out_dir=str(tmp_path))

    assert len(rpc.metadata_added) == 1
    assert len(contents.calls) == 1
    assert len(_rows(db_path)) == 1
    assert errors and errors[0] in (_RESOLVING, _QUEUED)


# ---------------------------------------------------------------------------
# RED 50-59: the reviewed routes downstream, consumed unchanged
# ---------------------------------------------------------------------------


def test_a_subset_reaches_the_reviewed_normal_aria2_selection(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, decide=_accept((0, 2))
    )
    meta = _plan(rpc)
    _running(queue)

    host.add_urls_checked([_magnet(meta.info_hash)], out_dir=str(tmp_path))
    tid = _rows(db_path)[0]["id"]
    _inline(queue)
    queue._launch(queue.tasks[tid])

    assert rpc.torrents[0]["select_file"] == "1,3"


def test_all_files_keeps_the_legacy_route_with_no_select_file(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, decide=_accept(None)
    )
    meta = _plan(rpc)
    _running(queue)

    host.add_urls_checked([_magnet(meta.info_hash)], out_dir=str(tmp_path))
    tid = _rows(db_path)[0]["id"]
    _inline(queue)
    queue._launch(queue.tasks[tid])

    assert rpc.torrents[0]["select_file"] is None


def _fixture_cached(meta, paths):
    """The provider's view of `meta`, under whichever paths a test gives it."""
    return CachedTorrent(
        ALL_DEBRID, meta.info_hash, meta.name,
        tuple(
            CachedTorrentFile(
                i, tuple(p), meta.files[i].size, f"https://alldebrid.test/f/{i}"
            )
            for i, p in enumerate(paths)
        ),
    )


def test_a_subset_reaches_the_reviewed_cached_provider_route(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, decide=_accept((1,)),
        all_debrid_enabled=True, all_debrid_api_key="ad-key-value",
    )
    meta = _plan(rpc)
    cached = _fixture_cached(meta, [("A.bin",), ("B.bin",), ("Folder", "C.bin")])
    unlocked = []
    monkeypatch.setattr(debrid, "resolve_torrent", lambda *a, **k: cached)
    monkeypatch.setattr(
        debrid, "unlock_torrent_file",
        lambda link, provider, _s, **kw: (
            unlocked.append(link),
            Unrestricted("https://cdn.test/B.bin", "B.bin", 20, provider),
        )[1],
    )
    _running(queue)

    host.add_urls_checked([_magnet(meta.info_hash)], out_dir=str(tmp_path))
    tid = _rows(db_path)[0]["id"]
    _inline(queue)
    queue._launch(queue.tasks[tid])

    children = [r for r in _rows(db_path) if r["source_type"] == SOURCE_TORRENT_FILE]
    assert unlocked == ["https://alldebrid.test/f/1"]
    assert [r["filename"] for r in children] == ["B.bin"]
    assert [r["url"] for r in children] == ["https://alldebrid.test/f/1"]
    assert rpc.torrents == []  # no local payload when the provider served it


def test_an_unmappable_cached_result_falls_back_with_the_subset_intact(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, decide=_accept((0, 2)),
        all_debrid_enabled=True, all_debrid_api_key="ad-key-value",
    )
    meta = _plan(rpc)
    # A manifest the provider reports under names that map to nothing.
    cached = _fixture_cached(meta, [("X.bin",), ("Y.bin",), ("Z.bin",)])
    monkeypatch.setattr(debrid, "resolve_torrent", lambda *a, **k: cached)
    _running(queue)

    host.add_urls_checked([_magnet(meta.info_hash)], out_dir=str(tmp_path))
    tid = _rows(db_path)[0]["id"]
    _inline(queue)
    queue._launch(queue.tasks[tid])

    assert [r for r in _rows(db_path) if r["source_type"] == SOURCE_TORRENT_FILE] == []
    assert rpc.torrents[0]["select_file"] == "1,3"


def test_no_provider_is_asked_before_the_user_presses_download(
    queue_env, monkeypatch, tmp_path
):
    probes = []
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, decide=_REJECT,
        all_debrid_enabled=True, all_debrid_api_key="ad-key-value",
    )
    monkeypatch.setattr(
        debrid, "resolve_torrent", lambda *a, **k: probes.append(a) or None
    )
    meta = _plan(rpc)

    host.add_urls_checked([_magnet(meta.info_hash)], out_dir=str(tmp_path))

    assert probes == []


def test_a_manual_magnet_never_reaches_the_download_file_info_dialog(
    queue_env, monkeypatch, tmp_path
):
    info_calls = []
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    monkeypatch.setattr(
        host, "_preflight_download_info",
        lambda prepared: (info_calls.append(prepared), prepared)[1],
    )
    meta = _plan(rpc)

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert info_calls == []
    assert len(contents.calls) == 1


def test_the_preflight_never_changes_the_global_destination(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, decide=_accept((0,))
    )
    before = queue.settings.download_dir
    meta = _plan(rpc)
    other = tmp_path / "elsewhere"
    other.mkdir()

    host.add_urls_checked([_magnet(meta.info_hash)], out_dir=str(other))

    assert queue.settings.download_dir == before


def test_a_single_file_magnet_still_opens_torrent_contents(
    queue_env, monkeypatch, tmp_path
):
    from tests.test_queue import _multi_file_torrent_bytes

    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, decide=_accept(None)
    )
    meta = _plan(rpc, raw=_multi_file_torrent_bytes(b"One", ((10, (b"only.bin",)),)))

    host.add_urls_checked([_magnet(meta.info_hash)], out_dir=str(tmp_path))

    assert len(contents.calls) == 1
    assert len(contents.calls[0].metadata.files) == 1
    assert _rows(db_path)[0]["selected_files"] == ""
