"""Manual direct HTTP Download File Info preflight (MainWindow coordination).

The dialog itself is display-only; `MainWindow.add_urls_checked` is the
manual-intake coordinator that decides when the preflight may appear. This
suite pins eligibility mechanically: only ONE manual direct HTTP request with
`show_download_options=True` reaches the dialog, and every other intake path
(extension, Search, yt-dlp, HLS, magnet/torrent, debrid, batch, clipboard,
internal queue callers) bypasses it and keeps the exact legacy add path.
"""
from dataclasses import replace

import pytest
from PySide6.QtWidgets import QMainWindow

import cove.main_window as mw
from cove.queue import PreparedDownload

# Fixture reuse: the real QueueManager environment lives in the queue suite.
from tests.test_queue import (  # noqa: F401
    _persisted_row,
    _rows,
    diag,
    queue_env,
)

DIRECT_URL = "https://example.com/dir/archive.zip"
HLS_URL = "https://example.com/live/stream.m3u8"
YTDLP_URL = "https://www.youtube.com/watch?v=fake"
MAGNET = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"


class Host(mw.MainWindow):
    """The real MainWindow methods, without its heavy constructor."""

    def __init__(self):
        QMainWindow.__init__(self)
        self.settings = replace(
            mw.Settings(), show_download_options=True, torrent_support_enabled=True
        )
        self._items = {}


def _dialog_calls(monkeypatch):
    """Record (and neuter) DownloadFileInfoDialog construction and exec.

    The fake dialog reads its behaviour from a shared, mutable `plan` dict at
    exec time — the real dialog is constructed inside add_urls_checked, so a
    test cannot touch the dialog instance before the call.
    """
    calls = []
    plan = {"result": 0, "filename": None, "dir": None, "dont_show_again": False}

    class _FakeDialog:
        Accepted = 1
        Rejected = 0

        def __init__(self, url, default_dir, parent=None):
            self.url = url
            self.default_dir = default_dir
            self.parent = parent
            # The real dialog holds its checkbox state from construction, so
            # it is readable before exec() runs — that is exactly what the
            # coordinator's ordering discipline depends on.
            self.dont_show_again = _FakeCheck(plan["dont_show_again"])
            calls.append(self)

        def exec(self):
            return plan["result"]

        def result_filename(self):
            return plan["filename"]

        def result_dir(self):
            return plan["dir"] if plan["dir"] is not None else self.default_dir

        def result_dont_show_again(self):
            return self.dont_show_again.checked

    class _FakeCheck:
        def __init__(self, checked):
            self.checked = checked

        def setChecked(self, value):
            self.checked = value

    monkeypatch.setattr(mw, "DownloadFileInfoDialog", _FakeDialog)
    return calls, plan


def _host(queue):
    """A host that shares the queue's settings object, like production does
    (MainWindow and QueueManager receive the same Settings instance)."""
    host = Host()
    host.settings = queue.settings
    host.queue = queue
    host._confirm_duplicate = lambda *a, **k: True
    return host


def _commit_spy(monkeypatch, queue):
    """Count commits and record what reaches the authoritative path."""
    committed = []

    def _commit(prepared):
        committed.append(prepared)
        return 42 + len(committed)

    monkeypatch.setattr(queue, "commit_prepared", _commit)
    return committed


# ---- eligible: single manual direct HTTP --------------------------------


def test_single_manual_direct_http_shows_the_dialog(queue_env, monkeypatch):
    queue, _rpc, _db = queue_env()
    calls, plan = _dialog_calls(monkeypatch)
    committed = _commit_spy(monkeypatch, queue)
    host = _host(queue)
    plan["result"] = 1  # Accepted

    ids = host.add_urls_checked([DIRECT_URL], intake="manual")

    assert len(calls) == 1
    assert calls[0].url == DIRECT_URL
    assert calls[0].default_dir == str(_db.parent)  # queue's effective default
    assert len(committed) == 1
    assert ids == [43]


