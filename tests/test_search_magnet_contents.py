"""Search-result magnet -> metadata resolver -> Torrent Contents, in the GUI.

Search results are magnets, and a magnet is only an info hash until the swarm
answers. Until now a chosen Search result committed immediately and downloaded
every file in the torrent. This module is the caller change that makes it enter
the *same* interactive preflight a manually pasted magnet already uses: the
reviewed resolver, the reviewed "Loading torrent metadata..." modal, the
reviewed Torrent Contents dialog and the reviewed commit.

Nothing here is a second implementation of any of those. What it does own:

*Search keeps its origin*: the intake label a Search download has always
carried survives the resolver, the preflight and the commit, and the
destination policy is exactly the one Search already had.

*The clicked result is the request*: the whole SearchResult crosses the
boundary at activation time, so refreshing, re-sorting, replacing or
destroying the rows underneath a pending resolution cannot retarget it.

*Search display context is not torrent metadata*: the result's title and
reported size describe a search hit. The manifest, the name and the file
indexes in Torrent Contents come from the verified metainfo, and only from it.
"""
import ast
import inspect
import time
from collections import deque
from pathlib import Path

import pytest
from PySide6.QtWidgets import QMainWindow

import cove.main_window as mw
from cove import debrid
from cove import torrent as torrent_mod
from cove.queue import (
    SOURCE_TORRENT,
    SOURCE_TORRENT_FILE,
    MagnetResolution,
)
from cove.search.models import SearchResult
from cove.search.widget import SearchWidget

# Harness reuse, deliberately total: the manual-magnet suite owns the Host,
# the dialog doubles and the deferred worker, and the resolver suite owns the
# aria2 metadata double and the torrent fixtures. A Search-shaped copy of
# either would be a second framework that could drift from the path it is
# supposed to be proving Search now shares.
from tests.test_queue import queue_env  # noqa: F401
from tests.test_queue import _rows
from tests.test_magnet_metadata import _MetadataRpc, _fixture_bytes, _magnet
from tests.test_manual_magnet_contents import (
    _REJECT,
    _accept,
    _env,
    _errors,
    _fixture_cached,
    _inline,
    _plan,
    _running,
    Host,
)

_QUEUED = "That torrent is already in Cove's queue."
_RESOLVING = "Cove is already fetching this torrent's file list."
_HELD = "That torrent is already waiting for you to choose its files."

#: What a Search source says a result is called. Chosen to differ from both
#: the magnet's `dn` and the metainfo name, so any of the three being used as
#: another's authority is visible rather than coincidentally right.
SEARCH_TITLE = "Search Display Name"
FAKE_DN = "Fake+Magnet+Name"
#: `_fixture_bytes()` names its torrent this. The Torrent Contents dialog must
#: show it, whatever the Search row says.
REAL_NAME = "Resolver Fixture"


def _result(
    meta,
    *,
    name=SEARCH_TITLE,
    dn=FAKE_DN,
    trackers=("http://127.0.0.1:1/announce",),
    size_bytes=999,
    source="nyaa",
):
    """A real SearchResult for `meta`, shaped like what a source returns."""
    return SearchResult(
        info_hash=meta.info_hash,
        name=name,
        magnet=_magnet(meta.info_hash, dn=dn, trackers=trackers),
        size_bytes=size_bytes,
        seeders=12,
        leechers=3,
        added=1_700_000_000,
        source=source,
    )


class _ServiceSpy:
    """Stands in for SearchService, counting every lifecycle call it gets.

    Only what a download could plausibly touch: starting another query,
    cancelling the current one, or reading the generation to invalidate a
    cache. Any of them happening because a result was downloaded is the bug.
    """

    def __init__(self):
        self.starts = []
        self.cancels = 0
        self.generation = 7
        self.active = False

    def start(self, query, category=None):
        self.starts.append((query, category))

    def cancel(self):
        self.cancels += 1


def _watched(host):
    """Give `host` a Search service double and return it."""
    host.search_service = _ServiceSpy()
    return host.search_service


# ---------------------------------------------------------------------------
# RED 1-3: which Search results reach the resolver
# ---------------------------------------------------------------------------


def test_a_search_magnet_is_resolved_before_anything_is_committed(
    queue_env, monkeypatch, tmp_path
):
    """The whole slice in one assertion set: resolver first, commit last."""
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)

    host.add_search_result(_result(meta))

    assert len(rpc.metadata_added) == 1
    assert loading.calls != []
    assert len(contents.calls) == 1
    assert len(_rows(db_path)) == 1  # committed only after the choice


def test_the_old_immediate_search_route_no_longer_runs(
    queue_env, monkeypatch, tmp_path
):
    """`add_urls` was the whole-magnet commit Search used to take."""
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, decide=_REJECT
    )
    meta = _plan(rpc)
    added = []
    monkeypatch.setattr(
        queue, "add_urls", lambda *a, **k: added.append((a, k)) or []
    )

    host.add_search_result(_result(meta))

    assert added == []
    assert _rows(db_path) == []


def test_a_search_result_whose_magnet_will_not_parse_creates_nothing(
    queue_env, monkeypatch, tmp_path
):
    """The resolver's own validation refuses it; Search adds no parser.

    A SearchResult validates its magnet on construction, so this is reached
    by handing the coordinator a hostile string directly - the shape a future
    source or a relaxed model could produce.
    """
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    errors = _errors(queue)
    probes = []
    monkeypatch.setattr(
        debrid, "resolve_torrent", lambda *a, **k: probes.append(a) or None
    )

    ids = host.add_urls_checked(["magnet:?xt=urn:btih:nothex"], intake="search")

    assert ids == []
    assert _rows(db_path) == []
    assert probes == []
    assert contents.calls == []
    assert rpc.metadata_added == []
    assert errors  # the resolver said why, exactly once


