"""Torrent Contents preflight: the dialog, and the GUI local `.torrent` hook.

Two halves, deliberately separated.

The dialog half is pure presentation over an already-parsed manifest. It
never parses a `.torrent`, stores one, creates a task, writes a row, probes a
provider, calls aria2 or serialises anything: it turns a `TorrentMetadata`
into a checkable tree and answers one question, "which files".

The intake half is the MainWindow coordinator that puts the dialog between a
GUI-originated local `.torrent` and any commitment. The order it pins is
parse -> managed copy -> side-effect-free prepare -> dialog -> commit, so a
cancelled preflight leaves nothing behind at all.

The domain answer is the point of both halves: every file selected is
`None` (the exact legacy whole-torrent path), a proper subset is the
canonical 0-based tuple Slice 1 defined, and no selection at all can be
confirmed.
"""
from collections import deque
from dataclasses import replace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMainWindow

import cove.main_window as mw
from cove import torrent as torrent_mod
from cove.dialogs import TorrentContentsDialog
from cove.queue import SOURCE_TORRENT, TorrentPreflight
from cove.torrent import TorrentError, TorrentFile, TorrentMetadata

# Fixture reuse: the real QueueManager environment lives in the queue suite.
from tests.test_queue import queue_env  # noqa: F401
from tests.test_queue import (
    _local_settings,
    _multi_file_torrent_bytes,
    _rows,
    _sync_spawn,
)


# ---------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------


def _meta(*files, name="Show S01", multi_file=True):
    total = sum(f.size for f in files)
    return TorrentMetadata(
        info_hash="0" * 40,
        name=name,
        files=tuple(files),
        total_size=total,
        multi_file=multi_file,
    )


def _nested():
    """Season 01/Episode01.mkv, .../Episode02.mkv, .../Subs/Episode02.srt.

    The indexes are deliberately not 0,1,2 and deliberately not in visual
    order: nothing in the dialog may derive a file's identity from where its
    row happens to land.
    """
    return _meta(
        TorrentFile(5, ("Season 01", "Episode01.mkv"), 1000),
        TorrentFile(9, ("Season 01", "Episode02.mkv"), 2000),
        TorrentFile(2, ("Season 01", "Subs", "Episode02.srt"), 100),
    )


def _flat():
    """Three files at the root, indexes 0/1/2, sizes 10/20/30."""
    return _meta(
        TorrentFile(0, ("A.bin",), 10),
        TorrentFile(1, ("B.bin",), 20),
        TorrentFile(2, ("C.bin",), 30),
    )


def _single():
    return _meta(TorrentFile(0, ("movie.mkv",), 700), name="movie.mkv",
                 multi_file=False)


def _dialog(metadata, save_to="/home/user/Downloads"):
    return TorrentContentsDialog(metadata, save_to)


def _tree_rows(dlg):
    """(depth, label, index) for every row, in display order."""
    rows = []

    def walk(item, depth):
        for i in range(item.childCount()):
            child = item.child(i)
            rows.append((depth, child.text(0), dlg.file_index(child)))
            walk(child, depth + 1)

    walk(dlg.tree.invisibleRootItem(), 0)
    return rows


def _leaf(dlg, relative_path):
    for item, index, _size in dlg.leaves():
        if dlg.item_path(item) == tuple(relative_path.split("/")):
            return item
    raise AssertionError(f"no leaf at {relative_path!r}")


def _folder(dlg, relative_path):
    parts = tuple(relative_path.split("/"))
    item = dlg.tree.invisibleRootItem()
    for part in parts:
        for i in range(item.childCount()):
            child = item.child(i)
            if child.text(0) == part:
                item = child
                break
        else:
            raise AssertionError(f"no folder at {relative_path!r}")
    return item


def _checked_paths(dlg):
    return {
        "/".join(dlg.item_path(item))
        for item, _index, _size in dlg.leaves()
        if item.checkState(0) == Qt.Checked
    }


# ---------------------------------------------------------------------------
# RED 1-3: defaults, name, destination
# ---------------------------------------------------------------------------


def test_every_file_starts_checked_and_download_is_enabled():
    dlg = _dialog(_nested())

    assert _checked_paths(dlg) == {
        "Season 01/Episode01.mkv",
        "Season 01/Episode02.mkv",
        "Season 01/Subs/Episode02.srt",
    }
    assert dlg.selected_count() == 3
    assert dlg.selected_bytes() == 3100
    assert dlg.download_button().isEnabled()


def test_every_folder_starts_checked():
    dlg = _dialog(_nested())

    assert _folder(dlg, "Season 01").checkState(0) == Qt.Checked
    assert _folder(dlg, "Season 01/Subs").checkState(0) == Qt.Checked


def test_the_dialog_shows_the_parsed_torrent_name_read_only():
    dlg = _dialog(_nested())

    assert dlg.windowTitle() == "Torrent Contents"
    assert dlg.name_edit.text() == "Show S01"
    assert dlg.name_edit.isReadOnly()


def test_save_to_is_the_prepared_destination_and_is_read_only():
    dlg = _dialog(_nested(), save_to="/srv/media/incoming")

    assert dlg.dir_edit.text() == "/srv/media/incoming"
    assert dlg.dir_edit.isReadOnly()


def test_the_dialog_offers_no_destination_editing():
    dlg = _dialog(_nested())
    labels = {b.text() for b in dlg.buttons()}

    assert "Browse" not in labels and "Browse..." not in labels
    assert labels == {"Select All", "Select None", "Cancel", "Download"}