def test_dialog_commit_uses_task_local_overrides_only(queue_env, monkeypatch):
    """The committed prepared request carries the dialog's overrides and the
    global default is never touched."""
    queue, _rpc, _db = queue_env()
    calls, plan = _dialog_calls(monkeypatch)
    committed = _commit_spy(monkeypatch, queue)
    host = _host(queue)
    plan["result"] = 1
    plan["filename"] = "custom-name.zip"
    plan["dir"] = "/tmp/cove-alt"
    before = queue.settings.download_dir

    host.add_urls_checked([DIRECT_URL], intake="manual")

    assert committed[0].filename == "custom-name.zip"
    assert committed[0].out_dir == "/tmp/cove-alt"
    assert queue.settings.download_dir == before
    assert _db.parent != "/tmp/cove-alt"


def test_dialog_reject_commits_nothing_and_changes_no_preference(
    queue_env, monkeypatch
):
    queue, _rpc, db_path = queue_env()
    calls, plan = _dialog_calls(monkeypatch)
    committed = _commit_spy(monkeypatch, queue)
    host = _host(queue)
    plan["result"] = 0  # Rejected
    plan["dont_show_again"] = True
    before = queue.settings.show_download_options

    ids = host.add_urls_checked([DIRECT_URL], intake="manual")

    assert ids == []
    assert committed == []
    assert _rows(db_path) == []
    assert queue.tasks == {}
    assert queue.settings.show_download_options is before