def test_the_downloadable_search_domain_is_magnet_only():
    """RED 2 is N/A by construction, and this is what makes it so.

    A SearchResult cannot exist carrying anything but a magnet that already
    parses to its own info hash, so there is no non-magnet Search target whose
    legacy route this slice could have broken. Recorded as a test rather than
    a comment: relaxing the model would have to come past this line.
    """
    fields = {f for f in SearchResult.__dataclass_fields__}
    assert "magnet" in fields
    assert not fields & {"url", "download_url", "torrent_url", "link"}
    with pytest.raises(ValueError):
        SearchResult(
            info_hash="0" * 39 + "1",
            name="n",
            magnet="https://example.invalid/x.torrent",
            size_bytes=1,
            seeders=0,
            leechers=0,
            added=None,
            source="s",
        )


def test_the_eligibility_gate_is_explicit_about_magnets(
    queue_env, monkeypatch, tmp_path
):
    """Search intake does not blanket-route: the URL is still checked."""
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    routed = []
    monkeypatch.setattr(
        host, "_magnet_preflight",
        lambda url, out_dir, intake: routed.append((url, out_dir, intake)) or [],
    )

    host.add_urls_checked(["https://example.invalid/a.zip"], intake="search")

    assert routed == []


# ---------------------------------------------------------------------------
# RED 4-5: Search origin and destination survive the preflight
# ---------------------------------------------------------------------------


def test_the_coordinators_default_origin_is_manual(
    queue_env, monkeypatch, tmp_path
):
    """Called with no origin at all, the shared coordinator says "manual".

    Both production callers pass one explicitly, so nothing else covers the
    default - and a default of "search" would silently relabel every future
    caller that forgets to.
    """
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)
    seen = []
    real = queue.resolve_magnet_preflight
    monkeypatch.setattr(
        queue, "resolve_magnet_preflight",
        lambda url, **kw: (seen.append(kw.get("intake")), real(url, **kw))[1],
    )

    host._magnet_preflight(_magnet(meta.info_hash), None)

    assert seen == ["manual"]
    assert len(contents.calls) == 1


def test_search_attribution_survives_the_resolver_and_the_commit(
    queue_env, monkeypatch, tmp_path
):
    """The committed task is a Search download, not a manual magnet.

    `intake` is the field Cove actually distinguishes origins by, and it now
    has to travel through a resolver, a preflight and a commit that were
    written for the manual path.
    """
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)
    seen = []
    real = queue.prepare_url
    monkeypatch.setattr(
        queue, "prepare_url",
        lambda url, **kw: (seen.append(kw.get("intake")), real(url, **kw))[1],
    )

    host.add_search_result(_result(meta))

    assert seen == ["search"]
    # Constrained to the interactive route, not merely to the label: the old
    # immediate path already said "search", and asserting only that would
    # stay green whether or not the resolver ran at all.
    assert len(rpc.metadata_added) == 1
    assert len(contents.calls) == 1
    assert len(_rows(db_path)) == 1


def test_the_resolver_is_told_the_request_is_a_search(
    queue_env, monkeypatch, tmp_path
):
    """Pinned at the resolver seam itself, not only at its far end."""
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)
    seen = []
    real = queue.resolve_magnet_preflight
    monkeypatch.setattr(
        queue, "resolve_magnet_preflight",
        lambda url, **kw: (seen.append(kw.get("intake")), real(url, **kw))[1],
    )

    host.add_search_result(_result(meta))

    assert seen == ["search"]


def test_a_manual_magnet_still_says_manual(queue_env, monkeypatch, tmp_path):
    """The shared coordinator must not relabel the origin it was given."""
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)
    seen = []
    real = queue.resolve_magnet_preflight
    monkeypatch.setattr(
        queue, "resolve_magnet_preflight",
        lambda url, **kw: (seen.append(kw.get("intake")), real(url, **kw))[1],
    )

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert seen == ["manual"]


def test_the_search_destination_is_the_one_search_always_had(
    queue_env, monkeypatch, tmp_path
):
    """Search passes no destination, so the queue's own default applies.

    Torrent Contents must show that same directory read-only, and the
    resolver's temporary workspace must never become Save to.
    """
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)

    host.add_search_result(_result(meta))

    assert contents.calls[0].save_to == queue.settings.download_dir
    assert _rows(db_path)[0]["out_dir"] == queue.settings.download_dir
    # And never the resolver's scratch directory, which is where the
    # metainfo -- but nothing the user asked for -- was fetched.
    workspace = rpc.metadata_added[0]["out_dir"]
    assert contents.calls[0].save_to != workspace
    assert _rows(db_path)[0]["out_dir"] != workspace


# ---------------------------------------------------------------------------
# RED 6-8, 70: authority - Search displays, metainfo decides
# ---------------------------------------------------------------------------


def test_torrent_contents_is_named_by_the_metainfo_not_the_search_row(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)

    host.add_search_result(_result(meta))

    shown = contents.calls[0].metadata
    assert shown.name == REAL_NAME
    assert shown.name != SEARCH_TITLE
    assert "Fake" not in shown.name


def test_the_committed_name_is_the_metainfo_name(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)

    host.add_search_result(_result(meta))

    # The route that no longer runs took this straight from the magnet's
    # `dn`, so a regression here reads back as the stranger's name.
    assert _rows(db_path)[0]["torrent_name"] == REAL_NAME
    assert "Fake" not in _rows(db_path)[0]["torrent_name"]


def test_the_search_reported_size_never_becomes_the_manifest(
    queue_env, monkeypatch, tmp_path
):
    """A source's aggregate is a hint. The file list is measured."""
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)

    host.add_search_result(_result(meta, size_bytes=999_999_999))

    shown = contents.calls[0].metadata
    assert [f.size for f in shown.files] == [10, 20, 30]
    assert [f.index for f in shown.files] == [0, 1, 2]
    assert sum(f.size for f in shown.files) != 999_999_999