def test_selection_changes_never_touch_the_destination():
    dlg = _dialog(_nested())

    dlg.select_none()
    _leaf(dlg, "Season 01/Episode01.mkv").setCheckState(0, Qt.Checked)

    assert dlg.dir_edit.text() == "/home/user/Downloads"


# ---------------------------------------------------------------------------
# RED 4-9: canonical identity and tree shape
# ---------------------------------------------------------------------------


def test_each_leaf_carries_its_manifest_index_not_its_row():
    dlg = _dialog(_nested())

    assert dlg.file_index(_leaf(dlg, "Season 01/Episode01.mkv")) == 5
    assert dlg.file_index(_leaf(dlg, "Season 01/Episode02.mkv")) == 9
    assert dlg.file_index(_leaf(dlg, "Season 01/Subs/Episode02.srt")) == 2


def test_the_tree_nests_by_relative_path_without_repeating_the_root():
    dlg = _dialog(_nested())

    assert _tree_rows(dlg) == [
        (0, "Season 01", None),
        (1, "Episode01.mkv", 5),
        (1, "Episode02.mkv", 9),
        (1, "Subs", None),
        (2, "Episode02.srt", 2),
    ]


def test_repeated_directory_basenames_under_different_parents_stay_distinct():
    dlg = _dialog(_meta(
        TorrentFile(0, ("A", "Sub", "file1.mkv"), 1),
        TorrentFile(1, ("B", "Sub", "file2.mkv"), 2),
    ))

    assert _tree_rows(dlg) == [
        (0, "A", None),
        (1, "Sub", None),
        (2, "file1.mkv", 0),
        (0, "B", None),
        (1, "Sub", None),
        (2, "file2.mkv", 1),
    ]
    a_sub = _folder(dlg, "A/Sub")
    b_sub = _folder(dlg, "B/Sub")
    a_sub.setCheckState(0, Qt.Unchecked)

    assert b_sub.checkState(0) == Qt.Checked
    assert dlg.result_selection() == (1,)


def test_a_single_file_torrent_is_one_checked_leaf():
    dlg = _dialog(_single())

    assert _tree_rows(dlg) == [(0, "movie.mkv", 0)]
    assert dlg.summary_text() == "Selected: 1 of 1 file - 700 B"
    assert dlg.download_button().isEnabled()
    assert dlg.result_selection() is None


def test_unchecking_the_only_file_disables_download():
    dlg = _dialog(_single())

    _leaf(dlg, "movie.mkv").setCheckState(0, Qt.Unchecked)

    assert dlg.summary_text() == "Selected: 0 of 1 file - 0 B"
    assert not dlg.download_button().isEnabled()


def test_duplicate_file_basenames_stay_separate_leaves():
    dlg = _dialog(_meta(
        TorrentFile(0, ("Disc1", "movie.mkv"), 5),
        TorrentFile(1, ("Disc2", "movie.mkv"), 7),
    ))

    _leaf(dlg, "Disc2/movie.mkv").setCheckState(0, Qt.Unchecked)

    assert dlg.result_selection() == (0,)
    assert dlg.selected_bytes() == 5


def test_manifest_order_is_preserved_and_sorting_is_disabled():
    dlg = _dialog(_meta(
        TorrentFile(0, ("zeta.bin",), 1),
        TorrentFile(1, ("alpha.bin",), 1),
        TorrentFile(2, ("Mid", "beta.bin"), 1),
    ))

    assert [row[1] for row in _tree_rows(dlg)] == [
        "zeta.bin", "alpha.bin", "Mid", "beta.bin",
    ]
    assert not dlg.tree.isSortingEnabled()
    assert dlg.tree.header().sectionsClickable() is False


# ---------------------------------------------------------------------------
# RED 10-15: check-state propagation
# ---------------------------------------------------------------------------


def test_unchecking_a_leaf_updates_the_summary_and_its_parent():
    dlg = _dialog(_nested())

    _leaf(dlg, "Season 01/Episode01.mkv").setCheckState(0, Qt.Unchecked)

    assert dlg.selected_count() == 2
    assert dlg.selected_bytes() == 2100
    assert _folder(dlg, "Season 01").checkState(0) == Qt.PartiallyChecked
    assert dlg.download_button().isEnabled()


def test_rechecking_a_leaf_restores_the_summary_and_the_parent():
    dlg = _dialog(_nested())
    leaf = _leaf(dlg, "Season 01/Episode01.mkv")

    leaf.setCheckState(0, Qt.Unchecked)
    leaf.setCheckState(0, Qt.Checked)

    assert dlg.selected_count() == 3
    assert dlg.selected_bytes() == 3100
    assert _folder(dlg, "Season 01").checkState(0) == Qt.Checked


def test_unchecking_a_directory_clears_every_nested_descendant():
    dlg = _dialog(_meta(
        TorrentFile(0, ("Keep", "keep.bin"), 1),
        TorrentFile(1, ("Drop", "a.bin"), 2),
        TorrentFile(2, ("Drop", "Deep", "b.bin"), 4),
    ))

    _folder(dlg, "Drop").setCheckState(0, Qt.Unchecked)

    assert _checked_paths(dlg) == {"Keep/keep.bin"}
    assert _folder(dlg, "Drop/Deep").checkState(0) == Qt.Unchecked
    assert _folder(dlg, "Keep").checkState(0) == Qt.Checked
    assert dlg.selected_count() == 1
    assert dlg.selected_bytes() == 1


def test_checking_a_directory_selects_every_nested_descendant():
    dlg = _dialog(_nested())
    dlg.select_none()

    _folder(dlg, "Season 01").setCheckState(0, Qt.Checked)

    assert dlg.selected_count() == 3
    assert _folder(dlg, "Season 01/Subs").checkState(0) == Qt.Checked
    assert dlg.result_selection() is None