def test_dialog_cancel_leaves_no_db_row_or_backend_job(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env()
    calls, plan = _dialog_calls(monkeypatch)
    host = _host(queue)
    plan["result"] = 0  # Rejected

    host.add_urls_checked([DIRECT_URL], intake="manual")

    assert _rows(db_path) == []
    assert rpc.added == []
    assert queue.tasks == {}


def test_dont_show_again_persists_only_on_start(queue_env, monkeypatch, tmp_path):
    from cove import config as _config

    queue, _rpc, _db = queue_env()
    # Isolate the settings file: the coordinator persists the opt-out via
    # Settings.save(), which writes CONFIG_FILE — never the real user config.
    monkeypatch.setattr(_config, "CONFIG_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(_config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(_config, "DATA_DIR", tmp_path)
    calls, plan = _dialog_calls(monkeypatch)
    committed = _commit_spy(monkeypatch, queue)
    host = _host(queue)
    plan["result"] = 1
    plan["dont_show_again"] = True

    host.add_urls_checked([DIRECT_URL], intake="manual")

    assert len(committed) == 1
    assert queue.settings.show_download_options is False


def test_setting_off_bypasses_the_preflight(queue_env, monkeypatch):
    queue, _rpc, db_path = queue_env()
    queue.settings.show_download_options = False
    calls, _plan = _dialog_calls(monkeypatch)
    committed = _commit_spy(monkeypatch, queue)
    host = _host(queue)

    ids = host.add_urls_checked([DIRECT_URL], intake="manual")

    assert calls == []
    assert len(committed) == 1
    assert committed[0].filename is None
    assert ids == [43]


def test_setting_off_preserves_an_explicit_destination(queue_env, monkeypatch):
    """Codex #1: bypassing the dialog must not discard the caller's out_dir."""
    queue, _rpc, _db = queue_env()
    queue.settings.show_download_options = False
    calls, _plan = _dialog_calls(monkeypatch)
    host = _host(queue)
    host._confirm_duplicate = lambda *a, **k: True

    ids = host.add_urls_checked([DIRECT_URL], out_dir="/srv/explicit", intake="manual")

    assert calls == []
    assert len(ids) == 1
    task = queue.tasks[ids[0]]
    assert task.out_dir == "/srv/explicit"


def test_preflight_prepares_once_and_commits_the_same_object(
    queue_env, monkeypatch
):
    """Codex #2: the prepared request is committed directly — never prepared
    again by a second add_url call."""
    queue, _rpc, _db = queue_env()
    calls, plan = _dialog_calls(monkeypatch)
    host = _host(queue)
    plan["result"] = 1
    plan["filename"] = "typed.zip"
    plan["dir"] = "/tmp/cove-typed"

    prepared_calls = []
    real_prepare = queue.prepare_url
    monkeypatch.setattr(
        queue, "prepare_url",
        lambda *a, **k: prepared_calls.append(a[0]) or real_prepare(*a, **k),
    )

    ids = host.add_urls_checked([DIRECT_URL], intake="manual")

    assert len(prepared_calls) == 1
    assert len(ids) == 1
    task = queue.tasks[ids[0]]
    assert task.filename == "typed.zip"
    assert task.out_dir == "/tmp/cove-typed"


def test_preflight_rejected_request_commits_nothing_and_emits_once(
    queue_env, monkeypatch
):
    """Codex #2: a classification rejection (e.g. magnet with torrents off)
    produces no commit and no dialog."""
    queue, _rpc, db_path = queue_env()  # torrent support off
    calls, _plan = _dialog_calls(monkeypatch)
    host = _host(queue)
    errors = []
    queue.error.connect(errors.append)

    ids = host.add_urls_checked([MAGNET], intake="manual")

    assert ids == []
    assert calls == []
    assert _rows(db_path) == []
    assert errors, "the legacy rejection message is still emitted once"


def test_duplicate_confirmed_manual_direct_still_shows_the_preflight(
    queue_env, monkeypatch
):
    """Codex round 2: duplicate status affects only the confirmation; an
    eligible single manual direct HTTP URL still gets the Download File Info
    dialog after the user picks 'Download Anyway'."""
    import cove.dedup as dedup_mod

    queue, _rpc, _db = queue_env()
    calls, plan = _dialog_calls(monkeypatch)
    committed = _commit_spy(monkeypatch, queue)
    host = _host(queue)

    dup = dedup_mod.DuplicateMatch(
        category=dedup_mod.LIVE, identity=dedup_mod.ID_URL,
        task_id=99, status="queued", name="archive.zip", can_duplicate=True,
    )
    host._confirm_duplicate = lambda *a, **k: True
    monkeypatch.setattr(
        queue, "find_duplicate",
        lambda url, **kw: dup if url == DIRECT_URL else None,
    )
    plan["result"] = 1
    plan["filename"] = "dup-confirmed.zip"

    ids = host.add_urls_checked([DIRECT_URL], intake="manual")

    assert len(calls) == 1, "the preflight dialog must still appear"
    assert calls[0].url == DIRECT_URL
    assert len(committed) == 1
    assert committed[0].filename == "dup-confirmed.zip"
    assert ids == [43]


def test_blank_filename_keeps_none_and_backend_naming(queue_env, monkeypatch):
    """RED 6 / the dominant regression: no URL-basename forcing."""
    queue, _rpc, _db = queue_env()
    calls, plan = _dialog_calls(monkeypatch)
    committed = _commit_spy(monkeypatch, queue)
    host = _host(queue)
    plan["result"] = 1
    plan["filename"] = None  # blank field -> None

    host.add_urls_checked([DIRECT_URL], intake="manual")

    assert committed[0].filename is None


# ---- ineligible: never a dialog -----------------------------------------


@pytest.mark.parametrize(
    "urls,intake,label",
    [
        ([DIRECT_URL], "extension", "extension direct"),
        ([HLS_URL], "manual", "manual HLS"),
        ([YTDLP_URL], "manual", "manual yt-dlp"),
        ([MAGNET], "manual", "manual magnet"),
        ([DIRECT_URL, "https://example.com/b.bin"], "manual", "batch"),
        ([DIRECT_URL], "clipboard", "clipboard"),
        ([DIRECT_URL], "search", "search direct"),
        (["ftp://example.com/file.bin"], "manual", "manual ftp"),
        (["https://www.alldebrid.com/f/XYZ789"], "manual", "debrid share link"),
    ],
)
def test_ineligible_paths_never_show_the_dialog(
    queue_env, monkeypatch, urls, intake, label
):
    queue, _rpc, _db = queue_env(**_torrent_settings())
    calls, _plan = _dialog_calls(monkeypatch)
    committed = _commit_spy(monkeypatch, queue)
    host = _host(queue)

    host.add_urls_checked(urls, intake=intake)

    assert calls == [], label
    # The legacy path still commits each eligible URL.
    if label in ("batch",):
        assert len(committed) == 2, label
    else:
        assert len(committed) == 1, label


def test_ftp_manual_direct_bypasses_the_preflight(queue_env, monkeypatch):
    """Codex round 3 #1: FTP is accepted by aria2 but is not a direct HTTP
    download — the preflight must not appear for it."""
    queue, _rpc, _db = queue_env()
    calls, _plan = _dialog_calls(monkeypatch)
    host = _host(queue)

    ids = host.add_urls_checked(["ftp://example.com/file.bin"], intake="manual")

    assert calls == []
    assert len(ids) == 1
    assert queue.tasks[ids[0]].backend == "aria2"


def test_debrid_delivery_url_is_eligible_for_the_preflight(queue_env, monkeypatch):
    """Codex round 3 #2: a generated debrid delivery URL (*.debrid.it/dl/...)
    is a plain direct download — it keeps the preflight, matching the queue's
    own share-link gate."""
    queue, _rpc, _db = queue_env()
    calls, plan = _dialog_calls(monkeypatch)
    host = _host(queue)
    plan["result"] = 1

    ids = host.add_urls_checked(
        ["https://s1.debrid.it/dl/NODE/file.zip"], intake="manual"
    )

    assert len(calls) == 1
    assert len(ids) == 1


def test_debrid_share_link_bypasses_the_preflight(queue_env, monkeypatch):
    """Codex round 3 #2: an AllDebrid /f/ share link stays on the legacy path."""
    queue, _rpc, _db = queue_env()
    calls, _plan = _dialog_calls(monkeypatch)
    host = _host(queue)

    ids = host.add_urls_checked(
        ["https://www.alldebrid.com/f/XYZ789"], intake="manual"
    )

    assert calls == []
    assert len(ids) == 1


def test_mixed_case_http_scheme_is_still_eligible(queue_env, monkeypatch):
    """Codex round 4 #1: the scheme compare is case-insensitive, matching the
    queue's own URL acceptance."""
    queue, _rpc, _db = queue_env()
    calls, plan = _dialog_calls(monkeypatch)
    host = _host(queue)
    plan["result"] = 1

    ids = host.add_urls_checked(["HTTPS://example.com/file.zip"], intake="manual")

    assert len(calls) == 1
    assert len(ids) == 1


def test_dont_show_again_save_failure_still_commits_and_reverts(
    queue_env, monkeypatch
):
    """Codex round 5: an optional preference-save failure must not lose the
    accepted download, and runtime state must not claim a dismissal that
    never persisted."""
    from pathlib import Path as _Path

    from cove import config as _config

    queue, _rpc, db_path = queue_env()
    monkeypatch.setattr(_config, "CONFIG_FILE", _Path("/nonexistent/root/settings.json"))
    monkeypatch.setattr(_config, "CONFIG_DIR", _Path("/nonexistent/root"))
    calls, plan = _dialog_calls(monkeypatch)
    committed = _commit_spy(monkeypatch, queue)
    host = _host(queue)
    plan["result"] = 1
    plan["dont_show_again"] = True

    host.add_urls_checked([DIRECT_URL], intake="manual")

    assert len(committed) == 1, "the accepted download must still commit"
    assert queue.settings.show_download_options is True, (
        "a failed save must not leave the preference disabled in memory"
    )


def _torrent_settings():
    return dict(
        torrent_support_enabled=True,
        all_debrid_enabled=True,
        all_debrid_api_key="ad-key-value",
    )


def test_search_result_bypasses_the_preflight(queue_env, monkeypatch):
    """Search intake goes through add_search_result -> add_urls_checked with
    intake='search'; the magnet is a torrent and must never see the dialog."""
    from cove.search.models import SearchResult

    queue, _rpc, _db = queue_env(**_torrent_settings())
    calls, _plan = _dialog_calls(monkeypatch)
    committed = _commit_spy(monkeypatch, queue)
    host = _host(queue)
    result = SearchResult(
        info_hash="0123456789abcdef0123456789abcdef01234567",
        name="Season 1",
        magnet=MAGNET,
        size_bytes=1,
        seeders=1,
        leechers=0,
        added=1_700_000_000,
        source="nyaa",
    )

    ids = host.add_search_result(result)

    assert calls == []
    assert len(ids) == 1
    assert committed[0].source_type == "torrent"


def test_extension_direct_http_keeps_the_direct_queue_path(queue_env, monkeypatch):
    """The extension path is queue.add_url(intake='extension') from the
    BrowserDownloadGate; it never routes through the manual coordinator, so
    it can never show the modal."""
    import cove.app as app_mod

    queue, _rpc, db_path = queue_env()
    gate = app_mod.BrowserDownloadGate()
    gate.queue = queue
    gate.ready = True
    calls, _plan = _dialog_calls(monkeypatch)

    accepted = gate.accept({"url": DIRECT_URL, "request_id": "req-1"})

    assert accepted is True
    assert calls == []
    assert len(_rows(db_path)) == 1


def test_multi_url_manual_never_shows_a_dialog_for_any_item(
    queue_env, monkeypatch
):
    queue, _rpc, _db = queue_env()
    calls, _plan = _dialog_calls(monkeypatch)
    committed = _commit_spy(monkeypatch, queue)
    host = _host(queue)

    host.add_urls_checked([DIRECT_URL, "https://example.com/b.bin"], intake="manual")

    assert calls == []
    assert len(committed) == 2


def test_queue_internal_callers_stay_ui_free(queue_env, monkeypatch):
    """Direct QueueManager callers (API server, restore, retry) never show UI."""
    from cove.api_server import QueueApiBridge

    queue, _rpc, _db = queue_env()
    calls, _plan = _dialog_calls(monkeypatch)
    bridge = QueueApiBridge(queue, timeout=2)

    task_id = bridge.invoke("add", {"url": DIRECT_URL})

    assert calls == []
    assert task_id["task_id"] is not None


def test_prepared_request_is_request_local(queue_env):
    """No global mutable pending override: two prepared requests cannot
    cross-assign filename or directory (pin at the coordinator level too)."""
    queue, _rpc, _db = queue_env()
    a = queue.prepare_url(DIRECT_URL, intake="manual")
    b = queue.prepare_url("https://example.com/b.bin", intake="manual")

    a = replace(a, out_dir="/srv/a", filename="a.bin")

    assert b.out_dir != "/srv/a"
    assert b.filename is None


def test_prepared_value_is_immutable(queue_env):
    """The prepared value is frozen, so no in-place override can ever leak
    into another prepared request — the only way to override is replace(),
    which is request-local by construction."""
    import dataclasses

    queue, _rpc, _db = queue_env()
    a = queue.prepare_url(DIRECT_URL, intake="manual")

    with pytest.raises(dataclasses.FrozenInstanceError):
        a.out_dir = "/srv/evil"
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.filename = "evil.zip"

    assert isinstance(a, PreparedDownload)


def test_preflight_causes_no_extra_metadata_request(queue_env, monkeypatch):
    """RED 33: the prepare + dialog path adds no HEAD/GET probe; the launch
    probe still happens only at _launch time via _spawn."""
    queue, _rpc, _db = queue_env()
    calls, plan = _dialog_calls(monkeypatch)
    host = _host(queue)
    plan["result"] = 1
    spawned = []
    monkeypatch.setattr(queue, "_spawn", lambda *a, **k: spawned.append(a))

    host.add_urls_checked([DIRECT_URL], intake="manual")

    assert len(calls) == 1
    assert spawned == []  # commit is local; launch is the caller's next step