def test_the_search_result_object_never_reaches_the_dialogs(
    queue_env, monkeypatch, tmp_path
):
    """Structural: only a magnet string crosses into the coordinator."""
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)
    result = _result(meta)

    host.add_search_result(result)

    assert contents.calls[0].metadata is not result
    for dlg in contents.calls:
        assert not isinstance(dlg.save_to, SearchResult)


def test_the_search_row_keeps_showing_the_search_title(monkeypatch):
    """Adding Torrent Contents must not rewrite the result table."""
    widget = SearchWidget()
    try:
        raw = _fixture_bytes()
        meta = torrent_mod.parse_torrent(raw)
        result = _result(meta)
        widget.set_results((result,))

        assert widget.table.item(0, 0).text() == SEARCH_TITLE
        assert widget.selected_result() is None
        widget.table.selectRow(0)
        assert widget.selected_result() is result
    finally:
        widget.deleteLater()


# ---------------------------------------------------------------------------
# RED 9-12, 69: the clicked result is the request
# ---------------------------------------------------------------------------


def _wired(host):
    """A real SearchWidget wired to the real window handler."""
    widget = SearchWidget()
    widget.download_requested.connect(host._on_search_download_requested)
    host.search_widget = widget
    return widget


def test_the_clicked_result_owns_the_request_after_the_list_is_replaced(
    queue_env, monkeypatch, tmp_path
):
    """Click A, then let the table become B while A's metadata is loading."""
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, hooks=[None]
    )
    raw_a, raw_b = _fixture_bytes(), _fixture_bytes(b"Other Fixture")
    meta_a = _plan(rpc, raw=raw_a)
    meta_b = torrent_mod.parse_torrent(raw_b)
    widget = _wired(host)
    result_a, result_b = _result(meta_a), _result(meta_b, name="Other Row")
    widget.set_results((result_a,))
    widget.table.selectRow(0)

    def swap_then_deliver(dlg):
        # The service published a newer snapshot while the modal was up.
        widget.set_results((result_b,))
        deliver(dlg)

    loading.hooks = deque([swap_then_deliver])
    widget.download_button.click()

    assert len(contents.calls) == 1
    assert contents.calls[0].metadata.info_hash == meta_a.info_hash
    assert _rows(db_path)[0]["info_hash"] == meta_a.info_hash


def test_a_reorder_under_a_pending_request_cannot_retarget_it(
    queue_env, monkeypatch, tmp_path
):
    """The row at position N changes; the request does not."""
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, hooks=[None]
    )
    raw_a, raw_b = _fixture_bytes(), _fixture_bytes(b"Other Fixture")
    meta_a = _plan(rpc, raw=raw_a)
    meta_b = torrent_mod.parse_torrent(raw_b)
    widget = _wired(host)
    result_a, result_b = _result(meta_a), _result(meta_b, name="Other Row")
    widget.set_results((result_b, result_a))
    widget.table.selectRow(1)

    loading.hooks = deque([
        lambda dlg: (widget.set_results((result_a, result_b)), deliver(dlg))[1]
    ])
    widget.download_button.click()

    assert contents.calls[0].metadata.info_hash == meta_a.info_hash


def test_clearing_the_results_does_not_invalidate_a_pending_request(
    queue_env, monkeypatch, tmp_path
):
    """The rows the click came from are gone before the metadata lands."""
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, hooks=[None]
    )
    meta = _plan(rpc)
    widget = _wired(host)
    widget.set_results((_result(meta),))
    widget.table.selectRow(0)

    loading.hooks = deque([
        lambda dlg: (widget.set_results(()), deliver(dlg))[1]
    ])
    widget.download_button.click()

    assert len(contents.calls) == 1
    assert len(_rows(db_path)) == 1


def test_destroying_the_result_row_does_not_break_a_late_success(
    queue_env, monkeypatch, tmp_path
):
    """RED 69: the widget itself is gone by the time the resolver answers."""
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, hooks=[None]
    )
    meta = _plan(rpc)
    widget = _wired(host)
    widget.set_results((_result(meta),))
    widget.table.selectRow(0)

    def drop_the_table(dlg):
        widget.table.clearContents()
        widget.table.setRowCount(0)
        deliver(dlg)

    loading.hooks = deque([drop_the_table])
    widget.download_button.click()

    assert len(contents.calls) == 1
    assert len(_rows(db_path)) == 1
    widget.deleteLater()


def test_no_row_or_list_index_is_the_asynchronous_identity():
    """Structural: the coordinator is handed a URL, never a position.

    A row index would be correct in every test where nothing moves, which is
    exactly why this is checked in the source rather than only in behaviour.
    """
    source = inspect.getsource(mw.MainWindow._on_search_download_requested)
    source += inspect.getsource(mw.MainWindow.add_search_result)
    for forbidden in (
        "currentRow", "selectedRows", "selected_result", "row(", "table",
        "_results", "search_widget",
    ):
        assert forbidden not in source, forbidden


def test_cancelling_the_search_query_does_not_cancel_a_chosen_download(
    queue_env, monkeypatch, tmp_path
):
    """RED 12: two different operations, two different ownerships."""
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, hooks=[None]
    )
    meta = _plan(rpc)
    cancels = []
    real_cancel = MagnetResolution.cancel
    monkeypatch.setattr(
        MagnetResolution, "cancel",
        lambda self: (cancels.append(self), real_cancel(self))[1],
    )
    service = _watched(host)

    def cancel_the_search(dlg):
        host.search_service.cancel()
        deliver(dlg)

    loading.hooks = deque([cancel_the_search])
    host.add_search_result(_result(meta))

    assert service.cancels == 1
    assert cancels == []  # the download preflight was never asked to stop
    assert len(contents.calls) == 1
    assert len(_rows(db_path)) == 1