def test_a_partially_selected_directory_reports_partially_checked():
    dlg = _dialog(_nested())

    _leaf(dlg, "Season 01/Subs/Episode02.srt").setCheckState(0, Qt.Unchecked)

    assert _folder(dlg, "Season 01/Subs").checkState(0) == Qt.Unchecked
    assert _folder(dlg, "Season 01").checkState(0) == Qt.PartiallyChecked


def test_directories_are_not_counted_as_files():
    dlg = _dialog(_nested())

    assert dlg.total_count() == 3
    assert dlg.summary_text() == "Selected: 3 of 3 files - 3.0 KB"
    assert all(index is not None for _d, _t, index in _tree_rows(dlg)
               if _t.endswith(".mkv") or _t.endswith(".srt"))
    assert dlg.file_index(_folder(dlg, "Season 01")) is None


# ---------------------------------------------------------------------------
# RED 16-21: bulk actions and the domain result
# ---------------------------------------------------------------------------


def test_select_none_empties_the_selection_and_disables_download():
    dlg = _dialog(_nested())

    dlg.select_none()

    assert _checked_paths(dlg) == set()
    assert _folder(dlg, "Season 01").checkState(0) == Qt.Unchecked
    assert dlg.selected_count() == 0
    assert dlg.selected_bytes() == 0
    assert not dlg.download_button().isEnabled()


def test_select_all_after_select_none_restores_everything():
    dlg = _dialog(_nested())

    dlg.select_none()
    dlg.select_all()

    assert dlg.selected_count() == 3
    assert dlg.selected_bytes() == 3100
    assert _folder(dlg, "Season 01/Subs").checkState(0) == Qt.Checked
    assert dlg.download_button().isEnabled()
    assert dlg.result_selection() is None


def test_zero_selection_cannot_be_confirmed_and_has_no_valid_result():
    dlg = _dialog(_nested())
    dlg.select_none()

    dlg.confirm()

    assert dlg.result() != TorrentContentsDialog.Accepted
    assert not dlg.isVisible()
    with pytest.raises(TorrentError):
        dlg.result_selection()


@pytest.mark.parametrize("reach_all", [
    lambda dlg: None,
    lambda dlg: (dlg.select_none(), dlg.select_all()),
    lambda dlg: (
        _leaf(dlg, "Season 01/Episode02.mkv").setCheckState(0, Qt.Unchecked),
        _leaf(dlg, "Season 01/Episode02.mkv").setCheckState(0, Qt.Checked),
    ),
])
def test_every_route_back_to_all_files_returns_none(reach_all):
    dlg = _dialog(_nested())

    reach_all(dlg)

    assert dlg.result_selection() is None


def test_a_proper_subset_returns_canonical_ascending_indexes():
    dlg = _dialog(_flat())

    dlg.select_none()
    _leaf(dlg, "C.bin").setCheckState(0, Qt.Checked)
    _leaf(dlg, "A.bin").setCheckState(0, Qt.Checked)

    assert dlg.result_selection() == (0, 2)


def test_cancel_is_a_rejection_and_accepted_all_files_is_not():
    cancelled = _dialog(_nested())
    cancelled.reject()

    accepted = _dialog(_nested())
    accepted.confirm()

    assert cancelled.result() == TorrentContentsDialog.Rejected
    assert accepted.result() == TorrentContentsDialog.Accepted
    assert accepted.result_selection() is None


# ---------------------------------------------------------------------------
# RED 22-26: sizes, hiding, scale
# ---------------------------------------------------------------------------


def test_a_zero_byte_entry_is_a_selectable_file_showing_zero_bytes():
    dlg = _dialog(_meta(
        TorrentFile(0, ("empty.txt",), 0),
        TorrentFile(1, ("data.bin",), 4096),
    ))
    empty = _leaf(dlg, "empty.txt")

    assert empty.text(1) == "0 B"
    assert empty.childCount() == 0
    assert dlg.total_count() == 2

    empty.setCheckState(0, Qt.Unchecked)
    assert dlg.result_selection() == (1,)


def test_the_selected_total_is_an_exact_integer_sum():
    dlg = _dialog(_meta(
        TorrentFile(0, ("a",), 1),
        TorrentFile(1, ("b",), 3),
        TorrentFile(2, ("c",), 1_099_511_627_776),
    ))

    _leaf(dlg, "b").setCheckState(0, Qt.Unchecked)

    assert dlg.selected_bytes() == 1_099_511_627_777
    assert dlg.summary_text() == "Selected: 2 of 3 files - 1.0 TB"


def test_a_directory_row_shows_its_aggregate_descendant_size():
    dlg = _dialog(_meta(
        TorrentFile(0, ("Season 01", "a.mkv"), 1000),
        TorrentFile(1, ("Season 01", "Subs", "a.srt"), 24),
    ))

    assert _folder(dlg, "Season 01").text(1) == "1.0 KB"
    assert _folder(dlg, "Season 01/Subs").text(1) == "24 B"


def test_the_dialog_reuses_an_existing_byte_formatter():
    """No third implementation: the search widget's is imported verbatim."""
    from cove import dialogs
    from cove.search.widget import _human_size

    assert dialogs._human_size is _human_size