# ---------------------------------------------------------------------------
# RED 13-16: the existing dialogs, and nothing durable before the choice
# ---------------------------------------------------------------------------


def test_search_uses_the_two_existing_dialogs_and_no_others(
    queue_env, monkeypatch, tmp_path
):
    """Both doubles stand in for the reviewed classes by name.

    Patching `mw.MagnetMetadataDialog` and `mw.TorrentContentsDialog` is what
    makes this an identity check rather than a resemblance one: a Search-only
    dialog would leave both counts at zero.
    """
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)

    host.add_search_result(_result(meta))

    assert len(loading.calls) == 1
    assert loading.max_open == 1
    assert len(contents.calls) == 1


def test_no_search_specific_dialog_class_exists():
    """Structural: one loading UI and one file selector, for every origin."""
    from cove import dialogs

    for forbidden in (
        "SearchTorrentContentsDialog", "SearchMagnetContentsDialog",
        "TorrentSearchContentsDialog", "SearchMetadataDialog",
    ):
        assert not hasattr(dialogs, forbidden), forbidden
        assert not hasattr(mw, forbidden), forbidden


def test_nothing_durable_exists_while_search_metadata_loads(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, hooks=[None],
        all_debrid_enabled=True, all_debrid_api_key="ad-key-value",
    )
    probes = []
    monkeypatch.setattr(
        debrid, "resolve_torrent", lambda *a, **k: probes.append(a) or None
    )
    meta = _plan(rpc, pending=3)
    observed = {}

    def look_around(dlg):
        observed["rows"] = _rows(db_path)
        observed["tasks"] = dict(queue.tasks)
        observed["payloads"] = list(rpc.magnets) + list(rpc.torrents)
        observed["contents"] = list(contents.calls)
        deliver(dlg)

    loading.hooks = deque([look_around])
    host.add_search_result(_result(meta))

    assert observed["rows"] == []
    assert observed["tasks"] == {}
    assert observed["payloads"] == []
    assert observed["contents"] == []
    assert probes == []


def test_nothing_durable_exists_while_search_torrent_contents_is_open(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path,
        all_debrid_enabled=True, all_debrid_api_key="ad-key-value",
    )
    probes = []
    monkeypatch.setattr(
        debrid, "resolve_torrent", lambda *a, **k: probes.append(a) or None
    )
    meta = _plan(rpc)
    observed = {}

    contents.during_exec = lambda dlg: observed.update(
        rows=_rows(db_path), tasks=dict(queue.tasks),
        payloads=list(rpc.magnets) + list(rpc.torrents),
    )
    host.add_search_result(_result(meta))

    assert observed["rows"] == []
    assert observed["tasks"] == {}
    assert observed["payloads"] == []
    assert probes == []


def test_a_search_magnet_never_reaches_the_download_file_info_dialog(
    queue_env, monkeypatch, tmp_path
):
    """That dialog describes a direct HTTP download; a magnet has no size."""
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)
    shown = []
    monkeypatch.setattr(
        host, "_preflight_download_info",
        lambda prepared: shown.append(prepared) or prepared,
    )

    host.add_search_result(_result(meta))

    assert shown == []
    # The magnet did go somewhere interactive - just not to this dialog.
    assert len(contents.calls) == 1


# ---------------------------------------------------------------------------
# RED 17-20: the selection domain, inherited whole
# ---------------------------------------------------------------------------


def test_every_file_selected_commits_the_legacy_whole_torrent_choice(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, decide=_accept(None)
    )
    meta = _plan(rpc)

    host.add_search_result(_result(meta))

    assert len(contents.calls) == 1  # the choice was actually offered
    assert _rows(db_path)[0]["selected_files"] == ""


def test_a_proper_subset_commits_canonical_zero_based_indexes(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, decide=_accept((2, 0))
    )
    meta = _plan(rpc)

    host.add_search_result(_result(meta))

    # Sorted, deduplicated, 0-based - exactly the existing domain.
    assert _rows(db_path)[0]["selected_files"] == "0,2"


def test_an_empty_selection_commits_nothing(queue_env, monkeypatch, tmp_path):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, decide=_accept(())
    )
    errors = _errors(queue)
    meta = _plan(rpc)

    ids = host.add_search_result(_result(meta))

    assert ids == []
    assert _rows(db_path) == []
    assert errors


def test_cancelling_torrent_contents_commits_nothing(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, decide=_REJECT
    )
    meta = _plan(rpc)

    ids = host.add_search_result(_result(meta))

    assert ids == []
    assert _rows(db_path) == []
    assert meta.info_hash not in queue._preflight_hashes


# ---------------------------------------------------------------------------
# RED 21-27: cancel, failure and timeout all fail closed
# ---------------------------------------------------------------------------


def _cancel_then_deliver(deliver):
    def hook(dlg):
        dlg.cancelled.emit()
        deliver(dlg)
    return hook


def test_cancelling_search_metadata_cancels_the_resolution_only(
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

    host.add_search_result(_result(meta))

    assert len(cancels) == 1
    assert contents.calls == []
    assert _rows(db_path) == []
    assert rpc.magnets == []
    assert meta.info_hash not in queue._preflight_hashes


@pytest.mark.parametrize("plan", [{"add_error": True}, {"no_artifact": True}])
def test_a_failed_search_resolution_never_falls_back_to_the_whole_magnet(
    queue_env, monkeypatch, tmp_path, plan
):
    """The one outcome this slice exists to prevent."""
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc, **plan)

    ids = host.add_search_result(_result(meta))

    assert ids == []
    assert contents.calls == []
    assert _rows(db_path) == []
    assert rpc.magnets == []
    assert meta.info_hash not in queue._preflight_hashes


def test_a_timed_out_search_resolution_fails_closed(
    queue_env, monkeypatch, tmp_path
):
    """Injected, not waited for: no wall clock is spent proving this."""
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, hooks=[None]
    )
    meta = _plan(rpc, pending=10_000)

    def time_out(dlg):
        fn, args, kwargs, on_done, on_fail = pending.pop()
        on_fail("Timed out waiting for this torrent's file list.")

    loading.hooks = deque([time_out])
    started = time.monotonic()
    ids = host.add_search_result(_result(meta))
    elapsed = time.monotonic() - started

    # Cost, not just outcome: the reviewed resolver's real deadline is 60s,
    # and a version of this that actually waited it out would still satisfy
    # every assertion below.
    assert elapsed < 5, elapsed
    assert ids == []
    assert contents.calls == []
    assert _rows(db_path) == []
    assert rpc.magnets == []
    assert meta.info_hash not in queue._preflight_hashes


@pytest.mark.parametrize(
    "stop", ["metadata", "contents"], ids=["loading-cancel", "contents-cancel"]
)
def test_the_same_search_result_can_be_retried_after_it_was_stopped(
    queue_env, monkeypatch, tmp_path, stop
):
    """A stopped request must release the torrent, not poison it."""
    decide = _REJECT if stop == "contents" else _accept(None)
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, decide=decide, hooks=[None]
    )
    meta = _plan(rpc, pending=3 if stop == "metadata" else 0)
    result = _result(meta)
    loading.hooks = deque([
        _cancel_then_deliver(deliver) if stop == "metadata" else deliver
    ])

    host.add_search_result(result)
    assert _rows(db_path) == []
    assert meta.info_hash not in queue._preflight_hashes

    # Second attempt on the very same result object.
    rpc.plan(meta.info_hash, payload=_fixture_bytes())
    contents._decide = _accept(None)
    loading.hooks = deque([deliver])
    ids = host.add_search_result(result)

    assert len(ids) == 1
    assert len(_rows(db_path)) == 1


# ---------------------------------------------------------------------------
# RED 28-31: one resolution, one publication, the original magnet
# ---------------------------------------------------------------------------


def test_one_search_download_resolves_exactly_once(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)

    host.add_search_result(_result(meta))

    assert len(rpc.metadata_added) == 1
    assert len(loading.calls) == 1
    assert len(_rows(db_path)) == 1


def test_search_publishes_no_second_managed_copy(
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

    host.add_search_result(_result(meta))

    assert stores == [meta.info_hash]


def test_the_original_search_magnet_reaches_the_resolver_byte_for_byte(
    queue_env, monkeypatch, tmp_path
):
    """No infohash-only rebuild: `dn` and every tracker survive."""
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)
    trackers = ("http://127.0.0.1:6969/announce", "http://127.0.0.1:7070/announce")
    result = _result(meta, trackers=trackers)

    host.add_search_result(result)

    assert rpc.metadata_added[0]["uri"] == result.magnet
    for tracker in trackers:
        assert tracker in rpc.metadata_added[0]["uri"]


def test_the_committed_torrent_keeps_the_search_magnets_trackers(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)
    trackers = ("http://127.0.0.1:6969/announce",)
    result = _result(meta, trackers=trackers)

    host.add_search_result(result)

    assert rpc.metadata_added[0]["uri"] == result.magnet
    assert _rows(db_path)[0]["url"] == result.magnet
    assert trackers[0] in _rows(db_path)[0]["url"]


def test_search_never_reaches_into_the_resolver_internals():
    """Structural: no aria2 options, no parser, no workspace, in Search."""
    search_dir = Path(inspect.getfile(mw)).parent / "search"
    blob = "".join(
        p.read_text(encoding="utf-8") for p in sorted(search_dir.rglob("*.py"))
    )
    for forbidden in (
        "bt-metadata-only", "bt-save-metadata", "followedBy", "parse_torrent",
        "store_managed_torrent", "TorrentPreflight", "resolve_magnet_preflight",
        "MagnetMetadataDialog", "TorrentContentsDialog", "commit_torrent_preflight",
    ):
        assert forbidden not in blob, forbidden


# ---------------------------------------------------------------------------
# RED 32-37: the routes after the user presses Download
# ---------------------------------------------------------------------------


def test_a_search_subset_reaches_aria2_one_based_exactly_once(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, decide=_accept((1, 2))
    )
    meta = _plan(rpc)
    _running(queue)

    host.add_search_result(_result(meta))
    tid = _rows(db_path)[0]["id"]
    _inline(queue)
    queue._launch(queue.tasks[tid])

    assert rpc.torrents[0]["select_file"] == "2,3"


def test_a_search_download_of_every_file_sets_no_select_file_override(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, decide=_accept(None)
    )
    meta = _plan(rpc)
    _running(queue)

    host.add_search_result(_result(meta))
    tid = _rows(db_path)[0]["id"]
    _inline(queue)
    queue._launch(queue.tasks[tid])

    assert rpc.torrents[0]["select_file"] is None


def test_a_search_subset_reaches_the_cached_provider_route(
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
            debrid.Unrestricted("https://cdn.test/B.bin", "B.bin", 20, provider),
        )[1],
    )
    _running(queue)

    host.add_search_result(_result(meta))
    tid = _rows(db_path)[0]["id"]
    _inline(queue)
    queue._launch(queue.tasks[tid])

    children = [r for r in _rows(db_path) if r["source_type"] == SOURCE_TORRENT_FILE]
    assert unlocked == ["https://alldebrid.test/f/1"]
    assert [r["filename"] for r in children] == ["B.bin"]
    assert rpc.torrents == []