def test_nothing_in_the_manifest_is_hidden():
    dlg = _dialog(_meta(
        TorrentFile(0, ("Sample.mkv",), 1),
        TorrentFile(1, ("movie.nfo",), 2),
        TorrentFile(2, ("subtitle.srt",), 3),
        TorrentFile(3, ("__padding_file_0",), 4),
    ))

    assert [row[1] for row in _tree_rows(dlg)] == [
        "Sample.mkv", "movie.nfo", "subtitle.srt", "__padding_file_0",
    ]
    assert dlg.total_count() == 4


def test_a_large_manifest_bulk_action_refreshes_the_summary_once():
    files = tuple(
        TorrentFile(i, (f"d{i // 100:03d}", f"f{i:05d}.bin"), i)
        for i in range(2000)
    )
    dlg = _dialog(_meta(*files))
    total = sum(f.size for f in files)
    assert dlg.selected_count() == 2000
    assert dlg.selected_bytes() == total

    before = dlg.summary_refreshes()
    dlg.select_none()
    assert dlg.summary_refreshes() - before == 1
    assert dlg.selected_count() == 0
    assert dlg.selected_bytes() == 0
    assert not dlg.download_button().isEnabled()

    before = dlg.summary_refreshes()
    dlg.select_all()
    assert dlg.summary_refreshes() - before == 1
    assert dlg.selected_count() == 2000
    assert dlg.selected_bytes() == total
    assert dlg.result_selection() is None


# ---------------------------------------------------------------------------
# RED 27-29: reject routes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reject", [
    lambda dlg: dlg.cancel_button().click(),
    lambda dlg: dlg.reject(),
    lambda dlg: dlg.close(),
])
def test_every_reject_route_leaves_the_dialog_rejected(reject):
    dlg = _dialog(_nested())

    reject(dlg)

    assert dlg.result() == TorrentContentsDialog.Rejected


def test_the_download_button_is_the_dialogs_default():
    dlg = _dialog(_nested())

    assert dlg.download_button().isDefault()


# ---------------------------------------------------------------------------
# Intake: the MainWindow coordinator
# ---------------------------------------------------------------------------


TORRENT_FILES = (
    (1000, (b"Season 01", b"Episode01.mkv")),
    (2000, (b"Season 01", b"Episode02.mkv")),
    (100, (b"Season 01", b"Subs", b"Episode02.srt")),
    (50, (b"Sample.mkv",)),
)


def _torrent_bytes(name=b"Show S01", files=TORRENT_FILES):
    return _multi_file_torrent_bytes(name, files)


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

    def _refresh_status_pill(self):
        pass


class _Dialogs:
    """Stands in for TorrentContentsDialog and records every construction."""

    def __init__(self, monkeypatch, decide):
        self.calls = []
        self._decide = decide
        outer = self

        class _Fake:
            Accepted = TorrentContentsDialog.Accepted

            def __init__(self, metadata, save_to, parent=None):
                self.metadata = metadata
                self.save_to = save_to
                outer.calls.append(self)
                self._accepted, self._selection = outer._decide(metadata)

            def exec(self):
                if outer.open_now:
                    raise AssertionError("a second Torrent Contents dialog opened")
                outer.open_now = True
                try:
                    # One-shot: the hook simulates the *other* torrent's
                    # parse landing, which happens exactly once.
                    hook, outer.during_exec = outer.during_exec, None
                    if hook is not None:
                        hook(self)
                finally:
                    outer.open_now = False
                return (
                    TorrentContentsDialog.Accepted if self._accepted
                    else TorrentContentsDialog.Rejected
                )

            def result_selection(self):
                return self._selection

        self.open_now = False
        self.during_exec = None
        monkeypatch.setattr(mw, "TorrentContentsDialog", _Fake)


def _intake(queue_env, monkeypatch, tmp_path, decide, raw=None):
    """A Host wired to a real QueueManager over a real generated `.torrent`."""
    from cove import config as config_mod

    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path / "data")
    queue, rpc, db_path = queue_env(**_local_settings())
    _sync_spawn(queue)
    host = Host(queue, queue.settings)
    dialogs = _Dialogs(monkeypatch, decide)
    source = tmp_path / "picked.torrent"
    source.write_bytes(raw if raw is not None else _torrent_bytes())
    return host, queue, rpc, db_path, dialogs, source


def _accept(selection):
    return lambda metadata: (True, selection)


_REJECT = lambda metadata: (False, None)  # noqa: E731


def _add(host, source, out_dir):
    host.queue.add_torrent_file(
        str(source), str(out_dir),
        duplicate_check=host._confirm_duplicate,
        precommit=host._torrent_preflight,
    )


# ---------------------------------------------------------------------------
# RED 30-34: precommit ordering and selection propagation
# ---------------------------------------------------------------------------


def test_the_manifest_is_parsed_and_prepared_before_anything_is_committed(
    queue_env, monkeypatch, tmp_path
):
    seen = {}

    def decide(metadata):
        seen["rows"] = len(_rows(db_path))
        seen["tasks"] = len(queue.tasks)
        seen["backend"] = list(rpc.added) + list(rpc.magnets) + list(rpc.torrents)
        seen["probes"] = list(probes)
        return True, None

    host, queue, rpc, db_path, dialogs, source = _intake(
        queue_env, monkeypatch, tmp_path, decide
    )
    probes = []
    monkeypatch.setattr(
        "cove.debrid.resolve_torrent",
        lambda *a, **kw: probes.append(a) or None,
    )

    _add(host, source, tmp_path)

    assert seen == {"rows": 0, "tasks": 0, "backend": [], "probes": []}
    assert len(dialogs.calls) == 1
    assert dialogs.calls[0].metadata.name == "Show S01"
    assert [f.index for f in dialogs.calls[0].metadata.files] == [0, 1, 2, 3]