def test_an_unmappable_cached_search_result_falls_back_with_the_subset(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, decide=_accept((0, 2)),
        all_debrid_enabled=True, all_debrid_api_key="ad-key-value",
    )
    meta = _plan(rpc)
    cached = _fixture_cached(meta, [("X.bin",), ("Y.bin",), ("Z.bin",)])
    monkeypatch.setattr(debrid, "resolve_torrent", lambda *a, **k: cached)
    _running(queue)

    host.add_search_result(_result(meta))
    tid = _rows(db_path)[0]["id"]
    _inline(queue)
    queue._launch(queue.tasks[tid])

    assert [r for r in _rows(db_path) if r["source_type"] == SOURCE_TORRENT_FILE] == []
    assert rpc.torrents[0]["select_file"] == "1,3"


def test_no_provider_is_asked_before_the_user_presses_download(
    queue_env, monkeypatch, tmp_path
):
    probes = []
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, decide=_REJECT,
        all_debrid_enabled=True, all_debrid_api_key="ad-key-value",
    )
    monkeypatch.setattr(
        debrid, "resolve_torrent", lambda *a, **k: probes.append(a) or None
    )
    meta = _plan(rpc)

    host.add_search_result(_result(meta))

    assert probes == []
    # The user was asked and said no, so the probe never became due - the
    # point is that it was not made *before* the asking either.
    assert len(contents.calls) == 1
    assert _rows(db_path) == []


def test_a_committed_search_torrent_is_an_ordinary_torrent_task(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)

    host.add_search_result(_result(meta))

    row = _rows(db_path)[0]
    assert len(contents.calls) == 1
    assert row["source_type"] == SOURCE_TORRENT
    assert row["info_hash"] == meta.info_hash


# ---------------------------------------------------------------------------
# RED 38-53: ownership and FIFO, across every origin
# ---------------------------------------------------------------------------


def test_activating_the_same_result_twice_starts_one_resolver(
    queue_env, monkeypatch, tmp_path
):
    """A double-click, or a click plus a double-click, is still one request."""
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, hooks=[None]
    )
    meta = _plan(rpc)
    result = _result(meta)

    def click_again(dlg):
        host.add_search_result(result)
        deliver(dlg)

    loading.hooks = deque([click_again, deliver])
    host.add_search_result(result)

    assert len(rpc.metadata_added) == 1
    assert len(contents.calls) == 1
    assert len(_rows(db_path)) == 1


def test_a_second_search_result_waits_rather_than_stacking_a_modal(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, hooks=[None, None]
    )
    raw_a, raw_b = _fixture_bytes(), _fixture_bytes(b"Other Fixture")
    meta_a = _plan(rpc, raw=raw_a)
    meta_b = _plan(rpc, raw=raw_b)

    def start_b(dlg):
        host.add_search_result(_result(meta_b, name="B"))
        deliver(dlg)

    loading.hooks = deque([start_b, deliver])
    host.add_search_result(_result(meta_a, name="A"))

    assert loading.max_open == 1
    assert [c.metadata.info_hash for c in contents.calls] == [
        meta_a.info_hash, meta_b.info_hash
    ]
    assert sorted(r["info_hash"] for r in _rows(db_path)) == sorted(
        [meta_a.info_hash, meta_b.info_hash]
    )


@pytest.mark.parametrize(
    "ending",
    ["cancel", "error", "contents-cancel", "commit"],
)
def test_however_the_first_search_request_ends_the_next_one_proceeds(
    queue_env, monkeypatch, tmp_path, ending
):
    """RED 41-44: no terminal path may strand what is queued behind it."""
    decide_first = _REJECT if ending == "contents-cancel" else _accept(None)
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, decide=decide_first, hooks=[None, None]
    )
    raw_a, raw_b = _fixture_bytes(), _fixture_bytes(b"Other Fixture")
    meta_a = _plan(rpc, raw=raw_a, **({"add_error": True} if ending == "error" else {}))
    meta_b = _plan(rpc, raw=raw_b)

    def start_b_then_finish_a(dlg):
        # B is asked for while A owns the interactive slot, so it waits.
        host.add_search_result(_result(meta_b, name="B"))
        if ending == "cancel":
            dlg.cancelled.emit()
        deliver(dlg)

    # Only A ends badly; B must be offered and committed regardless.
    contents._decide = lambda metadata: (
        (False, None) if (ending == "contents-cancel"
                          and metadata.info_hash == meta_a.info_hash)
        else (True, None)
    )
    loading.hooks = deque([start_b_then_finish_a, deliver])
    host.add_search_result(_result(meta_a, name="A"))

    committed = [r["info_hash"] for r in _rows(db_path)]
    assert meta_b.info_hash in committed, ending
    if ending in ("cancel", "error", "contents-cancel"):
        assert meta_a.info_hash not in committed, ending
        assert meta_a.info_hash not in queue._preflight_hashes


@pytest.mark.parametrize(
    "first,second",
    [("manual", "search"), ("search", "manual"), ("search", "search")],
)
def test_the_same_torrent_cannot_be_owned_twice_across_origins(
    queue_env, monkeypatch, tmp_path, first, second
):
    """RED 45-48: one canonical hash, one owner, whatever asked for it."""
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, hooks=[None]
    )
    meta = _plan(rpc, pending=3)
    errors = _errors(queue)
    result = _result(meta)

    def submit(kind):
        if kind == "search":
            return host.add_search_result(result)
        return host.add_urls_checked([_magnet(meta.info_hash)])

    def collide(dlg):
        submit(second)
        deliver(dlg)

    loading.hooks = deque([collide])
    submit(first)

    assert len(rpc.metadata_added) == 1, (first, second)
    assert len(contents.calls) == 1
    assert len(_rows(db_path)) == 1
    assert any(_RESOLVING in e or _QUEUED in e for e in errors)


def test_a_local_torrent_preflight_blocks_the_same_search_result(
    queue_env, monkeypatch, tmp_path
):
    """RED 47: the `.torrent` origin holds the hash the same way."""
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    raw = _fixture_bytes()
    meta = _plan(rpc, raw=raw)
    errors = _errors(queue)
    path = tmp_path / "held.torrent"
    path.write_bytes(raw)
    held = []

    def hold(request):
        held.append(request)
        # The dialog is on screen: the Search result arrives now.
        host.add_search_result(_result(meta))
        return None

    _inline(queue)
    queue.add_torrent_file(str(path), str(tmp_path), precommit=hold)

    assert len(held) == 1
    assert rpc.metadata_added == []  # no second resolver started
    assert _rows(db_path) == []
    # Refused by the existing held-hash registry, in its own words.
    assert any(_HELD in e for e in errors), errors


def test_a_live_search_torrent_blocks_a_second_identical_search_result(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)
    result = _result(meta)

    host.add_search_result(result)
    before = len(rpc.metadata_added)
    host.add_search_result(result)

    assert len(rpc.metadata_added) == before
    assert len(_rows(db_path)) == 1
    assert host.duplicate_prompts  # the ordinary gate still asked


def test_an_unrelated_search_result_is_not_a_false_duplicate(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta_a = _plan(rpc, raw=_fixture_bytes())
    meta_b = _plan(rpc, raw=_fixture_bytes(b"Other Fixture"))

    host.add_search_result(_result(meta_a, name="A"))
    host.add_search_result(_result(meta_b, name="B"))

    assert len(rpc.metadata_added) == 2
    assert sorted(r["info_hash"] for r in _rows(db_path)) == sorted(
        [meta_a.info_hash, meta_b.info_hash]
    )


def test_a_manual_magnet_asked_for_during_a_search_request_waits_its_turn(
    queue_env, monkeypatch, tmp_path
):
    """RED 51/53: one interactive magnet preflight at a time, either order."""
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, hooks=[None, None]
    )
    raw_a, raw_b = _fixture_bytes(), _fixture_bytes(b"Other Fixture")
    meta_a = _plan(rpc, raw=raw_a)
    meta_b = _plan(rpc, raw=raw_b)

    def start_manual(dlg):
        host.add_urls_checked([_magnet(meta_b.info_hash)])
        deliver(dlg)

    loading.hooks = deque([start_manual, deliver])
    host.add_search_result(_result(meta_a))

    assert loading.max_open == 1
    assert contents.open_now is False
    assert sorted(r["info_hash"] for r in _rows(db_path)) == sorted(
        [meta_a.info_hash, meta_b.info_hash]
    )


def test_a_local_torrent_landing_during_search_contents_waits_its_turn(
    queue_env, monkeypatch, tmp_path
):
    """RED 52: the two origins share the one Torrent Contents modal.

    The `.torrent` parse finishes while the Search result's file choice is on
    screen, which is the only moment the two can collide. `_Contents` refuses
    to open a second dialog inside the first, so a nested modal fails here
    rather than being asserted about afterwards.
    """
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, decide=_accept(None)
    )
    raw_a, raw_b = _fixture_bytes(), _fixture_bytes(b"Other Fixture")
    meta_a = _plan(rpc, raw=raw_a)
    meta_b = torrent_mod.parse_torrent(raw_b)
    path = tmp_path / "second.torrent"
    path.write_bytes(raw_b)

    def land_a_local_torrent(dlg):
        _inline(queue)
        queue.add_torrent_file(
            str(path), str(tmp_path), precommit=host._torrent_preflight
        )

    contents.during_exec = land_a_local_torrent
    host.add_search_result(_result(meta_a))

    assert [c.metadata.info_hash for c in contents.calls] == [
        meta_a.info_hash, meta_b.info_hash
    ]
    assert contents.open_now is False
    assert sorted(r["info_hash"] for r in _rows(db_path)) == sorted(
        [meta_a.info_hash, meta_b.info_hash]
    )


# ---------------------------------------------------------------------------
# RED 56-60: what a download must not cost the Search subsystem
# ---------------------------------------------------------------------------


def test_downloading_a_result_never_queries_the_search_sources_again(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)
    service = _watched(host)

    host.add_search_result(_result(meta))

    assert service.starts == []
    assert service.cancels == 0
    assert service.generation == 7
    # And the download really did happen, the long way round.
    assert len(rpc.metadata_added) == 1
    assert len(contents.calls) == 1


def test_the_download_path_never_touches_the_search_service():
    """Structural: no requery, no cache flush, no re-rank, in the source."""
    source = inspect.getsource(mw.MainWindow._on_search_download_requested)
    source += inspect.getsource(mw.MainWindow.add_search_result)
    source += inspect.getsource(mw.MainWindow._magnet_preflight)
    source += inspect.getsource(mw.MainWindow._resolve_magnet_request)
    for forbidden in (
        "search_service", "SearchService", "set_results", "set_status",
        "invalidate", "_cache", "relevance", "rank",
    ):
        assert forbidden not in source, forbidden