def test_the_dialog_is_shown_the_prepared_destination(
    queue_env, monkeypatch, tmp_path
):
    dest = tmp_path / "elsewhere"
    dest.mkdir()
    host, queue, rpc, db_path, dialogs, source = _intake(
        queue_env, monkeypatch, tmp_path, _accept(None)
    )

    _add(host, source, dest)

    assert dialogs.calls[0].save_to == str(dest)
    assert _rows(db_path)[0]["out_dir"] == str(dest)


def test_cancelling_the_preflight_creates_nothing_and_cleans_the_copy(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, dialogs, source = _intake(
        queue_env, monkeypatch, tmp_path, _REJECT
    )
    original = source.read_bytes()

    _add(host, source, tmp_path)

    managed = torrent_mod.managed_torrent_path(
        torrent_mod.parse_torrent(original).info_hash
    )
    # The user's own file first: it is the one thing a cleanup bug could
    # destroy rather than merely leave behind.
    assert source.exists() and source.read_bytes() == original
    assert _rows(db_path) == []
    assert queue.tasks == {}
    assert rpc.added == [] and rpc.magnets == [] and rpc.torrents == []
    assert not __import__("os").path.exists(managed)


def test_accepting_every_file_commits_the_legacy_whole_torrent_selection(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, dialogs, source = _intake(
        queue_env, monkeypatch, tmp_path, _accept(None)
    )

    _add(host, source, tmp_path)

    row = _rows(db_path)[0]
    assert row["source_type"] == SOURCE_TORRENT
    assert row["selected_files"] == ""
    assert queue.tasks[row["id"]].selected_files is None


def test_accepting_a_subset_commits_canonical_zero_based_indexes(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, dialogs, source = _intake(
        queue_env, monkeypatch, tmp_path, _accept((2, 0))
    )

    _add(host, source, tmp_path)

    row = _rows(db_path)[0]
    assert row["selected_files"] == "0,2"
    assert queue.tasks[row["id"]].selected_files == (0, 2)


def test_the_preflight_commits_exactly_one_task(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, dialogs, source = _intake(
        queue_env, monkeypatch, tmp_path, _accept((1,))
    )

    _add(host, source, tmp_path)

    assert len(_rows(db_path)) == 1


# ---------------------------------------------------------------------------
# RED 35-37: the reviewed routes downstream
# ---------------------------------------------------------------------------


def _running(queue):
    queue._running = True
    queue._scheduler_allows = True


def test_a_subset_reaches_the_reviewed_normal_aria2_selection(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, dialogs, source = _intake(
        queue_env, monkeypatch, tmp_path, _accept((0, 2))
    )
    monkeypatch.setattr(
        "cove.debrid.resolve_torrent", lambda *a, **kw: None
    )
    _running(queue)

    _add(host, source, tmp_path)
    tid = _rows(db_path)[0]["id"]
    queue._launch(queue.tasks[tid])

    assert rpc.torrents[0]["select_file"] == "1,3"


def test_all_files_keeps_the_legacy_route_with_no_select_file(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, dialogs, source = _intake(
        queue_env, monkeypatch, tmp_path, _accept(None)
    )
    monkeypatch.setattr(
        "cove.debrid.resolve_torrent", lambda *a, **kw: None
    )
    _running(queue)

    _add(host, source, tmp_path)
    tid = _rows(db_path)[0]["id"]
    queue._launch(queue.tasks[tid])

    assert rpc.torrents[0]["select_file"] is None


# ---------------------------------------------------------------------------
# RED 38-42: the dialog never leaks into another intake
# ---------------------------------------------------------------------------


def test_a_local_torrent_never_reaches_the_download_file_info_preflight(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, dialogs, source = _intake(
        queue_env, monkeypatch, tmp_path, _accept(None)
    )
    info_calls = []
    monkeypatch.setattr(
        mw, "DownloadFileInfoDialog",
        lambda *a, **kw: info_calls.append(a) or pytest.fail("info dialog shown"),
    )

    _add(host, source, tmp_path)

    assert len(dialogs.calls) == 1
    assert info_calls == []


@pytest.mark.parametrize("magnet_intake", ["manual", "search"])
def test_a_magnet_never_opens_torrent_contents(
    queue_env, monkeypatch, tmp_path, magnet_intake
):
    host, queue, rpc, db_path, dialogs, source = _intake(
        queue_env, monkeypatch, tmp_path, _accept(None)
    )
    magnet = "magnet:?xt=urn:btih:" + "ab" * 20

    host.add_urls_checked([magnet], intake=magnet_intake)

    assert dialogs.calls == []
    assert len(_rows(db_path)) == 1


def test_a_non_interactive_caller_gets_no_dialog_and_the_legacy_add(
    queue_env, monkeypatch, tmp_path
):
    """`precommit` is opt-in: automation keeps the exact pre-feature path."""
    host, queue, rpc, db_path, dialogs, source = _intake(
        queue_env, monkeypatch, tmp_path, _accept((0,))
    )

    queue.add_torrent_file(str(source), str(tmp_path))

    assert dialogs.calls == []
    row = _rows(db_path)[0]
    assert row["selected_files"] == ""
    assert row["source_type"] == SOURCE_TORRENT


# ---------------------------------------------------------------------------
# RED 43-45: failure routes
# ---------------------------------------------------------------------------


def test_an_unreadable_torrent_shows_no_dialog_and_creates_no_task(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, dialogs, source = _intake(
        queue_env, monkeypatch, tmp_path, _accept(None), raw=b"not a torrent"
    )
    errors = []
    queue.error.connect(errors.append)

    _add(host, source, tmp_path)

    assert dialogs.calls == []
    assert _rows(db_path) == []
    assert errors


def test_a_refused_preparation_shows_no_dialog_and_cleans_the_copy(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, dialogs, source = _intake(
        queue_env, monkeypatch, tmp_path, _accept(None)
    )
    monkeypatch.setattr(queue, "prepare_url", lambda *a, **kw: None)

    _add(host, source, tmp_path)

    managed = torrent_mod.managed_torrent_path(
        torrent_mod.parse_torrent(source.read_bytes()).info_hash
    )
    assert dialogs.calls == []
    assert _rows(db_path) == []
    assert not __import__("os").path.exists(managed)


def test_a_dialog_that_raises_fails_closed(queue_env, monkeypatch, tmp_path):
    def boom(metadata):
        raise RuntimeError("dialog exploded")

    host, queue, rpc, db_path, dialogs, source = _intake(
        queue_env, monkeypatch, tmp_path, boom
    )
    errors = []
    queue.error.connect(errors.append)

    _add(host, source, tmp_path)

    managed = torrent_mod.managed_torrent_path(
        torrent_mod.parse_torrent(source.read_bytes()).info_hash
    )
    assert _rows(db_path) == []
    assert queue.tasks == {}
    assert rpc.added == [] and rpc.magnets == [] and rpc.torrents == []
    assert not __import__("os").path.exists(managed)
    assert errors


# ---------------------------------------------------------------------------
# RED 46-48: threads and managed-copy ownership
# ---------------------------------------------------------------------------


def test_the_torrent_is_parsed_and_stored_on_a_worker(
    queue_env, monkeypatch, tmp_path
):
    """The parse stays where it is; only the decision moved to the GUI."""
    from cove import config as config_mod

    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path / "data")
    queue, rpc, db_path = queue_env(**_local_settings())
    spawned = _sync_spawn(queue)
    host = Host(queue, queue.settings)
    _Dialogs(monkeypatch, _accept(None))
    source = tmp_path / "picked.torrent"
    source.write_bytes(_torrent_bytes())

    _add(host, source, tmp_path)

    assert queue._read_and_store_torrent in spawned


def test_the_dialog_is_constructed_on_the_owning_gui_thread(
    queue_env, monkeypatch, tmp_path
):
    import threading

    gui_thread = threading.get_ident()
    seen = []

    def decide(metadata):
        seen.append(threading.get_ident())
        return True, None

    host, queue, rpc, db_path, dialogs, source = _intake(
        queue_env, monkeypatch, tmp_path, decide
    )

    _add(host, source, tmp_path)

    assert seen == [gui_thread]


def test_an_accepted_torrent_keeps_its_managed_copy(
    queue_env, monkeypatch, tmp_path
):
    import os

    host, queue, rpc, db_path, dialogs, source = _intake(
        queue_env, monkeypatch, tmp_path, _accept((1,))
    )

    _add(host, source, tmp_path)

    row = _rows(db_path)[0]
    assert os.path.isfile(row["torrent_path"])
    assert row["torrent_path"] == torrent_mod.managed_torrent_path(row["info_hash"])


# ---------------------------------------------------------------------------
# RED 49-53: concurrency, shutdown, destination isolation
# ---------------------------------------------------------------------------


def _two_intake(queue_env, monkeypatch, tmp_path, decide):
    from cove import config as config_mod

    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path / "data")
    queue, rpc, db_path = queue_env(**_local_settings())
    host = Host(queue, queue.settings)
    dialogs = _Dialogs(monkeypatch, decide)
    a = tmp_path / "a.torrent"
    a.write_bytes(_torrent_bytes(b"Torrent A"))
    b = tmp_path / "b.torrent"
    b.write_bytes(_torrent_bytes(b"Torrent B"))
    return host, queue, rpc, db_path, dialogs, a, b


def _deferred_spawn(queue):
    """Hold every worker result so a test can land two at chosen moments."""
    pending = []

    def spawn(fn, *args, on_done=None, on_fail=None, **kwargs):
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            pending.append(lambda: on_fail and on_fail(str(exc)))
        else:
            pending.append(lambda: on_done and on_done(result))

    queue._spawn = spawn
    return pending


def test_two_pending_torrents_keep_their_own_manifests_and_selections(
    queue_env, monkeypatch, tmp_path
):
    def decide(metadata):
        return True, ((0,) if metadata.name == "Torrent A" else (1,))

    host, queue, rpc, db_path, dialogs, a, b = _two_intake(
        queue_env, monkeypatch, tmp_path, decide
    )
    pending = _deferred_spawn(queue)

    _add(host, a, tmp_path)
    _add(host, b, tmp_path)
    for land in pending:
        land()

    rows = {r["torrent_name"]: r["selected_files"] for r in _rows(db_path)}
    assert rows == {"Torrent A": "0", "Torrent B": "1"}
    assert [d.metadata.name for d in dialogs.calls] == ["Torrent A", "Torrent B"]


def test_several_queued_preflights_each_keep_their_own_request(
    queue_env, monkeypatch, tmp_path
):
    """Two more land while the first modal is up, so two sit queued at once.

    A single shared "current request" slot survives one pending torrent by
    accident; it cannot survive two, because the second overwrites the first
    before either has been shown.
    """
    from cove import config as config_mod

    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path / "data")
    queue, _rpc, db_path = queue_env(**_local_settings())
    host = Host(queue, queue.settings)
    picked = {"Torrent A": (0,), "Torrent B": (1,), "Torrent C": (2,)}
    dialogs = _Dialogs(monkeypatch, lambda m: (True, picked[m.name]))
    pending = _deferred_spawn(queue)
    for label in ("A", "B", "C"):
        path = tmp_path / f"{label}.torrent"
        path.write_bytes(_torrent_bytes(f"Torrent {label}".encode()))
        _add(host, path, tmp_path)
    landed = iter(pending)
    first = next(landed)
    rest = list(landed)
    dialogs.during_exec = lambda dlg: [land() for land in rest]

    first()

    assert [d.metadata.name for d in dialogs.calls] == [
        "Torrent A", "Torrent B", "Torrent C",
    ]
    assert {r["torrent_name"]: r["selected_files"] for r in _rows(db_path)} == {
        "Torrent A": "0", "Torrent B": "1", "Torrent C": "2",
    }


def test_only_one_torrent_contents_dialog_is_open_at_a_time(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, dialogs, a, b = _two_intake(
        queue_env, monkeypatch, tmp_path, _accept(None)
    )
    pending = _deferred_spawn(queue)
    _add(host, a, tmp_path)
    _add(host, b, tmp_path)
    landed = iter(pending)
    first = next(landed)
    second = next(landed)

    # B's worker result lands while A's modal is up: the fake dialog fails
    # the test outright if that opens a second modal.
    dialogs.during_exec = lambda dlg: second()
    first()

    assert len(dialogs.calls) == 2
    assert len(_rows(db_path)) == 2


def test_cancelling_the_first_preflight_still_opens_the_second(
    queue_env, monkeypatch, tmp_path
):
    def decide(metadata):
        return (metadata.name == "Torrent B"), None

    host, queue, rpc, db_path, dialogs, a, b = _two_intake(
        queue_env, monkeypatch, tmp_path, decide
    )
    pending = _deferred_spawn(queue)
    _add(host, a, tmp_path)
    _add(host, b, tmp_path)
    landed = iter(pending)
    first = next(landed)
    second = next(landed)
    dialogs.during_exec = lambda dlg: second()
    first()

    rows = _rows(db_path)
    assert [d.metadata.name for d in dialogs.calls] == ["Torrent A", "Torrent B"]
    assert [r["torrent_name"] for r in rows] == ["Torrent B"]


def test_a_failing_preflight_does_not_strand_the_one_behind_it(
    queue_env, monkeypatch, tmp_path
):
    def decide(metadata):
        if metadata.name == "Torrent A":
            raise RuntimeError("dialog exploded")
        return True, None

    host, queue, rpc, db_path, dialogs, a, b = _two_intake(
        queue_env, monkeypatch, tmp_path, decide
    )
    pending = _deferred_spawn(queue)
    _add(host, a, tmp_path)
    _add(host, b, tmp_path)
    for land in pending:
        land()

    assert [r["torrent_name"] for r in _rows(db_path)] == ["Torrent B"]
    assert host._torrent_preflights == deque()


def test_a_failing_commit_does_not_strand_the_preflight_behind_it(
    queue_env, monkeypatch, tmp_path
):
    """A commit that raises must not abort the drain.

    The dialog failure path was already fail-closed; the commit itself was
    not, so a database error would escape the FIFO loop and leave everything
    queued behind it with its info hash held and its managed copy retained -
    no dialog, and the same torrent blocked from being added again.
    """
    import os

    host, queue, rpc, db_path, dialogs, a, b = _two_intake(
        queue_env, monkeypatch, tmp_path, _accept(None)
    )
    errors = []
    queue.error.connect(errors.append)
    real_commit = queue.commit_prepared
    calls = []

    def exploding_commit(prepared):
        calls.append(prepared)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        return real_commit(prepared)

    monkeypatch.setattr(queue, "commit_prepared", exploding_commit)
    pending = _deferred_spawn(queue)
    _add(host, a, tmp_path)
    _add(host, b, tmp_path)
    landed = iter(pending)
    first = next(landed)
    second = next(landed)
    dialogs.during_exec = lambda dlg: second()

    first()

    a_hash = torrent_mod.parse_torrent(a.read_bytes()).info_hash
    assert [d.metadata.name for d in dialogs.calls] == ["Torrent A", "Torrent B"]
    assert [r["torrent_name"] for r in _rows(db_path)] == ["Torrent B"]
    assert host._torrent_preflights == deque()
    assert queue._preflight_hashes == set()
    assert not os.path.exists(torrent_mod.managed_torrent_path(a_hash))
    assert errors


def test_shutdown_discards_preflights_that_never_opened(
    queue_env, monkeypatch, tmp_path
):
    import os

    host, queue, rpc, db_path, dialogs, a, b = _two_intake(
        queue_env, monkeypatch, tmp_path, _accept(None)
    )
    pending = _deferred_spawn(queue)
    _add(host, a, tmp_path)
    _add(host, b, tmp_path)
    landed = iter(pending)
    first = next(landed)
    second = next(landed)
    # B lands while A's modal is up and is left queued when the window closes.
    dialogs.during_exec = lambda dlg: (second(), host.discard_torrent_preflights())
    first()

    b_hash = torrent_mod.parse_torrent(b.read_bytes()).info_hash
    assert host._torrent_preflights == deque()
    assert not os.path.exists(torrent_mod.managed_torrent_path(b_hash))
    assert [r["torrent_name"] for r in _rows(db_path)] == ["Torrent A"]
    assert b.exists()


def test_the_same_torrent_twice_gets_one_preflight_and_one_task(
    queue_env, monkeypatch, tmp_path
):
    """A pending preflight owns its info hash just like a live task does.

    The duplicate guard used to run immediately before the commit. Now the
    commit waits for the user, so a second copy of the same torrent would
    sail past a guard that only looks at committed tasks and end up as a
    second dialog and a second task for one info hash.
    """
    host, queue, rpc, db_path, dialogs, source = _intake(
        queue_env, monkeypatch, tmp_path, _accept(None)
    )
    pending = _deferred_spawn(queue)
    errors = []
    queue.error.connect(errors.append)
    _add(host, source, tmp_path)
    _add(host, source, tmp_path)
    landed = iter(pending)
    first = next(landed)
    second = next(landed)
    dialogs.during_exec = lambda dlg: second()

    first()

    assert len(dialogs.calls) == 1
    assert len(_rows(db_path)) == 1
    assert errors


@pytest.mark.parametrize("intake", ["manual", "search", "api"])
def test_a_matching_magnet_is_refused_while_a_preflight_is_open(
    queue_env, monkeypatch, tmp_path, intake
):
    """The hold has to bind every intake, not just another local `.torrent`.

    The commit that used to block a duplicate magnet now waits for the user,
    so for the whole time a dialog is open an identical magnet from Search,
    the extension, the API or a second instance would sail straight past
    `_live_torrent` and commit a second task for the same torrent.
    """
    seen = {}

    def decide(metadata):
        magnet = torrent_mod.minimal_magnet(metadata.info_hash)
        seen["tid"] = queue.add_url(magnet, intake=intake)
        seen["rows"] = len(_rows(db_path))
        return True, None

    host, queue, rpc, db_path, dialogs, source = _intake(
        queue_env, monkeypatch, tmp_path, decide
    )
    errors = []
    queue.error.connect(errors.append)

    _add(host, source, tmp_path)

    assert seen["tid"] is None
    assert seen["rows"] == 0
    assert errors
    # The preflight itself still commits, exactly once.
    assert len(_rows(db_path)) == 1


def test_the_hold_is_released_so_the_magnet_works_afterwards(
    queue_env, monkeypatch, tmp_path
):
    """The hold is a window, not a permanent block."""
    host, queue, rpc, db_path, dialogs, source = _intake(
        queue_env, monkeypatch, tmp_path, _REJECT
    )
    meta = torrent_mod.parse_torrent(source.read_bytes())

    _add(host, source, tmp_path)

    assert queue._preflight_hashes == set()
    assert queue.add_url(torrent_mod.minimal_magnet(meta.info_hash)) is not None
    assert len(_rows(db_path)) == 1


def test_a_pending_preflight_keeps_the_managed_copy_of_a_removed_task(
    queue_env, monkeypatch, tmp_path
):
    """Removing a task must not delete a copy a waiting preflight still needs.

    Both sides key the managed `.torrent` by info hash, so they share one
    file. Task removal already refuses to delete a copy another task is
    using; a preflight that has not been answered is exactly the same kind
    of owner.
    """
    import os

    host, queue, rpc, db_path, dialogs, source = _intake(
        queue_env, monkeypatch, tmp_path, _accept(None)
    )
    meta = torrent_mod.parse_torrent(source.read_bytes())
    managed = torrent_mod.store_managed_torrent(meta)
    tid = queue.add_url(
        torrent_mod.minimal_magnet(meta.info_hash), out_dir=str(tmp_path),
        source_type=SOURCE_TORRENT, info_hash=meta.info_hash,
        torrent_name=meta.name, torrent_path=managed,
    )
    request = TorrentPreflight(
        metadata=meta,
        prepared=queue.prepare_url(
            torrent_mod.minimal_magnet(meta.info_hash), out_dir=str(tmp_path),
            source_type=SOURCE_TORRENT, info_hash=meta.info_hash,
            torrent_name=meta.name, torrent_path=managed,
        ),
    )
    queue.hold_torrent_preflight(request)

    queue._remove_torrent(queue.tasks[tid], False)

    assert os.path.isfile(managed)

    queue.discard_torrent_preflight(request)

    assert not os.path.exists(managed)


def test_the_preflight_never_changes_the_global_destination(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, dialogs, source = _intake(
        queue_env, monkeypatch, tmp_path, _REJECT
    )
    before = queue.settings.download_dir
    before_categories = replace(queue.settings.category_dirs)
    dest = tmp_path / "elsewhere"
    dest.mkdir()

    _add(host, source, dest)

    assert queue.settings.download_dir == before
    assert queue.settings.category_dirs == before_categories


# ---------------------------------------------------------------------------
# RED 54: the S1 guard is not weakened by the new commit seam
# ---------------------------------------------------------------------------


def test_an_empty_selection_is_still_refused_at_the_commit_seam(
    queue_env, monkeypatch, tmp_path
):
    host, queue, rpc, db_path, dialogs, source = _intake(
        queue_env, monkeypatch, tmp_path, _accept(())
    )
    errors = []
    queue.error.connect(errors.append)

    _add(host, source, tmp_path)

    assert _rows(db_path) == []
    assert errors


def test_the_preflight_record_is_plain_data(queue_env, monkeypatch, tmp_path):
    captured = []
    host, queue, rpc, db_path, dialogs, source = _intake(
        queue_env, monkeypatch, tmp_path, _accept(None)
    )
    monkeypatch.setattr(
        host, "_torrent_preflight",
        lambda request: captured.append(request),
    )

    _add(host, source, tmp_path)

    assert len(captured) == 1
    request = captured[0]
    assert isinstance(request, TorrentPreflight)
    assert request.metadata.name == "Show S01"
    assert request.prepared.source_type == SOURCE_TORRENT
    assert request.prepared.selected_files is None
    with pytest.raises(Exception):
        request.metadata = None