def test_downloading_a_result_leaves_the_rows_and_their_order_alone(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    raw_a, raw_b = _fixture_bytes(), _fixture_bytes(b"Other Fixture")
    meta_a = _plan(rpc, raw=raw_a)
    meta_b = torrent_mod.parse_torrent(raw_b)
    widget = _wired(host)
    results = (_result(meta_b, name="Zed"), _result(meta_a, name="Alpha"))
    widget.set_results(results)
    # Captured before the click rather than asserted as the supplied order:
    # the widget sorts rows for display (see tests/test_search_sorting.py), and
    # what this test is about is that downloading disturbs nothing.
    before = [widget.table.item(r, 0).text() for r in range(widget.table.rowCount())]
    rendered = [
        widget.table.item(r, 0).data(mw.Qt.UserRole)
        for r in range(widget.table.rowCount())
    ]
    widget.table.selectRow(before.index("Alpha"))

    widget.download_button.click()

    shown = [widget.table.item(r, 0).text() for r in range(widget.table.rowCount())]
    assert shown == before
    assert [
        widget.table.item(r, 0).data(mw.Qt.UserRole)
        for r in range(widget.table.rowCount())
    ] == rendered
    assert {id(r) for r in rendered} == {id(r) for r in results}
    widget.deleteLater()


def test_the_source_column_still_shows_the_configured_display_name():
    """RED 60: the custom-indexer display-name work is untouched."""
    widget = SearchWidget(custom_source_names=lambda: {"custom:abc": "My Indexer"})
    try:
        meta = torrent_mod.parse_torrent(_fixture_bytes())
        widget.set_results((_result(meta, source="custom:abc"),))
        assert widget.table.item(0, 5).text() == "My Indexer"
    finally:
        widget.deleteLater()


# ---------------------------------------------------------------------------
# RED 61-65: every other origin, unchanged
# ---------------------------------------------------------------------------


def test_a_manual_magnet_still_takes_the_manual_route(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, decide=_accept((1,))
    )
    meta = _plan(rpc)

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert len(loading.calls) == 1
    assert len(contents.calls) == 1
    assert _rows(db_path)[0]["selected_files"] == "1"


def test_a_local_torrent_file_still_opens_contents_without_a_loading_modal(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    raw = _fixture_bytes()
    path = tmp_path / "local.torrent"
    path.write_bytes(raw)
    _inline(queue)

    queue.add_torrent_file(
        str(path), str(tmp_path), precommit=host._torrent_preflight
    )

    assert loading.calls == []
    assert len(contents.calls) == 1


@pytest.mark.parametrize("intake", ["api", "extension", "clipboard"])
def test_non_gui_magnet_intakes_stay_non_interactive(
    queue_env, monkeypatch, tmp_path, intake
):
    """RED 64-65: no modal may appear for a caller that cannot answer one."""
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)

    host.add_urls_checked([_magnet(meta.info_hash)], intake=intake)

    assert loading.calls == []
    assert contents.calls == []
    assert len(_rows(db_path)) == 1


def test_a_batch_containing_a_search_magnet_is_still_not_interactive(
    queue_env, monkeypatch, tmp_path
):
    """Only the single-result Search gesture is interactive; batches are not."""
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta_a = _plan(rpc, raw=_fixture_bytes())
    meta_b = _plan(rpc, raw=_fixture_bytes(b"Other Fixture"))

    host.add_urls_checked(
        [_magnet(meta_a.info_hash), _magnet(meta_b.info_hash)], intake="search"
    )

    assert loading.calls == []
    assert len(_rows(db_path)) == 2


# ---------------------------------------------------------------------------
# RED 66-68: shutdown
# ---------------------------------------------------------------------------


def test_quit_while_search_metadata_is_loading_ends_the_modal(
    queue_env, monkeypatch, tmp_path
):
    seen = {}
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, hooks=[None]
    )
    monkeypatch.setattr(
        mw.QApplication, "quit",
        staticmethod(lambda: seen.update(
            quit_called=True, modal_finished=loading.calls[0].finished)),
    )
    loading.hooks = deque([lambda dlg: host.request_quit()])
    meta = _plan(rpc, pending=10_000)

    host.add_search_result(_result(meta))

    assert seen == {"quit_called": True, "modal_finished": True}, seen
    assert contents.calls == []
    assert _rows(db_path) == []
    assert meta.info_hash not in queue._preflight_hashes


def test_quit_while_search_torrent_contents_is_open_discards_the_preflight(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, decide=_accept(None)
    )
    monkeypatch.setattr(mw.QApplication, "quit", staticmethod(lambda: None))
    meta = _plan(rpc)
    contents.during_exec = lambda dlg: host.request_quit()

    host.add_search_result(_result(meta))

    assert contents.rejected == contents.calls
    assert _rows(db_path) == []
    assert meta.info_hash not in queue._preflight_hashes


def test_a_search_request_queued_behind_shutdown_never_opens_a_modal(
    queue_env, monkeypatch, tmp_path
):
    """RED 68: the latch refuses a request that arrives after Cove is leaving."""
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path
    )
    meta = _plan(rpc)
    host.close_interactive_preflights()

    ids = host.add_search_result(_result(meta))

    assert ids == []
    assert loading.calls == []
    assert contents.calls == []
    assert _rows(db_path) == []


def test_the_shutdown_sweep_stops_a_pending_search_resolution(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, hooks=[None]
    )
    meta = _plan(rpc, pending=10_000)
    loading.hooks = deque([lambda dlg: host.discard_magnet_requests()])

    host.add_search_result(_result(meta))

    assert loading.calls[0].finished is True
    assert contents.calls == []
    assert _rows(db_path) == []
    assert meta.info_hash not in queue._preflight_hashes


# ---------------------------------------------------------------------------
# Structural: the slice stayed a caller change
# ---------------------------------------------------------------------------


def test_the_search_download_handler_still_uses_only_the_intake_boundary():
    """The widget -> window seam is unchanged by this slice."""
    source = Path(inspect.getfile(mw)).read_text(encoding="utf-8")
    node = next(
        n for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.FunctionDef)
        and n.name == "_on_search_download_requested"
    )
    calls = {
        child.func.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
    }
    assert calls == {"add_search_result"}


def test_the_widget_still_emits_the_whole_result_object():
    """GATE D at its source: the signal carries the result, not a position."""
    source = inspect.getsource(SearchWidget._request_download)
    source += inspect.getsource(SearchWidget._request_download_for_row)
    assert "download_requested.emit(result)" in source
    assert "emit(row)" not in source


def test_search_owns_no_second_coordinator():
    """One interactive magnet coordinator, reached by every eligible origin."""
    names = [n for n in dir(mw.MainWindow) if "magnet" in n and "search" in n]
    assert names == []
