"""Regression tests for cove.queue._load_persisted row restoration.

Guards against a pre-existing bug where sqlite3.Row (which has no .get())
was accessed with row.get("backend", ...), raising AttributeError whenever
a persisted queued/active/paused task was restored on startup.
"""

import errno
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication

from cove import config, db, debrid
from cove.config import CategoryDirs, Settings
from cove.aria2 import Aria2Error
from cove.debrid import ALL_DEBRID, DebridError, Unrestricted
from cove.extractor import FINAL_PATH_MARKER
import cove.output_paths as output_paths
import cove.queue as queue_module
from cove.output_paths import (
    OutputPathError,
    cleanup_work_directory,
    collision_candidates,
    create_work_directory,
    publish_output,
    validate_public_filename,
)
from cove.queue import QueueManager, _row_get, _task_from_persisted_row


def _row(conn, **overrides):
    values = {
        "url": "https://example.com/f.zip",
        "out_dir": "/dl",
        "created_at": time.time(),
        "status": "queued",
    }
    values.update(overrides)
    cols = ", ".join(values.keys())
    placeholders = ", ".join("?" for _ in values)
    conn.execute(
        f"INSERT INTO downloads ({cols}) VALUES ({placeholders})",
        tuple(values.values()),
    )
    return conn.execute("SELECT * FROM downloads WHERE id = last_insert_rowid()").fetchone()


def test_row_get_returns_default_for_missing_column(tmp_path):
    path = tmp_path / "cove.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE t (id INTEGER PRIMARY KEY, url TEXT);"
        "INSERT INTO t (url) VALUES ('x');"
    )
    row = conn.execute("SELECT * FROM t").fetchone()
    assert _row_get(row, "backend", "aria2") == "aria2"
    assert _row_get(row, "url") == "x"
    conn.close()


def test_queued_row_restores_without_attribute_error(tmp_path):
    path = tmp_path / "cove.db"
    db.init(path)
    with db.connect(path) as conn:
        row = _row(conn, status="queued")
        task = _task_from_persisted_row(row)
    assert task.status == "queued"
    assert task.backend == "aria2"


def test_active_row_restores_as_queued_for_repoll(tmp_path):
    path = tmp_path / "cove.db"
    db.init(path)
    with db.connect(path) as conn:
        row = _row(conn, status="active")
        task = _task_from_persisted_row(row)
    # _load_persisted always resets restored tasks to "queued" so the
    # queue manager re-adopts/re-polls them rather than assuming an
    # aria2 gid that no longer exists.
    assert task.status == "queued"


def test_video_row_restores_browser_headers(tmp_path):
    path = tmp_path / "cove.db"
    db.init(path)
    with db.connect(path) as conn:
        row = _row(
            conn,
            backend="ffmpeg",
            cookies="session=abc",
            referrer="https://example.com/page",
            user_agent="TestUA/1.0",
        )
        task = _task_from_persisted_row(row)
    assert task.cookies == "session=abc"
    assert task.referrer == "https://example.com/page"
    assert task.user_agent == "TestUA/1.0"


def test_backend_restored_when_present(tmp_path):
    path = tmp_path / "cove.db"
    db.init(path)
    with db.connect(path) as conn:
        row = _row(conn, status="paused", backend="hls")
        task = _task_from_persisted_row(row)
    assert task.backend == "hls"


def test_row_missing_optional_columns_uses_defaults(tmp_path):
    """A DB predating the backend migration (column absent entirely) must
    not crash and should fall back to safe defaults."""
    path = tmp_path / "cove.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            filename TEXT,
            out_dir TEXT NOT NULL,
            connections INTEGER NOT NULL DEFAULT 16,
            speed_limit_kbps INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'queued',
            total_bytes INTEGER NOT NULL DEFAULT 0,
            completed_bytes INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            segments INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    row = _row(conn, status="queued")
    conn.commit()
    task = _task_from_persisted_row(row)
    conn.close()
    assert task.backend == "aria2"


def test_add_url_accepts_api_overrides_and_preserves_hls_backend(tmp_path, monkeypatch):
    QCoreApplication.instance() or QCoreApplication([])
    path = tmp_path / "cove.db"
    original_init = db.init
    original_connect = db.connect
    monkeypatch.setattr(db, "init", lambda: original_init(path))
    monkeypatch.setattr(db, "connect", lambda: original_connect(path))
    monkeypatch.setattr("shutil.which", lambda name: "C:/ffmpeg.exe" if name == "ffmpeg" else None)

    settings = Settings(
        download_dir=str(tmp_path),
        connections_per_server=16,
        auto_sort_by_category=True,
        category_dirs=CategoryDirs(),
    )
    queue = QueueManager(settings, SimpleNamespace())
    queue._scheduler_allows = False
    try:
        task_id = queue.add_url(
            "https://example.com/live/stream.m3u8",
            out_dir=str(tmp_path),
            filename="requested.mp4",
            connections=4,
            speed_limit_kbps=128,
        )
        task = queue.tasks[task_id]
        assert task.backend == "ffmpeg"
        assert task.filename == "requested.mp4"
        assert task.connections == 4
        assert task.speed_limit_kbps == 128

        default_id = queue.add_url("https://example.com/archive.zip")
        default_task = queue.tasks[default_id]
        assert default_task.connections == 16
        assert default_task.out_dir == str(tmp_path / "Archives")
    finally:
        queue._poll.stop()
        queue._ext_poll.stop()


# ---------------------------------------------------------------------------
# Debrid resolution at launch time
# ---------------------------------------------------------------------------

NODE_URL = "https://s1.debrid.it/dl/SECRETNODE/movie.mkv"
ORIGINAL_URL = "https://rapidgator.net/file/abc"


class _FakeRpc:
    """Records what actually reaches aria2."""

    def __init__(self):
        self.added = []
        self.paused = []

    def pause(self, gid):
        # The queue pauses a freshly launched gid when the scheduler is
        # holding the queue, which the tests below drive synchronously.
        self.paused.append(gid)
        return gid

    def add_uri(self, uris, out_dir, connections, speed_limit_kbps, filename):
        self.added.append({
            "uris": list(uris),
            "out_dir": out_dir,
            "connections": connections,
            "filename": filename,
        })
        return "gid-1"

    # ---- BitTorrent (Slice B) -----------------------------------------

    version = {"version": "1.37.0", "enabledFeatures": ["BitTorrent", "HTTPS"]}

    def __getattr__(self, name):
        # Torrent state lives in lazily created lists so the plain HTTP
        # tests above keep their tiny stub.
        if name in ("magnets", "torrents", "removed", "unpaused", "version_calls",
                    "paused_all", "status_calls"):
            value = []
            setattr(self, name, value)
            return value
        raise AttributeError(name)

    def get_version(self):
        self.version_calls.append(True)
        return self.version

    def add_magnet(self, uri, out_dir, speed_limit_kbps=0):
        self.magnets.append({"uri": uri, "out_dir": out_dir,
                             "speed_limit_kbps": speed_limit_kbps})
        return "gid-meta"

    def add_torrent(self, data, out_dir, speed_limit_kbps=0, select_file=None):
        self.torrents.append({"data": data, "out_dir": out_dir,
                              "speed_limit_kbps": speed_limit_kbps,
                              "select_file": select_file})
        return "gid-file"

    def get_files(self, gid):
        return getattr(self, "files_result", [])

    def remove(self, gid, force=True):
        self.removed.append(gid)
        return gid

    def unpause(self, gid):
        self.unpaused.append(gid)
        return gid

    def pause_all(self):
        self.paused_all.append(True)
        return "OK"

    def tell_status(self, gid):
        self.status_calls.append(gid)
        return dict(getattr(self, "status_result", {}))


@pytest.fixture
def queue_env(tmp_path, monkeypatch):
    """A QueueManager backed by a throwaway DB and a recording RPC stub."""
    QCoreApplication.instance() or QCoreApplication([])
    path = tmp_path / "cove.db"
    original_init = db.init
    original_connect = db.connect
    monkeypatch.setattr(db, "init", lambda: original_init(path))
    monkeypatch.setattr(db, "connect", lambda: original_connect(path))
    # Keep the debrid host-domain cache out of the real data directory.
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    def _build(**settings_kwargs):
        settings = Settings(
            download_dir=str(tmp_path),
            category_dirs=CategoryDirs(),
            **settings_kwargs,
        )
        rpc = _FakeRpc()
        queue = QueueManager(settings, rpc)
        queue._scheduler_allows = False
        queue._poll.stop()
        queue._ext_poll.stop()
        return queue, rpc, path

    return _build


def _debrid_settings(**extra):
    base = dict(all_debrid_enabled=True, all_debrid_api_key="ad-key-value")
    base.update(extra)
    return base


def _persisted_row(db_path, tid):
    # Read straight through sqlite3: db.connect is monkeypatched to take no
    # arguments inside the queue_env fixture.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM downloads WHERE id=?", (tid,)).fetchone()
    finally:
        conn.close()


def test_legacy_browser_drop_files_are_purged_without_being_downloaded(
    queue_env, monkeypatch, tmp_path
):
    """The old native-messaging host wrote a durable request here and the
    queue consumed it at the next launch - which is exactly how a download
    intercepted while Cove was closed reappeared later. Delivery is now
    synchronous, so any file left behind by the buggy version must be
    retired, never added.
    """
    import json as _json
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    drop_dir = tmp_path / "drop"
    drop_dir.mkdir(parents=True)
    legacy = [
        drop_dir / "download-1-abcd1234.json",
        drop_dir / "download-2-efab5678.json.bad",
        drop_dir / "download-3-0badf00d.tmp",
    ]
    for f in legacy:
        f.write_text(_json.dumps({"url": "https://example.invalid/stale.rar"}))
    unrelated = drop_dir / "notes.txt"
    unrelated.write_text("user file")

    queue, _rpc, _db = queue_env()
    calls = []
    monkeypatch.setattr(queue, "add_url", lambda *a, **k: calls.append(a))

    queue._purge_legacy_drop_dir()

    assert calls == []
    assert queue.tasks == {}
    for f in legacy:
        assert not f.exists()
    # Only the browser host's own naming convention is touched.
    assert unrelated.exists()


def test_legacy_drop_purge_is_idempotent_and_tolerates_a_missing_dir(
    queue_env, monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "nonexistent")
    queue, _rpc, _db = queue_env()
    queue._purge_legacy_drop_dir()
    queue._purge_legacy_drop_dir()


def test_queue_no_longer_polls_a_drop_directory(queue_env, monkeypatch, tmp_path):
    """The deferred-delivery consumer is retired, not merely disabled."""
    import json as _json
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    drop_dir = tmp_path / "drop"
    drop_dir.mkdir(parents=True)
    (drop_dir / "download-9-99999999.json").write_text(
        _json.dumps({"url": "https://example.invalid/stale.rar"})
    )

    queue, _rpc, _db = queue_env()
    assert not hasattr(queue, "_check_drop_dir")
    assert not hasattr(queue, "_drop_poll")
    assert queue.tasks == {}


def _persisted_url(db_path, tid):
    return _persisted_row(db_path, tid)["url"]


def _persisted_row_text(db_path, tid):
    row = _persisted_row(db_path, tid)
    return " ".join(str(row[k]) for k in row.keys())


def test_debrid_url_goes_to_aria2_while_the_task_keeps_the_original(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(**_debrid_settings())
    monkeypatch.setattr(
        debrid, "resolve",
        lambda url, settings, **kw: Unrestricted(NODE_URL, "movie.mkv", 4096, ALL_DEBRID),
    )
    tid = queue.add_url(ORIGINAL_URL)
    task = queue.tasks[tid]

    queue._probe_and_add(task)

    assert rpc.added[0]["uris"] == [NODE_URL]
    assert task.url == ORIGINAL_URL
    assert task.debrid_provider == ALL_DEBRID
    assert _persisted_url(db_path, tid) == ORIGINAL_URL


def test_debrid_generated_url_is_never_persisted(queue_env, monkeypatch):
    queue, _rpc, db_path = queue_env(**_debrid_settings())
    monkeypatch.setattr(
        debrid, "resolve",
        lambda url, settings, **kw: Unrestricted(NODE_URL, "movie.mkv", 4096, ALL_DEBRID),
    )
    tid = queue.add_url(ORIGINAL_URL)
    task = queue.tasks[tid]

    queue._probe_and_add(task)
    task.status = "active"
    queue._persist(task)

    row_text = _persisted_row_text(db_path, tid)
    assert "SECRETNODE" not in row_text
    assert NODE_URL not in row_text
    assert "debrid.it" not in row_text


def test_provider_filename_fills_an_empty_filename(queue_env, monkeypatch):
    queue, _rpc, _db = queue_env(**_debrid_settings())
    monkeypatch.setattr(
        debrid, "resolve",
        lambda url, settings, **kw: Unrestricted(NODE_URL, "movie.mkv", 4096, ALL_DEBRID),
    )
    tid = queue.add_url(ORIGINAL_URL)
    task = queue.tasks[tid]
    assert task.filename is None

    queue._probe_and_add(task)
    assert task.filename == "movie.mkv"


def test_provider_filename_never_overwrites_a_user_supplied_name(queue_env, monkeypatch):
    queue, rpc, _db = queue_env(**_debrid_settings())
    monkeypatch.setattr(
        debrid, "resolve",
        lambda url, settings, **kw: Unrestricted(NODE_URL, "provider.mkv", 4096, ALL_DEBRID),
    )
    tid = queue.add_url(ORIGINAL_URL, filename="my choice.mkv")
    task = queue.tasks[tid]

    queue._probe_and_add(task)
    assert task.filename == "my choice.mkv"
    assert rpc.added[0]["filename"] == "my choice.mkv"


def test_provider_filesize_seeds_progress(queue_env, monkeypatch):
    queue, _rpc, _db = queue_env(**_debrid_settings())
    monkeypatch.setattr(
        debrid, "resolve",
        lambda url, settings, **kw: Unrestricted(NODE_URL, "movie.mkv", 4096, ALL_DEBRID),
    )
    tid = queue.add_url(ORIGINAL_URL)
    task = queue.tasks[tid]

    queue._probe_and_add(task)
    assert task.total_bytes == 4096


def test_provider_filesize_skips_the_head_probe(queue_env, monkeypatch):
    """A provider that already told us the size does not need a HEAD, which
    would otherwise send the secret node URL on a second round trip."""
    queue, _rpc, _db = queue_env(**_debrid_settings())
    monkeypatch.setattr(
        debrid, "resolve",
        lambda url, settings, **kw: Unrestricted(NODE_URL, "movie.mkv", 4096, ALL_DEBRID),
    )
    calls = []
    monkeypatch.setattr(
        "requests.Session.head",
        lambda self, url, **kw: calls.append(url) or (_ for _ in ()).throw(AssertionError("HEAD")),
    )
    tid = queue.add_url(ORIGINAL_URL)
    queue._probe_and_add(queue.tasks[tid])
    assert calls == []


def test_zero_total_length_does_not_erase_a_seeded_filesize(queue_env):
    queue, _rpc, _db = queue_env(**_debrid_settings())
    tid = queue.add_url(ORIGINAL_URL)
    task = queue.tasks[tid]
    task.total_bytes = 4096

    queue._apply_status(tid, {"totalLength": "0", "completedLength": "512",
                              "downloadSpeed": "10", "status": "active"})
    assert task.total_bytes == 4096
    assert task.completed_bytes == 512


def test_missing_total_length_does_not_erase_a_seeded_filesize(queue_env):
    queue, _rpc, _db = queue_env(**_debrid_settings())
    tid = queue.add_url(ORIGINAL_URL)
    task = queue.tasks[tid]
    task.total_bytes = 4096

    queue._apply_status(tid, {"completedLength": "512", "status": "active"})
    assert task.total_bytes == 4096


def test_positive_total_length_still_updates_size(queue_env):
    queue, _rpc, _db = queue_env(**_debrid_settings())
    tid = queue.add_url(ORIGINAL_URL)
    task = queue.tasks[tid]
    task.total_bytes = 4096

    queue._apply_status(tid, {"totalLength": "9000", "completedLength": "10",
                              "status": "active"})
    assert task.total_bytes == 9000


def test_ordinary_head_content_length_seeds_size(queue_env, monkeypatch):
    """Non-debrid path: a positive Content-Length gives the progress bar a
    denominator before aria2 reports one."""
    queue, _rpc, _db = queue_env()
    tid = queue.add_url("https://example.com/big.zip")
    task = queue.tasks[tid]

    monkeypatch.setattr(
        "requests.Session.head",
        lambda self, url, **kw: SimpleNamespace(
            ok=True, headers={"Accept-Ranges": "bytes", "Content-Length": "20971520"}
        ),
    )
    queue._probe_and_add(task)
    assert task.total_bytes == 20971520


def test_ordinary_head_does_not_overwrite_an_existing_size(queue_env, monkeypatch):
    queue, _rpc, _db = queue_env()
    tid = queue.add_url("https://example.com/big.zip")
    task = queue.tasks[tid]
    task.total_bytes = 111

    monkeypatch.setattr(
        "requests.Session.head",
        lambda self, url, **kw: SimpleNamespace(
            ok=True, headers={"Accept-Ranges": "bytes", "Content-Length": "20971520"}
        ),
    )
    queue._probe_and_add(task)
    assert task.total_bytes == 111


def test_debrid_disabled_keeps_the_plain_add_uri_path(queue_env, monkeypatch):
    queue, _rpc, _db = queue_env(intelligent_segments=False)
    tid = queue.add_url("https://example.com/big.zip")

    spawned = []
    monkeypatch.setattr(queue, "_spawn",
                        lambda fn, *a, **kw: spawned.append(getattr(fn, "__name__", fn)))
    queue._launch(queue.tasks[tid])
    assert spawned == ["add_uri"]


def test_intelligent_segments_off_still_routes_through_debrid(queue_env, monkeypatch):
    queue, _rpc, _db = queue_env(intelligent_segments=False, **_debrid_settings())
    tid = queue.add_url(ORIGINAL_URL)

    spawned = []
    monkeypatch.setattr(queue, "_spawn",
                        lambda fn, *a, **kw: spawned.append(getattr(fn, "__name__", fn)))
    queue._launch(queue.tasks[tid])
    assert spawned == ["_probe_and_add"]


def test_enabled_provider_without_a_key_still_routes_through_the_resolver(queue_env, monkeypatch):
    """Otherwise the user silently gets a free-tier download instead of the
    "no API key saved" error."""
    queue, _rpc, _db = queue_env(intelligent_segments=False, all_debrid_enabled=True)
    tid = queue.add_url(ORIGINAL_URL)

    spawned = []
    monkeypatch.setattr(queue, "_spawn",
                        lambda fn, *a, **kw: spawned.append(getattr(fn, "__name__", fn)))
    queue._launch(queue.tasks[tid])
    assert spawned == ["_probe_and_add"]


def test_resolver_failure_propagates_to_the_task_error_path(queue_env, monkeypatch):
    queue, rpc, _db = queue_env(**_debrid_settings())

    def _boom(url, settings, **kw):
        raise DebridError(ALL_DEBRID, "AUTH_BAD_APIKEY",
                          "the API key was rejected. Check the key in Settings.")

    monkeypatch.setattr(debrid, "resolve", _boom)
    tid = queue.add_url(ORIGINAL_URL)

    with pytest.raises(DebridError):
        queue._probe_and_add(queue.tasks[tid])
    assert rpc.added == [], "a failed resolution must not fall through to aria2"


def test_debrid_error_reaches_the_task_row_as_a_readable_message(queue_env, monkeypatch):
    from cove.queue import _RpcCall

    queue, _rpc, _db = queue_env(**_debrid_settings())
    tid = queue.add_url(ORIGINAL_URL)

    def _boom(*_a, **_kw):
        raise DebridError(ALL_DEBRID, "AUTH_BAD_APIKEY",
                          "the API key was rejected. Check the key in Settings.")

    messages = []
    call = _RpcCall(_boom)
    call.signals.failed.connect(messages.append)
    call.run()

    assert messages == ["AllDebrid: the API key was rejected. Check the key in Settings."]
    assert "DebridError" not in messages[0]


def test_unsupported_host_falls_through_to_the_direct_download(queue_env, monkeypatch):
    queue, rpc, _db = queue_env(**_debrid_settings())
    monkeypatch.setattr(debrid, "resolve", lambda url, settings, **kw: None)
    monkeypatch.setattr(
        "requests.Session.head",
        lambda self, url, **kw: SimpleNamespace(ok=True, headers={"Content-Length": "5"}),
    )
    tid = queue.add_url("https://example.com/plain.zip")
    task = queue.tasks[tid]

    queue._probe_and_add(task)
    assert rpc.added[0]["uris"] == ["https://example.com/plain.zip"]
    assert task.debrid_provider == ""
    assert task.resolved_url == ""


def test_transient_debrid_fields_are_cleared_on_relaunch(queue_env, monkeypatch):
    queue, _rpc, _db = queue_env(**_debrid_settings())
    tid = queue.add_url(ORIGINAL_URL)
    task = queue.tasks[tid]
    task.resolved_url = NODE_URL
    task.debrid_provider = ALL_DEBRID

    monkeypatch.setattr(queue, "_spawn", lambda fn, *a, **kw: None)
    queue._launch(task)
    assert task.resolved_url == ""
    assert task.debrid_provider == ""


def test_transient_debrid_fields_are_cleared_on_completion(queue_env):
    queue, _rpc, _db = queue_env(**_debrid_settings())
    tid = queue.add_url(ORIGINAL_URL)
    task = queue.tasks[tid]
    task.resolved_url = NODE_URL
    task.debrid_provider = ALL_DEBRID

    queue._apply_status(tid, {"totalLength": "10", "completedLength": "10",
                              "status": "complete"})
    assert task.status == "completed"
    assert task.resolved_url == ""


def test_resolved_url_is_not_a_persisted_column(queue_env):
    """The transient fields must stay off the schema entirely."""
    queue, _rpc, db_path = queue_env(**_debrid_settings())
    conn = sqlite3.connect(db_path)
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(downloads)")}
    finally:
        conn.close()
    assert "resolved_url" not in columns
    assert "debrid_provider" not in columns


# ---------------------------------------------------------------------------
# Provider share links fail with a readable reason instead of downloading
# the provider's "forbidden" HTML page
# ---------------------------------------------------------------------------

SHARE_URL = "https://real-debrid.com/d/ALJRILITCGUEW127"


def test_share_link_fails_the_task_with_an_explanatory_reason(queue_env, monkeypatch):
    queue, rpc, _db = queue_env()
    tid = queue.add_url(SHARE_URL)
    task = queue.tasks[tid]

    spawned = []
    monkeypatch.setattr(queue, "_spawn", lambda fn, *a, **kw: spawned.append(fn))
    queue._launch(task)

    assert task.status == "error"
    assert "Real-Debrid" in task.error
    assert "original" in task.error.lower()
    assert task.finished_at is not None
    assert spawned == [], "a share link must never reach aria2"
    assert rpc.added == []


def test_share_link_reason_is_persisted_for_the_row(queue_env, monkeypatch):
    queue, _rpc, db_path = queue_env()
    tid = queue.add_url(SHARE_URL)
    monkeypatch.setattr(queue, "_spawn", lambda fn, *a, **kw: None)
    queue._launch(queue.tasks[tid])

    row = _persisted_row(db_path, tid)
    assert row["status"] == "error"
    assert "Real-Debrid" in row["error"]


def test_share_link_is_rejected_even_with_debrid_disabled(queue_env, monkeypatch):
    """The user's case: no key configured at all. Previously this silently
    downloaded the provider's forbidden page as a file."""
    queue, rpc, _db = queue_env(intelligent_segments=False)
    tid = queue.add_url("https://www.alldebrid.com/f/XYZ789")
    monkeypatch.setattr(queue, "_spawn", lambda fn, *a, **kw: None)
    queue._launch(queue.tasks[tid])

    assert queue.tasks[tid].status == "error"
    assert "AllDebrid" in queue.tasks[tid].error
    assert rpc.added == []


def test_generated_node_urls_still_download_normally(queue_env, monkeypatch):
    """Pasting a link Cove itself would hand to aria2 must keep working."""
    queue, _rpc, _db = queue_env()
    for url in (
        "https://s1.debrid.it/dl/abc/file.zip",
        "https://45.download.real-debrid.com/d/ABC123/file.zip",
    ):
        tid = queue.add_url(url)
        spawned = []
        monkeypatch.setattr(queue, "_spawn",
                            lambda fn, *a, **kw: spawned.append(getattr(fn, "__name__", fn)))
        queue._launch(queue.tasks[tid])
        assert queue.tasks[tid].status == "active", url
        assert spawned, url


def test_ordinary_download_is_unaffected_by_the_share_link_check(queue_env, monkeypatch):
    queue, _rpc, _db = queue_env(intelligent_segments=False)
    tid = queue.add_url("https://example.com/big.zip")
    spawned = []
    monkeypatch.setattr(queue, "_spawn",
                        lambda fn, *a, **kw: spawned.append(getattr(fn, "__name__", fn)))
    queue._launch(queue.tasks[tid])
    assert queue.tasks[tid].status == "active"
    assert spawned == ["add_uri"]


# ---------------------------------------------------------------------------
# Torrent routing (Slice A: cached debrid only)
# ---------------------------------------------------------------------------

from cove import torrent as torrent_mod          # noqa: E402
from cove.debrid import CachedTorrent, CachedTorrentFile, REAL_DEBRID  # noqa: E402
from cove.queue import (                          # noqa: E402
    SOURCE_TORRENT,
    SOURCE_TORRENT_FILE,
    TORRENT_ARIA2_FAILED,
    TORRENT_CONSENT_DECLINED,
    TORRENT_CANCELLED_UNCACHED,
    TORRENT_NO_BITTORRENT,
    TORRENT_METADATA_FAILED,
    TORRENT_SUPPORT_DISABLED,
    TORRENT_PROXY_BLOCKED,
    TorrentError,
)

INFO_HASH = "0123456789abcdef0123456789abcdef01234567"
MAGNET = (
    f"magnet:?xt=urn:btih:{INFO_HASH}&dn=Season+1"
    "&tr=http://tracker.example/announce?passkey=SECRETPASS"
)
AD_LOCKED_1 = "https://alldebrid.com/f/LOCKEDONE"
AD_LOCKED_2 = "https://alldebrid.com/f/LOCKEDTWO"
TORRENT_NODE_URL = "https://s1.debrid.it/dl/SECRETNODE/ep1.mkv"
RD_LOCKED_1 = "https://real-debrid.com/d/LOCKEDONE"
# The short-lived delivery URL a packed link unrestricts into. Never persisted.
RD_PACKED_DELIVERY = "https://sgp.download.real-debrid.com/d/PACKEDONE/Season+1.rar"


def _torrent_settings(**extra):
    base = dict(
        torrent_support_enabled=True,
        all_debrid_enabled=True,
        all_debrid_api_key="ad-key-value",
    )
    base.update(extra)
    return base


def _cached(files=None, name="Season 1", provider=ALL_DEBRID):
    if files is None:
        files = [
            CachedTorrentFile(0, ("ep1.mkv",), 10, AD_LOCKED_1),
            CachedTorrentFile(1, ("extras", "ep2.mkv"), 20, AD_LOCKED_2),
        ]
    return CachedTorrent(provider, INFO_HASH, name, tuple(files))


def _sync_spawn(queue):
    """Run worker calls inline so a test can drive _launch end to end."""
    calls = []

    def spawn(fn, *args, on_done=None, on_fail=None, **kwargs):
        calls.append(fn)
        try:
            result = fn(*args, **kwargs)
        except (Aria2Error, DebridError, TorrentError) as exc:
            if on_fail is not None:
                on_fail(str(exc))
        else:
            if on_done is not None:
                on_done(result)

    queue._spawn = spawn
    return calls


def _rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM downloads ORDER BY id").fetchall()
    finally:
        conn.close()


# --- feature flag ----------------------------------------------------------


def test_magnet_never_enters_the_plain_download_lifecycle_while_the_flag_is_off(
    queue_env, monkeypatch
):
    """A magnet is torrent work whether or not torrent support is on.

    aria2 accepts a magnet through addUri and answers with the *metadata*
    download: it completes at 100% with a `[METADATA]<hash>` file name and
    nothing of the torrent itself on disk. Routed as an ordinary download
    that is indistinguishable from a finished file, which is exactly the
    false "Done / 100%" the user sees. So the magnet is refused outright
    instead, and no aria2 job is created for it.
    """
    queue, rpc, db_path = queue_env(**_debrid_settings())
    _sync_spawn(queue)
    _running(queue)
    called = []
    monkeypatch.setattr(
        debrid, "resolve_torrent",
        lambda *a, **k: called.append(a) or None,
    )
    errors = []
    queue.error.connect(errors.append)

    assert queue.add_url(MAGNET) is None

    assert queue.tasks == {}
    assert _rows(db_path) == []
    assert rpc.added == []
    assert rpc.magnets == []
    assert called == []
    # The reason is shown, and it never quotes the magnet's passkey.
    assert errors == [TORRENT_SUPPORT_DISABLED]
    assert "SECRETPASS" not in errors[0]


def test_magnet_refusal_survives_a_torrent_source_type_argument(queue_env):
    """The internal source_type kwarg is not a way around the gate."""
    queue, rpc, db_path = queue_env(**_debrid_settings())
    _sync_spawn(queue)
    _running(queue)

    assert queue.add_url(MAGNET, source_type=SOURCE_TORRENT) is None
    assert _rows(db_path) == []
    assert rpc.added == []
    assert rpc.magnets == []


def test_magnet_becomes_a_torrent_source_task_when_enabled(queue_env):
    queue, rpc, db_path = queue_env(**_torrent_settings())
    tid = queue.add_url(MAGNET)
    task = queue.tasks[tid]

    assert task.source_type == SOURCE_TORRENT
    assert task.info_hash == INFO_HASH
    assert task.torrent_name == "Season 1"
    row = _persisted_row(db_path, tid)
    assert row["source_type"] == SOURCE_TORRENT
    assert row["info_hash"] == INFO_HASH


def test_malformed_magnet_is_rejected_without_a_row(queue_env):
    queue, rpc, db_path = queue_env(**_torrent_settings())
    errors = []
    queue.error.connect(errors.append)
    assert queue.add_url("magnet:?xt=urn:btih:nonsense") is None
    assert _rows(db_path) == []
    assert errors and "SECRETPASS" not in errors[0]


def test_duplicate_info_hash_is_blocked(queue_env):
    queue, rpc, db_path = queue_env(**_torrent_settings())
    first = queue.add_url(MAGNET)
    errors = []
    queue.error.connect(errors.append)
    second = queue.add_url(f"magnet:?xt=urn:btih:{INFO_HASH.upper()}")

    assert first is not None
    assert second is None
    assert len(_rows(db_path)) == 1
    assert errors


def test_completed_torrent_does_not_block_a_re_add(queue_env):
    queue, rpc, db_path = queue_env(**_torrent_settings())
    tid = queue.add_url(MAGNET)
    queue.tasks[tid].status = "completed"
    assert queue.add_url(MAGNET) is not None


# --- probing ---------------------------------------------------------------


def test_probing_happens_on_a_worker_not_the_gui_thread(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(**_torrent_settings())
    calls = _sync_spawn(queue)
    monkeypatch.setattr(debrid, "resolve_torrent", lambda *a, **k: None)
    tid = queue.add_url(MAGNET)

    queue._launch(queue.tasks[tid])

    assert queue._probe_torrent in calls


def test_probe_sends_only_the_info_hash_for_a_magnet(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(**_torrent_settings())
    seen = {}

    def fake_resolve(info_hash, settings, *, torrent_bytes=None, **kw):
        seen["hash"] = info_hash
        seen["bytes"] = torrent_bytes
        return None

    monkeypatch.setattr(debrid, "resolve_torrent", fake_resolve)
    tid = queue.add_url(MAGNET)
    queue._probe_torrent(queue.tasks[tid])

    assert seen == {"hash": INFO_HASH, "bytes": None}


def test_probe_reads_a_local_torrent_file_on_the_worker(queue_env, monkeypatch, tmp_path):
    queue, rpc, db_path = queue_env(**_torrent_settings())
    raw = (
        b"d4:infod6:lengthi7e4:name9:movie.mkv12:piece lengthi16384e"
        b"6:pieces20:" + b"\x01" * 20 + b"ee"
    )
    path = tmp_path / "s.torrent"
    path.write_bytes(raw)
    expected = torrent_mod.parse_torrent(raw).info_hash

    seen = {}

    def fake_resolve(info_hash, settings, *, torrent_bytes=None, **kw):
        seen["hash"] = info_hash
        seen["bytes"] = torrent_bytes
        return None

    monkeypatch.setattr(debrid, "resolve_torrent", fake_resolve)
    tid = queue.add_url(
        torrent_mod.minimal_magnet(expected),
        source_type=SOURCE_TORRENT,
        info_hash=expected,
        torrent_name="movie.mkv",
        torrent_path=str(path),
    )
    queue._probe_torrent(queue.tasks[tid])

    assert seen["hash"] == expected
    assert seen["bytes"] == raw


def test_probe_survives_a_deleted_torrent_file(queue_env, monkeypatch, tmp_path):
    queue, rpc, db_path = queue_env(**_torrent_settings())
    seen = {}
    monkeypatch.setattr(
        debrid, "resolve_torrent",
        lambda info_hash, settings, **kw: seen.setdefault("hash", info_hash),
    )
    tid = queue.add_url(
        torrent_mod.minimal_magnet(INFO_HASH),
        source_type=SOURCE_TORRENT,
        info_hash=INFO_HASH,
        torrent_path=str(tmp_path / "gone.torrent"),
    )
    queue._probe_torrent(queue.tasks[tid])
    assert seen["hash"] == INFO_HASH


# --- uncached --------------------------------------------------------------


def test_uncached_torrent_falls_back_to_local_bittorrent(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(**_local_settings())
    _sync_spawn(queue)
    monkeypatch.setattr(debrid, "resolve_torrent", lambda *a, **k: None)
    tid = queue.add_url(MAGNET)

    _running(queue)
    queue._launch(queue.tasks[tid])

    task = queue.tasks[tid]
    assert task.status == "active"
    assert task.error is None
    assert task.gid == "gid-meta"
    assert rpc.magnets[0]["uri"] == MAGNET
    # Never handed to the plain HTTP add path.
    assert rpc.added == []


def test_no_provider_configured_goes_straight_to_local(queue_env):
    queue, rpc, db_path = queue_env(
        torrent_support_enabled=True,
        all_debrid_enabled=False,
        torrent_ip_disclosure_shown=True,
    )
    _sync_spawn(queue)
    tid = queue.add_url(MAGNET)

    _running(queue)
    queue._launch(queue.tasks[tid])

    assert queue.tasks[tid].status == "active"
    assert len(rpc.magnets) == 1
    assert rpc.added == []


def test_provider_auth_failure_uses_the_task_failure_path(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(**_torrent_settings())
    _sync_spawn(queue)

    def boom(*a, **k):
        raise DebridError(ALL_DEBRID, 8, "the API key was rejected.", False, False)

    monkeypatch.setattr(debrid, "resolve_torrent", boom)
    tid = queue.add_url(MAGNET)

    queue._launch(queue.tasks[tid])

    task = queue.tasks[tid]
    assert task.status == "error"
    assert "API key was rejected" in task.error
    assert rpc.magnets == []


# --- materialisation -------------------------------------------------------


def test_cached_multi_file_torrent_becomes_https_tasks(queue_env, monkeypatch, tmp_path):
    queue, rpc, db_path = queue_env(**_torrent_settings())
    _sync_spawn(queue)
    monkeypatch.setattr(debrid, "resolve_torrent", lambda *a, **k: _cached())
    tid = queue.add_url(MAGNET)

    queue._launch(queue.tasks[tid])

    rows = _rows(db_path)
    assert len(rows) == 2
    assert [r["filename"] for r in rows] == ["ep1.mkv", "ep2.mkv"]
    assert [r["out_dir"] for r in rows] == [
        str(tmp_path / "Season 1"),
        str(tmp_path / "Season 1" / "extras"),
    ]
    assert [r["url"] for r in rows] == [AD_LOCKED_1, AD_LOCKED_2]
    assert [r["total_bytes"] for r in rows] == [10, 20]
    for row in rows:
        assert row["source_type"] == SOURCE_TORRENT_FILE
        assert row["info_hash"] == INFO_HASH
        assert row["torrent_name"] == "Season 1"
        assert row["debrid_route"] == ALL_DEBRID
        assert row["backend"] == "aria2"
        assert row["torrent_path"] == ""
        assert row["status"] == "queued"
    # The source row was reused as the first file, so no orphan remains.
    assert rows[0]["id"] == tid
    assert len(queue.tasks) == 2


def test_cached_single_file_torrent_writes_into_the_output_dir(queue_env, monkeypatch, tmp_path):
    queue, rpc, db_path = queue_env(**_torrent_settings())
    _sync_spawn(queue)
    single = _cached(
        files=[CachedTorrentFile(0, ("movie.mkv",), 9, AD_LOCKED_1)], name="movie.mkv"
    )
    monkeypatch.setattr(debrid, "resolve_torrent", lambda *a, **k: single)
    tid = queue.add_url(MAGNET)

    queue._launch(queue.tasks[tid])

    row = _persisted_row(db_path, tid)
    assert row["out_dir"] == str(tmp_path)
    assert row["filename"] == "movie.mkv"
    assert row["total_bytes"] == 9


def test_packed_cached_torrent_becomes_one_flat_row(queue_env, monkeypatch, tmp_path):
    """Real-Debrid can pack a multi-file torrent into one file. The queue has
    to treat that honestly: one row, no torrent wrapper folder."""
    queue, rpc, db_path = queue_env(**_torrent_settings(
        real_debrid_enabled=True, real_debrid_api_token="rd-token-value",
    ))
    _sync_spawn(queue)
    packed = _cached(
        files=[CachedTorrentFile(0, ("Season 1.rar",), 4096, RD_LOCKED_1)],
        provider=REAL_DEBRID,
    )
    monkeypatch.setattr(debrid, "resolve_torrent", lambda *a, **k: packed)
    tid = queue.add_url(MAGNET)

    queue._launch(queue.tasks[tid])

    rows = _rows(db_path)
    assert len(rows) == 1
    assert rows[0]["filename"] == "Season 1.rar"
    assert rows[0]["out_dir"] == str(tmp_path)
    assert rows[0]["url"] == RD_LOCKED_1
    assert rows[0]["total_bytes"] == 4096
    assert rows[0]["debrid_route"] == REAL_DEBRID
    assert RD_PACKED_DELIVERY not in repr(rows[0])


def test_provider_filesize_seeds_the_task(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(**_torrent_settings())
    _sync_spawn(queue)
    monkeypatch.setattr(debrid, "resolve_torrent", lambda *a, **k: _cached())
    tid = queue.add_url(MAGNET)
    queue._launch(queue.tasks[tid])
    assert queue.tasks[tid].total_bytes == 10


def test_repeated_materialisation_does_not_duplicate_rows(queue_env, monkeypatch, tmp_path):
    queue, rpc, db_path = queue_env(**_torrent_settings())
    _sync_spawn(queue)
    monkeypatch.setattr(debrid, "resolve_torrent", lambda *a, **k: _cached())
    tid = queue.add_url(MAGNET)
    queue._launch(queue.tasks[tid])
    assert len(_rows(db_path)) == 2

    # A crash-and-retry: a fresh source task for the same torrent probes
    # again and finds the files already expanded.
    second = queue.add_url(
        MAGNET, source_type=SOURCE_TORRENT, info_hash=INFO_HASH
    )
    removed = []
    queue.task_removed.connect(removed.append)
    queue._launch(queue.tasks[second])

    assert len(_rows(db_path)) == 2
    assert second not in queue.tasks
    assert removed == [second]


def test_materialised_rows_survive_a_restart(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(**_torrent_settings())
    _sync_spawn(queue)
    monkeypatch.setattr(debrid, "resolve_torrent", lambda *a, **k: _cached())
    tid = queue.add_url(MAGNET)
    queue._launch(queue.tasks[tid])

    restored = [_task_from_persisted_row(row) for row in _rows(db_path)]
    assert [t.source_type for t in restored] == [SOURCE_TORRENT_FILE] * 2
    assert [t.url for t in restored] == [AD_LOCKED_1, AD_LOCKED_2]
    assert [t.debrid_route for t in restored] == [ALL_DEBRID] * 2
    assert [t.info_hash for t in restored] == [INFO_HASH] * 2
    assert [t.torrent_name for t in restored] == ["Season 1"] * 2


def test_malicious_provider_path_is_refused(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(**_torrent_settings())
    _sync_spawn(queue)
    evil = CachedTorrent(
        ALL_DEBRID, INFO_HASH, "Season 1",
        (CachedTorrentFile(0, ("..", "..", "escape.bin"), 1, AD_LOCKED_1),),
    )
    monkeypatch.setattr(debrid, "resolve_torrent", lambda *a, **k: evil)
    tid = queue.add_url(MAGNET)

    queue._launch(queue.tasks[tid])

    task = queue.tasks[tid]
    assert task.status == "error"
    assert task.source_type == SOURCE_TORRENT
    assert len(_rows(db_path)) == 1
    assert rpc.added == []


# --- relaunching a materialised file ---------------------------------------


def _materialised(queue_env, monkeypatch, **settings):
    queue, rpc, db_path = queue_env(**_torrent_settings(**settings))
    _sync_spawn(queue)
    monkeypatch.setattr(debrid, "resolve_torrent", lambda *a, **k: _cached())
    tid = queue.add_url(MAGNET)
    queue._launch(queue.tasks[tid])
    return queue, rpc, db_path, tid


def test_torrent_file_relaunch_unlocks_the_stored_link(queue_env, monkeypatch):
    queue, rpc, db_path, tid = _materialised(queue_env, monkeypatch)
    seen = {}

    def fake_unlock(link, provider, settings, **kw):
        seen["link"] = link
        seen["provider"] = provider
        return Unrestricted(TORRENT_NODE_URL, "ep1.mkv", 10, provider)

    monkeypatch.setattr(debrid, "unlock_torrent_file", fake_unlock)

    task = queue.tasks[tid]
    queue._launch(task)

    assert seen == {"link": AD_LOCKED_1, "provider": ALL_DEBRID}
    # The node URL is what aria2 fetches...
    assert rpc.added[0]["uris"] == [TORRENT_NODE_URL]
    assert rpc.added[0]["filename"] == "ep1.mkv"
    # ...and the account-bound link is what stays on disk.
    assert task.url == AD_LOCKED_1
    assert _persisted_url(db_path, tid) == AD_LOCKED_1
    assert "SECRETNODE" not in _persisted_row_text(db_path, tid)


def test_torrent_file_relaunch_bypasses_the_share_link_guard(queue_env, monkeypatch):
    """The stored URL is an alldebrid.com/f/... link by construction."""
    queue, rpc, db_path, tid = _materialised(queue_env, monkeypatch)
    assert debrid.share_link_reason(AD_LOCKED_1) != ""
    monkeypatch.setattr(
        debrid, "unlock_torrent_file",
        lambda link, provider, settings, **kw: Unrestricted(
            TORRENT_NODE_URL, "ep1.mkv", 10, provider
        ),
    )

    queue._launch(queue.tasks[tid])

    assert queue.tasks[tid].status != "error"
    assert rpc.added[0]["uris"] == [TORRENT_NODE_URL]


def test_pasted_provider_share_link_is_still_rejected(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(**_torrent_settings())
    _sync_spawn(queue)
    tid = queue.add_url(AD_LOCKED_1)

    queue._launch(queue.tasks[tid])

    task = queue.tasks[tid]
    assert task.status == "error"
    assert "share links are tied to the account" in task.error
    assert rpc.added == []


def test_provider_domain_exclusion_bypass_is_limited_to_internal_rows(queue_env, monkeypatch):
    """resolve() refuses provider-owned domains; the torrent route does not
    go through resolve() at all, and a pasted one still does."""
    queue, rpc, db_path, tid = _materialised(queue_env, monkeypatch)
    monkeypatch.setattr(
        debrid, "resolve",
        lambda *a, **k: pytest.fail("torrent_file rows must not use resolve()"),
    )
    monkeypatch.setattr(
        debrid, "unlock_torrent_file",
        lambda link, provider, settings, **kw: Unrestricted(
            TORRENT_NODE_URL, "ep1.mkv", 10, provider
        ),
    )
    queue._probe_and_add(queue.tasks[tid])
    assert rpc.added[0]["uris"] == [TORRENT_NODE_URL]

    # An ordinary task on a provider-owned domain is left alone by resolve.
    plain = queue.add_url("https://s1.debrid.it/dl/abc/file.zip")
    assert queue.tasks[plain].source_type == ""


def test_unlock_failure_fails_the_task_without_leaking(queue_env, monkeypatch):
    queue, rpc, db_path, tid = _materialised(queue_env, monkeypatch)

    def boom(*a, **k):
        raise DebridError(ALL_DEBRID, 8, "the API key was rejected.", False, False)

    monkeypatch.setattr(debrid, "unlock_torrent_file", boom)
    queue._launch(queue.tasks[tid])

    task = queue.tasks[tid]
    assert task.status == "error"
    assert AD_LOCKED_1 not in task.error
    assert rpc.added == []


# --- regressions -----------------------------------------------------------


def test_ordinary_http_task_is_untouched_by_the_torrent_route(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(**_torrent_settings())
    _sync_spawn(queue)
    monkeypatch.setattr(
        debrid, "resolve",
        lambda url, settings, **kw: Unrestricted(NODE_URL, "movie.mkv", 4096, ALL_DEBRID),
    )
    tid = queue.add_url(ORIGINAL_URL)
    task = queue.tasks[tid]
    assert task.source_type == ""

    queue._probe_and_add(task)

    assert rpc.added[0]["uris"] == [NODE_URL]
    assert _persisted_url(db_path, tid) == ORIGINAL_URL


def test_add_url_refuses_an_unapproved_source_type(queue_env):
    queue, rpc, db_path = queue_env(**_torrent_settings())
    assert queue.add_url("https://example.com/f.zip", source_type="anything") is None
    assert _rows(db_path) == []


def test_add_torrent_file_is_inert_while_the_flag_is_off(queue_env, tmp_path):
    queue, rpc, db_path = queue_env(**_debrid_settings())
    spawned = _sync_spawn(queue)
    queue.add_torrent_file(str(tmp_path / "x.torrent"))
    assert spawned == []
    assert _rows(db_path) == []


def test_add_torrent_file_creates_a_source_task(queue_env, tmp_path):
    queue, rpc, db_path = queue_env(**_torrent_settings())
    _sync_spawn(queue)
    raw = (
        b"d4:infod6:lengthi7e4:name9:movie.mkv12:piece lengthi16384e"
        b"6:pieces20:" + b"\x01" * 20 + b"ee"
    )
    path = tmp_path / "s.torrent"
    path.write_bytes(raw)
    expected = torrent_mod.parse_torrent(raw).info_hash

    queue.add_torrent_file(str(path), str(tmp_path))

    row = _rows(db_path)[0]
    assert row["source_type"] == SOURCE_TORRENT
    assert row["info_hash"] == expected
    assert row["torrent_name"] == "movie.mkv"
    # Slice B: the row points at Cove's own copy, not at the file the user
    # picked, which they are free to move or delete afterwards.
    assert row["torrent_path"] == torrent_mod.managed_torrent_path(expected)
    assert open(row["torrent_path"], "rb").read() == raw
    # The persisted URL is the minimal magnet: no trackers, no passkey.
    assert row["url"] == f"magnet:?xt=urn:btih:{expected}"


# --- Codex review follow-ups -----------------------------------------------


def test_a_replaced_torrent_file_cannot_change_the_task_identity(
    queue_env, monkeypatch, tmp_path
):
    """The persisted info hash is the row's durable identity: swapping the
    file on disk after queueing must not redirect the task."""
    queue, rpc, db_path = queue_env(**_torrent_settings())
    other = (
        b"d4:infod6:lengthi9e4:name9:other.mkv12:piece lengthi16384e"
        b"6:pieces20:" + b"\x09" * 20 + b"ee"
    )
    path = tmp_path / "s.torrent"
    path.write_bytes(other)
    other_hash = torrent_mod.parse_torrent(other).info_hash
    assert other_hash != INFO_HASH

    seen = {}

    def fake_resolve(info_hash, settings, *, torrent_bytes=None, **kw):
        seen["hash"] = info_hash
        seen["bytes"] = torrent_bytes
        return None

    monkeypatch.setattr(debrid, "resolve_torrent", fake_resolve)
    tid = queue.add_url(
        torrent_mod.minimal_magnet(INFO_HASH),
        source_type=SOURCE_TORRENT,
        info_hash=INFO_HASH,
        torrent_path=str(path),
    )
    queue._probe_torrent(queue.tasks[tid])

    assert seen["hash"] == INFO_HASH
    # The mismatched file's bytes are not uploaded either.
    assert seen["bytes"] is None


def test_containment_check_resolves_symlinks(queue_env, monkeypatch, tmp_path):
    queue, rpc, db_path = queue_env(**_torrent_settings())
    _sync_spawn(queue)
    outside = tmp_path.parent / "outside-cove"
    outside.mkdir(exist_ok=True)
    root = tmp_path / "Season 1"
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)

    escaping = CachedTorrent(
        ALL_DEBRID, INFO_HASH, "Season 1",
        (CachedTorrentFile(0, ("link", "file.bin"), 1, AD_LOCKED_1),),
    )
    monkeypatch.setattr(debrid, "resolve_torrent", lambda *a, **k: escaping)
    tid = queue.add_url(MAGNET)

    queue._launch(queue.tasks[tid])

    task = queue.tasks[tid]
    assert task.status == "error"
    assert task.source_type == SOURCE_TORRENT
    assert len(_rows(db_path)) == 1
    assert rpc.added == []


def test_containment_check_accepts_ordinary_nesting(tmp_path):
    from cove.queue import _within

    assert _within(str(tmp_path), str(tmp_path))
    assert _within(str(tmp_path), str(tmp_path / "a" / "b"))
    assert not _within(str(tmp_path), str(tmp_path.parent))
    assert not _within(str(tmp_path), str(tmp_path.parent / "sibling"))


def test_only_the_info_dictionary_is_sent_to_providers(queue_env, monkeypatch, tmp_path):
    """A .torrent's announce URLs (and any private-tracker passkey in them)
    must not reach a debrid provider."""
    queue, rpc, db_path = queue_env(**_torrent_settings())
    announce = b"http://tracker.example/announce?passkey=SECRETPASS"
    raw = (
        b"d8:announce%d:%s" % (len(announce), announce)
        + b"4:infod6:lengthi7e4:name9:movie.mkv12:piece lengthi16384e"
        b"6:pieces20:" + b"\x01" * 20 + b"ee"
    )
    path = tmp_path / "s.torrent"
    path.write_bytes(raw)
    expected = torrent_mod.parse_torrent(raw).info_hash

    seen = {}

    def fake_resolve(info_hash, settings, *, torrent_bytes=None, **kw):
        seen["hash"] = info_hash
        seen["bytes"] = torrent_bytes
        return None

    monkeypatch.setattr(debrid, "resolve_torrent", fake_resolve)
    tid = queue.add_url(
        torrent_mod.minimal_magnet(expected),
        source_type=SOURCE_TORRENT,
        info_hash=expected,
        torrent_path=str(path),
    )
    queue._probe_torrent(queue.tasks[tid])

    assert seen["hash"] == expected
    assert seen["bytes"] != raw
    assert b"SECRETPASS" not in seen["bytes"]
    assert b"tracker.example" not in seen["bytes"]
    # ...and it is still the same torrent.
    assert torrent_mod.parse_torrent(seen["bytes"]).info_hash == expected


def test_a_completed_torrent_can_be_downloaded_again(queue_env, monkeypatch):
    """Idempotence must stop a duplicate in-flight expansion, not turn
    finished history into a permanent block."""
    queue, rpc, db_path = queue_env(**_torrent_settings())
    _sync_spawn(queue)
    monkeypatch.setattr(debrid, "resolve_torrent", lambda *a, **k: _cached())
    first = queue.add_url(MAGNET)
    queue._launch(queue.tasks[first])
    assert len(_rows(db_path)) == 2

    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE downloads SET status='completed'")
    conn.commit()
    conn.close()
    for task in list(queue.tasks.values()):
        task.status = "completed"

    second = queue.add_url(MAGNET)
    assert second is not None
    queue._launch(queue.tasks[second])

    rows = _rows(db_path)
    assert len(rows) == 4
    fresh = [r for r in rows if r["status"] == "queued"]
    assert len(fresh) == 2
    assert [r["filename"] for r in fresh] == ["ep1.mkv", "ep2.mkv"]


# ---------------------------------------------------------------------------
# Slice B: local BitTorrent fallback
# ---------------------------------------------------------------------------


def _local_settings(**extra):
    """Torrent support on, with the one-time P2P consent already given."""
    base = _torrent_settings(torrent_ip_disclosure_shown=True)
    base.update(extra)
    return base


def _uncached(queue, monkeypatch):
    monkeypatch.setattr(debrid, "resolve_torrent", lambda *a, **k: None)


def _running(queue):
    """The fixture parks the scheduler; a launch under test must not be
    auto-paused the moment its gid lands."""
    queue._running = True
    queue._scheduler_allows = True
    return queue


def _torrent_bytes(name=b"movie.mkv"):
    return (
        b"d4:infod6:lengthi7e4:name" + str(len(name)).encode() + b":" + name
        + b"12:piece lengthi16384e6:pieces20:" + b"\x01" * 20 + b"ee"
    )


def _local_torrent_file(queue_env, monkeypatch, tmp_path, **settings):
    """Queue a local .torrent through the managed-copy path."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    queue, rpc, db_path = queue_env(**_local_settings(**settings))
    calls = _sync_spawn(queue)
    _uncached(queue, monkeypatch)
    raw = _torrent_bytes()
    src = tmp_path / "picked.torrent"
    src.write_bytes(raw)
    queue.add_torrent_file(str(src), str(tmp_path))
    tid = next(iter(queue.tasks))
    _running(queue)
    return queue, rpc, db_path, tid, raw, calls


# --- policy gates ----------------------------------------------------------


def test_fallback_mode_never_refuses_without_touching_the_swarm(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(**_local_settings(torrent_fallback_mode="never"))
    _sync_spawn(queue)
    _uncached(queue, monkeypatch)
    tid = queue.add_url(MAGNET)

    queue._launch(queue.tasks[tid])

    task = queue.tasks[tid]
    assert task.status == "error"
    assert task.error == TORRENT_CANCELLED_UNCACHED
    assert rpc.magnets == [] and rpc.torrents == [] and rpc.added == []


def test_configured_proxy_blocks_local_bittorrent(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(
        **_local_settings(proxy_type="socks5", proxy_host="127.0.0.1", proxy_port=1080)
    )
    _sync_spawn(queue)
    _uncached(queue, monkeypatch)
    tid = queue.add_url(MAGNET)

    queue._launch(queue.tasks[tid])

    assert queue.tasks[tid].error == TORRENT_PROXY_BLOCKED
    assert rpc.magnets == []


def test_proxy_override_permits_local_bittorrent(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(
        **_local_settings(
            proxy_type="socks5", proxy_host="127.0.0.1", proxy_port=1080,
            torrent_allow_with_proxy=True,
        )
    )
    _sync_spawn(queue)
    _uncached(queue, monkeypatch)
    tid = queue.add_url(MAGNET)

    _running(queue)
    queue._launch(queue.tasks[tid])

    assert queue.tasks[tid].status == "active"
    assert len(rpc.magnets) == 1


def test_cached_debrid_route_still_works_while_the_proxy_blocks_local(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(
        **_local_settings(proxy_type="http", proxy_host="proxy.example", proxy_port=8080)
    )
    _sync_spawn(queue)
    monkeypatch.setattr(debrid, "resolve_torrent", lambda *a, **k: _cached())
    tid = queue.add_url(MAGNET)

    queue._launch(queue.tasks[tid])

    rows = _rows(db_path)
    assert [r["source_type"] for r in rows] == [SOURCE_TORRENT_FILE] * 2
    assert rpc.magnets == []


# --- privacy disclosure ----------------------------------------------------


def test_first_local_torrent_asks_for_consent_before_any_connection(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(**_torrent_settings())
    _sync_spawn(queue)
    _uncached(queue, monkeypatch)
    asked = []
    queue.torrent_consent_needed.connect(asked.append)
    tid = queue.add_url(MAGNET)

    queue._launch(queue.tasks[tid])

    assert asked == [tid]
    assert rpc.magnets == [] and rpc.torrents == []
    assert queue.settings.torrent_ip_disclosure_shown is False


def test_declining_consent_fails_the_task_without_downloading(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(**_torrent_settings())
    _sync_spawn(queue)
    _uncached(queue, monkeypatch)
    tid = queue.add_url(MAGNET)
    queue._launch(queue.tasks[tid])

    queue.torrent_consent(tid, False)

    assert queue.tasks[tid].status == "error"
    assert queue.tasks[tid].error == TORRENT_CONSENT_DECLINED
    assert rpc.magnets == []
    assert queue.settings.torrent_ip_disclosure_shown is False


def test_accepting_consent_saves_it_before_the_torrent_starts(queue_env, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    queue, rpc, db_path = queue_env(**_torrent_settings())
    _sync_spawn(queue)
    _uncached(queue, monkeypatch)
    _running(queue)
    tid = queue.add_url(MAGNET)
    queue._launch(queue.tasks[tid])

    order = []
    real_save = queue.settings.save
    monkeypatch.setattr(
        queue.settings, "save",
        lambda: (order.append("save"), real_save())[1],
    )
    original_add = rpc.add_magnet
    rpc.add_magnet = lambda *a, **k: (order.append("add"), original_add(*a, **k))[1]

    queue.torrent_consent(tid, True, remember=True)

    assert queue.settings.torrent_ip_disclosure_shown is True
    assert order == ["save", "add"]
    assert queue.tasks[tid].gid == "gid-meta"


def test_accepting_without_remember_does_not_persist_consent(queue_env, monkeypatch):
    """The notice's checkbox is the only thing that silences it later."""
    queue, rpc, db_path = queue_env(**_torrent_settings())
    _sync_spawn(queue)
    _uncached(queue, monkeypatch)
    _running(queue)
    tid = queue.add_url(MAGNET)
    queue._launch(queue.tasks[tid])

    queue.torrent_consent(tid, True)

    assert queue.settings.torrent_ip_disclosure_shown is False
    assert len(rpc.magnets) == 1


def test_reevaluating_after_settings_re_asks_without_starting_anything(
    queue_env, monkeypatch
):
    """Open Settings parks the task; nothing reaches aria2 in the meantime."""
    queue, rpc, db_path = queue_env(**_torrent_settings())
    _sync_spawn(queue)
    _uncached(queue, monkeypatch)
    _running(queue)
    asked = []
    queue.torrent_consent_needed.connect(asked.append)
    tid = queue.add_url(MAGNET)
    assert asked == [tid]

    queue.torrent_consent_reevaluate(tid)

    assert rpc.magnets == []
    assert asked == [tid, tid]


def test_reevaluating_honours_cancel_chosen_in_settings(queue_env, monkeypatch):
    """Switching to "Cancel the download" while parked fails the task."""
    queue, rpc, db_path = queue_env(**_torrent_settings())
    _sync_spawn(queue)
    _uncached(queue, monkeypatch)
    _running(queue)
    tid = queue.add_url(MAGNET)
    queue._launch(queue.tasks[tid])
    assert tid in queue._awaiting_consent

    queue.settings.torrent_fallback_mode = "never"
    queue.torrent_consent_reevaluate(tid)

    assert queue.tasks[tid].status == "error"
    assert queue.tasks[tid].error == TORRENT_CANCELLED_UNCACHED
    assert rpc.magnets == []
    assert queue.settings.torrent_ip_disclosure_shown is False


def test_consent_is_asked_once(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(**_local_settings())
    _sync_spawn(queue)
    _uncached(queue, monkeypatch)
    asked = []
    queue.torrent_consent_needed.connect(asked.append)
    tid = queue.add_url(MAGNET)
    _running(queue)

    queue._launch(queue.tasks[tid])

    assert asked == []
    assert len(rpc.magnets) == 1


def test_cached_torrent_never_asks_for_p2p_consent(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(**_torrent_settings())
    _sync_spawn(queue)
    monkeypatch.setattr(debrid, "resolve_torrent", lambda *a, **k: _cached())
    asked = []
    queue.torrent_consent_needed.connect(asked.append)
    tid = queue.add_url(MAGNET)

    queue._launch(queue.tasks[tid])

    assert asked == []
    assert queue.settings.torrent_ip_disclosure_shown is False


# --- BitTorrent capability -------------------------------------------------


def test_missing_bittorrent_support_fails_with_an_actionable_reason(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(**_local_settings())
    _sync_spawn(queue)
    _uncached(queue, monkeypatch)
    rpc.version = {"version": "1.37.0", "enabledFeatures": ["HTTPS"]}
    tid = queue.add_url(MAGNET)

    queue._launch(queue.tasks[tid])

    assert queue.tasks[tid].error == TORRENT_NO_BITTORRENT
    assert rpc.magnets == []


def test_malformed_capability_response_is_treated_as_missing(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(**_local_settings())
    _sync_spawn(queue)
    _uncached(queue, monkeypatch)
    rpc.version = {"enabledFeatures": "BitTorrent"}
    tid = queue.add_url(MAGNET)

    queue._launch(queue.tasks[tid])

    assert queue.tasks[tid].error == TORRENT_NO_BITTORRENT


def test_capability_rpc_failure_fails_the_task(queue_env, monkeypatch):
    from cove.aria2 import Aria2Error

    queue, rpc, db_path = queue_env(**_local_settings())
    _sync_spawn(queue)
    _uncached(queue, monkeypatch)

    def boom():
        raise Aria2Error("RPC transport error")

    rpc.get_version = boom
    tid = queue.add_url(MAGNET)

    queue._launch(queue.tasks[tid])

    assert queue.tasks[tid].error == TORRENT_NO_BITTORRENT
    assert rpc.magnets == []


def test_capability_is_checked_once_per_daemon(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(**_local_settings())
    _sync_spawn(queue)
    _uncached(queue, monkeypatch)
    first = queue.add_url(MAGNET)
    second = queue.add_url(f"magnet:?xt=urn:btih:{'b' * 40}")
    _running(queue)
    queue._launch(queue.tasks[first])
    queue._launch(queue.tasks[second])

    assert len(rpc.version_calls) == 1
    assert len(rpc.magnets) == 2


# --- magnet metadata lifecycle --------------------------------------------


def _start_local_magnet(queue_env, monkeypatch, **settings):
    queue, rpc, db_path = queue_env(**_local_settings(**settings))
    _sync_spawn(queue)
    _uncached(queue, monkeypatch)
    tid = queue.add_url(MAGNET)
    _running(queue)
    queue._launch(queue.tasks[tid])
    return queue, rpc, db_path, tid


def test_magnet_shows_a_metadata_phase_before_the_torrent_starts(queue_env, monkeypatch):
    queue, rpc, db_path, tid = _start_local_magnet(queue_env, monkeypatch)
    task = queue.tasks[tid]

    assert task.phase == "metadata"
    queue._apply_status(tid, {"status": "active", "totalLength": "0",
                              "completedLength": "0", "downloadSpeed": "0"})
    assert task.status == "active"
    assert task.phase == "metadata"


def test_metadata_completion_swaps_to_the_child_gid(queue_env, monkeypatch):
    queue, rpc, db_path, tid = _start_local_magnet(queue_env, monkeypatch)

    queue._apply_status(tid, {
        "status": "complete", "totalLength": "0", "completedLength": "0",
        "downloadSpeed": "0", "followedBy": ["gid-child"],
    })

    task = queue.tasks[tid]
    assert task.status == "active"
    assert task.gid == "gid-child"
    assert task.phase == ""
    assert task.finished_at is None
    assert _persisted_row(db_path, tid)["gid"] == "gid-child"
    assert "gid-child" in queue._seen_gids


def test_child_gid_completion_completes_the_task(queue_env, monkeypatch):
    queue, rpc, db_path, tid = _start_local_magnet(queue_env, monkeypatch)
    queue._apply_status(tid, {"status": "complete", "followedBy": ["gid-child"]})

    queue._apply_status(tid, {
        "status": "complete", "totalLength": "100", "completedLength": "100",
        "downloadSpeed": "0",
    })

    assert queue.tasks[tid].status == "completed"
    assert queue.tasks[tid].total_bytes == 100


def test_empty_followed_by_at_metadata_completion_is_an_error(queue_env, monkeypatch):
    queue, rpc, db_path, tid = _start_local_magnet(queue_env, monkeypatch)

    queue._apply_status(tid, {"status": "complete", "followedBy": [],
                              "totalLength": "0", "completedLength": "0"})

    task = queue.tasks[tid]
    assert task.status == "error"
    assert task.error == TORRENT_METADATA_FAILED


def test_malformed_followed_by_is_an_error_not_a_crash(queue_env, monkeypatch):
    for bad in ("gid-child", [None], [""], 7, {}):
        queue, rpc, db_path, tid = _start_local_magnet(queue_env, monkeypatch)
        queue._apply_status(tid, {"status": "complete", "followedBy": bad})
        assert queue.tasks[tid].status == "error"
        assert queue.tasks[tid].error == TORRENT_METADATA_FAILED


def test_pause_and_resume_target_the_child_gid(queue_env, monkeypatch):
    queue, rpc, db_path, tid = _start_local_magnet(queue_env, monkeypatch)
    queue._apply_status(tid, {"status": "complete", "followedBy": ["gid-child"]})

    queue.pause(tid)
    assert rpc.paused == ["gid-child"]
    queue.resume(tid)
    assert rpc.unpaused == ["gid-child"]
    assert "gid-meta" not in rpc.unpaused


def test_torrent_name_becomes_the_display_name(queue_env, monkeypatch):
    queue, rpc, db_path, tid = _start_local_magnet(queue_env, monkeypatch)
    queue._apply_status(tid, {"status": "complete", "followedBy": ["gid-child"]})

    queue._apply_status(tid, {
        "status": "active", "totalLength": "300", "completedLength": "10",
        "downloadSpeed": "5", "infoHash": INFO_HASH,
        "bittorrent": {"info": {"name": "Season 1"}},
        "files": [{"path": "/dl/Season 1/ep1.mkv"}],
    })

    task = queue.tasks[tid]
    # Never the first inner file: this row is the whole torrent.
    assert task.filename == "Season 1"
    assert task.torrent_name == "Season 1"
    assert task.total_bytes == 300


def test_aggregate_progress_survives_a_zero_total(queue_env, monkeypatch):
    queue, rpc, db_path, tid = _start_local_magnet(queue_env, monkeypatch)
    queue._apply_status(tid, {"status": "complete", "followedBy": ["gid-child"]})
    queue._apply_status(tid, {"status": "active", "totalLength": "500",
                              "completedLength": "100", "downloadSpeed": "1"})
    queue._apply_status(tid, {"status": "active", "totalLength": "0",
                              "completedLength": "120", "downloadSpeed": "1"})

    assert queue.tasks[tid].total_bytes == 500
    assert queue.tasks[tid].completed_bytes == 120


def test_hash_checking_is_not_reported_as_complete(queue_env, monkeypatch):
    queue, rpc, db_path, tid = _start_local_magnet(queue_env, monkeypatch)
    queue._apply_status(tid, {"status": "complete", "followedBy": ["gid-child"]})

    queue._apply_status(tid, {"status": "active", "totalLength": "500",
                              "completedLength": "500", "downloadSpeed": "0",
                              "verifiedLength": "120"})

    assert queue.tasks[tid].status == "active"


def test_completed_local_torrent_is_not_left_seeding(queue_env, monkeypatch):
    queue, rpc, db_path, tid = _start_local_magnet(queue_env, monkeypatch)
    queue._apply_status(tid, {"status": "complete", "followedBy": ["gid-child"]})
    queue._apply_status(tid, {"status": "complete", "totalLength": "500",
                              "completedLength": "500", "downloadSpeed": "0"})

    assert queue.tasks[tid].status == "completed"
    assert _persisted_row(db_path, tid)["status"] == "completed"


# --- external adoption guard ----------------------------------------------


def test_torrent_child_gid_is_never_adopted_as_an_external_download(queue_env, monkeypatch):
    queue, rpc, db_path, tid = _start_local_magnet(queue_env, monkeypatch)
    before = len(queue.tasks)

    rpc.tell_external_snapshot = lambda: [
        {"gid": "gid-child", "status": "active", "following": "gid-meta",
         "infoHash": INFO_HASH, "totalLength": "500", "completedLength": "1",
         "files": [{"path": "/dl/Season 1/ep1.mkv", "uris": []}]},
    ]
    queue._check_external()

    assert len(queue.tasks) == before
    assert len(_rows(db_path)) == 1


def test_cove_metadata_gid_is_not_adopted_while_its_callback_is_in_flight(
    queue_env, monkeypatch
):
    """Characterisation (green before this slice, and it must stay green).

    There is a real ownership window: aria2 has answered `add_magnet` on a
    worker, but `_on_local_torrent_gid` has not yet run on the GUI thread,
    so the gid is in aria2's snapshot before it is in `_seen_gids`. What
    closes it is not ownership but identification - aria2 reports a
    magnet's metadata download with the info hash and bittorrent block
    from the moment the group exists, and `_check_external` skips any job
    carrying those. This test pins that guard to the window, because
    adopting there would produce exactly the reported ghost row:
    `[METADATA]<hash>`, complete, 100%.
    """
    queue, rpc, db_path = queue_env(**_local_settings())
    _uncached(queue, monkeypatch)
    _running(queue)
    deferred = []

    def spawn(fn, *args, on_done=None, on_fail=None, **kwargs):
        try:
            result = fn(*args, **kwargs)
        except (Aria2Error, DebridError, TorrentError) as exc:
            if on_fail is not None:
                on_fail(str(exc))
            return
        if getattr(fn, "__name__", "") == "_add_local_magnet":
            # aria2 owns the metadata job and has returned its gid; Cove's
            # callback is still queued. Hold it to open the window.
            deferred.append((on_done, result))
            return
        if on_done is not None:
            on_done(result)

    queue._spawn = spawn
    tid = queue.add_url(MAGNET)
    queue._launch(queue.tasks[tid])
    assert deferred, "the magnet add must still be in flight"
    assert not queue.tasks[tid].gid

    # aria2's own report of a magnet's metadata download: it carries the
    # info hash and the bittorrent block from the moment the group exists,
    # and its single file is the metadata placeholder.
    rpc.tell_external_snapshot = lambda: [
        {"gid": "gid-meta", "status": "complete", "infoHash": INFO_HASH,
         "bittorrent": {"announceList": [["http://tracker.example/announce"]]},
         "totalLength": "31000", "completedLength": "31000",
         "downloadSpeed": "0",
         "files": [{"path": f"/dl/[METADATA]{INFO_HASH}", "uris": []}]},
    ]
    queue._check_external()

    assert len(queue.tasks) == 1, "Cove's own metadata gid was adopted as a download"
    assert len(_rows(db_path)) == 1
    assert queue.tasks[tid].status != "completed"

    # The callback lands afterwards and still claims the gid exactly once.
    on_done, gid = deferred.pop()
    on_done(gid)
    assert queue.tasks[tid].gid == "gid-meta"
    assert len(queue.tasks) == 1
    assert len(_rows(db_path)) == 1


def test_external_torrent_jobs_are_not_adopted_as_one_file_tasks(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(**_local_settings())
    _sync_spawn(queue)
    rpc.tell_external_snapshot = lambda: [
        {"gid": "gid-foreign", "status": "active", "infoHash": "f" * 40,
         "bittorrent": {"info": {"name": "Someone else"}},
         "totalLength": "9", "completedLength": "1",
         "files": [{"path": "/dl/x/a.bin", "uris": []},
                   {"path": "/dl/x/b.bin", "uris": []}]},
    ]

    queue._check_external()

    assert queue.tasks == {}
    assert _rows(db_path) == []


def test_plain_external_http_download_is_still_adopted(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(**_local_settings())
    _sync_spawn(queue)
    rpc.tell_external_snapshot = lambda: [
        {"gid": "gid-ext", "status": "active", "totalLength": "9",
         "completedLength": "1", "downloadSpeed": "2",
         "files": [{"path": "/dl/a.bin",
                    "uris": [{"uri": "https://example.com/a.bin"}]}]},
    ]

    queue._check_external()

    assert len(queue.tasks) == 1
    adopted = next(iter(queue.tasks.values()))
    assert adopted.gid == "gid-ext"
    assert adopted.url == "https://example.com/a.bin"


# --- local .torrent files --------------------------------------------------


def test_local_torrent_file_is_copied_into_coves_store(queue_env, monkeypatch, tmp_path):
    queue, rpc, db_path, tid, raw, _calls = _local_torrent_file(
        queue_env, monkeypatch, tmp_path
    )
    task = queue.tasks[tid]

    assert task.torrent_path == torrent_mod.managed_torrent_path(task.info_hash)
    assert torrent_mod.is_managed_torrent_path(task.torrent_path)
    assert open(task.torrent_path, "rb").read() == raw
    assert _persisted_row(db_path, tid)["torrent_path"] == task.torrent_path


def test_local_torrent_file_survives_the_original_being_deleted(queue_env, monkeypatch, tmp_path):
    queue, rpc, db_path, tid, raw, _calls = _local_torrent_file(
        queue_env, monkeypatch, tmp_path
    )
    (tmp_path / "picked.torrent").unlink()

    queue._launch(queue.tasks[tid])

    assert rpc.torrents[0]["data"] == raw
    assert rpc.torrents[0]["out_dir"] == str(tmp_path)
    assert queue.tasks[tid].gid == "gid-file"
    # A .torrent has its metadata already; there is no metadata phase.
    assert queue.tasks[tid].phase == ""


def test_missing_managed_torrent_fails_safely(queue_env, monkeypatch, tmp_path):
    queue, rpc, db_path, tid, _raw, _calls = _local_torrent_file(
        queue_env, monkeypatch, tmp_path
    )
    os.unlink(queue.tasks[tid].torrent_path)

    queue._launch(queue.tasks[tid])

    assert queue.tasks[tid].status == "error"
    assert queue.tasks[tid].error
    assert rpc.torrents == []


def test_corrupted_managed_torrent_fails_without_starting_a_download(queue_env, monkeypatch, tmp_path):
    queue, rpc, db_path, tid, _raw, _calls = _local_torrent_file(
        queue_env, monkeypatch, tmp_path
    )
    open(queue.tasks[tid].torrent_path, "wb").write(b"SECRETPASS junk")

    queue._launch(queue.tasks[tid])

    task = queue.tasks[tid]
    assert task.status == "error"
    assert "SECRETPASS" not in task.error
    assert rpc.torrents == []


def test_replaced_managed_torrent_is_refused(queue_env, monkeypatch, tmp_path):
    queue, rpc, db_path, tid, _raw, _calls = _local_torrent_file(
        queue_env, monkeypatch, tmp_path
    )
    open(queue.tasks[tid].torrent_path, "wb").write(_torrent_bytes(b"other.mkv"))

    queue._launch(queue.tasks[tid])

    assert queue.tasks[tid].status == "error"
    assert rpc.torrents == []


# --- restart ---------------------------------------------------------------


def _restart(queue_env, db_path, monkeypatch, **settings):
    """A second QueueManager over the same database, as startup does."""
    queue, rpc, _ = queue_env(**_local_settings(**settings))
    _sync_spawn(queue)
    _uncached(queue, monkeypatch)
    _running(queue)
    return queue, rpc


def test_restart_during_metadata_re_adds_the_magnet_once(queue_env, monkeypatch):
    queue, rpc, db_path, tid = _start_local_magnet(queue_env, monkeypatch)

    queue2, rpc2 = _restart(queue_env, db_path, monkeypatch)
    assert list(queue2.tasks) == [tid]
    restored = queue2.tasks[tid]
    assert restored.source_type == SOURCE_TORRENT
    assert restored.gid is None

    queue2._launch(restored)

    assert len(rpc2.magnets) == 1
    assert rpc2.magnets[0]["uri"] == MAGNET
    assert len(_rows(db_path)) == 1


def test_restart_after_the_gid_swap_does_not_duplicate_the_task(queue_env, monkeypatch):
    queue, rpc, db_path, tid = _start_local_magnet(queue_env, monkeypatch)
    queue._apply_status(tid, {"status": "complete", "followedBy": ["gid-child"]})

    queue2, rpc2 = _restart(queue_env, db_path, monkeypatch)
    queue2._launch(queue2.tasks[tid])

    assert len(_rows(db_path)) == 1
    assert len(rpc2.magnets) == 1


def test_restart_while_paused_restores_a_single_row(queue_env, monkeypatch):
    queue, rpc, db_path, tid = _start_local_magnet(queue_env, monkeypatch)
    queue._apply_status(tid, {"status": "complete", "followedBy": ["gid-child"]})
    queue.pause(tid)

    queue2, _rpc2 = _restart(queue_env, db_path, monkeypatch)
    assert list(queue2.tasks) == [tid]
    assert len(_rows(db_path)) == 1


def test_restart_of_a_local_torrent_file_re_adds_from_the_managed_copy(queue_env, monkeypatch, tmp_path):
    queue, rpc, db_path, tid, raw, _calls = _local_torrent_file(
        queue_env, monkeypatch, tmp_path
    )
    queue._launch(queue.tasks[tid])
    assert len(rpc.torrents) == 1

    queue2, rpc2 = _restart(queue_env, db_path, monkeypatch)
    restored = queue2.tasks[tid]
    assert restored.torrent_path == queue.tasks[tid].torrent_path

    queue2._launch(restored)
    assert rpc2.torrents[0]["data"] == raw
    assert len(_rows(db_path)) == 1


def test_a_stale_gid_is_not_reused_after_a_restart(queue_env, monkeypatch):
    queue, rpc, db_path, tid = _start_local_magnet(queue_env, monkeypatch)
    queue._apply_status(tid, {"status": "complete", "followedBy": ["gid-child"]})

    queue2, rpc2 = _restart(queue_env, db_path, monkeypatch)
    assert queue2.tasks[tid].gid is None
    queue2._launch(queue2.tasks[tid])
    assert queue2.tasks[tid].gid == "gid-meta"


# --- duplicates ------------------------------------------------------------


def test_duplicate_local_torrent_file_is_blocked(queue_env, monkeypatch, tmp_path):
    queue, rpc, db_path, tid, _raw, _calls = _local_torrent_file(
        queue_env, monkeypatch, tmp_path
    )
    errors = []
    queue.error.connect(errors.append)

    queue.add_torrent_file(str(tmp_path / "picked.torrent"), str(tmp_path))

    assert len(_rows(db_path)) == 1
    assert errors


def test_magnet_and_equivalent_torrent_file_are_one_torrent(queue_env, monkeypatch, tmp_path):
    queue, rpc, db_path, tid, _raw, _calls = _local_torrent_file(
        queue_env, monkeypatch, tmp_path
    )
    same = torrent_mod.minimal_magnet(queue.tasks[tid].info_hash)
    errors = []
    queue.error.connect(errors.append)

    assert queue.add_url(same) is None
    assert len(_rows(db_path)) == 1
    assert errors


def test_duplicate_is_still_blocked_after_a_restart(queue_env, monkeypatch):
    queue, rpc, db_path, tid = _start_local_magnet(queue_env, monkeypatch)

    queue2, _rpc2 = _restart(queue_env, db_path, monkeypatch)
    assert queue2.add_url(MAGNET) is None
    assert len(_rows(db_path)) == 1


def test_cached_child_rows_do_not_look_like_a_live_local_torrent(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(**_local_settings())
    _sync_spawn(queue)
    monkeypatch.setattr(debrid, "resolve_torrent", lambda *a, **k: _cached())
    tid = queue.add_url(MAGNET)
    queue._launch(queue.tasks[tid])

    # The expansion rows share the info hash; a *new* add is still blocked
    # (the torrent is live), but nothing about them is a local source task.
    assert all(t.source_type == SOURCE_TORRENT_FILE for t in queue.tasks.values())
    assert not any(t.phase for t in queue.tasks.values())


# --- removal ---------------------------------------------------------------


def _finished_local_torrent(queue_env, monkeypatch, tmp_path):
    queue, rpc, db_path, tid = _start_local_magnet(queue_env, monkeypatch)
    queue._apply_status(tid, {"status": "complete", "followedBy": ["gid-child"]})
    root = tmp_path / "Season 1"
    (root / "extras").mkdir(parents=True)
    (root / "ep1.mkv").write_bytes(b"a")
    (root / "ep1.mkv.aria2").write_bytes(b"c")
    (root / "extras" / "ep2.mkv").write_bytes(b"b")
    rpc.files_result = [
        {"path": str(root / "ep1.mkv")},
        {"path": str(root / "extras" / "ep2.mkv")},
    ]
    return queue, rpc, db_path, tid, root


def test_removing_a_torrent_keeps_the_downloaded_files(queue_env, monkeypatch, tmp_path):
    queue, rpc, db_path, tid, root = _finished_local_torrent(
        queue_env, monkeypatch, tmp_path
    )

    queue.remove(tid, delete_file=False)

    assert (root / "ep1.mkv").exists()
    assert (root / "extras" / "ep2.mkv").exists()
    assert rpc.removed == ["gid-child"]
    assert _rows(db_path) == []


def test_removing_a_torrent_with_files_deletes_only_aria2s_paths(queue_env, monkeypatch, tmp_path):
    queue, rpc, db_path, tid, root = _finished_local_torrent(
        queue_env, monkeypatch, tmp_path
    )
    bystander = root / "my notes.txt"
    bystander.write_text("mine")

    queue.remove(tid, delete_file=True)

    assert not (root / "ep1.mkv").exists()
    assert not (root / "ep1.mkv.aria2").exists()
    assert not (root / "extras").exists()      # emptied, so cleaned up
    assert bystander.exists()                  # unrelated file preserved
    assert root.exists()                       # not blindly rmtree'd


def test_torrent_file_deletion_refuses_paths_outside_the_destination(queue_env, monkeypatch, tmp_path):
    queue, rpc, db_path, tid, root = _finished_local_torrent(
        queue_env, monkeypatch, tmp_path
    )
    outside = tmp_path.parent / "outside.bin"
    outside.write_bytes(b"keep")
    rpc.files_result = [
        {"path": str(outside)},
        {"path": str(root / ".." / ".." / "escape.bin")},
        {"path": "/etc/passwd"},
    ]

    queue.remove(tid, delete_file=True)

    assert outside.exists()
    outside.unlink()


def test_torrent_file_deletion_refuses_a_symlink_escape(queue_env, monkeypatch, tmp_path):
    queue, rpc, db_path, tid, root = _finished_local_torrent(
        queue_env, monkeypatch, tmp_path
    )
    victim = tmp_path.parent / "victim.bin"
    victim.write_bytes(b"keep")
    link = root / "link.bin"
    os.symlink(victim, link)
    rpc.files_result = [{"path": str(link)}]

    queue.remove(tid, delete_file=True)

    assert victim.read_bytes() == b"keep"
    victim.unlink()


def test_removing_a_local_torrent_cleans_up_its_managed_copy(queue_env, monkeypatch, tmp_path):
    queue, rpc, db_path, tid, _raw, _calls = _local_torrent_file(
        queue_env, monkeypatch, tmp_path
    )
    managed = queue.tasks[tid].torrent_path
    queue._launch(queue.tasks[tid])

    queue.remove(tid, delete_file=False)

    assert not os.path.exists(managed)


def test_a_second_task_keeps_the_managed_copy_alive(queue_env, monkeypatch, tmp_path):
    queue, rpc, db_path, tid, _raw, _calls = _local_torrent_file(
        queue_env, monkeypatch, tmp_path
    )
    managed = queue.tasks[tid].torrent_path
    twin = queue.add_url(
        torrent_mod.minimal_magnet(queue.tasks[tid].info_hash),
        source_type=SOURCE_TORRENT,
        info_hash=queue.tasks[tid].info_hash,
        torrent_path=managed,
    )
    assert twin is not None

    queue.remove(tid, delete_file=False)

    assert os.path.exists(managed)


def test_removing_a_torrent_while_the_add_is_in_flight_stops_it(queue_env, monkeypatch):
    """The gid lands after the user removed the row; it must not survive."""
    queue, rpc, db_path = queue_env(**_local_settings())
    _uncached(queue, monkeypatch)
    pending = []

    def spawn(fn, *args, on_done=None, on_fail=None, **kwargs):
        # Bound methods compare by value, not identity, so match by name.
        if getattr(fn, "__name__", "") == "_add_local_magnet":
            pending.append((fn, args, on_done))
            return
        result = fn(*args, **kwargs)
        if on_done is not None:
            on_done(result)

    tid = queue.add_url(MAGNET)
    _running(queue)
    queue._spawn = spawn
    queue._launch(queue.tasks[tid])
    assert pending      # add_magnet is in flight

    queue.remove(tid, delete_file=False)
    fn, args, on_done = pending[0]
    on_done(fn(*args))

    assert tid not in queue.tasks
    assert _rows(db_path) == []
    assert rpc.removed == ["gid-meta"]


def test_removing_a_torrent_awaiting_consent_cancels_it(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(**_torrent_settings())
    _sync_spawn(queue)
    _uncached(queue, monkeypatch)
    tid = queue.add_url(MAGNET)
    _running(queue)
    queue._launch(queue.tasks[tid])
    assert tid in queue._awaiting_consent

    queue.remove(tid, delete_file=False)
    # A late answer must not resurrect a removed torrent.
    queue.torrent_consent(tid, True)

    assert tid not in queue.tasks
    assert rpc.magnets == []
    assert _rows(db_path) == []


def _in_flight_magnet(queue_env, monkeypatch):
    """A local magnet whose add_magnet RPC has not come back yet."""
    queue, rpc, db_path = queue_env(**_local_settings())
    _uncached(queue, monkeypatch)
    pending = []

    def spawn(fn, *args, on_done=None, on_fail=None, **kwargs):
        if getattr(fn, "__name__", "") == "_add_local_magnet":
            pending.append((fn, args, on_done))
            return
        result = fn(*args, **kwargs)
        if on_done is not None:
            on_done(result)

    tid = queue.add_url(MAGNET)
    _running(queue)
    queue._spawn = spawn
    queue._launch(queue.tasks[tid])
    assert pending
    return queue, rpc, db_path, tid, pending


def test_pausing_an_in_flight_torrent_pauses_the_late_gid(queue_env, monkeypatch):
    queue, rpc, db_path, tid, pending = _in_flight_magnet(queue_env, monkeypatch)

    queue.pause(tid)
    fn, args, on_done = pending[0]
    on_done(fn(*args))

    task = queue.tasks[tid]
    assert task.status == "paused"
    assert task.gid == "gid-meta"
    assert rpc.paused == ["gid-meta"]


def test_resuming_an_in_flight_torrent_keeps_it_tracked(queue_env, monkeypatch):
    """Pause then resume before the gid lands: the task must end up active
    with the gid, not stranded in a state the poll loop ignores."""
    queue, rpc, db_path, tid, pending = _in_flight_magnet(queue_env, monkeypatch)

    queue.pause(tid)
    queue.resume(tid)
    # The add is still in flight, so no second torrent may be started.
    assert len(pending) == 1

    fn, args, on_done = pending[0]
    on_done(fn(*args))

    task = queue.tasks[tid]
    assert task.gid == "gid-meta"
    assert task.status == "active"
    assert rpc.paused == []
    assert _persisted_row(db_path, tid)["status"] == "active"
    # Pollable: _poll_active only looks at active/paused tasks with a gid.
    assert task.status in {"active", "paused"} and task.gid


def test_stopping_the_queue_during_consent_prevents_any_peer_traffic(queue_env, monkeypatch):
    """Stop Queue while the disclosure is open: answering Continue must not
    open a single peer connection until the queue runs again."""
    queue, rpc, db_path = queue_env(**_torrent_settings())
    _sync_spawn(queue)
    _uncached(queue, monkeypatch)
    tid = queue.add_url(MAGNET)
    _running(queue)
    queue._launch(queue.tasks[tid])
    assert tid in queue._awaiting_consent

    queue._running = False
    queue.torrent_consent(tid, True)

    assert rpc.magnets == []
    assert queue.tasks[tid].status == "paused"
    assert queue.tasks[tid].gid is None

    # Starting the queue again releases it.
    queue.start_queue()
    assert len(rpc.magnets) == 1
    assert queue.tasks[tid].gid == "gid-meta"


def test_scheduler_block_during_the_capability_check_defers_the_add(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(**_local_settings())
    _uncached(queue, monkeypatch)

    def spawn(fn, *args, on_done=None, on_fail=None, **kwargs):
        result = fn(*args, **kwargs)
        if getattr(fn, "__name__", "") == "_bittorrent_capable":
            # The scheduler window closes while the check is in flight.
            queue._scheduler_allows = False
        if on_done is not None:
            on_done(result)

    tid = queue.add_url(MAGNET)
    _running(queue)
    queue._spawn = spawn
    queue._launch(queue.tasks[tid])

    assert rpc.magnets == []
    assert queue.tasks[tid].status == "paused"


def test_pausing_during_consent_leaves_the_torrent_paused(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(**_torrent_settings())
    _sync_spawn(queue)
    _uncached(queue, monkeypatch)
    tid = queue.add_url(MAGNET)
    _running(queue)
    queue._launch(queue.tasks[tid])

    queue.pause(tid)
    queue.torrent_consent(tid, True)

    assert rpc.magnets == []
    assert queue.tasks[tid].status == "paused"


def test_aria2_torrent_add_failure_never_reaches_the_task_row(queue_env, monkeypatch):
    """An aria2 error message for a magnet can quote the magnet itself."""
    queue, rpc, db_path = queue_env(**_local_settings())
    _sync_spawn(queue)
    _uncached(queue, monkeypatch)

    def boom(*a, **k):
        raise Aria2Error(
            "RPC aria2.addUri failed: bad uri "
            "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
            "&tr=http://tracker.example/announce?passkey=SECRETPASS"
        )

    rpc.add_magnet = boom
    tid = queue.add_url(MAGNET)
    _running(queue)

    queue._launch(queue.tasks[tid])

    task = queue.tasks[tid]
    assert task.status == "error"
    assert task.error == TORRENT_ARIA2_FAILED
    # The row's own URL is the user's magnet by design (Slice A); what must
    # never appear is aria2's echo of it in the persisted error.
    persisted_error = _persisted_row(db_path, tid)["error"]
    for secret in ("SECRETPASS", "passkey", "tracker.example", "magnet:?"):
        assert secret not in persisted_error


def test_aria2_torrent_status_error_is_sanitized(queue_env, monkeypatch):
    queue, rpc, db_path, tid = _start_local_magnet(queue_env, monkeypatch)
    queue._apply_status(tid, {"status": "complete", "followedBy": ["gid-child"]})

    queue._apply_status(tid, {
        "status": "error", "errorCode": "24",
        "errorMessage": "tracker http://tracker.example/announce?passkey=SECRETPASS "
                        "refused peer 203.0.113.9",
    })

    task = queue.tasks[tid]
    assert task.status == "error"
    assert TORRENT_ARIA2_FAILED in task.error
    assert "24" in task.error          # the numeric code is still useful
    persisted_error = _persisted_row(db_path, tid)["error"]
    for secret in ("SECRETPASS", "passkey", "tracker.example", "203.0.113.9"):
        assert secret not in persisted_error


def test_managed_torrent_errors_are_not_flattened_into_the_generic_one(queue_env, monkeypatch, tmp_path):
    """Sanitising aria2's messages must not hide Cove's own diagnoses."""
    queue, rpc, db_path, tid, _raw, _calls = _local_torrent_file(
        queue_env, monkeypatch, tmp_path
    )
    os.unlink(queue.tasks[tid].torrent_path)

    queue._launch(queue.tasks[tid])

    assert queue.tasks[tid].error != TORRENT_ARIA2_FAILED
    assert "stored copy" in queue.tasks[tid].error


def test_remove_and_delete_during_an_in_flight_add_still_deletes(queue_env, monkeypatch, tmp_path):
    queue, rpc, db_path, tid, pending = _in_flight_magnet(queue_env, monkeypatch)
    root = tmp_path / "Season 1"
    root.mkdir(parents=True)
    partial = root / "ep1.mkv"
    partial.write_bytes(b"partial")
    (root / "ep1.mkv.aria2").write_bytes(b"ctrl")
    bystander = root / "notes.txt"
    bystander.write_text("mine")
    rpc.files_result = [{"path": str(partial)}]

    queue.remove(tid, delete_file=True)
    fn, args, on_done = pending[0]
    on_done(fn(*args))

    assert rpc.removed == ["gid-meta"]
    assert not partial.exists()
    assert not (root / "ep1.mkv.aria2").exists()
    assert bystander.exists()
    assert _rows(db_path) == []


def test_pause_then_consent_then_resume_recovers_the_torrent(queue_env, monkeypatch):
    """Pausing while the disclosure is open must not strand the task in an
    'active' state with no aria2 work behind it."""
    queue, rpc, db_path = queue_env(**_torrent_settings())
    _sync_spawn(queue)
    _uncached(queue, monkeypatch)
    tid = queue.add_url(MAGNET)
    _running(queue)
    queue._launch(queue.tasks[tid])
    assert tid in queue._awaiting_consent

    queue.pause(tid)
    queue.torrent_consent(tid, True)
    assert queue.tasks[tid].status == "paused"
    assert rpc.magnets == []

    queue.resume(tid)

    task = queue.tasks[tid]
    assert task.status == "active"
    assert task.gid == "gid-meta"
    assert len(rpc.magnets) == 1


def test_declining_consent_does_not_block_a_later_retry(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(**_torrent_settings())
    _sync_spawn(queue)
    _uncached(queue, monkeypatch)
    tid = queue.add_url(MAGNET)
    _running(queue)
    queue._launch(queue.tasks[tid])
    queue.pause(tid)
    queue.torrent_consent(tid, False)
    assert queue.tasks[tid].status == "error"

    # Retry: consent is now recorded, so it should actually start.
    queue.settings.torrent_ip_disclosure_shown = True
    queue.resume(tid)

    assert queue.tasks[tid].gid == "gid-meta"
    assert len(rpc.magnets) == 1


def test_metadata_handoff_keeps_a_paused_torrent_paused(queue_env, monkeypatch):
    """The child gid is the real transfer: a pause taken during metadata
    must be re-applied to it, not silently lifted."""
    queue, rpc, db_path, tid = _start_local_magnet(queue_env, monkeypatch)
    queue.pause(tid)
    assert queue.tasks[tid].status == "paused"

    queue._apply_status(tid, {"status": "complete", "followedBy": ["gid-child"]})

    task = queue.tasks[tid]
    assert task.gid == "gid-child"
    assert task.status == "paused"
    assert "gid-child" in rpc.paused
    assert _persisted_row(db_path, tid)["status"] == "paused"


def test_metadata_handoff_respects_a_stopped_queue(queue_env, monkeypatch):
    queue, rpc, db_path, tid = _start_local_magnet(queue_env, monkeypatch)
    queue._running = False

    queue._apply_status(tid, {"status": "complete", "followedBy": ["gid-child"]})

    task = queue.tasks[tid]
    assert task.gid == "gid-child"
    assert task.status == "paused"
    assert "gid-child" in rpc.paused


def test_metadata_handoff_respects_a_closed_scheduler_window(queue_env, monkeypatch):
    queue, rpc, db_path, tid = _start_local_magnet(queue_env, monkeypatch)
    queue._scheduler_allows = False

    queue._apply_status(tid, {"status": "complete", "followedBy": ["gid-child"]})

    assert queue.tasks[tid].status == "paused"
    assert "gid-child" in rpc.paused


# ---------------------------------------------------------------------------
# TorBox (T1: hoster route, create-once/reuse lifecycle, provider ordering)
# ---------------------------------------------------------------------------

TORBOX_NODE_URL = "https://cdn-01.torbox.app/dl/secret/movie.mkv"


@pytest.fixture(autouse=False)
def torbox_available(monkeypatch):
    monkeypatch.setattr(debrid, "TORBOX_FEATURE_AVAILABLE", True)


def _torbox_settings(**extra):
    base = dict(torbox_enabled=True, torbox_api_token="torbox-token-value")
    base.update(extra)
    return base


def test_first_torbox_success_persists_item_id_and_keeps_original_url(
    queue_env, monkeypatch, torbox_available
):
    queue, rpc, db_path = queue_env(**_torbox_settings())
    monkeypatch.setattr(
        debrid, "resolve",
        lambda url, settings, **kw: Unrestricted(
            TORBOX_NODE_URL, "movie.mkv", 4096, debrid.TORBOX, item_id="42"
        ),
    )
    tid = queue.add_url(ORIGINAL_URL)
    task = queue.tasks[tid]

    queue._probe_and_add(task)

    assert rpc.added[0]["uris"] == [TORBOX_NODE_URL]
    assert task.url == ORIGINAL_URL
    assert task.debrid_route == debrid.TORBOX
    assert task.debrid_item_id == "42"
    assert _persisted_url(db_path, tid) == ORIGINAL_URL

    task.status = "active"
    queue._persist(task)
    row = _persisted_row(db_path, tid)
    assert row["debrid_route"] == debrid.TORBOX
    assert row["debrid_item_id"] == "42"
    row_text = _persisted_row_text(db_path, tid)
    assert TORBOX_NODE_URL not in row_text
    assert "secret" not in row_text


def test_torbox_cdn_url_is_not_persisted(queue_env, monkeypatch, torbox_available):
    queue, _rpc, db_path = queue_env(**_torbox_settings())
    monkeypatch.setattr(
        debrid, "resolve",
        lambda url, settings, **kw: Unrestricted(
            TORBOX_NODE_URL, "movie.mkv", 4096, debrid.TORBOX, item_id="42"
        ),
    )
    tid = queue.add_url(ORIGINAL_URL)
    queue._probe_and_add(queue.tasks[tid])
    queue._persist(queue.tasks[tid])
    assert TORBOX_NODE_URL not in _persisted_row_text(db_path, tid)


def test_retry_reuses_the_existing_torbox_item(queue_env, monkeypatch, torbox_available):
    queue, rpc, _db = queue_env(**_torbox_settings())
    tid = queue.add_url(ORIGINAL_URL)
    task = queue.tasks[tid]
    task.debrid_route = debrid.TORBOX
    task.debrid_item_id = "42"

    calls = []

    def fake_refresh(item_id, hoster_url, settings, **kw):
        calls.append((item_id, hoster_url))
        return Unrestricted(TORBOX_NODE_URL, "movie.mkv", 4096, debrid.TORBOX, item_id=item_id)

    monkeypatch.setattr(debrid, "torbox_refresh_web_download", fake_refresh)
    queue._probe_and_add(task)

    assert calls == [("42", ORIGINAL_URL)]
    assert task.debrid_item_id == "42"
    assert rpc.added[0]["uris"] == [TORBOX_NODE_URL]


def test_restart_reuses_the_existing_torbox_item(queue_env, monkeypatch, torbox_available):
    queue, _rpc, _db = queue_env(**_torbox_settings())
    tid = queue.add_url(ORIGINAL_URL)
    task = queue.tasks[tid]
    task.debrid_route = debrid.TORBOX
    task.debrid_item_id = "42"

    monkeypatch.setattr(
        debrid, "torbox_refresh_web_download",
        lambda item_id, url, settings, **kw: Unrestricted(
            TORBOX_NODE_URL, "movie.mkv", 4096, debrid.TORBOX, item_id=item_id
        ),
    )
    monkeypatch.setattr(queue, "_spawn", lambda fn, *a, **kw: fn(*a) if fn is queue._probe_and_add else None)
    # _launch clears transient fields before resolving; the pinned route
    # must survive that (debrid_route/debrid_item_id are not transient).
    queue._launch(task)
    assert task.debrid_route == debrid.TORBOX
    assert task.debrid_item_id == "42"


def test_missing_remote_item_soft_recreates_and_persists_replacement_id(
    queue_env, monkeypatch, torbox_available
):
    queue, rpc, db_path = queue_env(**_torbox_settings())
    tid = queue.add_url(ORIGINAL_URL)
    task = queue.tasks[tid]
    task.debrid_route = debrid.TORBOX
    task.debrid_item_id = "42"

    monkeypatch.setattr(
        debrid, "torbox_refresh_web_download",
        lambda item_id, url, settings, **kw: Unrestricted(
            TORBOX_NODE_URL, "movie.mkv", 4096, debrid.TORBOX, item_id="99"
        ),
    )
    queue._probe_and_add(task)
    assert task.debrid_item_id == "99"

    task.status = "active"
    queue._persist(task)
    assert _persisted_row(db_path, tid)["debrid_item_id"] == "99"


def test_no_concurrent_duplicate_create_when_already_pinned(
    queue_env, monkeypatch, torbox_available
):
    """Once a task is pinned to a TorBox item, a relaunch must go through
    the reuse/refresh path, never back through resolve() (which would
    create a second account item)."""
    queue, _rpc, _db = queue_env(**_torbox_settings())
    tid = queue.add_url(ORIGINAL_URL)
    task = queue.tasks[tid]
    task.debrid_route = debrid.TORBOX
    task.debrid_item_id = "42"

    def _resolve_must_not_be_called(*a, **kw):
        raise AssertionError("resolve() must not run for a pinned TorBox task")

    monkeypatch.setattr(debrid, "resolve", _resolve_must_not_be_called)
    monkeypatch.setattr(
        debrid, "torbox_refresh_web_download",
        lambda item_id, url, settings, **kw: Unrestricted(
            TORBOX_NODE_URL, "movie.mkv", 4096, debrid.TORBOX, item_id=item_id
        ),
    )
    queue._probe_and_add(task)  # must not raise


def test_torbox_preferred_first_when_gate_is_on(queue_env, monkeypatch, torbox_available):
    queue, rpc, _db = queue_env(
        all_debrid_enabled=True, all_debrid_api_key="ad-key",
        real_debrid_enabled=True, real_debrid_api_token="rd-token",
        debrid_preferred_provider="torbox",
        **_torbox_settings(),
    )
    order = []

    def fake_resolve(url, settings, **kw):
        order.append([p for p, _ in debrid._enabled_providers(settings)])
        return Unrestricted(TORBOX_NODE_URL, "f", 1, debrid.TORBOX, item_id="1")

    monkeypatch.setattr(debrid, "resolve", fake_resolve)
    tid = queue.add_url(ORIGINAL_URL)
    queue._probe_and_add(queue.tasks[tid])
    assert order[0][0] == debrid.TORBOX


def test_torbox_second_or_third_in_fallback_order(queue_env, torbox_available):
    queue, _rpc, _db = queue_env(
        all_debrid_enabled=True, all_debrid_api_key="ad-key",
        debrid_preferred_provider="alldebrid",
        **_torbox_settings(),
    )
    order = [p for p, _ in debrid._enabled_providers(queue.settings)]
    assert order == [ALL_DEBRID, debrid.TORBOX]


def test_ad_rd_ordering_regression_unaffected_by_torbox(queue_env):
    """TorBox gate is off (default) here: existing two-provider ordering
    must be identical to pre-TorBox behaviour."""
    queue, _rpc, _db = queue_env(
        all_debrid_enabled=True, all_debrid_api_key="ad-key",
        real_debrid_enabled=True, real_debrid_api_token="rd-token",
        debrid_preferred_provider="real_debrid",
    )
    order = [p for p, _ in debrid._enabled_providers(queue.settings)]
    assert order == [debrid.REAL_DEBRID, debrid.ALL_DEBRID]


def test_provider_auth_error_does_not_silently_fall_back(queue_env, monkeypatch, torbox_available):
    queue, rpc, _db = queue_env(debrid_preferred_provider="torbox", **_torbox_settings())

    def _boom(url, settings, **kw):
        raise DebridError(debrid.TORBOX, "auth",
                          "the API token was rejected. Check the token in Settings.")

    monkeypatch.setattr(debrid, "resolve", _boom)
    tid = queue.add_url(ORIGINAL_URL)
    with pytest.raises(DebridError):
        queue._probe_and_add(queue.tasks[tid])
    assert rpc.added == []


def test_torbox_disabled_by_default_gate_gives_current_behavior(queue_env, monkeypatch):
    """With the availability gate off (the shipped T1 default), enabling
    torbox_enabled in Settings must have zero routing effect."""
    monkeypatch.setattr(debrid, "TORBOX_FEATURE_AVAILABLE", False)
    queue, rpc, _db = queue_env(**_torbox_settings())
    monkeypatch.setattr(
        debrid, "resolve",
        lambda url, settings, **kw: None,
    )
    monkeypatch.setattr(
        "requests.Session.head",
        lambda self, url, **kw: SimpleNamespace(ok=True, headers={"Content-Length": "5"}),
    )
    tid = queue.add_url("https://example.com/plain.zip")
    queue._probe_and_add(queue.tasks[tid])
    assert rpc.added[0]["uris"] == ["https://example.com/plain.zip"]
    assert debrid._enabled_providers(queue.settings) == []


def test_pinned_torbox_task_falls_through_when_gate_is_off(queue_env, monkeypatch):
    """A row pinned to a TorBox item during earlier development testing must
    not route through torbox_refresh_web_download once the availability
    gate is off again -- it should fall through to ordinary resolution."""
    monkeypatch.setattr(debrid, "TORBOX_FEATURE_AVAILABLE", False)
    queue, rpc, _db = queue_env()
    tid = queue.add_url(ORIGINAL_URL)
    task = queue.tasks[tid]
    task.debrid_route = debrid.TORBOX
    task.debrid_item_id = "42"

    def _refresh_must_not_be_called(*a, **kw):
        raise AssertionError("torbox_refresh_web_download must not run while the gate is off")

    monkeypatch.setattr(debrid, "torbox_refresh_web_download", _refresh_must_not_be_called)
    queue._probe_and_add(task)  # must not raise
    assert rpc.added[0]["uris"] == [ORIGINAL_URL]


# ---------------------------------------------------------------------------
# TorBox (T2: cached torrent routing, materialisation, requestdl relaunch)
# ---------------------------------------------------------------------------

TORBOX_TORRENT_ITEM = "900"
TORBOX_TORRENT_CDN_URL = "https://cdn-01.torbox.app/dl/secret/ep1.mkv"


def _torbox_torrent_settings(**extra):
    base = _torrent_settings()
    base.update(_torbox_settings())
    base.update(extra)
    return base


def _tb_cached(files=None, name="Season 1"):
    if files is None:
        files = [
            CachedTorrentFile(0, ("ep1.mkv",), 10, item_id=TORBOX_TORRENT_ITEM, file_id="1"),
            CachedTorrentFile(1, ("extras", "ep2.mkv"), 20, item_id=TORBOX_TORRENT_ITEM, file_id="2"),
        ]
    return CachedTorrent(debrid.TORBOX, INFO_HASH, name, tuple(files))


def test_torbox_cached_torrent_becomes_https_tasks_with_item_and_file_ids(
    queue_env, monkeypatch, torbox_available
):
    queue, rpc, db_path = queue_env(**_torbox_torrent_settings())
    _sync_spawn(queue)
    monkeypatch.setattr(debrid, "resolve_torrent", lambda *a, **k: _tb_cached())
    tid = queue.add_url(MAGNET)

    queue._launch(queue.tasks[tid])

    rows = _rows(db_path)
    assert len(rows) == 2
    assert [r["debrid_route"] for r in rows] == [debrid.TORBOX] * 2
    assert [r["debrid_item_id"] for r in rows] == [TORBOX_TORRENT_ITEM] * 2
    assert [r["debrid_file_id"] for r in rows] == ["1", "2"]
    # No locked_link exists for TorBox: url is a stable non-secret
    # https reference built from the item/file IDs, never a magnet (which
    # would bypass debrid resolution) and never a CDN/requestdl URL.
    assert rows[0]["url"] == f"https://torbox.app/torrent/{TORBOX_TORRENT_ITEM}/1"
    assert rows[1]["url"] == f"https://torbox.app/torrent/{TORBOX_TORRENT_ITEM}/2"


def test_torbox_cached_route_does_not_show_the_p2p_disclosure(
    queue_env, monkeypatch, torbox_available
):
    queue, rpc, db_path = queue_env(**_torbox_torrent_settings())
    _sync_spawn(queue)
    monkeypatch.setattr(debrid, "resolve_torrent", lambda *a, **k: _tb_cached())
    tid = queue.add_url(MAGNET)

    queue._launch(queue.tasks[tid])

    # Materialisation turns the row into SOURCE_TORRENT_FILE, which is
    # never subject to the local-BitTorrent consent gate.
    assert queue.tasks[tid].source_type == SOURCE_TORRENT_FILE


def test_torbox_materialised_rows_survive_a_restart(queue_env, monkeypatch, torbox_available):
    queue, rpc, db_path = queue_env(**_torbox_torrent_settings())
    _sync_spawn(queue)
    monkeypatch.setattr(debrid, "resolve_torrent", lambda *a, **k: _tb_cached())
    tid = queue.add_url(MAGNET)
    queue._launch(queue.tasks[tid])

    restored = [_task_from_persisted_row(row) for row in _rows(db_path)]
    assert [t.source_type for t in restored] == [SOURCE_TORRENT_FILE] * 2
    assert [t.debrid_route for t in restored] == [debrid.TORBOX] * 2
    assert [t.debrid_item_id for t in restored] == [TORBOX_TORRENT_ITEM] * 2
    assert [t.debrid_file_id for t in restored] == ["1", "2"]
    assert [t.info_hash for t in restored] == [INFO_HASH] * 2


def test_torbox_repeated_materialisation_does_not_duplicate_rows(
    queue_env, monkeypatch, torbox_available
):
    queue, rpc, db_path = queue_env(**_torbox_torrent_settings())
    _sync_spawn(queue)
    monkeypatch.setattr(debrid, "resolve_torrent", lambda *a, **k: _tb_cached())
    tid = queue.add_url(MAGNET)
    queue._launch(queue.tasks[tid])
    assert len(_rows(db_path)) == 2

    second = queue.add_url(MAGNET, source_type=SOURCE_TORRENT, info_hash=INFO_HASH)
    removed = []
    queue.task_removed.connect(removed.append)
    queue._launch(queue.tasks[second])

    assert len(_rows(db_path)) == 2
    assert second not in queue.tasks
    assert removed == [second]


def _tb_materialised(queue_env, monkeypatch, **settings):
    queue, rpc, db_path = queue_env(**_torbox_torrent_settings(**settings))
    _sync_spawn(queue)
    monkeypatch.setattr(debrid, "resolve_torrent", lambda *a, **k: _tb_cached())
    tid = queue.add_url(MAGNET)
    queue._launch(queue.tasks[tid])
    return queue, rpc, db_path, tid


def test_torbox_torrent_file_relaunch_calls_requestdl_with_item_and_file_id(
    queue_env, monkeypatch, torbox_available
):
    queue, rpc, db_path, tid = _tb_materialised(queue_env, monkeypatch)
    seen = {}

    def fake_refresh(item_id, file_id, token, **kw):
        seen["item_id"] = item_id
        seen["file_id"] = file_id
        seen["token"] = token
        return TORBOX_TORRENT_CDN_URL

    monkeypatch.setattr(debrid, "torbox_refresh_torrent_file", fake_refresh)
    task = queue.tasks[tid]
    queue._launch(task)

    assert seen["item_id"] == TORBOX_TORRENT_ITEM
    assert seen["file_id"] == "1"
    assert rpc.added[0]["uris"] == [TORBOX_TORRENT_CDN_URL]
    # The persisted url/item/file identifiers are untouched by the launch.
    assert task.url == f"https://torbox.app/torrent/{TORBOX_TORRENT_ITEM}/1"
    assert task.debrid_item_id == TORBOX_TORRENT_ITEM
    assert task.debrid_file_id == "1"


def test_torbox_torrent_file_cdn_url_is_not_persisted(
    queue_env, monkeypatch, torbox_available
):
    queue, rpc, db_path, tid = _tb_materialised(queue_env, monkeypatch)
    monkeypatch.setattr(
        debrid, "torbox_refresh_torrent_file",
        lambda item_id, file_id, token, **kw: TORBOX_TORRENT_CDN_URL,
    )
    queue._launch(queue.tasks[tid])
    queue._persist(queue.tasks[tid])
    row_text = _persisted_row_text(db_path, tid)
    assert TORBOX_TORRENT_CDN_URL not in row_text
    assert "secret" not in row_text


def test_torbox_torrent_file_relaunch_generates_a_fresh_url_each_time(
    queue_env, monkeypatch, torbox_available
):
    queue, rpc, db_path, tid = _tb_materialised(queue_env, monkeypatch)
    urls = iter(["https://cdn-01.torbox.app/dl/a", "https://cdn-01.torbox.app/dl/b"])
    monkeypatch.setattr(
        debrid, "torbox_refresh_torrent_file",
        lambda item_id, file_id, token, **kw: next(urls),
    )
    task = queue.tasks[tid]
    queue._launch(task)
    first = rpc.added[0]["uris"][0]
    rpc.added.clear()
    queue._launch(task)
    second = rpc.added[0]["uris"][0]
    assert first != second


def test_torbox_torrent_file_missing_item_fails_without_recreating(
    queue_env, monkeypatch, torbox_available
):
    queue, rpc, db_path, tid = _tb_materialised(queue_env, monkeypatch)

    def boom(*a, **k):
        raise DebridError(debrid.TORBOX, "missing_item", "this TorBox torrent is no longer in your account.", False, False)

    monkeypatch.setattr(debrid, "torbox_refresh_torrent_file", boom)
    monkeypatch.setattr(
        debrid, "resolve_torrent",
        lambda *a, **k: pytest.fail("must not re-probe a pinned TorBox torrent file"),
    )
    queue._launch(queue.tasks[tid])

    task = queue.tasks[tid]
    assert task.status == "error"
    assert "no longer in your account" in task.error
    assert rpc.added == []


def test_torbox_torrent_file_bypasses_unlock_torrent_file(
    queue_env, monkeypatch, torbox_available
):
    """The AD/RD unlock path must never be asked to handle a TorBox row."""
    queue, rpc, db_path, tid = _tb_materialised(queue_env, monkeypatch)
    monkeypatch.setattr(
        debrid, "unlock_torrent_file",
        lambda *a, **k: pytest.fail("unlock_torrent_file must not run for a TorBox row"),
    )
    monkeypatch.setattr(
        debrid, "torbox_refresh_torrent_file",
        lambda item_id, file_id, token, **kw: TORBOX_TORRENT_CDN_URL,
    )
    queue._launch(queue.tasks[tid])
    assert rpc.added[0]["uris"] == [TORBOX_TORRENT_CDN_URL]


def test_pinned_torbox_torrent_file_falls_through_when_gate_is_off(
    queue_env, monkeypatch, torbox_available
):
    """A row materialised during earlier development testing (gate on) must
    not keep calling into TorBox once the availability gate is off again --
    it should fail cleanly instead of silently reusing the hidden route."""
    queue, rpc, db_path, tid = _tb_materialised(queue_env, monkeypatch)
    monkeypatch.setattr(debrid, "TORBOX_FEATURE_AVAILABLE", False)

    def _refresh_must_not_be_called(*a, **kw):
        raise AssertionError("torbox_refresh_torrent_file must not run while the gate is off")

    monkeypatch.setattr(debrid, "torbox_refresh_torrent_file", _refresh_must_not_be_called)
    queue._launch(queue.tasks[tid])

    task = queue.tasks[tid]
    assert task.status == "error"
    assert rpc.added == []


def test_ad_torrent_file_relaunch_regression_with_torbox_gate_on(
    queue_env, monkeypatch, torbox_available
):
    """AD/RD cached-torrent relaunch must be unaffected by TorBox's gate."""
    queue, rpc, db_path, tid = _materialised(queue_env, monkeypatch)
    monkeypatch.setattr(
        debrid, "unlock_torrent_file",
        lambda link, provider, settings, **kw: Unrestricted(
            TORRENT_NODE_URL, "ep1.mkv", 10, provider
        ),
    )
    queue._launch(queue.tasks[tid])
    assert rpc.added[0]["uris"] == [TORRENT_NODE_URL]


# ---- duplicate detection (QueueManager.find_duplicate) ---------------

DUP_HEX = "0123456789abcdef0123456789abcdef01234567"
DUP_URL = "https://example.com/dir/f.zip?id=1"


def _complete_in_db(db_path, tid, **overrides):
    """Mark a persisted row completed, the way a finished download leaves it."""
    values = {"status": "completed", "finished_at": time.time()}
    values.update(overrides)
    assignments = ", ".join(f"{k}=?" for k in values)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"UPDATE downloads SET {assignments} WHERE id=?",
            (*values.values(), tid),
        )
        conn.commit()
    finally:
        conn.close()


def test_find_duplicate_matches_a_queued_url(queue_env):
    queue, _rpc, _db = queue_env()
    tid = queue.add_url(DUP_URL)
    match = queue.find_duplicate(DUP_URL)
    assert match is not None
    assert match.category == "live"
    assert match.identity == "url"
    assert match.task_id == tid
    assert match.can_duplicate is True


def test_find_duplicate_matches_an_active_task(queue_env):
    queue, _rpc, _db = queue_env()
    tid = queue.add_url(DUP_URL)
    queue.tasks[tid].status = "active"
    match = queue.find_duplicate(DUP_URL)
    assert match is not None and match.status == "active"


def test_find_duplicate_matches_a_paused_task(queue_env):
    queue, _rpc, _db = queue_env()
    tid = queue.add_url(DUP_URL)
    queue.tasks[tid].status = "paused"
    match = queue.find_duplicate(DUP_URL)
    assert match is not None and match.status == "paused"


def test_find_duplicate_ignores_an_errored_task(queue_env):
    queue, _rpc, _db = queue_env()
    tid = queue.add_url(DUP_URL)
    queue.tasks[tid].status = "error"
    assert queue.find_duplicate(DUP_URL) is None


def test_find_duplicate_ignores_a_removed_task(queue_env):
    queue, _rpc, _db = queue_env()
    tid = queue.add_url(DUP_URL)
    queue.tasks[tid].status = "removed"
    assert queue.find_duplicate(DUP_URL) is None


def test_find_duplicate_ignores_a_different_query_string(queue_env):
    queue, _rpc, _db = queue_env()
    queue.add_url(DUP_URL)
    assert queue.find_duplicate("https://example.com/dir/f.zip?id=2") is None


def test_find_duplicate_honours_exclude_task_id(queue_env):
    queue, _rpc, _db = queue_env()
    tid = queue.add_url(DUP_URL)
    assert queue.find_duplicate(DUP_URL, exclude_task_id=tid) is None


def test_find_duplicate_matches_completed_history(queue_env):
    queue, _rpc, db_path = queue_env()
    tid = queue.add_url(DUP_URL)
    _complete_in_db(db_path, tid, filename="f.zip")
    queue.tasks.pop(tid)
    match = queue.find_duplicate(DUP_URL)
    assert match is not None
    assert match.category == "completed"
    assert match.filename == "f.zip"
    assert match.out_dir


def test_completed_history_survives_a_restart(queue_env):
    queue, _rpc, db_path = queue_env()
    tid = queue.add_url(DUP_URL)
    _complete_in_db(db_path, tid, filename="f.zip")
    # A fresh QueueManager over the same database is what a restart is.
    restarted, _rpc2, _db2 = queue_env()
    assert tid not in restarted.tasks
    match = restarted.find_duplicate(DUP_URL)
    assert match is not None and match.category == "completed"


def test_completed_history_ignores_errored_rows(queue_env):
    queue, _rpc, db_path = queue_env()
    tid = queue.add_url(DUP_URL)
    _complete_in_db(db_path, tid, status="error", error="boom")
    queue.tasks.pop(tid)
    assert queue.find_duplicate(DUP_URL) is None


def test_completed_history_ignores_removed_rows(queue_env):
    queue, _rpc, db_path = queue_env()
    tid = queue.add_url(DUP_URL)
    _complete_in_db(db_path, tid, status="removed")
    queue.tasks.pop(tid)
    assert queue.find_duplicate(DUP_URL) is None


def test_find_duplicate_matches_an_equivalent_magnet(queue_env):
    queue, _rpc, _db = queue_env(torrent_support_enabled=True)
    tid = queue.add_url(
        f"magnet:?xt=urn:btih:{DUP_HEX}&dn=Alpha&tr=udp%3A%2F%2Ftracker.a"
    )
    assert tid is not None
    match = queue.find_duplicate(
        f"magnet:?tr=udp%3A%2F%2Ftracker.b&xt=urn:btih:{DUP_HEX.upper()}&dn=Beta",
        info_hash=DUP_HEX,
    )
    assert match is not None
    assert match.identity == "info_hash"
    assert match.category == "live"
    # The engine cannot run one info hash twice, so no honest "anyway".
    assert match.can_duplicate is False


def test_find_duplicate_matches_a_stable_provider_item(queue_env):
    queue, _rpc, _db = queue_env()
    tid = queue.add_url("https://hoster.example/a")
    queue.tasks[tid].debrid_route = "torbox"
    queue.tasks[tid].debrid_item_id = "item-1"
    match = queue.find_duplicate(
        "https://hoster.example/b", debrid_route="torbox", debrid_item_id="item-1"
    )
    assert match is not None and match.identity == "provider"


def test_differing_provider_items_do_not_match(queue_env):
    queue, _rpc, _db = queue_env()
    tid = queue.add_url("https://hoster.example/a")
    queue.tasks[tid].debrid_route = "torbox"
    queue.tasks[tid].debrid_item_id = "item-1"
    assert (
        queue.find_duplicate(
            "https://hoster.example/b",
            debrid_route="torbox",
            debrid_item_id="item-2",
        )
        is None
    )


def test_find_duplicate_never_uses_resolved_url(queue_env):
    queue, _rpc, _db = queue_env()
    tid = queue.add_url("https://hoster.example/a")
    queue.tasks[tid].resolved_url = "https://cdn.example/node?token=dummy-token"
    assert queue.find_duplicate("https://cdn.example/node?token=dummy-token") is None


def test_old_row_without_info_hash_falls_back_to_url(queue_env):
    queue, _rpc, db_path = queue_env()
    magnetless = "https://example.com/legacy.bin"
    tid = queue.add_url(magnetless)
    _complete_in_db(db_path, tid, info_hash="", source_type="", debrid_route="")
    queue.tasks.pop(tid)
    match = queue.find_duplicate(magnetless)
    assert match is not None and match.identity == "url"


def test_malformed_history_values_do_not_crash_lookup(queue_env):
    queue, _rpc, db_path = queue_env()
    tid = queue.add_url(DUP_URL)
    _complete_in_db(db_path, tid, url="::not a url::", info_hash="nothex")
    queue.tasks.pop(tid)
    assert queue.find_duplicate(DUP_URL) is None
    assert queue.find_duplicate("::not a url::") is not None


def test_find_duplicate_does_not_mutate_or_launch(queue_env):
    queue, rpc, _db = queue_env()
    tid = queue.add_url(DUP_URL)
    before = dict(vars(queue.tasks[tid]))
    added_before = list(rpc.added)
    queue.find_duplicate(DUP_URL)
    queue.find_duplicate(f"magnet:?xt=urn:btih:{DUP_HEX}")
    assert dict(vars(queue.tasks[tid])) == before
    assert list(rpc.added) == added_before
    assert len(queue.tasks) == 1


def test_find_duplicate_returns_none_for_unusable_input(queue_env):
    queue, _rpc, _db = queue_env()
    queue.add_url(DUP_URL)
    assert queue.find_duplicate("") is None
    assert queue.find_duplicate("   ") is None


class _FakeSignal:
    def __init__(self):
        self._callbacks = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def emit(self, *args):
        for callback in list(self._callbacks):
            callback(*args)


class _FakeBuffer:
    def __init__(self, data):
        self._data = data

    def data(self):
        return self._data


class _FakeQProcess:
    NotRunning = 0
    Running = 1
    FailedToStart = 2
    MergedChannels = 3
    instances = []

    def __init__(self, parent):
        self.parent = parent
        self.state_value = self.NotRunning
        self.readyReadStandardOutput = _FakeSignal()
        self.finished = _FakeSignal()
        self.errorOccurred = _FakeSignal()
        self.output = b""
        self.deleted = False
        self.finish_on_terminate = True
        self.finish_on_state_check = False
        self.terminate_calls = 0
        self.kill_calls = 0

    def setProcessChannelMode(self, mode):
        self.channel_mode = mode

    def state(self):
        if self.finish_on_state_check:
            self.finish_on_state_check = False
            self.finish(-15)
        return self.state_value

    def start(self, program, args):
        self.program = program
        self.args = list(args)
        self.state_value = self.Running
        type(self).instances.append(self)

    def readAllStandardOutput(self):
        data = self.output
        self.output = b""
        return _FakeBuffer(data)

    def emit_output(self, data):
        self.output += data.encode()
        self.readyReadStandardOutput.emit()

    def finish(self, exit_code=0):
        self.state_value = self.NotRunning
        self.finished.emit(exit_code, 0)

    def fail_to_start(self):
        self.state_value = self.NotRunning
        self.errorOccurred.emit(self.FailedToStart)

    def terminate(self):
        self.terminate_calls += 1
        if self.state_value != self.NotRunning and self.finish_on_terminate:
            self.finish(-15)

    def kill(self):
        self.kill_calls += 1
        if self.state_value != self.NotRunning:
            self.finish(-9)

    def deleteLater(self):
        self.deleted = True

    def errorString(self):
        return "fake process error"


@pytest.fixture
def fake_process(monkeypatch):
    _FakeQProcess.instances = []
    monkeypatch.setattr(queue_module, "QProcess", _FakeQProcess)
    return _FakeQProcess


def _start_hls(queue, fake_process, tmp_path, filename="movie.mp4"):
    tid = queue.add_url(
        "https://example.com/live/stream.m3u8",
        out_dir=str(tmp_path),
        filename=filename,
    )
    task = queue.tasks[tid]
    queue._launch_hls(task)
    return task, fake_process.instances[-1]


def _start_extractor(queue, fake_process, tmp_path, filename="movie.mp4"):
    tid = queue.add_url(
        "https://www.youtube.com/watch?v=fake",
        out_dir=str(tmp_path),
        filename=filename,
    )
    task = queue.tasks[tid]
    queue._launch_extractor(task)
    return task, fake_process.instances[-1]


def _hls_private_path(proc):
    return Path(proc.args[-1])


def _extractor_private_path(proc):
    template = proc.args[proc.args.index("-o") + 1]
    return Path(template.replace("%(ext)s", "mp4"))


def _assert_no_work_dirs(tmp_path):
    assert list(tmp_path.glob(".cove-work-*")) == []


def test_fake_extractor_pause_preserves_and_resume_reuses_private_work(
    queue_env, fake_process, tmp_path
):
    queue, _rpc, _db_path = queue_env()
    task, paused_proc = _start_extractor(queue, fake_process, tmp_path)
    _running(queue)
    task.status = "active"
    private = _extractor_private_path(paused_proc)
    work_path = private.parent
    partial = work_path / "movie.mp4.part"
    partial_bytes = b"yt-dlp resumable partial\x00data"
    partial.write_bytes(partial_bytes)
    paused_proc.finish_on_terminate = False

    queue.pause(task.id)

    assert task.status == "active"
    assert partial.read_bytes() == partial_bytes
    paused_proc.finish(-15)
    assert task.status == "paused"
    assert partial.read_bytes() == partial_bytes

    queue.resume(task.id)
    resumed_proc = fake_process.instances[-1]
    resumed_private = _extractor_private_path(resumed_proc)
    assert resumed_proc is not paused_proc
    assert resumed_private.parent == work_path
    assert resumed_proc.args[resumed_proc.args.index("-o") + 1] == str(
        work_path / "movie.%(ext)s"
    )
    assert partial.read_bytes() == partial_bytes

    paused_proc.finish(-15)
    assert task.status == "active"
    assert partial.read_bytes() == partial_bytes

    resumed_private.write_bytes(b"completed output")
    resumed_proc.emit_output(f"{FINAL_PATH_MARKER}{resumed_private}\n")
    resumed_proc.finish(0)

    assert task.status == "completed"
    assert (tmp_path / "movie.mp4").read_bytes() == b"completed output"
    assert not work_path.exists()
    _assert_no_work_dirs(tmp_path)


def test_fake_extractor_pause_completes_when_process_is_already_stopped(
    queue_env, fake_process, tmp_path
):
    queue, _rpc, _db_path = queue_env()
    task, proc = _start_extractor(queue, fake_process, tmp_path)
    _running(queue)
    task.status = "active"
    work_path = _extractor_private_path(proc).parent
    partial = work_path / "movie.mp4.part"
    partial.write_bytes(b"resumable")
    proc.state_value = proc.NotRunning

    queue.pause(task.id)

    assert task.status == "paused"
    assert task.id not in queue._extractor_pause_pending
    assert partial.read_bytes() == b"resumable"
    queue.resume(task.id)
    resumed_proc = fake_process.instances[-1]
    assert task.status == "active"
    assert resumed_proc is not proc
    assert _extractor_private_path(resumed_proc).parent == work_path
    assert partial.read_bytes() == b"resumable"


def test_fake_extractor_pause_stop_setup_race_completes_exactly_once(
    queue_env, fake_process, tmp_path
):
    queue, _rpc, _db_path = queue_env()
    task, proc = _start_extractor(queue, fake_process, tmp_path)
    _running(queue)
    task.status = "active"
    work_path = _extractor_private_path(proc).parent
    partial = work_path / "movie.mp4.part"
    partial.write_bytes(b"resumable")
    proc.finish_on_state_check = True
    pause_transitions = 0
    mark_paused = queue._mark_paused

    def counted_mark_paused(tid):
        nonlocal pause_transitions
        pause_transitions += 1
        mark_paused(tid)

    queue._mark_paused = counted_mark_paused
    queue.pause(task.id)

    assert task.status == "paused"
    assert pause_transitions == 1
    assert task.id not in queue._extractor_pause_pending
    assert partial.read_bytes() == b"resumable"
    proc.finished.emit(-15, 0)
    assert pause_transitions == 1

    queue.resume(task.id)
    assert task.status == "active"
    assert _extractor_private_path(fake_process.instances[-1]).parent == work_path
    proc.finished.emit(-15, 0)
    assert task.status == "active"
    assert pause_transitions == 1


def test_fake_extractor_pause_kill_fallback_completes(
    queue_env, fake_process, tmp_path, monkeypatch
):
    callbacks = []
    monkeypatch.setattr(queue_module.QTimer, "singleShot", lambda _ms, cb: callbacks.append(cb))
    queue, _rpc, _db_path = queue_env()
    task, proc = _start_extractor(queue, fake_process, tmp_path)
    _running(queue)
    task.status = "active"
    work_path = _extractor_private_path(proc).parent
    (work_path / "movie.mp4.part").write_bytes(b"resumable")
    proc.finish_on_terminate = False

    queue.pause(task.id)
    assert task.status == "active"
    assert proc.terminate_calls == 1
    assert len(callbacks) == 1

    callbacks[0]()

    assert proc.kill_calls == 1
    assert task.status == "paused"
    assert task.id not in queue._extractor_pause_pending
    assert work_path.exists()


def test_fake_extractor_remove_during_pause_owns_process_until_stopped(
    queue_env, fake_process, tmp_path, monkeypatch
):
    queue, _rpc, _db_path = queue_env()
    task, proc = _start_extractor(queue, fake_process, tmp_path)
    _running(queue)
    task.status = "active"
    work_path = _extractor_private_path(proc).parent
    (work_path / "movie.mp4.part").write_bytes(b"partial")
    proc.finish_on_terminate = False
    cleanup_states = []
    original_cleanup = queue._cleanup_engine_work

    def record_cleanup(work):
        if work is not None:
            cleanup_states.append(proc.state())
        original_cleanup(work)

    monkeypatch.setattr(queue, "_cleanup_engine_work", record_cleanup)

    queue.pause(task.id)
    assert queue._extractor_pause_pending[task.id] is proc
    queue.remove(task.id)

    assert task.id not in queue.tasks
    assert queue._extractor_pause_pending[task.id] is proc
    assert proc.state() == fake_process.Running
    assert work_path.exists()
    assert cleanup_states == []

    proc.finish(-15)

    assert task.id not in queue._extractor_pause_pending
    assert cleanup_states == [fake_process.NotRunning]
    assert not work_path.exists()

    proc.finished.emit(-15, 0)
    assert task.status != "paused"
    assert cleanup_states == [fake_process.NotRunning]


def test_fake_extractor_queue_pause_blocks_resume_until_pending_stops(
    queue_env, fake_process, tmp_path
):
    queue, rpc, _db_path = queue_env()
    rpc.pause_all = lambda: None
    _sync_spawn(queue)
    task, old_proc = _start_extractor(queue, fake_process, tmp_path)
    _running(queue)
    task.status = "active"
    old_work = _extractor_private_path(old_proc).parent
    (old_work / "movie.mp4.part").write_bytes(b"partial")
    old_proc.finish_on_terminate = False

    queue.pause(task.id)
    queue.stop_queue()

    assert task.status == "paused"
    assert queue._extractor_pause_pending[task.id] is old_proc
    queue.start_queue()

    assert task.status == "queued"
    assert fake_process.instances == [old_proc]
    assert old_proc.state() == fake_process.Running

    old_proc.finish(-15)

    new_proc = fake_process.instances[-1]
    new_work = _extractor_private_path(new_proc).parent
    assert new_proc is not old_proc
    assert task.status == "active"
    assert queue._extractor_procs[task.id] is new_proc

    old_proc.finished.emit(-15, 0)
    assert task.status == "active"
    assert queue._extractor_procs[task.id] is new_proc
    assert new_work.exists()


def test_retire_extractor_deduplicates_active_and_pending_process(
    queue_env, fake_process, tmp_path, monkeypatch
):
    queue, _rpc, _db_path = queue_env()
    task, proc = _start_extractor(queue, fake_process, tmp_path)
    work_path = _extractor_private_path(proc).parent
    work = queue._extractor_work[task.id]
    queue._extractor_pause_pending[task.id] = proc
    cleanup_calls = []
    original_cleanup = queue._cleanup_engine_work

    def record_cleanup(work):
        cleanup_calls.append(work)
        original_cleanup(work)

    monkeypatch.setattr(queue, "_cleanup_engine_work", record_cleanup)

    queue._retire_extractor_run(task.id)

    assert proc.terminate_calls == 1
    assert cleanup_calls == [work]
    assert task.id not in queue._extractor_procs
    assert task.id not in queue._extractor_pause_pending
    assert not work_path.exists()


def test_fake_extractor_removal_while_paused_cleans_preserved_work(
    queue_env, fake_process, tmp_path
):
    queue, _rpc, _db_path = queue_env()
    task, proc = _start_extractor(queue, fake_process, tmp_path)
    _running(queue)
    task.status = "active"
    work_path = _extractor_private_path(proc).parent
    (work_path / "movie.mp4.part").write_bytes(b"partial")

    queue.pause(task.id)
    assert task.status == "paused"
    assert work_path.exists()
    queue.remove(task.id)

    assert task.id not in queue.tasks
    assert not work_path.exists()
    _assert_no_work_dirs(tmp_path)


def test_fake_extractor_active_removal_and_terminal_failure_clean_work(
    queue_env, fake_process, tmp_path
):
    queue, _rpc, _db_path = queue_env()
    removed, removed_proc = _start_extractor(queue, fake_process, tmp_path, "removed.mp4")
    removed_work = _extractor_private_path(removed_proc).parent
    (removed_work / "removed.mp4.part").write_bytes(b"partial")

    queue.remove(removed.id)
    assert removed.id not in queue.tasks
    assert not removed_work.exists()

    failed, failed_proc = _start_extractor(queue, fake_process, tmp_path, "failed.mp4")
    failed_work = _extractor_private_path(failed_proc).parent
    (failed_work / "failed.mp4.part").write_bytes(b"partial")
    failed_proc.finish(1)

    assert failed.status == "error"
    assert not failed_work.exists()
    _assert_no_work_dirs(tmp_path)


def test_fake_extractor_repeated_pause_resume_reuses_one_work_directory(
    queue_env, fake_process, tmp_path
):
    queue, _rpc, _db_path = queue_env()
    task, proc = _start_extractor(queue, fake_process, tmp_path)
    _running(queue)
    task.status = "active"
    work_path = _extractor_private_path(proc).parent
    partial = work_path / "movie.mp4.part"
    partial.write_bytes(b"same partial")

    for _ in range(3):
        queue.pause(task.id)
        assert task.status == "paused"
        assert partial.read_bytes() == b"same partial"
        assert list(tmp_path.glob(".cove-work-*")) == [work_path]
        queue.resume(task.id)
        proc = fake_process.instances[-1]
        assert task.status == "active"
        assert _extractor_private_path(proc).parent == work_path
        assert list(tmp_path.glob(".cove-work-*")) == [work_path]


def test_fake_hls_publishes_private_output_and_persists_actual_path(
    queue_env, fake_process, tmp_path
):
    queue, _rpc, db_path = queue_env()
    task, proc = _start_hls(queue, fake_process, tmp_path)

    _hls_private_path(proc).write_bytes(b"hls output")
    proc.finish(0)

    target = tmp_path / "movie.mp4"
    assert task.status == "completed"
    assert task.filename == target.name
    assert target.read_bytes() == b"hls output"
    assert queue._task_path(task) == target
    assert _persisted_row(db_path, task.id)["filename"] == target.name
    _assert_no_work_dirs(tmp_path)


def test_fake_hls_collision_renames_without_modifying_existing_target(
    queue_env, fake_process, tmp_path
):
    existing = tmp_path / "movie.mp4"
    existing.write_bytes(b"original")
    before = hashlib.sha256(existing.read_bytes()).hexdigest()
    queue, _rpc, db_path = queue_env()
    task, proc = _start_hls(queue, fake_process, tmp_path)

    _hls_private_path(proc).write_bytes(b"replacement")
    proc.finish(0)

    selected = tmp_path / "movie (1).mp4"
    assert task.status == "completed"
    assert task.filename == selected.name
    assert hashlib.sha256(existing.read_bytes()).hexdigest() == before
    assert selected.read_bytes() == b"replacement"
    assert _persisted_row(db_path, task.id)["filename"] == selected.name
    restored = _task_from_persisted_row(_persisted_row(db_path, task.id))
    assert restored.filename == selected.name
    assert queue._task_path(restored) == selected
    _assert_no_work_dirs(tmp_path)


def test_fake_hls_failure_cleans_private_output_and_keeps_public_target(
    queue_env, fake_process, tmp_path
):
    existing = tmp_path / "movie.mp4"
    existing.write_bytes(b"original")
    queue, _rpc, _db_path = queue_env()
    task, proc = _start_hls(queue, fake_process, tmp_path)

    _hls_private_path(proc).write_bytes(b"partial")
    proc.finish(1)

    assert task.status == "error"
    assert existing.read_bytes() == b"original"
    assert not (tmp_path / "movie (1).mp4").exists()
    _assert_no_work_dirs(tmp_path)


def test_fake_hls_cancellation_cleans_private_output_without_publishing(
    queue_env, fake_process, tmp_path
):
    existing = tmp_path / "movie.mp4"
    existing.write_bytes(b"original")
    queue, _rpc, _db_path = queue_env()
    task, proc = _start_hls(queue, fake_process, tmp_path)

    _hls_private_path(proc).write_bytes(b"partial")
    queue.pause(task.id)

    assert task.status == "paused"
    assert existing.read_bytes() == b"original"
    assert not (tmp_path / "movie (1).mp4").exists()
    _assert_no_work_dirs(tmp_path)


def test_fake_hls_concurrent_collision_tasks_publish_unique_files(
    queue_env, fake_process, tmp_path
):
    queue, _rpc, _db_path = queue_env()
    first, first_proc = _start_hls(queue, fake_process, tmp_path)
    second, second_proc = _start_hls(queue, fake_process, tmp_path)

    _hls_private_path(first_proc).write_bytes(b"first")
    _hls_private_path(second_proc).write_bytes(b"second")
    first_proc.finish(0)
    second_proc.finish(0)

    assert first.status == "completed"
    assert second.status == "completed"
    assert {first.filename, second.filename} == {"movie.mp4", "movie (1).mp4"}
    assert (tmp_path / first.filename).read_bytes() == (b"first" if first.filename == "movie.mp4" else b"second")
    assert (tmp_path / second.filename).read_bytes() == (b"first" if second.filename == "movie.mp4" else b"second")
    _assert_no_work_dirs(tmp_path)


def test_fake_extractor_collision_publishes_reported_final_path(
    queue_env, fake_process, tmp_path
):
    existing = tmp_path / "movie.mp4"
    existing.write_bytes(b"original")
    queue, _rpc, db_path = queue_env()
    task, proc = _start_extractor(queue, fake_process, tmp_path)
    private = _extractor_private_path(proc)
    private.write_bytes(b"extractor output")

    proc.emit_output(f"{FINAL_PATH_MARKER}{private}\n")
    proc.finish(0)

    selected = tmp_path / "movie (1).mp4"
    assert task.status == "completed"
    assert task.filename == selected.name
    assert existing.read_bytes() == b"original"
    assert selected.read_bytes() == b"extractor output"
    assert _persisted_row(db_path, task.id)["filename"] == selected.name
    _assert_no_work_dirs(tmp_path)


def test_fake_extractor_rejects_output_outside_owned_work_directory(
    queue_env, fake_process, tmp_path
):
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    queue, _rpc, _db_path = queue_env()
    task, proc = _start_extractor(queue, fake_process, tmp_path)

    proc.emit_output(f"{FINAL_PATH_MARKER}{outside}\n")
    proc.finish(0)

    assert task.status == "error"
    assert outside.read_bytes() == b"outside"
    assert not (tmp_path / "movie.mp4").exists()
    _assert_no_work_dirs(tmp_path)


def test_fake_extractor_rejects_symlink_escape(
    queue_env, fake_process, tmp_path
):
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    queue, _rpc, _db_path = queue_env()
    task, proc = _start_extractor(queue, fake_process, tmp_path)
    private = _extractor_private_path(proc)
    escaped = private.with_name("escape.mp4")
    os.symlink(outside, escaped)

    proc.emit_output(f"{FINAL_PATH_MARKER}{escaped}\n")
    proc.finish(0)

    assert task.status == "error"
    assert outside.read_bytes() == b"outside"
    assert not (tmp_path / "movie.mp4").exists()
    _assert_no_work_dirs(tmp_path)


def test_fake_extractor_unreported_result_publishes_the_only_candidate(
    queue_env, fake_process, tmp_path
):
    # A run that never printed the marker is the same recoverable situation as
    # one whose reported name is stale: exactly one finished file is present.
    queue, _rpc, _db_path = queue_env()
    task, proc = _start_extractor(queue, fake_process, tmp_path)
    _extractor_private_path(proc).write_bytes(b"unreported")

    proc.emit_output("Download complete\n")
    proc.finish(0)

    assert task.status == "completed"
    assert (tmp_path / "movie.mp4").read_bytes() == b"unreported"
    _assert_no_work_dirs(tmp_path)


def test_fake_extractor_unreported_ambiguous_result_does_not_complete(
    queue_env, fake_process, tmp_path
):
    queue, _rpc, _db_path = queue_env()
    task, proc = _start_extractor(queue, fake_process, tmp_path)
    work_path = _extractor_private_path(proc).parent
    (work_path / "a.mp4").write_bytes(b"one")
    (work_path / "b.mkv").write_bytes(b"two")

    proc.emit_output("Download complete\n")
    proc.finish(0)

    assert task.status == "error"
    assert not (tmp_path / "movie.mp4").exists()
    _assert_no_work_dirs(tmp_path)


def test_fake_hls_publication_failure_does_not_complete(
    queue_env, fake_process, tmp_path, monkeypatch
):
    existing = tmp_path / "movie.mp4"
    existing.write_bytes(b"original")
    queue, _rpc, _db_path = queue_env()
    task, proc = _start_hls(queue, fake_process, tmp_path)
    _hls_private_path(proc).write_bytes(b"new output")

    def fail_publish(*_args, **_kwargs):
        raise OutputPathError("atomic publication failed")

    monkeypatch.setattr(queue_module, "publish_output", fail_publish)
    proc.finish(0)

    assert task.status == "error"
    assert existing.read_bytes() == b"original"
    assert not (tmp_path / "movie (1).mp4").exists()
    _assert_no_work_dirs(tmp_path)


def test_fake_hls_superseded_process_cannot_publish_or_clean_new_run(
    queue_env, fake_process, tmp_path
):
    queue, _rpc, _db_path = queue_env()
    task, old_proc = _start_hls(queue, fake_process, tmp_path)
    old_private = _hls_private_path(old_proc)
    old_work = old_private.parent
    old_private.write_bytes(b"old")

    queue._launch_hls(task)
    new_proc = fake_process.instances[-1]
    new_private = _hls_private_path(new_proc)
    assert new_private.parent != old_work
    assert new_private.parent.exists()

    old_proc.finish(0)

    assert not (tmp_path / "movie.mp4").exists()
    assert new_private.parent.exists()

    new_private.write_bytes(b"new")
    new_proc.finish(0)
    assert task.status == "completed"
    assert (tmp_path / task.filename).read_bytes() == b"new"
    _assert_no_work_dirs(tmp_path)


def test_output_helper_collision_sequence_and_unsupported_no_clobber(
    tmp_path, monkeypatch
):
    assert [next(collision_candidates("name.ext"))]
    candidates = collision_candidates("name.ext")
    assert [next(candidates), next(candidates), next(candidates)] == [
        "name.ext",
        "name (1).ext",
        "name (2).ext",
    ]

    work = create_work_directory(tmp_path)
    source = work.path / "name.ext"
    source.write_bytes(b"private")
    target = tmp_path / "name.ext"
    target.write_bytes(b"existing")

    def unsupported_link(*_args, **_kwargs):
        raise OSError(errno.EOPNOTSUPP, "hard links unsupported")

    # Each platform reaches the destination through its own primitive.
    monkeypatch.setattr(
        output_paths,
        "_windows_link_candidate" if os.name == "nt" else "_link_pinned_fd",
        unsupported_link,
    )
    with pytest.raises(OutputPathError):
        publish_output(work, source, "name.ext")
    assert target.read_bytes() == b"existing"
    assert source.read_bytes() == b"private"
    cleanup_work_directory(work)


def test_collision_candidates_bound_ascii_names_and_suffix_growth(monkeypatch):
    monkeypatch.setattr(output_paths, "_is_windows_runtime", lambda: False)
    filename = f"{'a' * 251}.ext"

    first_run = collision_candidates(filename)
    candidates = [next(first_run) for _ in range(101)]
    second_run = collision_candidates(filename)

    assert candidates[0] == filename
    assert candidates[1] == f"{'a' * 247} (1).ext"
    assert candidates[2] == f"{'a' * 247} (2).ext"
    assert candidates[9].endswith(" (9).ext")
    assert candidates[10].endswith(" (10).ext")
    assert candidates[99].endswith(" (99).ext")
    assert candidates[100].endswith(" (100).ext")
    assert candidates == [next(second_run) for _ in range(101)]
    assert all(len(os.fsencode(candidate)) <= 255 for candidate in candidates)
    assert all(validate_public_filename(candidate) == candidate for candidate in candidates)


def test_collision_candidates_use_posix_encoded_length_without_splitting_unicode(
    monkeypatch,
):
    monkeypatch.setattr(output_paths, "_is_windows_runtime", lambda: False)
    filename = f"{'é' * 125}a.ext"

    candidates = collision_candidates(filename)
    original = next(candidates)
    collision = next(candidates)

    assert original == filename
    assert collision == f"{'é' * 123} (1).ext"
    assert collision.encode("utf-8").decode("utf-8") == collision
    assert len(os.fsencode(collision)) <= 255
    assert validate_public_filename(collision) == collision


def test_collision_candidates_use_windows_utf16_component_length(monkeypatch):
    monkeypatch.setattr(output_paths, "_is_windows_runtime", lambda: True)
    filename = f"{'😀' * 125}a.ext"

    candidates = collision_candidates(filename)
    original = next(candidates)
    collision = next(candidates)

    assert original == filename
    assert collision == f"{'😀' * 123} (1).ext"
    assert len(collision.encode("utf-16-le")) // 2 <= 255
    assert validate_public_filename(collision) == collision


def test_collision_candidates_truncate_pathological_extension_deterministically(
    monkeypatch,
):
    monkeypatch.setattr(output_paths, "_is_windows_runtime", lambda: False)
    filename = f"a.{'x' * 253}"

    candidates = collision_candidates(filename)
    assert next(candidates) == filename
    collision = next(candidates)

    assert collision == f" (1).{'x' * 250}"
    assert len(os.fsencode(collision)) == 255
    assert validate_public_filename(collision) == collision
    repeated = collision_candidates(filename)
    next(repeated)
    assert next(repeated) == collision


def test_output_helper_rejects_noncanonical_names_without_nested_output(tmp_path):
    work = create_work_directory(tmp_path)
    source = work.path / "name.ext"
    source.write_bytes(b"private")
    escaped = tmp_path.parent / f"{tmp_path.name}-escape.ext"
    invalid_names = (
        "",
        ".",
        "..",
        "/absolute.ext",
        r"\absolute.ext",
        r"C:\absolute.ext",
        "nested/child.ext",
        r"nested\child.ext",
        f"../{tmp_path.name}-escape.ext",
        "line\nbreak.ext",
        "CON.txt",
        "name. ",
        "name.",
        "x" * 256,
    )

    assert validate_public_filename("世界.ext") == "世界.ext"
    assert not escaped.exists()
    try:
        for filename in invalid_names:
            with pytest.raises(OutputPathError):
                publish_output(work, source, filename)
            assert source.read_bytes() == b"private"
            assert not escaped.exists()
            assert not (tmp_path / "nested").exists()
            assert not (tmp_path / "child.ext").exists()
    finally:
        cleanup_work_directory(work)


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX descriptor publication")
def test_output_helper_fails_closed_without_directory_relative_link(
    tmp_path, monkeypatch
):
    work = create_work_directory(tmp_path)
    source = work.path / "name.ext"
    source.write_bytes(b"private")
    path_based_calls = []

    def path_based_link(*args, **kwargs):
        path_based_calls.append((args, kwargs))

    monkeypatch.setattr(output_paths, "_relative_link_supported", lambda: False)
    monkeypatch.setattr(output_paths.os, "link", path_based_link)
    try:
        with pytest.raises(
            OutputPathError, match="Descriptor-relative publication is unsupported"
        ):
            publish_output(work, source, "name.ext")
        assert path_based_calls == []
        assert source.read_bytes() == b"private"
        assert not (tmp_path / "name.ext").exists()
    finally:
        monkeypatch.undo()
        cleanup_work_directory(work)


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX descriptor publication")
def test_output_helper_posix_near_limit_collisions_preserve_existing_files(tmp_path):
    filename = f"{'a' * 251}.ext"

    published = []
    for content in (b"first", b"second", b"third"):
        work = create_work_directory(tmp_path)
        source = work.path / "source.ext"
        source.write_bytes(content)
        published.append(publish_output(work, source, filename))

    assert [path.name for path in published] == [
        filename,
        f"{'a' * 247} (1).ext",
        f"{'a' * 247} (2).ext",
    ]
    assert [path.read_bytes() for path in published] == [b"first", b"second", b"third"]
    assert all(len(os.fsencode(path.name)) <= 255 for path in published)
    assert all(validate_public_filename(path.name) == path.name for path in published)


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX descriptor publication")
def test_output_helper_posix_publication_pins_validated_inode(tmp_path):
    work = create_work_directory(tmp_path)
    source = work.path / "name.ext"
    source.write_bytes(b"private")
    source_info = source.stat()

    result = publish_output(work, source, "name.ext")

    result_info = result.stat()
    assert result.read_bytes() == b"private"
    assert (result_info.st_dev, result_info.st_ino) == (
        source_info.st_dev,
        source_info.st_ino,
    )


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX descriptor publication")
def test_output_helper_posix_collision_preserves_existing_target(tmp_path):
    target = tmp_path / "name.ext"
    target.write_bytes(b"existing")
    work = create_work_directory(tmp_path)
    source = work.path / "name.ext"
    source.write_bytes(b"private")

    result = publish_output(work, source, "name.ext")

    assert result == tmp_path / "name (1).ext"
    assert result.read_bytes() == b"private"
    assert target.read_bytes() == b"existing"


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX descriptor publication")
def test_output_helper_posix_two_collisions_use_second_suffix(tmp_path):
    (tmp_path / "name.ext").write_bytes(b"first")
    (tmp_path / "name (1).ext").write_bytes(b"second")
    work = create_work_directory(tmp_path)
    source = work.path / "name.ext"
    source.write_bytes(b"private")

    result = publish_output(work, source, "name.ext")

    assert result == tmp_path / "name (2).ext"
    assert result.read_bytes() == b"private"
    assert (tmp_path / "name.ext").read_bytes() == b"first"
    assert (tmp_path / "name (1).ext").read_bytes() == b"second"


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX descriptor publication")
def test_output_helper_posix_retries_only_eexist(tmp_path, monkeypatch):
    work = create_work_directory(tmp_path)
    source = work.path / "name.ext"
    source.write_bytes(b"private")
    real_link = output_paths._link_pinned_fd
    calls = []

    def collide_once(source_fd, destination_fd, candidate):
        calls.append(candidate)
        if len(calls) == 1:
            raise FileExistsError(errno.EEXIST, "collision", candidate)
        real_link(source_fd, destination_fd, candidate)

    monkeypatch.setattr(output_paths, "_link_pinned_fd", collide_once)
    result = publish_output(work, source, "name.ext")

    assert result.name == "name (1).ext"
    assert calls == ["name.ext", "name (1).ext"]


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX descriptor publication")
def test_output_helper_posix_missing_proc_fd_fails_closed(tmp_path, monkeypatch):
    work = create_work_directory(tmp_path)
    source = work.path / "name.ext"
    source.write_bytes(b"private")
    real_stat = output_paths.os.stat

    def inaccessible_proc(path, *args, **kwargs):
        if os.fspath(path).startswith("/proc/self/fd/"):
            raise PermissionError(errno.EACCES, "procfs unavailable", path)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(output_paths.os, "stat", inaccessible_proc)
    with pytest.raises(OutputPathError, match="Pinned descriptor reference is unavailable"):
        publish_output(work, source, "name.ext")

    assert source.read_bytes() == b"private"
    assert not (tmp_path / "name.ext").exists()
    cleanup_work_directory(work)


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX descriptor publication")
def test_output_helper_posix_mismatched_proc_fd_fails_closed(tmp_path, monkeypatch):
    work = create_work_directory(tmp_path)
    source = work.path / "name.ext"
    source.write_bytes(b"private")
    real_stat = output_paths.os.stat

    def mismatched_proc(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if os.fspath(path).startswith("/proc/self/fd/"):
            return result.__class__(
                (result.st_mode, result.st_ino + 1, result.st_dev) + tuple(result)[3:]
            )
        return result

    monkeypatch.setattr(output_paths.os, "stat", mismatched_proc)
    with pytest.raises(
        OutputPathError, match="Pinned descriptor reference does not identify the source"
    ):
        publish_output(work, source, "name.ext")

    assert source.read_bytes() == b"private"
    assert not (tmp_path / "name.ext").exists()
    cleanup_work_directory(work)


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX descriptor publication")
def test_output_helper_posix_linkat_uses_proc_fd_follow_semantics(tmp_path, monkeypatch):
    work = create_work_directory(tmp_path)
    source = work.path / "name.ext"
    source.write_bytes(b"private")
    source_fd = output_paths._open_pinned_source(work, source)
    destination_fd = os.open(tmp_path, os.O_RDONLY)
    calls = []

    class FakeLinkat:
        def __call__(self, *args):
            calls.append(args)
            return 0

    class FakeLibc:
        linkat = FakeLinkat()

    monkeypatch.setattr(output_paths.ctypes, "CDLL", lambda *_args, **_kwargs: FakeLibc())
    try:
        output_paths._link_pinned_fd(source_fd, destination_fd, "name.ext")
    finally:
        os.close(destination_fd)
        os.close(source_fd)

    assert calls == [
        (-100, f"/proc/self/fd/{source_fd}".encode(), destination_fd, b"name.ext", 0x400)
    ]
    cleanup_work_directory(work)


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX descriptor publication")
def test_output_helper_posix_cleanup_failure_does_not_undo_publication(
    tmp_path, monkeypatch
):
    work = create_work_directory(tmp_path)
    source = work.path / "name.ext"
    source.write_bytes(b"private")

    def cleanup_failure(_work):
        raise OSError("private cleanup failed")

    monkeypatch.setattr(output_paths, "cleanup_work_directory", cleanup_failure)
    result = publish_output(work, source, "name.ext")

    assert result.read_bytes() == b"private"


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX descriptor publication")
@pytest.mark.parametrize("replacement_kind", ["symlink", "regular"])
def test_output_helper_posix_path_replacement_cannot_change_published_inode(
    tmp_path, monkeypatch, replacement_kind
):
    work = create_work_directory(tmp_path)
    source = work.path / "name.ext"
    source.write_bytes(b"validated")
    validated_info = source.stat()
    replacement = tmp_path / "replacement.ext"
    replacement.write_bytes(b"replacement")
    real_link = output_paths._link_pinned_fd

    def replace_path_then_link(source_fd, destination_fd, candidate):
        source.rename(work.path / "validated-source.ext")
        if replacement_kind == "symlink":
            source.symlink_to(replacement)
        else:
            source.write_bytes(b"different regular file")
        real_link(source_fd, destination_fd, candidate)

    monkeypatch.setattr(output_paths, "_link_pinned_fd", replace_path_then_link)
    result = publish_output(work, source, "name.ext")

    result_info = result.stat()
    assert not result.is_symlink()
    assert result.read_bytes() == b"validated"
    assert (result_info.st_dev, result_info.st_ino) == (
        validated_info.st_dev,
        validated_info.st_ino,
    )


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX descriptor publication")
def test_output_helper_posix_unlinked_pinned_source_fails_closed(tmp_path, monkeypatch):
    work = create_work_directory(tmp_path)
    source = work.path / "name.ext"
    source.write_bytes(b"validated")
    replacement = tmp_path / "replacement.ext"
    replacement.write_bytes(b"replacement")
    real_link = output_paths._link_pinned_fd

    def unlink_path_then_link(source_fd, destination_fd, candidate):
        source.unlink()
        source.symlink_to(replacement)
        real_link(source_fd, destination_fd, candidate)

    monkeypatch.setattr(output_paths, "_link_pinned_fd", unlink_path_then_link)
    with pytest.raises(OutputPathError, match="Could not publish output"):
        publish_output(work, source, "name.ext")

    assert source.is_symlink()
    assert not (tmp_path / "name.ext").exists()


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX descriptor publication")
def test_output_helper_posix_rejects_nonregular_pinned_source(tmp_path, monkeypatch):
    work = create_work_directory(tmp_path)
    source = work.path / "name.ext"
    source.write_bytes(b"private")
    directory = work.path / "replacement"
    directory.mkdir()
    monkeypatch.setattr(output_paths, "validate_engine_output", lambda *_args: directory)

    with pytest.raises(OutputPathError, match="Pinned engine output is not a regular file"):
        publish_output(work, source, "name.ext")

    assert source.read_bytes() == b"private"
    assert not (tmp_path / "name.ext").exists()
    cleanup_work_directory(work)


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX descriptor publication")
def test_output_helper_posix_publication_failure_preserves_private_source(
    tmp_path, monkeypatch
):
    work = create_work_directory(tmp_path)
    source = work.path / "name.ext"
    source.write_bytes(b"private")

    def fail_closed(*_args):
        raise OSError(errno.EOPNOTSUPP, "pinned publication unsupported")

    monkeypatch.setattr(output_paths, "_link_pinned_fd", fail_closed)
    with pytest.raises(OutputPathError, match="Could not publish output"):
        publish_output(work, source, "name.ext")

    assert source.read_bytes() == b"private"
    assert not (tmp_path / "name.ext").exists()
    cleanup_work_directory(work)


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX descriptor cleanup")
def test_output_helper_posix_cleanup_removes_descendants_and_preserves_symlink_target(
    tmp_path,
):
    work = create_work_directory(tmp_path)
    nested = work.path / "nested"
    nested.mkdir()
    (nested / "private.bin").write_bytes(b"private")
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "keep.bin"
    target.write_bytes(b"keep")
    (work.path / "escape").symlink_to(outside, target_is_directory=True)

    cleanup_work_directory(work)

    assert not work.path.exists()
    assert target.read_bytes() == b"keep"


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX descriptor cleanup")
def test_output_helper_posix_root_replacement_cannot_redirect_cleanup(
    tmp_path, monkeypatch
):
    work = create_work_directory(tmp_path)
    source = work.path / "name.ext"
    source.write_bytes(b"private")
    pinned_root = tmp_path / "pinned-root"
    real_remove = output_paths._remove_pinned_descendants

    def replace_root_then_remove(directory_fd):
        work.path.rename(pinned_root)
        work.path.mkdir()
        (work.path / "unrelated.bin").write_bytes(b"unrelated")
        real_remove(directory_fd)

    monkeypatch.setattr(
        output_paths, "_remove_pinned_descendants", replace_root_then_remove
    )

    result = publish_output(work, source, "name.ext")

    assert result.read_bytes() == b"private"
    assert not (pinned_root / "name.ext").exists()
    assert (work.path / "unrelated.bin").read_bytes() == b"unrelated"


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX descriptor cleanup")
def test_output_helper_posix_root_identity_mismatch_fails_closed(tmp_path):
    work = create_work_directory(tmp_path)
    (work.path / "private.bin").write_bytes(b"private")
    owned_root = tmp_path / "owned-root"
    work.path.rename(owned_root)
    work.path.mkdir()
    replacement = work.path / "unrelated.bin"
    replacement.write_bytes(b"unrelated")

    with pytest.raises(OutputPathError):
        cleanup_work_directory(work)

    assert (owned_root / "private.bin").read_bytes() == b"private"
    assert replacement.read_bytes() == b"unrelated"


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX descriptor cleanup")
def test_output_helper_posix_child_replacement_is_not_followed(tmp_path, monkeypatch):
    work = create_work_directory(tmp_path)
    child = work.path / "child"
    child.mkdir()
    (child / "private.bin").write_bytes(b"private")
    moved_child = tmp_path / "moved-child"
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "keep.bin"
    target.write_bytes(b"keep")
    real_open = output_paths._open_owned_child_directory
    replaced = False

    def replace_child_then_open(parent_fd, name, device, inode):
        nonlocal replaced
        if name == "child" and not replaced:
            replaced = True
            child.rename(moved_child)
            child.symlink_to(outside, target_is_directory=True)
        return real_open(parent_fd, name, device, inode)

    monkeypatch.setattr(
        output_paths, "_open_owned_child_directory", replace_child_then_open
    )

    cleanup_work_directory(work)

    assert not work.path.exists()
    assert (moved_child / "private.bin").read_bytes() == b"private"
    assert target.read_bytes() == b"keep"


class _FakeWindowsPublicationApi:
    def __init__(self, failures=None):
        self.failures = failures or {}
        self.identities = {}
        self.calls = []
        self.cleanup_calls = []

    def capture_directory_identity(self, path):
        path = Path(path).resolve()
        return self.identities.setdefault(path, (1, len(self.identities) + 1))

    def publish_no_replace(self, work, source_path, candidate):
        self.calls.append(candidate)
        failure = self.failures.get(candidate)
        if failure is not None:
            operation, winerror = failure
            raise output_paths._WindowsApiError(operation, winerror, candidate)
        if (
            work.native_work_identity != self.identities[work.path]
            or work.native_destination_identity != self.identities[work.destination]
        ):
            raise output_paths._WindowsApiError(
                "directory identity validation",
                output_paths._WINDOWS_ERROR_INVALID_HANDLE,
                work.destination,
            )
        target = work.destination / candidate
        if target.exists():
            raise output_paths._WindowsApiError(
                "rename",
                output_paths._WINDOWS_ERROR_ALREADY_EXISTS,
                candidate,
            )
        source_path.rename(target)

    def pin_cleanup_root(self, work):
        self.cleanup_calls.append(work.path)
        failure = self.failures.get("__cleanup__")
        if failure is not None:
            operation, winerror = failure
            raise output_paths._WindowsApiError(operation, winerror, work.path)
        if (
            work.native_work_identity != self.identities.get(work.path)
            or work.native_destination_identity != self.identities.get(work.destination)
        ):
            raise output_paths._WindowsApiError(
                "directory identity validation",
                output_paths._WINDOWS_ERROR_INVALID_HANDLE,
                work.path,
            )
        self._delete_cleanup_children(work.path, work.path)
        self.cleanup_calls.append(("delete", Path(".")))
        work.path.rmdir()

    def _delete_cleanup_children(self, root, directory):
        relative = directory.relative_to(root)
        self.cleanup_calls.append(("enumerate", relative))
        for child in list(directory.iterdir()):
            relative = child.relative_to(root)
            self.cleanup_calls.append(("open", relative))
            if child.is_dir() and not child.is_symlink():
                self._delete_cleanup_children(root, child)
            failure = self.failures.get("__delete__")
            if failure is not None:
                operation, winerror = failure
                raise output_paths._WindowsApiError(operation, winerror, child)
            self.cleanup_calls.append(("delete", relative))
            if child.is_dir() and not child.is_symlink():
                child.rmdir()
            else:
                child.unlink()


def _windows_test_work(tmp_path, monkeypatch, failures=None):
    api = _FakeWindowsPublicationApi(failures)
    monkeypatch.setattr(output_paths, "_is_windows_runtime", lambda: True)
    monkeypatch.setattr(
        output_paths, "_windows_publication_api_factory", lambda: api
    )
    work = create_work_directory(tmp_path)
    source = work.path / "name.ext"
    source.write_bytes(b"private")
    return api, work, source


def test_output_helper_windows_publication_succeeds_without_target(
    tmp_path, monkeypatch
):
    api, work, source = _windows_test_work(tmp_path, monkeypatch)

    result = publish_output(work, source, "name.ext")

    assert result == tmp_path / "name.ext"
    assert result.read_bytes() == b"private"
    assert api.calls == ["name.ext"]
    assert not work.path.exists()


def test_output_helper_windows_collision_preserves_existing_target(
    tmp_path, monkeypatch
):
    api, work, source = _windows_test_work(tmp_path, monkeypatch)
    target = tmp_path / "name.ext"
    target.write_bytes(b"existing")

    result = publish_output(work, source, "name.ext")

    assert result == tmp_path / "name (1).ext"
    assert result.read_bytes() == b"private"
    assert target.read_bytes() == b"existing"
    assert api.calls == ["name.ext", "name (1).ext"]


def test_output_helper_windows_two_collisions_use_second_suffix(
    tmp_path, monkeypatch
):
    api, work, source = _windows_test_work(tmp_path, monkeypatch)
    (tmp_path / "name.ext").write_bytes(b"first")
    (tmp_path / "name (1).ext").write_bytes(b"second")

    result = publish_output(work, source, "name.ext")

    assert result == tmp_path / "name (2).ext"
    assert result.read_bytes() == b"private"
    assert (tmp_path / "name.ext").read_bytes() == b"first"
    assert (tmp_path / "name (1).ext").read_bytes() == b"second"
    assert api.calls == ["name.ext", "name (1).ext", "name (2).ext"]


@pytest.mark.parametrize(
    "winerror",
    [
        output_paths._WINDOWS_ERROR_FILE_EXISTS,
        output_paths._WINDOWS_ERROR_ALREADY_EXISTS,
    ],
)
def test_output_helper_windows_retries_only_file_exists_errors(
    tmp_path, monkeypatch, winerror
):
    api, work, source = _windows_test_work(
        tmp_path,
        monkeypatch,
        {"name.ext": ("rename", winerror)},
    )

    result = publish_output(work, source, "name.ext")

    assert result.name == "name (1).ext"
    assert api.calls == ["name.ext", "name (1).ext"]


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (
            ("rename", output_paths._WINDOWS_ERROR_INVALID_PARAMETER),
            "unexpected Windows API error",
        ),
        (("rename", output_paths._WINDOWS_ERROR_ACCESS_DENIED), "permission"),
        (("rename", output_paths._WINDOWS_ERROR_NOT_SAME_DEVICE), "cross-device"),
        (("rename", output_paths._WINDOWS_ERROR_NOT_SUPPORTED), "unsupported"),
    ],
)
def test_output_helper_windows_noncollision_errors_fail_closed(
    tmp_path, monkeypatch, failure, message
):
    api, work, source = _windows_test_work(
        tmp_path, monkeypatch, {"name.ext": failure}
    )

    with pytest.raises(OutputPathError, match=message):
        publish_output(work, source, "name.ext")

    assert api.calls == ["name.ext"]
    assert source.read_bytes() == b"private"
    assert not (tmp_path / "name.ext").exists()
    cleanup_work_directory(work)


@pytest.mark.parametrize(
    ("winerror", "message"),
    [
        (output_paths._WINDOWS_ERROR_ACCESS_DENIED, "permission"),
        (output_paths._WINDOWS_ERROR_SHARING_VIOLATION, "permission"),
        (output_paths._WINDOWS_ERROR_LOCK_VIOLATION, "permission"),
        (output_paths._WINDOWS_ERROR_INVALID_PARAMETER, "unexpected Windows API error"),
    ],
)
def test_windows_api_error_preserves_raw_code(winerror, message):
    error = output_paths._WindowsApiError("rename", winerror, "name.ext")

    assert error.winerror_code == winerror
    assert message in str(output_paths._windows_output_error(error))


def test_windows_publication_handles_request_relative_rename_access(monkeypatch):
    api = object.__new__(output_paths._WindowsPublicationApi)
    calls = []
    default_share_mode = (
        output_paths._FILE_SHARE_READ
        | output_paths._FILE_SHARE_WRITE
        | output_paths._FILE_SHARE_DELETE
    )

    def open_handle(path, access, *, share_mode=default_share_mode):
        calls.append((path, access, share_mode))
        return len(calls)

    directory_info = SimpleNamespace(
        file_attributes=output_paths._FILE_ATTRIBUTE_DIRECTORY,
        volume_serial_number=1,
        file_index_high=0,
        file_index_low=2,
    )
    source_info = SimpleNamespace(
        file_attributes=0,
        volume_serial_number=1,
        file_index_high=0,
        file_index_low=3,
    )
    monkeypatch.setattr(api, "_open_handle", open_handle)
    monkeypatch.setattr(
        api,
        "_file_information",
        lambda handle, _path: directory_info if handle == 1 else source_info,
    )

    api._open_directory(Path("C:/destination"), None)
    api._open_source(Path("C:/private/source.ext"))

    assert calls == [
        (
            Path("C:/destination"),
            output_paths._FILE_LIST_DIRECTORY
            | output_paths._FILE_ADD_FILE
            | output_paths._FILE_TRAVERSE
            | output_paths._FILE_READ_ATTRIBUTES,
            output_paths._FILE_SHARE_READ
            | output_paths._FILE_SHARE_WRITE
            | output_paths._FILE_SHARE_DELETE,
        ),
        (
            Path("C:/private/source.ext"),
            output_paths._DELETE | output_paths._FILE_READ_ATTRIBUTES,
            output_paths._FILE_SHARE_READ
            | output_paths._FILE_SHARE_WRITE
            | output_paths._FILE_SHARE_DELETE,
        ),
    ]


def test_output_helper_windows_rejects_replaced_destination_identity(
    tmp_path, monkeypatch
):
    api, work, source = _windows_test_work(tmp_path, monkeypatch)
    broken_work = replace(work, native_destination_identity=(99, 99))

    with pytest.raises(OutputPathError, match="invalid or replaced directory handle"):
        publish_output(broken_work, source, "name.ext")

    assert api.calls == ["name.ext"]
    assert source.read_bytes() == b"private"
    cleanup_work_directory(work)


def test_output_helper_windows_rejects_reparse_destination_state(
    tmp_path, monkeypatch
):
    api, work, source = _windows_test_work(
        tmp_path,
        monkeypatch,
        {
            "name.ext": (
                "reparse-point validation",
                output_paths._WINDOWS_ERROR_CANT_ACCESS_FILE,
            )
        },
    )

    with pytest.raises(OutputPathError, match="reparse-point or containment"):
        publish_output(work, source, "name.ext")

    assert api.calls == ["name.ext"]
    assert source.read_bytes() == b"private"
    cleanup_work_directory(work)


def test_output_helper_windows_cleanup_root_replacement_fails_closed(
    tmp_path, monkeypatch
):
    api, work, source = _windows_test_work(tmp_path, monkeypatch)
    owned_root = tmp_path / "owned-root"
    work.path.rename(owned_root)
    work.path.mkdir()
    replacement = work.path / "unrelated.bin"
    replacement.write_bytes(b"unrelated")
    api.identities[work.path] = (99, 99)

    with pytest.raises(OutputPathError, match="invalid or replaced directory handle"):
        cleanup_work_directory(work)

    assert replacement.read_bytes() == b"unrelated"
    assert (owned_root / source.name).read_bytes() == b"private"


def test_output_helper_windows_cleanup_reparse_condition_fails_closed(
    tmp_path, monkeypatch
):
    _api, work, source = _windows_test_work(
        tmp_path,
        monkeypatch,
        {
            "__cleanup__": (
                "reparse-point validation",
                output_paths._WINDOWS_ERROR_CANT_ACCESS_FILE,
            )
        },
    )

    with pytest.raises(OutputPathError, match="reparse-point or containment"):
        cleanup_work_directory(work)

    assert source.read_bytes() == b"private"


def test_windows_cleanup_boundary_requests_recursive_and_root_deletion(
    tmp_path, monkeypatch
):
    destination = tmp_path / "destination"
    destination.mkdir()
    work = create_work_directory(destination)
    private = work.path
    source = private / "private.bin"
    source.write_bytes(b"private")
    work = replace(
        work,
        native_work_identity=(5, 6),
        native_destination_identity=(7, 8),
    )
    api = object.__new__(output_paths._WindowsPublicationApi)
    opened = []
    closed = []
    deleted = []

    def open_directory(path, identity):
        opened.append((path, identity))
        return len(opened), identity

    def open_cleanup_directory(path, identity):
        opened.append((path, identity))
        return len(opened)

    monkeypatch.setattr(api, "_open_directory", open_directory)
    monkeypatch.setattr(api, "_open_cleanup_directory", open_cleanup_directory)
    monkeypatch.setattr(api, "_delete_directory_contents", deleted.append)
    monkeypatch.setattr(api, "_verify_cleanup_empty", lambda _path: None)
    monkeypatch.setattr(
        api, "_delete_handle", lambda handle, path: deleted.append((handle, path))
    )
    monkeypatch.setattr(api, "_close", closed.append)

    api.pin_cleanup_root(work)

    assert opened == [
        (destination, (7, 8)),
        (private, (5, 6)),
    ]
    assert deleted == [private, (2, private)]
    assert closed == [2, 1]


def test_windows_cleanup_descendant_failure_prevents_root_deletion(
    tmp_path, monkeypatch
):
    destination = tmp_path / "destination"
    destination.mkdir()
    work = replace(
        create_work_directory(destination),
        native_work_identity=(5, 6),
        native_destination_identity=(7, 8),
    )
    api = object.__new__(output_paths._WindowsPublicationApi)
    failure = output_paths._WindowsApiError(
        "delete cleanup object",
        output_paths._WINDOWS_ERROR_SHARING_VIOLATION,
        work.path / "blocked.bin",
    )
    deleted = []
    closed = []
    monkeypatch.setattr(api, "_open_directory", lambda _path, identity: (1, identity))
    monkeypatch.setattr(api, "_open_cleanup_directory", lambda _path, _identity: 2)
    monkeypatch.setattr(api, "_delete_directory_contents", lambda _path: None)
    monkeypatch.setattr(api, "_enumerate_children", lambda _path: ["blocked.bin"])
    monkeypatch.setattr(
        api, "_open_cleanup_child", lambda _path: (_ for _ in ()).throw(failure)
    )
    monkeypatch.setattr(api, "_delete_handle", lambda handle, path: deleted.append((handle, path)))
    monkeypatch.setattr(api, "_close", closed.append)

    with pytest.raises(output_paths._WindowsApiError) as caught:
        api.pin_cleanup_root(work)

    assert caught.value is failure
    assert deleted == []
    assert closed == [2, 1]


def test_windows_cleanup_tree_deletes_opened_objects_and_skips_reparse(monkeypatch):
    root = Path("C:/private")
    children = {
        root: ["file.bin", "nested", "escape"],
        root / "nested": ["inner.bin"],
    }
    opened = {
        root / "file.bin": (10, 0),
        root / "nested": (20, output_paths._FILE_ATTRIBUTE_DIRECTORY),
        root / "nested" / "inner.bin": (30, 0),
        root / "escape": (
            40,
            output_paths._FILE_ATTRIBUTE_DIRECTORY
            | output_paths._FILE_ATTRIBUTE_REPARSE_POINT,
        ),
    }
    api = object.__new__(output_paths._WindowsPublicationApi)
    enumerated = []
    deleted = []
    monkeypatch.setattr(
        api,
        "_enumerate_children",
        lambda path: enumerated.append(path) or children[path],
    )
    monkeypatch.setattr(api, "_open_cleanup_child", lambda path: opened[path])
    monkeypatch.setattr(
        api, "_delete_handle", lambda handle, path: deleted.append((handle, path))
    )
    monkeypatch.setattr(api, "_close", lambda _handle: None)

    api._delete_directory_contents(root)

    assert enumerated == [root, root / "nested"]
    assert deleted == [
        (10, root / "file.bin"),
        (30, root / "nested" / "inner.bin"),
        (20, root / "nested"),
        (40, root / "escape"),
    ]


def test_windows_cleanup_root_open_denies_rename_and_validates_identity(monkeypatch):
    api = object.__new__(output_paths._WindowsPublicationApi)
    calls = []
    info = SimpleNamespace(
        file_attributes=output_paths._FILE_ATTRIBUTE_DIRECTORY,
        volume_serial_number=5,
        file_index_high=0,
        file_index_low=6,
    )
    monkeypatch.setattr(
        api,
        "_open_handle",
        lambda path, access, *, share_mode: calls.append(
            (path, access, share_mode)
        ) or 11,
    )
    monkeypatch.setattr(api, "_file_information", lambda _handle, _path: info)
    monkeypatch.setattr(api, "_close", lambda _handle: None)

    handle = api._open_cleanup_directory(Path("C:/private"), (5, 6))

    assert handle == 11
    _, access, share_mode = calls[0]
    assert access & output_paths._FILE_LIST_DIRECTORY
    assert access & output_paths._DELETE
    assert share_mode == (
        output_paths._FILE_SHARE_READ | output_paths._FILE_SHARE_WRITE
    )
    assert not share_mode & output_paths._FILE_SHARE_DELETE


@pytest.mark.parametrize(
    ("attributes", "identity", "message"),
    [
        (
            output_paths._FILE_ATTRIBUTE_DIRECTORY
            | output_paths._FILE_ATTRIBUTE_REPARSE_POINT,
            (5, 6),
            "reparse-point",
        ),
        (output_paths._FILE_ATTRIBUTE_DIRECTORY, (9, 9), "identity"),
    ],
)
def test_windows_cleanup_root_reparse_or_identity_mismatch_fails_closed(
    monkeypatch, attributes, identity, message
):
    api = object.__new__(output_paths._WindowsPublicationApi)
    info = SimpleNamespace(
        file_attributes=attributes,
        volume_serial_number=identity[0],
        file_index_high=0,
        file_index_low=identity[1],
    )
    monkeypatch.setattr(api, "_open_handle", lambda *_args, **_kwargs: 11)
    monkeypatch.setattr(api, "_file_information", lambda _handle, _path: info)
    monkeypatch.setattr(api, "_close", lambda _handle: None)

    with pytest.raises(output_paths._WindowsApiError, match=message):
        api._open_cleanup_directory(Path("C:/private"), (5, 6))


def test_windows_cleanup_deletion_failure_is_reported_and_root_remains(
    tmp_path, monkeypatch
):
    _api, work, source = _windows_test_work(
        tmp_path,
        monkeypatch,
        {"__delete__": ("delete cleanup object", output_paths._WINDOWS_ERROR_ACCESS_DENIED)},
    )

    with pytest.raises(OutputPathError, match="permission"):
        cleanup_work_directory(work)

    assert work.path.exists()
    assert source.read_bytes() == b"private"


def test_windows_fake_cleanup_recurses_and_unlinks_reparse_entry(tmp_path, monkeypatch):
    api, work, source = _windows_test_work(tmp_path, monkeypatch)
    nested = work.path / "nested"
    nested.mkdir()
    (nested / "inner.bin").write_bytes(b"inner")
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "keep.bin"
    target.write_bytes(b"keep")
    (work.path / "escape").symlink_to(outside, target_is_directory=True)

    cleanup_work_directory(work)

    assert not work.path.exists()
    assert target.read_bytes() == b"keep"
    assert ("enumerate", Path("escape")) not in api.cleanup_calls
    assert api.cleanup_calls[-1] == ("delete", Path("."))


def test_output_helper_windows_cleanup_failure_does_not_undo_publication(
    tmp_path, monkeypatch
):
    _api, work, source = _windows_test_work(tmp_path, monkeypatch)

    def cleanup_failure(_work):
        raise OSError("private cleanup failed")

    monkeypatch.setattr(output_paths, "cleanup_work_directory", cleanup_failure)
    result = publish_output(work, source, "name.ext")

    assert result.read_bytes() == b"private"
    assert not source.exists()


def test_output_helper_windows_supports_unicode_canonical_filename(
    tmp_path, monkeypatch
):
    _api, work, source = _windows_test_work(tmp_path, monkeypatch)
    filename = "世界-δοκιμή.ext"

    result = publish_output(work, source, filename)

    assert result == tmp_path / filename
    assert result.read_bytes() == b"private"


@pytest.mark.skipif(os.name != "nt", reason="requires a Windows runtime")
def test_windows_real_publication_file_rename_info_abi(monkeypatch):
    rename_info = output_paths._WindowsFileRenameInfo
    candidate = "世界-δοκιμή.ext"
    destination_directory = "C:\\δοκιμή dir"
    encoded_name = f"{destination_directory}\\{candidate}".encode("utf-16-le")
    observed = {}

    assert output_paths.ctypes.sizeof(output_paths._WindowsFileRenameInfoOptions) == 4
    assert output_paths.ctypes.sizeof(rename_info) == 24
    assert rename_info.replace_if_exists.offset == 0
    assert rename_info.root_directory.offset == 8
    assert rename_info.file_name_length.offset == 16
    assert rename_info.file_name.offset == 20
    assert rename_info.root_directory.offset % output_paths.ctypes.alignment(
        output_paths.wintypes.HANDLE
    ) == 0
    assert rename_info.file_name.offset == (
        rename_info.file_name_length.offset
        + output_paths.ctypes.sizeof(output_paths.wintypes.DWORD)
    )
    assert output_paths.ctypes.sizeof(rename_info) >= (
        rename_info.file_name.offset
        + output_paths.ctypes.sizeof(output_paths.ctypes.c_wchar)
    )

    api = output_paths._WindowsPublicationApi()

    def set_file_information(source_handle, info_class, buffer, buffer_length):
        info = output_paths.ctypes.cast(
            buffer, output_paths.ctypes.POINTER(rename_info)
        ).contents
        observed.update(
            source_handle=source_handle,
            info_class=info_class,
            buffer_length=buffer_length,
            replace_if_exists=info.replace_if_exists,
            root_directory=info.root_directory,
            file_name_length=info.file_name_length,
            encoded_name=output_paths.ctypes.string_at(
                buffer.value + rename_info.file_name.offset,
                info.file_name_length,
            ),
            terminating_nul=output_paths.ctypes.string_at(
                buffer.value + rename_info.file_name.offset + info.file_name_length,
                output_paths.ctypes.sizeof(output_paths.ctypes.c_wchar),
            ),
        )
        return True

    monkeypatch.setattr(api, "_set_file_information", set_file_information)
    api._rename_no_replace(101, destination_directory, candidate)

    assert observed == {
        "source_handle": 101,
        "info_class": output_paths._FILE_RENAME_INFO,
        "buffer_length": output_paths.ctypes.sizeof(rename_info) + len(encoded_name),
        "replace_if_exists": 0,
        "root_directory": None,
        "file_name_length": len(encoded_name),
        "encoded_name": encoded_name,
        "terminating_nul": b"\0" * output_paths.ctypes.sizeof(output_paths.ctypes.c_wchar),
    }


@pytest.mark.skipif(os.name != "nt", reason="requires a Windows runtime")
@pytest.mark.parametrize(
    ("filename", "existing_names", "expected_name"),
    [
        ("name.ext", (), "name.ext"),
        ("name.ext", ("name.ext",), "name (1).ext"),
        ("name.ext", ("name.ext", "name (1).ext"), "name (2).ext"),
        ("世界-δοκιμή.ext", (), "世界-δοκιμή.ext"),
    ],
)
def test_output_helper_windows_real_publication(
    tmp_path, filename, existing_names, expected_name
):
    work = create_work_directory(tmp_path)
    source = work.path / "private.bin"
    source.write_bytes(b"private")
    existing = {}
    for index, name in enumerate(existing_names):
        contents = f"existing-{index}".encode()
        (tmp_path / name).write_bytes(contents)
        existing[name] = contents

    result = publish_output(work, source, filename)

    assert result == tmp_path / expected_name
    assert result.read_bytes() == b"private"
    for name, contents in existing.items():
        assert (tmp_path / name).read_bytes() == contents


@pytest.mark.skipif(os.name != "nt", reason="requires a Windows runtime")
def test_windows_real_publication_collision_preserves_raw_winerror(tmp_path):
    work = create_work_directory(tmp_path)
    source = work.path / "private.bin"
    source.write_bytes(b"private")
    target = tmp_path / "name.ext"
    target.write_bytes(b"existing")

    with pytest.raises(output_paths._WindowsApiError) as caught:
        output_paths._WindowsPublicationApi().publish_no_replace(
            work, source, "name.ext"
        )

    assert caught.value.winerror_code in {
        output_paths._WINDOWS_ERROR_FILE_EXISTS,
        output_paths._WINDOWS_ERROR_ALREADY_EXISTS,
    }
    assert source.read_bytes() == b"private"
    assert target.read_bytes() == b"existing"
    cleanup_work_directory(work)


@pytest.mark.skipif(os.name != "nt", reason="requires a Windows runtime")
def test_output_helper_windows_cleanup_real_tree_and_failure(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "keep.bin"
    target.write_bytes(b"keep")
    work = create_work_directory(tmp_path)
    nested = work.path / "nested"
    nested.mkdir()
    (nested / "private.bin").write_bytes(b"private")
    link = work.path / "escape"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
        check=True,
        capture_output=True,
    )

    cleanup_work_directory(work)

    assert not work.path.exists()
    assert target.read_bytes() == b"keep"

    blocked = create_work_directory(tmp_path)
    blocked_file = blocked.path / "blocked.bin"
    blocked_file.write_bytes(b"blocked")
    api = output_paths._WindowsPublicationApi()
    # An attributes-only open is exempt from Windows share arbitration, so it
    # never blocks a delete; it only defers one.  Request real data access so the
    # handle genuinely denies DELETE while it is held.
    blocker = api._open_handle(
        blocked_file,
        output_paths._FILE_READ_DATA | output_paths._FILE_READ_ATTRIBUTES,
        share_mode=(
            output_paths._FILE_SHARE_READ | output_paths._FILE_SHARE_WRITE
        ),
    )
    try:
        with pytest.raises(OutputPathError, match="permission"):
            cleanup_work_directory(blocked)
        assert blocked.path.exists()
    finally:
        api._close(blocker)
    # The blocking handle denied FILE_SHARE_DELETE, so the file could only be
    # inspected once it was closed.  A failed cleanup must not have left it
    # delete-pending.
    assert blocked_file.exists()
    assert blocked_file.read_bytes() == b"blocked"
    cleanup_work_directory(blocked)


# --- publication beyond MAX_PATH -------------------------------------------
#
# A review raised that _win32_path stripping the \\?\ prefix could break
# publication to destinations longer than MAX_PATH (260). Native probing
# settled it: the silent no-clobber violation came from an undersized
# FILE_RENAME_INFO buffer, not from the prefix. With the correct
# ctypes.sizeof() header the \\?\ form renames correctly and still reports
# collisions, so the prefix is now preserved and publication no longer depends
# on LongPathsEnabled or interpreter long-path awareness.

_WINDOWS_LONG_PATH_PREFIX = "\\\\?\\"


def _windows_long_destination(tmp_path):
    r"""A destination directory whose path exceeds 260 UTF-16 code units.

    Created through the \\?\ form, which reaches the object manager directly
    and therefore works regardless of the LongPathsEnabled machine policy. The
    representation handed to the code under test is the plain path when this
    runtime can resolve it, and the extended-length path otherwise - the test
    must exercise real publication either way, never skip.
    """
    destination = tmp_path
    while len(str(destination)) <= 260:
        destination = destination / ("d" * 50)
    os.makedirs(_WINDOWS_LONG_PATH_PREFIX + str(destination), exist_ok=True)
    try:
        destination.stat()
    except OSError:  # pragma: no cover - depends on machine policy
        return Path(_WINDOWS_LONG_PATH_PREFIX + str(destination))
    return destination


@pytest.mark.skipif(os.name != "nt", reason="requires a Windows runtime")
def test_output_helper_windows_real_publication_long_path(tmp_path, monkeypatch):
    destination = _windows_long_destination(tmp_path)
    assert len(str(destination)) > 260

    observed = []
    rename_info = output_paths._WindowsFileRenameInfo
    real_factory = output_paths._windows_publication_api_factory

    def observing_factory():
        api = real_factory()
        real_set = api._set_file_information

        def observing_set(handle, info_class, buffer, buffer_length):
            # Not a mock: the real SetFileInformationByHandle still performs the
            # rename. This only records what the production code handed it.
            if info_class == output_paths._FILE_RENAME_INFO:
                info = output_paths.ctypes.cast(
                    buffer, output_paths.ctypes.POINTER(rename_info)
                ).contents
                name = output_paths.ctypes.string_at(
                    buffer.value + rename_info.file_name.offset,
                    info.file_name_length,
                ).decode("utf-16-le")
                observed.append(
                    {
                        "name": name,
                        "root_directory": info.root_directory,
                        "replace_if_exists": info.replace_if_exists,
                        "buffer_length": buffer_length,
                        "expected_length": (
                            output_paths.ctypes.sizeof(rename_info)
                            + info.file_name_length
                        ),
                        "undersized_length": (
                            rename_info.file_name.offset + info.file_name_length
                        ),
                    }
                )
            return real_set(handle, info_class, buffer, buffer_length)

        api._set_file_information = observing_set
        return api

    monkeypatch.setattr(
        output_paths, "_windows_publication_api_factory", observing_factory
    )

    def publish(filename, payload):
        work = create_work_directory(destination)
        source = work.path / "private.bin"
        source.write_bytes(payload)
        published = publish_output(work, source, filename)
        # The private copy must be gone, not merely superseded.
        assert not source.exists()
        assert not work.path.exists()
        return published

    first = publish("name.ext", b"first")
    assert first == destination / "name.ext"
    assert len(str(first)) > 260
    assert first.read_bytes() == b"first"

    # Collisions must still resolve deterministically and never overwrite.
    second = publish("name.ext", b"second")
    assert second == destination / "name (1).ext"
    assert second.read_bytes() == b"second"
    assert first.read_bytes() == b"first"

    third = publish("name.ext", b"third")
    assert third == destination / "name (2).ext"
    assert third.read_bytes() == b"third"
    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"second"

    unicode_published = publish("世界-δοκιμή.ext", b"unicode")
    assert unicode_published == destination / "世界-δοκιμή.ext"
    assert len(str(unicode_published)) > 260
    assert unicode_published.read_bytes() == b"unicode"

    # Every rename the production code issued kept the extended-length prefix,
    # used a NULL RootDirectory, refused replacement, and sized its buffer from
    # the complete ctypes.sizeof() header.
    assert observed
    for record in observed:
        assert record["name"].startswith(_WINDOWS_LONG_PATH_PREFIX)
        assert len(record["name"]) > 260
        assert record["root_directory"] is None
        assert record["replace_if_exists"] == 0
        assert record["buffer_length"] == record["expected_length"]
        assert record["buffer_length"] != record["undersized_length"]

    # A collision on a >260 destination must surface the raw Windows error.
    collision_work = create_work_directory(destination)
    collision_source = collision_work.path / "private.bin"
    collision_source.write_bytes(b"collision")
    with pytest.raises(output_paths._WindowsApiError) as caught:
        observing_factory().publish_no_replace(
            collision_work, collision_source, "name.ext"
        )
    assert caught.value.winerror_code in {
        output_paths._WINDOWS_ERROR_FILE_EXISTS,
        output_paths._WINDOWS_ERROR_ALREADY_EXISTS,
    }
    assert first.read_bytes() == b"first"
    assert collision_source.read_bytes() == b"collision"
    cleanup_work_directory(collision_work)

    # pytest's own tmp_path reaper cannot always remove a >260 tree.
    shutil.rmtree(_WINDOWS_LONG_PATH_PREFIX + str(tmp_path), ignore_errors=True)


@pytest.mark.skipif(os.name != "nt", reason="requires a Windows runtime")
def test_windows_rename_buffer_uses_complete_structure_header(monkeypatch):
    """The header must be ctypes.sizeof(), not FileName.offset.

    The undersized form leaves no room for the trailing FileName element and
    silently breaks no-clobber renames.
    """
    rename_info = output_paths._WindowsFileRenameInfo
    destination_directory = r"\\?\C:\folder"
    candidate = "name.ext"
    encoded_name = f"{destination_directory}\\{candidate}".encode("utf-16-le")
    seen = {}

    api = output_paths._WindowsPublicationApi()

    def set_file_information(handle, info_class, buffer, buffer_length):
        seen["buffer_length"] = buffer_length
        return True

    monkeypatch.setattr(api, "_set_file_information", set_file_information)
    api._rename_no_replace(101, destination_directory, candidate)

    assert seen["buffer_length"] == (
        output_paths.ctypes.sizeof(rename_info) + len(encoded_name)
    )
    assert seen["buffer_length"] != rename_info.file_name.offset + len(encoded_name)
    assert rename_info.file_name.offset < output_paths.ctypes.sizeof(rename_info)


@pytest.mark.skipif(os.name != "nt", reason="requires a Windows runtime")
@pytest.mark.parametrize(
    ("final_path", "expected"),
    [
        # The extended-length representation is preserved, never downgraded.
        (r"\\?\C:\folder\file.ext", r"\\?\C:\folder\file.ext"),
        (r"\\?\UNC\server\share\file.ext", r"\\?\UNC\server\share\file.ext"),
        (r"C:\folder\file.ext", r"C:\folder\file.ext"),
    ],
)
def test_windows_win32_path_preserves_final_path_forms(final_path, expected):
    assert output_paths._WindowsPublicationApi._win32_path(final_path) == expected


# ---------------------------------------------------------------------------
# Diagnostics: sanitized evidence for the three reported incidents.
#
# These tests assert on what is *recorded*. Every existing assertion about
# what the queue *does* stays untouched: diagnostics may not change routing,
# validation, error text or task outcomes.
# ---------------------------------------------------------------------------

from cove import diagnostics as diag_module  # noqa: E402
from tests.test_diagnostics import assert_clean as _assert_clean  # noqa: E402


@pytest.fixture
def diag(tmp_path):
    diag_module.shutdown_logger()
    log = diag_module.init_app_logger(tmp_path / "diag")
    yield log
    diag_module.shutdown_logger()


def _events(log, component=None, event=None):
    out = []
    for record in log.records():
        if component is not None and record["component"] != component:
            continue
        if event is not None and record["event"] != event:
            continue
        out.append(record)
    return out


def _one(log, component, event):
    found = _events(log, component, event)
    assert len(found) == 1, "expected one {}/{}, got {}".format(
        component, event, len(found)
    )
    return found[0]


# ---- Incident A: extractor publication ------------------------------------


def _reject_extractor_output(queue, fake_process, tmp_path):
    """Report a final path inside the work directory that does not exist.

    This is the shape of the Windows report: yt-dlp names a file under
    .cove-work-*, validation resolves it strictly, and the missing file turns
    into OutputPathError("Invalid engine output path") from FileNotFoundError.
    """
    task, proc = _start_extractor(queue, fake_process, tmp_path)
    private = _extractor_private_path(proc)
    proc.emit_output(f"{FINAL_PATH_MARKER}{private}\n")
    proc.finish(0)
    return task


def test_extractor_publication_logs_a_begin_event(queue_env, fake_process, tmp_path, diag):
    queue, _rpc, _db = queue_env()
    task = _reject_extractor_output(queue, fake_process, tmp_path)
    begin = _one(diag, "extractor.publish", "publish_begin")
    assert begin["task"] == task.id
    assert begin["fields"]["engine"] == "yt-dlp"


def test_engine_output_rejection_records_the_rule_that_rejected_it(
    queue_env, fake_process, tmp_path, diag
):
    queue, _rpc, _db = queue_env()
    _reject_extractor_output(queue, fake_process, tmp_path)
    rejected = _one(diag, "extractor.publish", "engine_output_rejected")
    assert rejected["level"] == "ERROR"
    # A file that is simply absent is not the same failure as a path we refuse
    # to touch; support needs to tell those two apart.
    assert rejected["fields"]["rule"] == "engine_output_missing"


def test_engine_output_rejection_records_safe_path_facts(
    queue_env, fake_process, tmp_path, diag
):
    queue, _rpc, _db = queue_env()
    _reject_extractor_output(queue, fake_process, tmp_path)
    fields = _one(diag, "extractor.publish", "engine_output_rejected")["fields"]
    assert fields["absolute"] is True
    assert fields["exists"] is False
    assert fields["within_expected_root"] is True
    assert fields["same_drive"] is True
    assert fields["stage"] == "validate_engine_output"
    assert "drive" in fields
    assert "is_file" in fields


def test_engine_output_rejection_hides_the_user_and_the_work_id(
    queue_env, fake_process, tmp_path, diag
):
    queue, _rpc, _db = queue_env()
    _reject_extractor_output(queue, fake_process, tmp_path)
    dumped = json.dumps(diag.records())
    assert ".cove-work-<work-id>" in dumped
    assert ".cove-work-" + "abc" not in dumped
    for record in diag.records():
        path = (record.get("fields") or {}).get("path", "")
        assert "cove-work-" not in path or "<work-id>" in path
    _assert_clean(dumped)
    assert str(tmp_path) not in dumped


def test_engine_output_rejection_records_the_exception_chain(
    queue_env, fake_process, tmp_path, diag
):
    queue, _rpc, _db = queue_env()
    _reject_extractor_output(queue, fake_process, tmp_path)
    rejected = _one(diag, "extractor.publish", "engine_output_rejected")
    assert rejected["exc"]["type"] == "MissingEngineOutputError"
    assert rejected["exc"]["cause"] == "FileNotFoundError"
    assert rejected["exc"]["errno"] == errno.ENOENT
    assert "winerror" in rejected["exc"]


def test_work_directory_cleanup_result_is_recorded(
    queue_env, fake_process, tmp_path, diag
):
    queue, _rpc, _db = queue_env()
    _reject_extractor_output(queue, fake_process, tmp_path)
    cleanup = _events(diag, "extractor.publish", "work_cleanup")
    assert cleanup, "cleanup result must be observable"
    assert cleanup[-1]["fields"]["result"] in {"success", "failure"}


def test_publication_failure_emits_a_task_failed_event(
    queue_env, fake_process, tmp_path, diag
):
    queue, _rpc, _db = queue_env()
    task = _reject_extractor_output(queue, fake_process, tmp_path)
    failed = _one(diag, "queue", "task_failed")
    assert failed["task"] == task.id
    assert failed["level"] == "ERROR"


def test_publication_failure_outcome_is_unchanged_by_diagnostics(
    queue_env, fake_process, tmp_path, diag
):
    queue, _rpc, _db = queue_env()
    task = _reject_extractor_output(queue, fake_process, tmp_path)
    assert task.status == "error"
    assert task.error.startswith("Could not publish extractor output:")
    assert task.finished_at is not None
    _assert_no_work_dirs(tmp_path)


def test_successful_publication_logs_success_not_rejection(
    queue_env, fake_process, tmp_path, diag
):
    queue, _rpc, _db = queue_env()
    task, proc = _start_extractor(queue, fake_process, tmp_path)
    private = _extractor_private_path(proc)
    private.write_bytes(b"extractor output")
    proc.emit_output(f"{FINAL_PATH_MARKER}{private}\n")
    proc.finish(0)

    assert task.status == "completed"
    assert _events(diag, "extractor.publish", "engine_output_rejected") == []
    assert _one(diag, "extractor.publish", "publish_success")["task"] == task.id


# ---- Windows publication fallback -----------------------------------------
#
# yt-dlp exits 0 and prints a final path inside Cove's own work directory, but
# on Windows that exact file is sometimes not the one left on disk. When the
# work directory holds exactly one legitimate finished file, publish that
# instead of failing an otherwise complete download.


def _finish_extractor_with_missing_report(queue, fake_process, tmp_path, filename="movie.mp4"):
    task, proc = _start_extractor(queue, fake_process, tmp_path, filename)
    private = _extractor_private_path(proc)
    work_path = private.parent
    return task, proc, private, work_path


def test_extractor_publishes_the_single_legitimate_output_when_the_report_is_missing(
    queue_env, fake_process, tmp_path
):
    queue, _rpc, _db = queue_env()
    task, proc, private, work_path = _finish_extractor_with_missing_report(
        queue, fake_process, tmp_path
    )
    actual = work_path / "movie.mkv"
    actual.write_bytes(b"merged output")

    proc.emit_output(f"{FINAL_PATH_MARKER}{private}\n")
    proc.finish(0)

    assert task.status == "completed"
    assert task.filename == "movie.mkv"
    assert (tmp_path / "movie.mkv").read_bytes() == b"merged output"
    assert not (tmp_path / "movie.mp4").exists()
    _assert_no_work_dirs(tmp_path)


def test_extractor_fallback_ignores_yt_dlp_intermediate_artifacts(
    queue_env, fake_process, tmp_path
):
    queue, _rpc, _db = queue_env()
    task, proc, private, work_path = _finish_extractor_with_missing_report(
        queue, fake_process, tmp_path
    )
    (work_path / "movie.mkv").write_bytes(b"merged output")
    (work_path / "movie.f137.mp4.part").write_bytes(b"video fragment")
    (work_path / "movie.f251.webm").write_bytes(b"audio fragment")
    (work_path / "movie.ytdl").write_bytes(b"resume state")
    (work_path / "movie.temp").write_bytes(b"remux scratch")

    proc.emit_output(f"{FINAL_PATH_MARKER}{private}\n")
    proc.finish(0)

    assert task.status == "completed"
    assert task.filename == "movie.mkv"
    assert (tmp_path / "movie.mkv").read_bytes() == b"merged output"
    _assert_no_work_dirs(tmp_path)


def test_extractor_fallback_fails_closed_when_two_outputs_are_plausible(
    queue_env, fake_process, tmp_path
):
    queue, _rpc, _db = queue_env()
    task, proc, private, work_path = _finish_extractor_with_missing_report(
        queue, fake_process, tmp_path
    )
    (work_path / "a.mp4").write_bytes(b"one")
    (work_path / "b.mkv").write_bytes(b"two")

    proc.emit_output(f"{FINAL_PATH_MARKER}{private}\n")
    proc.finish(0)

    assert task.status == "error"
    assert task.error.startswith("Could not publish extractor output:")
    assert list(tmp_path.glob("*.mp4")) == []
    assert list(tmp_path.glob("*.mkv")) == []
    _assert_no_work_dirs(tmp_path)


def test_extractor_fallback_fails_closed_when_the_work_directory_is_empty(
    queue_env, fake_process, tmp_path
):
    queue, _rpc, _db = queue_env()
    task, proc, private, _work_path = _finish_extractor_with_missing_report(
        queue, fake_process, tmp_path
    )

    proc.emit_output(f"{FINAL_PATH_MARKER}{private}\n")
    proc.finish(0)

    assert task.status == "error"
    _assert_no_work_dirs(tmp_path)


def test_extractor_fallback_does_not_run_for_a_path_outside_the_work_directory(
    queue_env, fake_process, tmp_path
):
    queue, _rpc, _db = queue_env()
    task, proc, _private, work_path = _finish_extractor_with_missing_report(
        queue, fake_process, tmp_path
    )
    (work_path / "movie.mkv").write_bytes(b"merged output")
    outside = tmp_path / "outside.mkv"

    proc.emit_output(f"{FINAL_PATH_MARKER}{outside}\n")
    proc.finish(0)

    assert task.status == "error"
    assert not (tmp_path / "movie.mkv").exists()
    _assert_no_work_dirs(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_extractor_fallback_rejects_a_symlinked_candidate(
    queue_env, fake_process, tmp_path
):
    queue, _rpc, _db = queue_env()
    task, proc, private, work_path = _finish_extractor_with_missing_report(
        queue, fake_process, tmp_path
    )
    real = tmp_path / "real.mkv"
    real.write_bytes(b"outside payload")
    (work_path / "movie.mkv").symlink_to(real)

    proc.emit_output(f"{FINAL_PATH_MARKER}{private}\n")
    proc.finish(0)

    assert task.status == "error"
    assert real.read_bytes() == b"outside payload"
    _assert_no_work_dirs(tmp_path)


def test_extractor_fallback_accepts_a_marker_split_across_output_chunks(
    queue_env, fake_process, tmp_path
):
    queue, _rpc, _db = queue_env()
    task, proc, private, work_path = _finish_extractor_with_missing_report(
        queue, fake_process, tmp_path
    )
    (work_path / "movie.mkv").write_bytes(b"merged output")
    reported = f"{FINAL_PATH_MARKER}{private}\r\n"
    half = len(reported) // 2

    proc.emit_output(reported[:half])
    proc.emit_output(reported[half:])
    proc.finish(0)

    assert task.status == "completed"
    assert task.filename == "movie.mkv"


def test_extractor_fallback_handles_spaces_brackets_and_unicode_names(
    queue_env, fake_process, tmp_path
):
    queue, _rpc, _db = queue_env()
    requested = "Ocean Waves [1080p] 海.mp4"
    task, proc, private, work_path = _finish_extractor_with_missing_report(
        queue, fake_process, tmp_path, requested
    )
    (work_path / "Ocean Waves [1080p] 海.mkv").write_bytes(b"merged output")

    proc.emit_output(f"{FINAL_PATH_MARKER}{private}\n")
    proc.finish(0)

    assert task.status == "completed"
    assert task.filename == "Ocean Waves [1080p] 海.mkv"
    assert (tmp_path / "Ocean Waves [1080p] 海.mkv").exists()


def test_an_unsafe_engine_output_path_keeps_its_own_classification(
    queue_env, fake_process, tmp_path, diag
):
    queue, _rpc, _db = queue_env()
    task, proc, _private, _work_path = _finish_extractor_with_missing_report(
        queue, fake_process, tmp_path
    )

    proc.emit_output(f"{FINAL_PATH_MARKER}{tmp_path / 'outside.mp4'}\n")
    proc.finish(0)

    assert task.status == "error"
    rejected = _one(diag, "extractor.publish", "engine_output_rejected")
    assert rejected["fields"]["rule"] == "outside_private_directory"


def test_work_directory_shape_is_recorded_without_private_names(
    queue_env, fake_process, tmp_path, diag
):
    queue, _rpc, _db = queue_env()
    task, proc, private, work_path = _finish_extractor_with_missing_report(
        queue, fake_process, tmp_path
    )
    (work_path / "a.mp4").write_bytes(b"one")
    (work_path / "b.mkv").write_bytes(b"two")
    (work_path / "movie.ytdl").write_bytes(b"resume state")

    proc.emit_output(f"{FINAL_PATH_MARKER}{private}\n")
    proc.finish(0)

    shape = _one(diag, "extractor.publish", "work_shape")
    fields = shape["fields"]
    assert shape["task"] == task.id
    assert fields["entries"] == 3
    assert fields["candidates"] == 2
    assert fields["reported_exists"] is False
    assert fields["single_candidate"] is False
    assert sorted(fields["exts"]) == [".mkv", ".mp4", ".ytdl"]
    # Shape only: no name from inside the private directory, no user path.
    dumped = json.dumps(shape)
    assert "movie" not in dumped
    assert "a.mp4" not in dumped
    assert "b.mkv" not in dumped
    assert str(tmp_path) not in dumped


# ---- Incident C: Real-Debrid generated /d/ link ----------------------------


def test_url_intake_is_classified_without_the_token(queue_env, diag):
    queue, _rpc, _db = queue_env()
    tid = queue.add_url(SHARE_URL, intake="manual")
    added = _one(diag, "queue", "url_added")
    assert added["task"] == tid
    assert added["fields"]["intake"] == "manual"
    assert added["fields"]["scheme"] == "https"
    assert added["fields"]["host"] == "real-debrid.com"
    assert added["fields"]["classification"] == "real_debrid_generated_link"
    assert added["fields"]["backend"] == "aria2"
    assert "ALJRILITCGUEW127" not in json.dumps(diag.records())


def test_rd_share_link_is_resolved_when_account_is_configured(queue_env, monkeypatch, diag):
    queue, rpc, _db = queue_env(
        real_debrid_enabled=True, real_debrid_api_token="rd-token-value"
    )
    monkeypatch.setattr(
        debrid, "resolve",
        lambda url, settings, **kw: Unrestricted(NODE_URL, "movie.mkv", 4096, REAL_DEBRID),
    )
    _sync_spawn(queue)
    tid = queue.add_url(SHARE_URL)
    task = queue.tasks[tid]
    queue._launch(task)

    assert task.status == "paused"  # auto-paused: the fixture disables the scheduler
    assert task.url == SHARE_URL
    assert task.debrid_provider == REAL_DEBRID
    assert task.resolved_url == NODE_URL
    assert rpc.added[0]["uris"] == [NODE_URL]
    assert _events(diag, "debrid", "share_link_rejected") == []
    add = _one(diag, "aria2", "add")
    assert add["fields"]["target"] == "debrid_delivery_link"
    assert add["fields"]["provider"] == "real_debrid"


def test_rd_share_link_resolution_keeps_no_secret_in_diag(queue_env, monkeypatch, diag):
    queue, _rpc, _db = queue_env(
        real_debrid_enabled=True, real_debrid_api_token="rd-token-value"
    )
    monkeypatch.setattr(
        debrid, "resolve",
        lambda url, settings, **kw: Unrestricted(NODE_URL, "movie.mkv", 4096, REAL_DEBRID),
    )
    _sync_spawn(queue)
    tid = queue.add_url(SHARE_URL)
    queue._launch(queue.tasks[tid])

    dumped = json.dumps(diag.records())
    assert "rd-token-value" not in dumped
    assert "ALJRILITCGUEW127" not in dumped
    assert "SECRETNODE" not in dumped
    _assert_clean(dumped)


def test_rd_share_link_persists_the_original_url_only(queue_env, monkeypatch):
    queue, _rpc, db_path = queue_env(
        real_debrid_enabled=True, real_debrid_api_token="rd-token-value"
    )
    monkeypatch.setattr(
        debrid, "resolve",
        lambda url, settings, **kw: Unrestricted(NODE_URL, "movie.mkv", 4096, REAL_DEBRID),
    )
    _sync_spawn(queue)
    tid = queue.add_url(SHARE_URL)
    queue._launch(queue.tasks[tid])

    row = _persisted_row(db_path, tid)
    assert row["url"] == SHARE_URL
    row_text = _persisted_row_text(db_path, tid)
    assert "SECRETNODE" not in row_text
    assert NODE_URL not in row_text


def test_rd_share_link_with_rd_enabled_but_no_token_fails_with_readable_error(
    queue_env, monkeypatch, diag
):
    queue, _rpc, _db = queue_env(real_debrid_enabled=True)
    _sync_spawn(queue)
    tid = queue.add_url(SHARE_URL)
    queue._launch(queue.tasks[tid])

    task = queue.tasks[tid]
    assert task.status == "error"
    assert "no API key" in task.error
    assert _events(diag, "debrid", "share_link_rejected") == []


def test_alldebrid_share_link_is_still_rejected_with_alldebrid_enabled(
    queue_env, monkeypatch, diag
):
    queue, _rpc, _db = queue_env(
        all_debrid_enabled=True, all_debrid_api_key="ad-key-value"
    )
    tid = queue.add_url("https://alldebrid.com/f/XYZ789")
    monkeypatch.setattr(queue, "_spawn", lambda fn, *a, **kw: None)
    queue._launch(queue.tasks[tid])

    task = queue.tasks[tid]
    assert task.status == "error"
    assert "AllDebrid" in task.error
    fields = _one(diag, "debrid", "share_link_rejected")["fields"]
    assert fields["provider"] == "all_debrid"
    assert fields["resolver"] == "unsupported_share_link"


def test_share_link_rejection_reports_unauthenticated_state(
    queue_env, monkeypatch, diag
):
    queue, _rpc, _db = queue_env()
    tid = queue.add_url(SHARE_URL)
    monkeypatch.setattr(queue, "_spawn", lambda fn, *a, **kw: None)
    queue._launch(queue.tasks[tid])

    fields = _one(diag, "debrid", "share_link_rejected")["fields"]
    assert fields["rd_enabled"] is False
    assert fields["rd_authenticated"] is False


def test_share_link_rejection_emits_task_failed_and_keeps_the_error_text(
    queue_env, monkeypatch, diag
):
    queue, _rpc, _db = queue_env()
    tid = queue.add_url(SHARE_URL)
    monkeypatch.setattr(queue, "_spawn", lambda fn, *a, **kw: None)
    queue._launch(queue.tasks[tid])

    task = queue.tasks[tid]
    assert task.status == "error"
    assert "Real-Debrid" in task.error
    assert _one(diag, "queue", "task_failed")["task"] == tid


def test_delivery_link_is_classified_separately_from_a_share_link(queue_env, diag):
    queue, _rpc, _db = queue_env()
    queue.add_url("https://sg5.download.real-debrid.com/d/TOKENTOKENTOKEN/video.mp4")
    added = _one(diag, "queue", "url_added")
    assert added["fields"]["classification"] == "debrid_delivery_link"
    assert added["fields"]["host"] == "<redacted>.download.real-debrid.com"
    assert "TOKENTOKENTOKEN" not in json.dumps(diag.records())


# ---- aria2 ----------------------------------------------------------------


def test_aria2_add_records_the_gid_and_no_url(queue_env, monkeypatch, diag):
    queue, _rpc, _db = queue_env()
    monkeypatch.setattr(
        "requests.Session.head",
        lambda self, url, **kw: SimpleNamespace(ok=True, headers={"Content-Length": "5"}),
    )
    tid = queue.add_url("https://example.com/video.mp4")
    queue._probe_and_add(queue.tasks[tid])

    add = _one(diag, "aria2", "add")
    assert add["task"] == tid
    assert add["fields"]["gid"] == "gid-1"
    dumped = json.dumps(diag.records())
    assert "example.com/video.mp4" not in dumped


def test_aria2_final_error_records_a_safe_code(queue_env, diag):
    queue, _rpc, _db = queue_env()
    tid = queue.add_url("https://example.com/video.mp4")
    queue.tasks[tid].gid = "gid-1"
    queue._apply_status(tid, {"status": "error", "errorCode": "3",
                              "errorMessage": "Resource not found"})

    result = _one(diag, "aria2", "final_error")
    assert result["task"] == tid
    assert result["fields"]["code"] == "3"
    assert _one(diag, "queue", "task_failed")["task"] == tid


def test_aria2_final_success_is_recorded_once(queue_env, diag):
    queue, _rpc, _db = queue_env()
    tid = queue.add_url("https://example.com/video.mp4")
    queue.tasks[tid].gid = "gid-1"
    queue._apply_status(tid, {"status": "complete"})
    queue._apply_status(tid, {"status": "complete"})

    assert len(_events(diag, "aria2", "final_success")) == 1
    assert _one(diag, "queue", "task_completed")["task"] == tid


def test_progress_polls_are_not_logged(queue_env, diag):
    queue, _rpc, _db = queue_env()
    tid = queue.add_url("https://example.com/video.mp4")
    queue.tasks[tid].gid = "gid-1"
    before = len(diag.records())
    for _ in range(20):
        queue._apply_status(tid, {"status": "active", "completedLength": "10",
                                  "totalLength": "100", "downloadSpeed": "5"})
    assert len(diag.records()) == before


def test_task_launched_is_recorded_with_the_backend(queue_env, diag):
    queue, _rpc, _db = queue_env()
    tid = queue.add_url("https://example.com/video.mp4")
    queue._launch(queue.tasks[tid])
    launched = _one(diag, "queue", "task_launched")
    assert launched["task"] == tid
    assert launched["fields"]["backend"] == "aria2"


def test_diagnostics_failures_never_fail_a_download(queue_env, monkeypatch, diag):
    monkeypatch.setattr(
        diag_module, "emit",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("diagnostics exploded")),
    )
    queue, rpc, _db = queue_env()
    monkeypatch.setattr(
        "requests.Session.head",
        lambda self, url, **kw: SimpleNamespace(ok=True, headers={"Content-Length": "5"}),
    )
    tid = queue.add_url("https://example.com/video.mp4")
    assert tid is not None
    queue._launch(queue.tasks[tid])
    queue._probe_and_add(queue.tasks[tid])
    assert rpc.added, "the download must still reach aria2"


# ---- Removing an unfinished aria2 download ---------------------------------
#
# Removing an unfinished download used to drop the row and leave the partial
# file and its .aria2 resume data on disk forever - unresumable, because the
# only thing that knew about them was the row just deleted. Explicit removal
# now means "abandon this download and clean up after it". Pause, errors and
# shutdown still keep everything, and a finished file is never touched without
# an explicit delete.


def _aria2_task(queue_env, tmp_path, filename="Show.S01E01.mkv"):
    queue, rpc, db_path = queue_env()
    _sync_spawn(queue)
    _running(queue)
    tid = queue.add_url(
        "https://example.invalid/" + filename,
        out_dir=str(tmp_path),
        filename=filename,
    )
    queue._launch(queue.tasks[tid])
    payload = tmp_path / filename
    control = tmp_path / (filename + ".aria2")
    payload.write_bytes(b"partial payload")
    control.write_bytes(b"aria2 resume state")
    return queue, rpc, db_path, tid, payload, control


def test_removing_an_active_aria2_download_cleans_its_partial_data(
    queue_env, tmp_path
):
    queue, rpc, db_path, tid, payload, control = _aria2_task(queue_env, tmp_path)
    queue.tasks[tid].status = "active"

    queue.remove(tid)

    assert rpc.removed == ["gid-1"]
    assert not payload.exists()
    assert not control.exists()
    assert _rows(db_path) == []


def test_removing_a_queued_aria2_download_that_never_started_is_safe(
    queue_env, tmp_path
):
    queue, _rpc, db_path = queue_env()
    tid = queue.add_url(
        "https://example.invalid/never.bin",
        out_dir=str(tmp_path),
        filename="never.bin",
    )

    queue.remove(tid)

    assert _rows(db_path) == []
    assert tid not in queue.tasks


def test_aria2_partial_data_is_unlinked_only_after_aria2_forgets_the_gid(
    queue_env, tmp_path
):
    queue, rpc, db_path, tid, payload, control = _aria2_task(queue_env, tmp_path)
    queue.tasks[tid].status = "active"
    order = []
    deferred = []

    def spawn(fn, *args, on_done=None, on_fail=None, **kwargs):
        if getattr(fn, "__name__", "") == "remove":
            deferred.append((fn, args, on_done))
            return
        result = fn(*args, **kwargs)
        if on_done is not None:
            on_done(result)

    queue._spawn = spawn
    queue.remove(tid)

    # aria2 has not answered yet, so nothing may be unlinked.
    assert payload.exists()
    assert control.exists()
    fn, args, on_done = deferred[0]
    order.append("rpc_remove")
    on_done(fn(*args))
    order.append("unlinked")

    assert order == ["rpc_remove", "unlinked"]
    assert not payload.exists()
    assert not control.exists()


def test_pausing_an_aria2_download_keeps_its_partial_data(queue_env, tmp_path):
    queue, _rpc, _db, tid, payload, control = _aria2_task(queue_env, tmp_path)
    queue.tasks[tid].status = "active"

    queue.pause(tid)

    assert payload.exists()
    assert control.exists()

    queue.remove(tid)

    assert not payload.exists()
    assert not control.exists()


def test_an_errored_aria2_download_keeps_its_data_until_removed(queue_env, tmp_path):
    queue, _rpc, _db, tid, payload, control = _aria2_task(queue_env, tmp_path)
    queue._apply_status(tid, {"status": "error", "errorCode": "1"})

    assert payload.exists()
    assert control.exists()

    queue.remove(tid)

    assert not payload.exists()
    assert not control.exists()


def test_stopping_the_queue_keeps_partial_aria2_data(queue_env, tmp_path):
    queue, rpc, _db, tid, payload, control = _aria2_task(queue_env, tmp_path)
    queue.tasks[tid].status = "active"
    rpc.pause_all = lambda: None

    queue.stop_queue()

    assert payload.exists()
    assert control.exists()


def test_removing_a_completed_aria2_download_keeps_the_finished_file(
    queue_env, tmp_path
):
    queue, _rpc, db_path, tid, payload, control = _aria2_task(queue_env, tmp_path)
    queue._apply_status(tid, {"status": "complete", "totalLength": "10",
                              "completedLength": "10"})
    assert queue.tasks[tid].status == "completed"

    queue.remove(tid)

    assert payload.exists()
    # A stray sidecar next to a finished file is not this command's business.
    assert control.exists()
    assert _rows(db_path) == []


def test_removing_a_completed_aria2_download_with_delete_still_deletes(
    queue_env, tmp_path
):
    queue, _rpc, _db, tid, payload, control = _aria2_task(queue_env, tmp_path)
    queue._apply_status(tid, {"status": "complete", "totalLength": "10",
                              "completedLength": "10"})

    queue.remove(tid, delete_file=True)

    assert not payload.exists()
    assert not control.exists()


def test_removing_an_aria2_download_mid_add_cleans_up_once_the_gid_lands(
    queue_env, tmp_path
):
    queue, rpc, db_path = queue_env()
    _running(queue)
    pending = []

    def spawn(fn, *args, on_done=None, on_fail=None, **kwargs):
        if getattr(fn, "__name__", "") in ("add_uri", "_probe_and_add"):
            pending.append((fn, args, on_done))
            return
        result = fn(*args, **kwargs)
        if on_done is not None:
            on_done(result)

    queue._spawn = spawn
    tid = queue.add_url(
        "https://example.invalid/late.bin",
        out_dir=str(tmp_path),
        filename="late.bin",
    )
    queue._launch(queue.tasks[tid])
    queue.tasks[tid].status = "active"
    assert pending

    payload = tmp_path / "late.bin"
    control = tmp_path / "late.bin.aria2"
    payload.write_bytes(b"partial")
    control.write_bytes(b"ctrl")

    queue.remove(tid)
    fn, args, on_done = pending[0]
    on_done(fn(*args))

    assert rpc.removed == ["gid-1"]
    assert not payload.exists()
    assert not control.exists()
    assert _rows(db_path) == []


def test_an_unknown_filename_is_never_guessed_at_removal(queue_env, tmp_path):
    queue, _rpc, db_path = queue_env()
    _sync_spawn(queue)
    _running(queue)
    tid = queue.add_url("https://example.invalid/x", out_dir=str(tmp_path))
    queue._launch(queue.tasks[tid])
    queue.tasks[tid].status = "active"
    queue.tasks[tid].filename = ""
    neighbour = tmp_path / "someone-elses.bin"
    neighbour.write_bytes(b"keep")
    (tmp_path / "someone-elses.bin.aria2").write_bytes(b"keep")

    queue.remove(tid)

    assert neighbour.exists()
    assert (tmp_path / "someone-elses.bin.aria2").exists()
    assert _rows(db_path) == []


def test_removal_touches_only_the_selected_tasks_own_files(queue_env, tmp_path):
    queue, _rpc, _db, tid, payload, control = _aria2_task(queue_env, tmp_path)
    queue.tasks[tid].status = "active"
    other = tmp_path / "Show.S01E02.mkv"
    other.write_bytes(b"other partial")
    (tmp_path / "Show.S01E02.mkv.aria2").write_bytes(b"other ctrl")

    queue.remove(tid)

    assert not payload.exists()
    assert other.exists()
    assert (tmp_path / "Show.S01E02.mkv.aria2").exists()


def test_clear_all_keeps_files_on_disk_as_its_prompt_promises(queue_env, tmp_path):
    queue, _rpc, _db, tid, payload, control = _aria2_task(queue_env, tmp_path)
    queue.tasks[tid].status = "active"

    # The Clear all prompt says "Files on disk are kept"; that is a promise,
    # not a default.
    queue.remove(tid, keep_incomplete=True)

    assert payload.exists()
    assert control.exists()
    assert tid not in queue.tasks


def test_clear_completed_never_deletes_incomplete_data(queue_env, tmp_path):
    queue, _rpc, _db, tid, payload, control = _aria2_task(queue_env, tmp_path)
    queue.tasks[tid].status = "active"

    queue.clear_completed()

    assert payload.exists()
    assert control.exists()
    assert tid in queue.tasks


def test_an_adopted_external_aria2_download_is_cleaned_on_removal(
    queue_env, tmp_path
):
    # A row Cove deliberately adopted from aria2 is a Cove-managed row: it
    # carries the authoritative path aria2 itself reported.
    queue, rpc, db_path = queue_env()
    _sync_spawn(queue)
    payload = tmp_path / "stranger.bin"
    control = tmp_path / "stranger.bin.aria2"
    payload.write_bytes(b"partial payload")
    control.write_bytes(b"aria2 resume state")
    rpc.tell_external_snapshot = lambda: [
        {
            "gid": "gid-external",
            "status": "active",
            "totalLength": "100",
            "completedLength": "10",
            "files": [{"path": str(payload),
                       "uris": [{"uri": "https://example.invalid/stranger.bin"}]}],
        }
    ]

    queue._check_external()
    tid = next(t.id for t in queue.tasks.values() if t.gid == "gid-external")
    assert queue.tasks[tid].status == "active"

    queue.remove(tid)

    assert rpc.removed == ["gid-external"]
    assert not payload.exists()
    assert not control.exists()


def test_removing_an_extractor_task_keeps_its_existing_behaviour(
    queue_env, fake_process, tmp_path
):
    queue, _rpc, _db = queue_env()
    task, proc = _start_extractor(queue, fake_process, tmp_path)
    task.status = "active"
    bystander = tmp_path / "movie.mp4"
    bystander.write_bytes(b"unrelated file with the same name")

    queue.remove(task.id)

    assert bystander.exists()
    _assert_no_work_dirs(tmp_path)


def test_removing_an_hls_task_keeps_its_existing_behaviour(
    queue_env, fake_process, tmp_path
):
    queue, _rpc, _db = queue_env()
    task, proc = _start_hls(queue, fake_process, tmp_path)
    task.status = "active"
    bystander = tmp_path / "movie.mp4"
    bystander.write_bytes(b"unrelated file with the same name")

    queue.remove(task.id)

    assert bystander.exists()
    _assert_no_work_dirs(tmp_path)


def test_removing_an_incomplete_torrent_keeps_its_dedicated_path(
    queue_env, monkeypatch, tmp_path
):
    """The incomplete-aria2 cleanup default must not reach a torrent.

    A torrent is a tree, not a single file, and its deletion is bounded by the
    paths aria2 itself reports. Routing it through the generic single-file
    cleanup would either miss most of the tree or delete by a reconstructed
    name, so a torrent keeps its own removal path unchanged.
    """
    queue, rpc, db_path, tid = _start_local_magnet(queue_env, monkeypatch)
    queue._apply_status(tid, {"status": "active", "followedBy": ["gid-child"],
                              "totalLength": "100", "completedLength": "10"})
    root = tmp_path / "Season 1"
    root.mkdir(parents=True)
    partial = root / "ep1.mkv"
    partial.write_bytes(b"partial")
    control = root / "ep1.mkv.aria2"
    control.write_bytes(b"ctrl")
    rpc.files_result = [{"path": str(partial)}]
    assert queue.tasks[tid].source_type == SOURCE_TORRENT
    assert queue.tasks[tid].status != "completed"

    queue.remove(tid)

    assert rpc.removed == ["gid-child"]
    assert partial.exists()
    assert control.exists()
    assert _rows(db_path) == []


def test_removing_an_incomplete_torrent_with_delete_still_deletes(
    queue_env, monkeypatch, tmp_path
):
    queue, rpc, db_path, tid = _start_local_magnet(queue_env, monkeypatch)
    queue._apply_status(tid, {"status": "active", "followedBy": ["gid-child"],
                              "totalLength": "100", "completedLength": "10"})
    root = tmp_path / "Season 1"
    root.mkdir(parents=True)
    partial = root / "ep1.mkv"
    partial.write_bytes(b"partial")
    (root / "ep1.mkv.aria2").write_bytes(b"ctrl")
    rpc.files_result = [{"path": str(partial)}]

    queue.remove(tid, delete_file=True)

    assert not partial.exists()
    assert not (root / "ep1.mkv.aria2").exists()


def test_extractor_fallback_ignores_yt_dlp_fragment_part_files(
    queue_env, fake_process, tmp_path
):
    """yt-dlp writes fragmented downloads as "<target>.part-Frag<n>".

    Those carry a compound suffix rather than a plain ".part", so a naive
    suffix match lets one through - and publishing a fragment as the finished
    file would present a broken download as a successful one.
    """
    queue, _rpc, _db = queue_env()
    task, proc, private, work_path = _finish_extractor_with_missing_report(
        queue, fake_process, tmp_path
    )
    # Exactly one, so ambiguity cannot be what saves us here.
    (work_path / "movie.mp4.part-Frag1").write_bytes(b"fragment one")

    proc.emit_output(f"{FINAL_PATH_MARKER}{private}\n")
    proc.finish(0)

    assert task.status == "error"
    assert list(tmp_path.glob("movie*")) == []
    _assert_no_work_dirs(tmp_path)


def test_extractor_fallback_picks_the_finished_file_beside_fragments(
    queue_env, fake_process, tmp_path
):
    queue, _rpc, _db = queue_env()
    task, proc, private, work_path = _finish_extractor_with_missing_report(
        queue, fake_process, tmp_path
    )
    (work_path / "movie.mkv").write_bytes(b"merged output")
    (work_path / "movie.mp4.part-Frag1").write_bytes(b"fragment one")
    (work_path / "movie.f137.mp4.ytdl").write_bytes(b"resume state")

    proc.emit_output(f"{FINAL_PATH_MARKER}{private}\n")
    proc.finish(0)

    assert task.status == "completed"
    assert task.filename == "movie.mkv"
    assert (tmp_path / "movie.mkv").read_bytes() == b"merged output"


def test_an_f_number_name_is_never_a_safe_fallback_candidate(
    queue_env, fake_process, tmp_path
):
    """An f###-shaped name is treated as ambiguous, on purpose.

    yt-dlp writes per-format streams as "<stem>.f137.<ext>", so inside a work
    directory that shape is not unambiguously the finished output. A user could
    in theory choose that basename themselves and would hit this rule too; that
    is the accepted trade. Failing closed costs a failed task and leaves the
    file where it is. Publishing it would present an unmerged stream as a
    completed download.
    """
    queue, _rpc, _db = queue_env()
    task, proc, private, work_path = _finish_extractor_with_missing_report(
        queue, fake_process, tmp_path, "video.mp4"
    )
    # The only file present, so ambiguity of count cannot be what rejects it.
    lone = work_path / "video.f137.mp4"
    lone.write_bytes(b"one per-format stream")

    proc.emit_output(f"{FINAL_PATH_MARKER}{private}\n")
    proc.finish(0)

    assert task.status == "error"
    assert task.error.startswith("Could not publish extractor output:")
    assert not (tmp_path / "video.f137.mp4").exists()
    assert not (tmp_path / "video.mp4").exists()
    assert list(tmp_path.glob("video*")) == []
    _assert_no_work_dirs(tmp_path)


def test_an_f_number_name_does_not_shadow_a_real_final_output(
    queue_env, fake_process, tmp_path
):
    queue, _rpc, _db = queue_env()
    task, proc, private, work_path = _finish_extractor_with_missing_report(
        queue, fake_process, tmp_path, "video.mp4"
    )
    (work_path / "video.mkv").write_bytes(b"merged output")
    (work_path / "video.f137.mp4").write_bytes(b"per-format stream")
    (work_path / "video.f251.webm").write_bytes(b"per-format stream")
    (work_path / "video.mp4.part-Frag1").write_bytes(b"fragment")
    (work_path / "video.ytdl").write_bytes(b"resume state")

    proc.emit_output(f"{FINAL_PATH_MARKER}{private}\n")
    proc.finish(0)

    # Exactly one legitimate final file among the intermediates: publish it.
    assert task.status == "completed"
    assert task.filename == "video.mkv"
    assert (tmp_path / "video.mkv").read_bytes() == b"merged output"
    assert not (tmp_path / "video.f137.mp4").exists()
    assert not (tmp_path / "video.mp4.part-Frag1").exists()
    _assert_no_work_dirs(tmp_path)


def test_the_f_number_rule_does_not_change_outside_work_root_handling(
    queue_env, fake_process, tmp_path, diag
):
    """Excluding f### names is a candidate rule, not a containment rule."""
    queue, _rpc, _db = queue_env()
    task, proc, _private, work_path = _finish_extractor_with_missing_report(
        queue, fake_process, tmp_path, "video.mp4"
    )
    (work_path / "video.f137.mp4").write_bytes(b"per-format stream")
    outside = tmp_path / "video.f137.mp4"

    proc.emit_output(f"{FINAL_PATH_MARKER}{outside}\n")
    proc.finish(0)

    assert task.status == "error"
    # Still rejected by containment, exactly as before, not by the shape rule.
    rejected = _one(diag, "extractor.publish", "engine_output_rejected")
    assert rejected["fields"]["rule"] == "outside_private_directory"
    assert not outside.exists()


def test_yt_dlp_reporting_no_final_marker_is_recoverable_by_design(
    queue_env, fake_process, tmp_path
):
    """No marker plus exactly one validated final file is a publish, not a bug.

    yt-dlp does not always print after_move:%(filepath)s, and the run is still
    complete. The candidate goes through the same validation as any reported
    path, so this widens nothing: it only stops discarding a finished download
    because the engine stayed quiet.
    """
    queue, _rpc, _db = queue_env()
    task, proc = _start_extractor(queue, fake_process, tmp_path, "video.mp4")
    work_path = _extractor_private_path(proc).parent
    (work_path / "video.mkv").write_bytes(b"merged output")

    proc.emit_output("[download] 100% of 10.00MiB\n")
    proc.finish(0)

    assert task.status == "completed"
    assert task.filename == "video.mkv"
    assert (tmp_path / "video.mkv").read_bytes() == b"merged output"
    _assert_no_work_dirs(tmp_path)


# ---- Sidecars are never a finished media output ----------------------------
#
# A user's own yt-dlp config can add --write-thumbnail or --write-info-json,
# which lands a sidecar in the private work directory. If the media output is
# the thing that went missing, that sidecar would be the lone candidate - and
# publishing a .webp as the completed download is a silent data loss.


def _publish_lone_candidate(queue, fake_process, tmp_path, name, requested="video.mp4"):
    task, proc, private, work_path = _finish_extractor_with_missing_report(
        queue, fake_process, tmp_path, requested
    )
    (work_path / name).write_bytes(b"sidecar payload")
    proc.emit_output(f"{FINAL_PATH_MARKER}{private}\n")
    proc.finish(0)
    return task


@pytest.mark.parametrize(
    "sidecar",
    [
        "video.info.json",
        "video.live_chat.json",
        "video.description",
        "video.webp",
        "video.jpg",
        "video.jpeg",
        "video.png",
        "video.en.vtt",
        "video.en.srt",
        "video.en.ass",
        "video.en.ssa",
        "video.en.lrc",
        "video.en.ttml",
        "video.en.srv1",
        "video.en.srv3",
        "video.en.json3",
    ],
)
def test_a_lone_sidecar_is_never_published_as_the_finished_download(
    queue_env, fake_process, tmp_path, sidecar
):
    queue, _rpc, _db = queue_env()

    task = _publish_lone_candidate(queue, fake_process, tmp_path, sidecar)

    assert task.status == "error"
    assert task.error.startswith("Could not publish extractor output:")
    assert list(tmp_path.glob("video*")) == []
    _assert_no_work_dirs(tmp_path)


def test_sidecars_do_not_shadow_the_real_media_output(
    queue_env, fake_process, tmp_path
):
    queue, _rpc, _db = queue_env()
    task, proc, private, work_path = _finish_extractor_with_missing_report(
        queue, fake_process, tmp_path, "video.mp4"
    )
    (work_path / "video.mkv").write_bytes(b"merged output")
    (work_path / "video.info.json").write_bytes(b"{}")
    (work_path / "video.webp").write_bytes(b"thumbnail")
    (work_path / "video.en.vtt").write_bytes(b"captions")
    (work_path / "video.description").write_bytes(b"description")

    proc.emit_output(f"{FINAL_PATH_MARKER}{private}\n")
    proc.finish(0)

    assert task.status == "completed"
    assert task.filename == "video.mkv"
    assert (tmp_path / "video.mkv").read_bytes() == b"merged output"
    assert not (tmp_path / "video.webp").exists()
    assert not (tmp_path / "video.info.json").exists()
    _assert_no_work_dirs(tmp_path)


def test_every_excluded_shape_together_still_yields_the_media_output(
    queue_env, fake_process, tmp_path
):
    """Sidecars, fragments, f### streams and control files in one directory."""
    queue, _rpc, _db = queue_env()
    task, proc, private, work_path = _finish_extractor_with_missing_report(
        queue, fake_process, tmp_path, "video.mp4"
    )
    (work_path / "video.mkv").write_bytes(b"merged output")
    for noise in (
        "video.info.json",
        "video.webp",
        "video.en.vtt",
        "video.f137.mp4",
        "video.f251.webm",
        "video.mp4.part-Frag1",
        "video.ytdl",
        "video.temp",
        "video.mkv.aria2",
    ):
        (work_path / noise).write_bytes(b"not the finished file")

    proc.emit_output(f"{FINAL_PATH_MARKER}{private}\n")
    proc.finish(0)

    assert task.status == "completed"
    assert task.filename == "video.mkv"
    assert (tmp_path / "video.mkv").read_bytes() == b"merged output"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cove.db", "video.mkv"]


def test_a_webm_output_stays_an_eligible_media_candidate(
    queue_env, fake_process, tmp_path
):
    """.webm is real media; only the thumbnail's .webp is a sidecar."""
    queue, _rpc, _db = queue_env()
    task, proc, private, work_path = _finish_extractor_with_missing_report(
        queue, fake_process, tmp_path, "video.mp4"
    )
    (work_path / "video.webm").write_bytes(b"merged output")
    (work_path / "video.webp").write_bytes(b"thumbnail")

    proc.emit_output(f"{FINAL_PATH_MARKER}{private}\n")
    proc.finish(0)

    assert task.status == "completed"
    assert task.filename == "video.webm"
    assert (tmp_path / "video.webm").read_bytes() == b"merged output"


def test_two_media_candidates_beside_sidecars_still_fail_closed(
    queue_env, fake_process, tmp_path
):
    queue, _rpc, _db = queue_env()
    task, proc, private, work_path = _finish_extractor_with_missing_report(
        queue, fake_process, tmp_path, "video.mp4"
    )
    (work_path / "video.mkv").write_bytes(b"one")
    (work_path / "video.webm").write_bytes(b"two")
    (work_path / "video.info.json").write_bytes(b"{}")

    proc.emit_output(f"{FINAL_PATH_MARKER}{private}\n")
    proc.finish(0)

    assert task.status == "error"
    assert list(tmp_path.glob("video*")) == []
    _assert_no_work_dirs(tmp_path)


# --- stale callbacks and unconfirmed removal -------------------------------
#
# Four independent races between Cove and aria2, all of the same shape: an
# asynchronous RPC result is applied without checking that the decision it was
# issued under still holds. Each test below delays one RPC, changes the world
# underneath it, then releases it.


class _DeferredSpawn:
    """Captures worker calls so a test can release them out of order."""

    def __init__(self, queue):
        self.calls = []
        queue._spawn = self._spawn

    def _spawn(self, fn, *args, on_done=None, on_fail=None, **kwargs):
        self.calls.append(SimpleNamespace(
            fn=fn, args=args, kwargs=kwargs, on_done=on_done, on_fail=on_fail,
        ))

    def names(self):
        return [getattr(c.fn, "__name__", "?") for c in self.calls]

    def take(self, name):
        """Pop the oldest pending call to rpc.<name>."""
        for index, call in enumerate(self.calls):
            if getattr(call.fn, "__name__", "") == name:
                return self.calls.pop(index)
        raise AssertionError(f"no pending {name}; have {self.names()}")

    def run(self, call):
        """Execute a captured call and deliver its result, as the pool would."""
        try:
            result = call.fn(*call.args, **call.kwargs)
        except Exception as exc:  # noqa: BLE001 - mirrors _RpcCall's own catch
            if call.on_fail is not None:
                call.on_fail(str(exc))
        else:
            if call.on_done is not None:
                call.on_done(result)

    def fail(self, call, msg="aria2 rpc failed"):
        if call.on_fail is not None:
            call.on_fail(msg)

    def drain(self, limit=10):
        """Run everything still pending, including work those calls queue.

        Bounded on purpose: a compensating command that re-triggered itself
        would be an infinite loop in production, so it fails here instead.
        """
        for _ in range(limit):
            if not self.calls:
                return
            pending, self.calls = self.calls, []
            for call in pending:
                self.run(call)
        raise AssertionError(f"compensating commands did not settle: {self.names()}")


def test_remove_keeps_the_task_until_aria2_confirms(queue_env, tmp_path):
    """BUG-001: the row and the payload outlive an unconfirmed cancellation."""
    queue, _rpc, db_path, tid, payload, control = _aria2_task(queue_env, tmp_path)
    queue.tasks[tid].status = "active"
    spawn = _DeferredSpawn(queue)

    queue.remove(tid, delete_file=True)

    # In flight: nothing may be gone yet.
    assert tid in queue.tasks
    assert [row["id"] for row in _rows(db_path)] == [tid]
    assert payload.exists()
    # A task Cove has not actually let go of still occupies a slot.
    assert queue._active_count() == 1

    spawn.fail(spawn.take("remove"))

    # aria2 refused, so the download is still real: keep showing it.
    assert tid in queue.tasks
    assert queue.tasks[tid].status == "active"
    assert [row["id"] for row in _rows(db_path)] == [tid]
    assert payload.exists()
    assert control.exists()


def test_remove_completes_once_aria2_confirms(queue_env, tmp_path):
    """The success path still removes the row and cleans the partial data."""
    queue, rpc, db_path, tid, payload, control = _aria2_task(queue_env, tmp_path)
    queue.tasks[tid].status = "active"
    spawn = _DeferredSpawn(queue)

    queue.remove(tid, delete_file=True)
    spawn.run(spawn.take("remove"))

    assert rpc.removed == ["gid-1"]
    assert tid not in queue.tasks
    assert _rows(db_path) == []
    assert not payload.exists()
    assert not control.exists()


def test_status_arriving_during_removal_cannot_complete_the_task(
    queue_env, tmp_path
):
    """A poll result must not resurrect a task that is being cancelled."""
    queue, _rpc, db_path, tid, _payload, _control = _aria2_task(queue_env, tmp_path)
    queue.tasks[tid].status = "active"
    spawn = _DeferredSpawn(queue)

    queue.remove(tid)
    queue._apply_status(tid, {"status": "complete", "totalLength": "10",
                              "completedLength": "10"})

    assert queue.tasks[tid].status == "active"
    spawn.run(spawn.take("remove"))
    assert tid not in queue.tasks


def test_a_late_pause_all_cannot_pause_a_restarted_queue(queue_env, tmp_path):
    """BUG-003: Stop -> Start, then the old pause_all result finally lands."""
    queue, rpc, _db, tid, _payload, _control = _aria2_task(queue_env, tmp_path)
    queue.tasks[tid].status = "active"
    spawn = _DeferredSpawn(queue)

    queue.stop_queue()
    pause_all = spawn.take("pause_all")   # held in flight
    queue.start_queue()

    # The pause never landed, so the task never left "active" locally and the
    # restart has nothing to resume. The UI shows a running queue.
    assert queue.tasks[tid].status == "active"

    spawn.run(pause_all)                  # the superseded result arrives
    spawn.drain()                         # ...and whatever it compensates with

    assert queue.tasks[tid].status == "active"
    # aria2 did receive the pause_all, so the backend has to be pulled back to
    # the state Cove is showing rather than silently disagreeing with it.
    assert rpc.unpaused == ["gid-1"]


def test_a_pause_that_wins_locally_also_wins_at_aria2(queue_env, tmp_path):
    """BUG-004: a pause raised while an unpause is in flight still wins.

    The two used to be issued together and race, with aria2 free to apply them
    in either order and reconciliation to clean up afterwards. Only one command
    per transfer is outstanding now, so the pause waits for the unpause to
    resolve and is then the only thing aria2 is told - there is no race left to
    lose and nothing to compensate for.
    """
    queue, rpc, _db, tid, _payload, _control = _aria2_task(queue_env, tmp_path)
    queue.tasks[tid].status = "paused"
    spawn = _DeferredSpawn(queue)

    queue.resume(tid)
    unpause = spawn.take("unpause")       # in flight
    queue.pause(tid)

    assert spawn.names() == [], "the pause waits rather than racing"

    spawn.run(unpause)
    spawn.drain()

    assert queue.tasks[tid].status == "paused"
    assert rpc.unpaused == ["gid-1"]
    assert rpc.paused == ["gid-1"], "sent once, after the unpause resolved"


def test_status_for_a_replaced_gid_is_discarded(queue_env, tmp_path):
    """BUG-005: a tellStatus answer for the old gid outlives a retry."""
    queue, rpc, _db, tid, _payload, _control = _aria2_task(queue_env, tmp_path)
    task = queue.tasks[tid]
    task.status = "active"
    task.completed_bytes = 10
    spawn = _DeferredSpawn(queue)

    rpc.status_result = {"status": "complete", "totalLength": "999",
                         "completedLength": "999"}
    queue._poll_active()
    poll = spawn.take("tell_status")

    # The task is retried in the meantime and gets a replacement transfer.
    task.gid = "gid-2"
    task.completed_bytes = 0

    spawn.run(poll)

    assert task.status == "active"
    assert task.completed_bytes == 0
    assert task.total_bytes != 999


def test_a_stale_pause_result_cannot_repaint_a_resumed_task(queue_env, tmp_path):
    """A pause that lost the race must not persist itself over the resume.

    Reconciling aria2 is only half the job: if the superseded callback still
    marks the task paused locally, Cove ends up showing and persisting the
    exact disagreement the generation check exists to prevent.
    """
    queue, rpc, db_path, tid, _payload, _control = _aria2_task(queue_env, tmp_path)
    queue.tasks[tid].status = "active"
    spawn = _DeferredSpawn(queue)

    queue.pause(tid)
    pause = spawn.take("pause")            # held in flight; still "active"
    # Stop marks it paused, and the restart resumes it - so by the time the
    # original pause result lands, the newest intent is "running". The resume
    # waits behind the pause rather than racing it.
    queue.stop_queue()
    spawn.run(spawn.take("pause_all"))
    assert queue.tasks[tid].status == "paused"
    queue.start_queue()

    assert queue.tasks[tid].status == "active", "the resume flips it optimistically"

    spawn.run(pause)                       # the superseded result arrives
    spawn.drain()                          # ...releasing the resume behind it

    assert queue.tasks[tid].status == "active", "the pause must not repaint it"
    assert _persisted_row(db_path, tid)["status"] == "active"
    assert rpc.unpaused == ["gid-1"]


def test_a_stale_unpause_failure_cannot_requeue_a_paused_task(queue_env, tmp_path):
    """Relaunch-on-failed-unpause is destructive, so it must be generation checked.

    A late failure for a superseded unpause would drop the gid and requeue a
    task the user has since paused - detaching it from aria2 and restarting a
    download nobody asked to restart.
    """
    queue, rpc, _db, tid, _payload, _control = _aria2_task(queue_env, tmp_path)
    task = queue.tasks[tid]
    task.status = "paused"
    spawn = _DeferredSpawn(queue)

    queue.resume(tid)
    unpause = spawn.take("unpause")        # held in flight
    queue.pause(tid)                       # the user changes their mind

    spawn.fail(unpause)                    # the superseded command fails
    spawn.drain()                          # ...releasing the pause behind it

    assert task.status == "paused"
    assert task.gid == "gid-1"             # not detached
    assert rpc.removed == []               # not dropped from aria2
    assert rpc.paused == ["gid-1"], "the wish it stepped aside for still runs"


def test_queue_wide_compensation_yields_to_a_newer_per_task_pause(queue_env, tmp_path):
    """Stop -> Start compensation must not resume a task the user just paused.

    A task whose own pause is still in flight is locally "active", so the
    queue-wide reconciliation would happily unpause it and throw away the more
    specific, more recent intent.
    """
    queue, rpc, _db, tid, _payload, _control = _aria2_task(queue_env, tmp_path)
    queue.tasks[tid].status = "active"
    spawn = _DeferredSpawn(queue)

    queue.stop_queue()
    pause_all = spawn.take("pause_all")   # held in flight
    queue.start_queue()
    queue.pause(tid)                      # the user pauses this one task
    pause = spawn.take("pause")           # also still in flight

    spawn.run(pause_all)                  # the superseded queue-wide result

    assert rpc.unpaused == [], "the newer per-task pause must win"

    spawn.run(pause)
    spawn.drain()

    assert queue.tasks[tid].status == "paused"
    assert rpc.unpaused == []


def test_a_failed_pause_stops_deferring_queue_wide_compensation(queue_env, tmp_path):
    """A pause aria2 refused is not a pause still pending.

    Compensation defers to a newer per-task pause, so an intent left set by a
    failure would make it defer forever - and a stale pause_all would then
    leave aria2 paused behind a task Cove shows as active.
    """
    queue, rpc, _db, tid, _payload, _control = _aria2_task(queue_env, tmp_path)
    queue.tasks[tid].status = "active"
    spawn = _DeferredSpawn(queue)

    queue.stop_queue()
    pause_all = spawn.take("pause_all")   # held in flight
    queue.start_queue()
    queue.pause(tid)
    spawn.fail(spawn.take("pause"))       # aria2 refuses the pause

    # The task never paused, so it is still active and still needs the stale
    # queue-wide pause compensated for.
    assert queue.tasks[tid].status == "active"

    spawn.run(pause_all)
    spawn.drain()

    assert rpc.unpaused == ["gid-1"]
    assert queue.tasks[tid].status == "active"


def test_a_retry_does_not_inherit_the_old_transfers_pause_intent(queue_env, tmp_path):
    """Pause intent belongs to a gid, not to a task id.

    A retry abandons the gid and relaunches under a new one. An intent left
    over from the old transfer describes a download that no longer exists, and
    would keep suppressing reconciliation for its replacement.
    """
    queue, rpc, _db, tid, _payload, _control = _aria2_task(queue_env, tmp_path)
    task = queue.tasks[tid]
    task.status = "active"
    spawn = _DeferredSpawn(queue)

    queue.pause(tid)
    spawn.take("pause")                   # never lands; intent recorded
    task.gid = "gid-2"                    # the retry's replacement transfer

    queue.stop_queue()
    pause_all = spawn.take("pause_all")
    queue.start_queue()
    spawn.run(pause_all)
    spawn.drain()

    # The stale intent belonged to gid-1, so it must not shield gid-2.
    assert rpc.unpaused == ["gid-2"]


def test_a_failed_compensation_is_reported_and_not_recorded_as_success(
    queue_env, tmp_path
):
    """Discarding a stale result is not enough if the repair also fails."""
    queue, _rpc, _db, tid, _payload, _control = _aria2_task(queue_env, tmp_path)
    queue.tasks[tid].status = "active"
    errors = []
    queue.error.connect(errors.append)
    spawn = _DeferredSpawn(queue)

    queue.stop_queue()
    pause_all = spawn.take("pause_all")
    queue.start_queue()
    spawn.run(pause_all)

    spawn.fail(spawn.take("unpause"), "rpc unreachable")

    assert errors == ["rpc unreachable"], "a failed repair must not be swallowed"
    # Cove no longer claims to know what aria2 holds for this gid, so the
    # stale belief cannot suppress the next reconciliation.
    assert queue._desired_for(tid, "gid-1") is None


def test_a_refused_removal_reasserts_the_tasks_intent_to_aria2(queue_env, tmp_path):
    """Reconciliation is suppressed during removal, so it must be replayed.

    A refused removal leaves the transfer live. Any pause or unpause that
    completed inside the removal window was never checked against the current
    intent, so aria2 can hold the opposite of what the restored task shows.
    """
    queue, rpc, _db, tid, _payload, _control = _aria2_task(queue_env, tmp_path)
    task = queue.tasks[tid]
    task.status = "active"
    spawn = _DeferredSpawn(queue)

    queue.pause(tid)
    pause = spawn.take("pause")            # held in flight
    queue.remove(tid)                      # user removes while it is pending
    spawn.run(pause)                       # lands during removal: not reconciled

    spawn.fail(spawn.take("remove"))       # aria2 refuses the removal
    spawn.drain()

    assert tid in queue.tasks
    # The recorded intent was "paused", so aria2 is told again now that the
    # task is live and reconciliation is no longer suppressed.
    assert rpc.paused == ["gid-1", "gid-1"]


def test_a_removing_task_is_not_recorded_as_paused_by_a_queue_wide_pause(
    queue_env, tmp_path
):
    """It was excluded from the pause_all, so it must not be marked by it."""
    queue, _rpc, _db, tid, _payload, _control = _aria2_task(queue_env, tmp_path)
    queue.tasks[tid].status = "active"
    spawn = _DeferredSpawn(queue)

    queue.remove(tid)                     # cancellation in flight
    queue.stop_queue()
    spawn.run(spawn.take("pause_all"))

    assert queue.tasks[tid].status == "active"


def test_a_refused_removal_asserts_state_even_without_a_per_task_intent(
    queue_env, tmp_path
):
    """Removal excluded the task from queue-wide reconciliation too.

    With no per-task command to replay, aria2's state for this gid is simply
    unknown once the removal is refused - so what the queue wants for it now
    has to be asserted rather than assumed to already hold.
    """
    queue, rpc, _db, tid, _payload, _control = _aria2_task(queue_env, tmp_path)
    queue.tasks[tid].status = "active"
    spawn = _DeferredSpawn(queue)

    queue.remove(tid)
    queue.stop_queue()
    spawn.run(spawn.take("pause_all"))    # aria2 paused everything, incl. this
    queue.start_queue()

    spawn.fail(spawn.take("remove"))      # aria2 refuses the removal
    spawn.drain()

    assert tid in queue.tasks
    assert queue.tasks[tid].status == "active"
    assert rpc.unpaused == ["gid-1"], "the restored task must be resumed at aria2"


# --- restoration and stream parsing ----------------------------------------


def test_a_user_paused_task_comes_back_paused(queue_env, tmp_path):
    """BUG-015: explicit pause intent must survive a restart.

    Restoration normalised every persisted row to "queued", so startup queue
    processing treated a deliberately paused download as eligible and started
    it - on metered or restricted connections, exactly what the user stopped.
    """
    queue, _rpc, db_path = queue_env()
    tid = queue.add_url("https://example.invalid/a.bin", out_dir=str(tmp_path))
    task = queue.tasks[tid]
    task.status = "paused"
    queue._persist(task)

    revived, _rpc2, _db2 = queue_env()
    revived.tasks.clear()
    revived._load_persisted()

    assert revived.tasks[tid].status == "paused"


def test_an_interrupted_active_task_comes_back_queued(queue_env, tmp_path):
    """Only states that represent interrupted work are normalised."""
    queue, _rpc, db_path = queue_env()
    tid = queue.add_url("https://example.invalid/b.bin", out_dir=str(tmp_path))
    task = queue.tasks[tid]
    task.status = "active"
    queue._persist(task)

    revived, _rpc2, _db2 = queue_env()
    revived.tasks.clear()
    revived._load_persisted()

    assert revived.tasks[tid].status == "queued"


def test_a_restored_user_paused_task_is_not_started_by_the_queue(queue_env, tmp_path):
    queue, _rpc, _db = queue_env()
    tid = queue.add_url("https://example.invalid/c.bin", out_dir=str(tmp_path))
    task = queue.tasks[tid]
    task.status = "paused"
    queue._persist(task)

    revived, _rpc2, _db2 = queue_env()
    revived.tasks.clear()
    revived._load_persisted()
    launched = []
    revived._launch = launched.append
    _running(revived)
    revived._maybe_start_next()

    assert launched == []


def test_an_ffmpeg_progress_record_split_across_reads_still_updates(
    queue_env, fake_process, tmp_path
):
    """BUG-025: QProcess may deliver one line in two readyRead events.

    Each chunk was decoded and split on its own, so a record straddling the
    boundary was parsed as two fragments and dropped - producing stalled or
    jumpy progress for HLS downloads.
    """
    queue, _rpc, _db = queue_env()
    task, proc = _start_hls(queue, fake_process, tmp_path)

    proc.emit_output("  Duration: 00:00:20.00, start: 0.0\n")
    line = "frame=250 size=1024kB time=00:00:10.00 bitrate=838.9kbits/s speed=1.5x\n"
    proc.emit_output(line[:30])           # split mid-record
    proc.emit_output(line[30:])

    assert task.completed_bytes == 10


def test_every_split_of_one_ffmpeg_record_produces_the_same_update(
    queue_env, fake_process, tmp_path
):
    """Chunk boundaries are arbitrary, so no single split may be special."""
    line = "frame=175 size=512kB time=00:00:07.00 bitrate=838.9kbits/s speed=1.5x\n"
    for cut in range(1, len(line)):
        queue, _rpc, _db = queue_env()
        task, proc = _start_hls(queue, fake_process, tmp_path)
        proc.emit_output("  Duration: 00:00:20.00, start: 0.0\n")

        proc.emit_output(line[:cut])
        proc.emit_output(line[cut:])

        assert task.completed_bytes == 7, f"split after {cut} chars"


def test_a_stale_compensation_failure_leaves_a_newer_command_alone(queue_env, tmp_path):
    """Failure callbacks need the same guard as successful ones.

    Clearing the wish recorded by a newer command would suppress exactly the
    reconciliation that wish exists to drive. The guard is on the wish rather
    than the generation, because a wish held behind an in-flight command has no
    generation of its own yet.
    """
    queue, _rpc, _db, tid, _payload, _control = _aria2_task(queue_env, tmp_path)
    queue.tasks[tid].status = "active"
    errors = []
    queue.error.connect(errors.append)
    spawn = _DeferredSpawn(queue)

    queue.stop_queue()
    pause_all = spawn.take("pause_all")
    queue.start_queue()
    spawn.run(pause_all)
    compensation = spawn.take("unpause")   # held in flight

    queue.pause(tid)                       # a newer wish supersedes it

    spawn.fail(compensation)

    assert errors == [], "a superseded failure is not the user's problem"
    assert queue._desired_for(tid, "gid-1") is True, "the newer wish survives"


def test_only_one_command_per_transfer_is_ever_in_flight(queue_env, tmp_path):
    """Concurrency against one gid is the bug, not a case to be handled.

    Two commands for the same transfer are independent requests on independent
    worker threads. aria2 can apply them in one order and deliver their
    callbacks in the other, and nothing downstream can then say which one the
    backend actually ended up applying - so the record of what aria2 holds
    would be a guess. A newer wish waits for the outstanding command instead,
    which is what makes that record trustworthy.
    """
    queue, rpc, _db, tid, _payload, _control = _aria2_task(queue_env, tmp_path)
    task = queue.tasks[tid]
    task.status = "paused"
    spawn = _DeferredSpawn(queue)

    queue.resume(tid)
    unpause = spawn.take("unpause")
    queue.pause(tid)

    assert spawn.names() == [], "nothing may go out alongside the unpause"

    spawn.run(unpause)
    spawn.drain()                          # raises if the commands never settle

    assert task.status == "paused"
    assert rpc.unpaused == ["gid-1"]
    assert rpc.paused == ["gid-1"], "one command each, in order"


def test_repeated_wishes_while_a_command_is_in_flight_send_one_command(
    queue_env, tmp_path,
):
    """Only the latest wish is worth issuing, so the held slot holds one.

    A task stays locally active until its pause callback lands, so an impatient
    user can raise the same wish several times over. Each of those used to
    become its own RPC, and the duplicates are what every ordering defect in
    this area was built on.
    """
    queue, rpc, _db, tid, _payload, _control = _aria2_task(queue_env, tmp_path)
    task = queue.tasks[tid]
    task.status = "paused"
    spawn = _DeferredSpawn(queue)

    queue.resume(tid)
    unpause = spawn.take("unpause")
    queue.pause(tid)
    queue.pause(tid)
    queue.pause(tid)

    assert spawn.names() == []

    spawn.run(unpause)
    spawn.drain()

    assert task.status == "paused"
    assert rpc.paused == ["gid-1"], "three clicks, one pause"


def test_a_failed_command_still_releases_the_wish_held_behind_it(
    queue_env, tmp_path,
):
    """A command that will never land must not hold a wish forever.

    Convergence waits for the outstanding command before sending anything else.
    If a failure did not end that command's flight, the wish raised while it
    was in flight would sit unsent and Cove would never act on it.
    """
    queue, rpc, _db, tid, _payload, _control = _aria2_task(queue_env, tmp_path)
    task = queue.tasks[tid]
    task.status = "paused"
    errors = []
    queue.error.connect(errors.append)
    spawn = _DeferredSpawn(queue)

    queue.resume(tid)
    unpause = spawn.take("unpause")
    queue.pause(tid)                       # held behind it

    spawn.fail(unpause, "unpause rejected")
    spawn.drain()

    assert task.status == "paused"
    assert rpc.paused == ["gid-1"], "the held wish went out after the failure"
    assert task.gid == "gid-1", "and the failure did not relaunch the transfer"


def test_a_crash_during_removal_does_not_resurrect_the_download(queue_env, tmp_path):
    """Two-phase removal must not make removal itself less durable.

    The row used to be deleted before the RPC was issued, so exiting mid-removal
    could never bring a download back. Holding the row until aria2 confirms is
    what keeps a refused removal recoverable - but it also means a crash inside
    that window would restore a task the user had already removed, and restart
    it downloading.
    """
    queue, _rpc, db_path, tid, _payload, _control = _aria2_task(queue_env, tmp_path)
    queue.tasks[tid].status = "active"
    _DeferredSpawn(queue)                  # aria2 never answers

    queue.remove(tid, delete_file=True)

    # Still live in memory - a refusal has to be able to bring it back.
    assert tid in queue.tasks
    assert _persisted_row(db_path, tid)["status"] == "removing"

    queue2, _rpc2, _db2 = queue_env()      # the next launch

    assert tid not in queue2.tasks, "a removed download must not come back"
    assert _persisted_row(db_path, tid) is None, "the row is finished off"


def test_a_refused_removal_clears_the_durable_removal_marker(queue_env, tmp_path):
    """A restored task must survive the next launch too.

    The marker outliving the refusal it was written for would delete a live
    download from the database at startup.
    """
    queue, _rpc, db_path, tid, _payload, _control = _aria2_task(queue_env, tmp_path)
    queue.tasks[tid].status = "active"
    spawn = _DeferredSpawn(queue)

    queue.remove(tid, delete_file=True)
    spawn.fail(spawn.take("remove"))       # aria2 refuses

    assert _persisted_row(db_path, tid)["status"] == "active"

    queue2, _rpc2, _db2 = queue_env()

    assert tid in queue2.tasks, "the transfer is still running"


def test_a_failed_pause_sends_the_unpause_that_deferred_to_it(queue_env, tmp_path):
    """A skipped queue-wide compensation still has to happen if the pause fails.

    Stale `pause_all` compensation steps aside for a pending per-task pause on
    the assumption that pause will land. When it fails instead, nothing else
    holds the unpause it withheld: aria2 keeps the task paused from the
    `pause_all` while Cove shows it running, with no command left to converge
    them.
    """
    queue, rpc, _db, tid, _payload, _control = _aria2_task(queue_env, tmp_path)
    task = queue.tasks[tid]
    task.status = "active"
    errors = []
    queue.error.connect(errors.append)
    spawn = _DeferredSpawn(queue)

    queue.stop_queue()
    pause_all = spawn.take("pause_all")    # reaches aria2, result held
    queue.start_queue()
    queue.pause(tid)                       # the user pauses this one task
    pause = spawn.take("pause")

    spawn.run(pause_all)                   # superseded; skips this task
    assert rpc.unpaused == [], "the pending per-task pause still outranks it"

    spawn.fail(pause, "pause rejected")    # but that pause never lands
    spawn.drain()

    assert errors == ["pause rejected"]
    assert task.status == "active"
    assert rpc.unpaused == ["gid-1"], "aria2 must be told the task is running"


def test_a_removal_refused_while_stopped_still_restarts_with_the_queue(
    queue_env, tmp_path,
):
    """A task restored into a stopped queue has to rejoin the Start that follows.

    `_mark_all_active_paused` skips a task being removed, so it stays locally
    active across the Stop. The refusal then pauses it in aria2 to match the
    stopped queue - and if that is not recorded locally, Start skips it, since
    it only resumes members of `_auto_paused` that are locally paused. The
    download would sit paused in aria2 forever behind a running queue.
    """
    queue, rpc, _db, tid, _payload, _control = _aria2_task(queue_env, tmp_path)
    task = queue.tasks[tid]
    task.status = "active"
    spawn = _DeferredSpawn(queue)

    queue.remove(tid)
    remove = spawn.take("remove")          # held in flight
    queue.stop_queue()
    spawn.run(spawn.take("pause_all"))

    assert task.status == "active", "the removal excluded it from the Stop"

    spawn.fail(remove)                     # aria2 refuses; the task is back
    spawn.drain()

    # Restored into a stopped queue: paused, and marked as the queue's doing.
    assert task.status == "paused"
    assert rpc.paused == ["gid-1"]

    queue.start_queue()
    spawn.drain()

    assert task.status == "active"
    assert rpc.unpaused == ["gid-1"], "Start must pick the restored task back up"


def test_hls_progress_survives_carriage_return_terminated_records(
    queue_env, fake_process, tmp_path
):
    """ffmpeg ends each status line with a bare CR, not a newline.

    Buffering a partial record must not stop treating a carriage return as the
    end of one. Splitting on "\\n" alone holds every update in the buffer until
    an unrelated log line arrives, and the progress regexes search rather than
    match, so the eventual parse reports the oldest record in the blob.
    """
    queue, _rpc, _db = queue_env()
    task, proc = _start_hls(queue, fake_process, tmp_path)

    proc.emit_output("  Duration: 00:00:20.00, start: 0.0\r\n")
    proc.emit_output("frame=125 time=00:00:05.00 bitrate=838.9kbits/s speed=1.5x\r")
    proc.emit_output("frame=275 time=00:00:11.00 bitrate=838.9kbits/s speed=1.5x\r")

    assert task.total_bytes == 20
    assert task.completed_bytes == 11, "the newest record wins, not the oldest"

    # A CR-terminated record split across two reads is still stitched together.
    proc.emit_output("frame=425 time=00:00:1")
    assert task.completed_bytes == 11
    proc.emit_output("7.00 bitrate=838.9kbits/s speed=1.5x\r")
    assert task.completed_bytes == 17


def test_activity_during_a_pending_removal_cannot_clear_the_marker(
    queue_env, tmp_path,
):
    """A task awaiting confirmation stays visible, so it stays interactive.

    Pause, resume and a pause callback landing inside the removal window all
    persist the task. Any of them writing the live status back over the durable
    marker restores the crash that two-phase removal introduced - and a row
    left as `active` comes back as queued and downloads again.
    """
    queue, _rpc, db_path, tid, _payload, _control = _aria2_task(queue_env, tmp_path)
    task = queue.tasks[tid]
    task.status = "active"
    spawn = _DeferredSpawn(queue)

    queue.pause(tid)
    pause = spawn.take("pause")            # in flight when the removal starts
    queue.remove(tid, delete_file=True)
    spawn.take("remove")                   # aria2 never answers

    spawn.run(pause)                       # lands inside the removal window
    assert _persisted_row(db_path, tid)["status"] == "removing"

    queue.resume(tid)                      # the row is still on screen
    assert _persisted_row(db_path, tid)["status"] == "removing"

    queue.pause(tid)
    assert _persisted_row(db_path, tid)["status"] == "removing"

    queue2, _rpc2, _db2 = queue_env()

    assert tid not in queue2.tasks, "the removal still wins after a crash"


def test_a_pause_the_user_repeats_is_still_only_paused_once(queue_env, tmp_path):
    """The impatient-click case, which serialisation now prevents outright.

    Findings 19, 21 and 23 were all built on two pauses for one transfer being
    in flight together: the first succeeding and the second failing, the second
    failing before the first landed, and a newer resolution being read as the
    all-clear while the older was still out. None of those states is reachable
    with one command outstanding at a time, so what is guarded here is the
    property that replaced them.
    """
    queue, rpc, db_path, tid, _payload, _control = _aria2_task(queue_env, tmp_path)
    task = queue.tasks[tid]
    task.status = "active"
    errors = []
    queue.error.connect(errors.append)
    spawn = _DeferredSpawn(queue)

    queue.pause(tid)
    first = spawn.take("pause")
    queue.pause(tid)                       # still shows active, so still allowed

    assert spawn.names() == [], "the repeat is held, not raced against the first"

    spawn.run(first)
    spawn.drain()

    assert errors == []
    assert task.status == "paused"
    assert _persisted_row(db_path, tid)["status"] == "paused"
    assert rpc.paused == ["gid-1"], "and the held repeat adds nothing"


def test_a_wish_dropped_as_redundant_still_settles_local_state(
    queue_env, tmp_path,
):
    """Granting a wish without an RPC is still granting it.

    A held pause is dropped when aria2 already holds a pause - there is nothing
    to send. The wish was still granted, though, so the local effect its
    callback would have applied has to happen anyway. Skipping it leaves the
    row, the UI and the concurrency accounting describing a running download
    while the transfer is paused.
    """
    queue, rpc, db_path, tid, _payload, _control = _aria2_task(queue_env, tmp_path)
    task = queue.tasks[tid]
    task.status = "active"
    spawn = _DeferredSpawn(queue)

    queue.pause(tid)
    spawn.run(spawn.take("pause"))         # aria2 is now observably paused
    assert task.status == "paused"

    queue.resume(tid)
    unpause = spawn.take("unpause")        # optimistically active again
    queue.pause(tid)                       # held behind the resume

    spawn.fail(unpause, "unpause rejected")
    spawn.drain()

    # aria2 never left the paused state, so the held pause needs no RPC.
    assert rpc.paused == ["gid-1"], "no second pause was necessary"
    assert task.status == "paused", "but the task is paused, and says so"
    assert _persisted_row(db_path, tid)["status"] == "paused"


# ---------------------------------------------------------------------------
# Download progress stability.
#
# One stable task identity - one gid, one content scope - must never render a
# byte count (and so a percentage) lower than one it has already rendered,
# when the backend's own samples only ever moved forward. Identity changes are
# a different matter: they are allowed, and required, to start over.
# ---------------------------------------------------------------------------

def _active_task(**overrides) -> queue_module.DownloadTask:
    fields = dict(
        id=1,
        url="https://example.com/big.zip",
        out_dir="/dl",
        gid="gid-a",
        status="active",
        total_bytes=1_000_000_000,
        completed_bytes=535_000_000,
        download_speed=20_000_000,
        last_status_at=time.time(),
    )
    fields.update(overrides)
    return queue_module.DownloadTask(**fields)


def test_display_does_not_step_back_when_the_next_sample_undershoots():
    """The average speed aria2 reports is not the instantaneous rate, so the
    extrapolation regularly lands ahead of where the next real sample says the
    transfer is. Both samples here move forward; only Cove's own prediction
    moved backward."""
    task = _active_task(last_status_at=time.time() - 0.4)

    first = task.interpolated_completed_bytes()
    # A genuine forward step - 535 MB to 538 MB - that is nevertheless behind
    # where a 20 MB/s average predicted the row would be by now.
    task.completed_bytes = 538_000_000
    task.download_speed = 5_000_000
    task.last_status_at = time.time()
    second = task.interpolated_completed_bytes()

    assert second >= first, "displayed bytes went backward for a stable gid"
    assert (int(second * 100 / task.total_bytes)
            >= int(first * 100 / task.total_bytes)), "visible percent regressed"


def test_extrapolation_never_reaches_beyond_one_poll_period():
    """Extrapolation exists to bridge the gap to the next sample. Predicting
    further than that is unfounded, and the overshoot is precisely what the
    next sample yanks back."""
    task = _active_task(completed_bytes=100_000_000, download_speed=10_000_000,
                        last_status_at=time.time() - 5.0)

    ceiling = 100_000_000 + int(10_000_000 * queue_module.POLL_INTERVAL_S)
    assert task.interpolated_completed_bytes() <= ceiling


def test_progress_still_advances_between_two_polls():
    """Positive control: the fix must not flatten the bar into a 2 Hz step."""
    task = _active_task(last_status_at=time.time() - 0.2)

    assert task.interpolated_completed_bytes() > task.completed_bytes


def test_interpolated_bytes_never_exceed_the_total():
    task = _active_task(completed_bytes=999_000_000, download_speed=500_000_000,
                        last_status_at=time.time() - 30.0)

    assert task.interpolated_completed_bytes() <= task.total_bytes


def test_an_unknown_total_leaves_the_raw_count_alone():
    """Characterisation: with no denominator the window renders no percentage,
    and the byte readout must stay whatever the backend last reported."""
    task = _active_task(total_bytes=0, completed_bytes=4096, download_speed=0)

    assert task.interpolated_completed_bytes() == 4096


def test_a_paused_task_does_not_keep_creeping_forward():
    task = _active_task(last_status_at=time.time() - 0.3)
    task.interpolated_completed_bytes()

    task.status = "paused"
    task.download_speed = 0
    held = task.interpolated_completed_bytes()
    assert task.interpolated_completed_bytes() == held


def test_a_completed_task_renders_the_whole_total():
    task = _active_task(status="completed", completed_bytes=1_000_000_000,
                        download_speed=0)

    assert task.interpolated_completed_bytes() == 1_000_000_000


def test_a_new_gid_is_not_held_up_by_the_old_one_s_progress():
    """A gid change is a new progress identity. Nothing already displayed for
    the previous one may act as a floor under it."""
    task = _active_task(completed_bytes=800_000_000)
    assert task.interpolated_completed_bytes() >= 800_000_000

    task.gid = "gid-b"
    task.completed_bytes = 50_000_000
    task.download_speed = 0
    assert task.interpolated_completed_bytes() == 50_000_000


def test_a_changed_total_is_not_held_up_by_the_old_scope_s_progress():
    task = _active_task(completed_bytes=800_000_000, download_speed=0)
    assert task.interpolated_completed_bytes() == 800_000_000

    task.total_bytes = 4_000_000_000
    task.completed_bytes = 20_000_000
    assert task.interpolated_completed_bytes() == 20_000_000


def _capture_polls(queue):
    """Record the callbacks _poll_active hands to the thread pool, without
    running anything. Returns (done_callbacks, fail_callbacks)."""
    done, failed = [], []

    def _fake_spawn(fn, *a, on_done=None, on_fail=None, **kw):
        done.append(on_done)
        failed.append(on_fail)

    queue._spawn = _fake_spawn
    return done, failed


def test_only_one_status_request_per_task_is_in_flight(queue_env):
    """The poll timer fires on a fixed period rather than on the previous
    answer, so an unguarded poll of a slow daemon puts two tellStatus calls for
    one gid on the pool at once. They sample aria2 in whatever order they reach
    it, not the order they were submitted, so the older snapshot can land last
    and walk the task's byte count backwards."""
    queue, _rpc, _db = queue_env()
    tid = queue.add_url("https://example.com/big.zip")
    task = queue.tasks[tid]
    task.gid, task.status = "gid-a", "active"
    done, _failed = _capture_polls(queue)

    queue._poll_active()
    queue._poll_active()

    assert len(done) == 1, "a second request started while the first was out"


def test_the_next_poll_starts_once_the_answer_lands(queue_env):
    """Positive control: the guard must not stop polling altogether."""
    queue, _rpc, _db = queue_env()
    tid = queue.add_url("https://example.com/big.zip")
    task = queue.tasks[tid]
    task.gid, task.status = "gid-a", "active"
    done, _failed = _capture_polls(queue)

    queue._poll_active()
    done[0]({"status": "active", "totalLength": "1000", "completedLength": "590",
             "downloadSpeed": "10"})
    assert task.completed_bytes == 590

    queue._poll_active()
    assert len(done) == 2
    done[1]({"status": "active", "totalLength": "1000", "completedLength": "600",
             "downloadSpeed": "10"})
    assert task.completed_bytes == 600


def test_a_failed_status_request_frees_the_slot(queue_env):
    """An RPC that errors out must not leave the task unpollable for good."""
    queue, _rpc, _db = queue_env()
    tid = queue.add_url("https://example.com/big.zip")
    task = queue.tasks[tid]
    task.gid, task.status = "gid-a", "active"
    done, failed = _capture_polls(queue)

    queue._poll_active()
    failed[0]("aria2 is not answering")

    queue._poll_active()
    assert len(done) == 2


def test_metadata_size_and_speed_do_not_survive_the_payload_promotion(
    queue_env, monkeypatch
):
    """The metadata gid's length, rate and sample time describe the torrent
    file, not the payload. Left in place they are the progress bar's
    denominator and extrapolation anchor until the first payload status lands,
    which renders a percentage derived from the wrong transfer."""
    queue, _rpc, _db_path, tid = _start_local_magnet(queue_env, monkeypatch)
    queue._apply_status(tid, {"status": "active", "totalLength": "30000",
                              "completedLength": "12000", "downloadSpeed": "6000"})

    queue._apply_status(tid, {"status": "complete", "followedBy": ["gid-child"]})

    task = queue.tasks[tid]
    assert task.gid == "gid-child"
    assert task.completed_bytes == 0
    assert task.total_bytes == 0, "metadata length became the payload denominator"
    assert task.download_speed == 0
    assert task.last_status_at == 0.0
    assert task.interpolated_completed_bytes() == 0


def test_the_payload_s_first_status_supplies_the_real_size(
    queue_env, monkeypatch
):
    """Positive control: clearing the metadata denominator must not leave the
    row sizeless once the payload reports for itself."""
    queue, _rpc, _db_path, tid = _start_local_magnet(queue_env, monkeypatch)
    queue._apply_status(tid, {"status": "active", "totalLength": "30000",
                              "completedLength": "12000", "downloadSpeed": "6000"})
    queue._apply_status(tid, {"status": "complete", "followedBy": ["gid-child"]})

    queue._apply_status(tid, {"status": "active", "totalLength": "8000000000",
                              "completedLength": "400000000", "downloadSpeed": "0"})

    task = queue.tasks[tid]
    assert task.total_bytes == 8_000_000_000
    assert task.interpolated_completed_bytes() == 400_000_000


def test_a_stalling_download_is_not_stranded_at_a_speculative_total():
    """Completion is aria2's to declare. If a prediction were allowed to
    assert the total, the display floor would hold the row there for good and
    a transfer that stalls one byte short would read as finished forever."""
    task = _active_task(total_bytes=1000, completed_bytes=999,
                        download_speed=5000, last_status_at=time.time() - 0.5)
    assert task.interpolated_completed_bytes() < 1000

    task.download_speed = 0
    task.last_status_at = time.time()
    assert task.interpolated_completed_bytes() == 999


def test_a_raw_count_that_reached_the_total_is_not_shaved():
    """Positive control: aria2 reports completedLength == totalLength before
    it reports status "complete" - during the final flush, and during a
    torrent's hash check. That is a measurement, not a prediction, and holding
    it back would invent a regression of its own."""
    task = _active_task(total_bytes=1000, completed_bytes=1000,
                        download_speed=10, last_status_at=time.time() - 0.5)

    assert task.interpolated_completed_bytes() == 1000


def test_a_retry_does_not_inherit_the_failed_attempt_s_displayed_progress(queue_env):
    """The ffmpeg and yt-dlp backends carry no gid, and a retry reuses the
    same total, so nothing in the display floor's identity key changes between
    one attempt and the next. A fresh attempt has to say so itself, or the row
    sits at the failed attempt's percentage until the new process catches up."""
    queue, _rpc, _db = queue_env()
    tid = queue.add_url("https://example.com/clip.m3u8")
    task = queue.tasks[tid]
    task.backend = "ffmpeg"
    task.gid = None
    task.status = "active"
    task.total_bytes = 1000
    task.completed_bytes = 990
    task.download_speed = 0
    task.last_status_at = time.time()
    assert task.interpolated_completed_bytes() == 990

    # The retry, exactly as production takes it: nothing clears the failed
    # attempt's raw count, and ffmpeg will not state a position of its own for
    # seconds yet.
    task.status = "error"
    # The process start itself is not what is under test here.
    queue._launch_hls = lambda t: None
    queue._launch(task)

    assert task.completed_bytes == 0
    assert task.interpolated_completed_bytes() == 0


def test_relaunching_an_aria2_transfer_keeps_its_raw_count(queue_env):
    """Positive control: aria2 picks up from its own control file and a status
    poll corrects the count inside one poll period. Zeroing here would drop the
    row to 0% for no reason at all."""
    queue, _rpc, _db = queue_env()
    tid = queue.add_url("https://example.com/big.zip")
    task = queue.tasks[tid]
    task.total_bytes, task.completed_bytes = 1000, 400
    task.gid = None
    queue._spawn = lambda *a, **kw: None

    queue._launch(task)

    assert task.completed_bytes == 400


def test_resuming_an_aria2_transfer_keeps_its_displayed_progress(queue_env):
    """Positive control: an unpause continues the same attempt on the same
    gid. Clearing the floor there would let the row step backwards on resume."""
    queue, _rpc, _db = queue_env()
    tid = queue.add_url("https://example.com/big.zip")
    task = queue.tasks[tid]
    task.gid, task.status = "gid-a", "paused"
    task.total_bytes, task.completed_bytes = 1000, 400
    assert task.interpolated_completed_bytes() == 400

    queue.resume(tid)

    assert task.interpolated_completed_bytes() == 400


# ---------------------------------------------------------------------------
# Download File Info — prepare/commit boundary
# ---------------------------------------------------------------------------
#
# S1 (Download File Info preflight) needs to classify a manual direct HTTP
# request and calculate its effective destination BEFORE any irreversible
# work, so the GUI can ask the user about task-local directory/filename.
# `prepare_url` is that side-effect-free phase; `commit_prepared` is the
# single authoritative commit path `add_url` also routes through. These tests
# pin the boundary, the parity with the legacy path and the request-local
# isolation of the prepared value.

DIRECT_URL = "https://example.com/dir/archive.zip"


def _prepared_fields(prepared):
    return {
        "url": prepared.url,
        "backend": prepared.backend,
        "category": prepared.category,
        "out_dir": prepared.out_dir,
        "filename": prepared.filename,
        "connections": prepared.connections,
        "speed_limit_kbps": prepared.speed_limit_kbps,
        "cookies": prepared.cookies,
        "referrer": prepared.referrer,
        "user_agent": prepared.user_agent,
        "source_type": prepared.source_type,
        "info_hash": prepared.info_hash,
        "torrent_name": prepared.torrent_name,
        "torrent_path": prepared.torrent_path,
        "debrid_route": prepared.debrid_route,
        "intake": prepared.intake,
    }


def _rows_for(db_path):
    return _rows(db_path)


def test_prepare_url_classifies_and_computes_the_effective_destination(queue_env):
    queue, _rpc, db_path = queue_env(auto_sort_by_category=True)
    prepared = queue.prepare_url(
        DIRECT_URL, out_dir=None, filename=None, intake="manual"
    )

    assert prepared.backend == "aria2"
    assert prepared.category == "Archives"
    assert prepared.out_dir == str(Path(db_path).parent / "Archives")
    assert prepared.filename is None
    assert prepared.url == DIRECT_URL
    assert prepared.intake == "manual"

def test_prepare_url_honours_an_explicit_out_dir_and_filename(queue_env):
    queue, _rpc, _db = queue_env()
    prepared = queue.prepare_url(
        DIRECT_URL, out_dir="/srv/alt", filename="custom.zip", intake="manual"
    )

    assert prepared.out_dir == "/srv/alt"
    assert prepared.filename == "custom.zip"


def test_prepare_url_has_zero_side_effects(queue_env, monkeypatch):
    """Preparation must not touch the DB, tasks, aria2, the network or
    anything else. Even the diagnostic "url_added" event must not fire: it
    belongs to the commit. The test DB gets no row and the queue owns no task.
    """
    queue, rpc, db_path = queue_env()
    spawned = []
    monkeypatch.setattr(queue, "_spawn", lambda *a, **k: spawned.append(a))
    # A session build or a probe during prepare is a test failure, even if
    # the probe's own errors would be swallowed.
    monkeypatch.setattr(queue, "_bound_session", lambda: _raise_on_use())
    queue.prepare_url(DIRECT_URL, intake="manual")

    assert queue.tasks == {}
    assert _rows_for(db_path) == []
    assert rpc.added == []
    assert spawned == []
    assert queue._pending_launch == {}


class _raise_on_use:
    def __init__(self):
        raise AssertionError("prepare_url built an HTTP session")

    def head(self, *a, **k):
        raise AssertionError("prepare_url performed a network probe")

    def get(self, *a, **k):
        raise AssertionError("prepare_url performed a network probe")


def test_prepare_url_rejects_the_same_inputs_legacy_add_url_rejects(queue_env):
    queue, _rpc, _db = queue_env()

    assert queue.prepare_url("not a url") is None
    assert queue.prepare_url("magnet:?xt=urn:btih:" + "a" * 40, source_type="bad") is None


def test_commit_prepared_matches_legacy_add_url(queue_env):
    """An untouched prepare+commit must equal the legacy single call."""
    queue, _rpc, db_path = queue_env()

    legacy_tid = queue.add_url(
        DIRECT_URL, out_dir="/srv/legacy", filename="legacy.zip",
        connections=4, speed_limit_kbps=128, intake="manual",
    )
    prepared = queue.prepare_url(
        DIRECT_URL, out_dir="/srv/legacy", filename="legacy.zip",
        connections=4, speed_limit_kbps=128, intake="manual",
    )
    tid = queue.commit_prepared(prepared)

    assert tid == legacy_tid + 1
    row = _persisted_row(db_path, tid)
    legacy_row = _persisted_row(db_path, legacy_tid)
    for col in ("url", "out_dir", "filename", "backend", "category",
                "connections", "speed_limit_kbps", "cookies", "referrer",
                "user_agent", "source_type", "info_hash", "torrent_name",
                "torrent_path", "debrid_route", "status"):
        assert row[col] == legacy_row[col], col
    assert row["created_at"] == pytest.approx(legacy_row["created_at"], abs=1.0)
    task = queue.tasks[tid]
    assert task.out_dir == "/srv/legacy"
    assert task.filename == "legacy.zip"


def test_commit_prepared_creates_exactly_one_task(queue_env):
    queue, _rpc, db_path = queue_env()
    prepared = queue.prepare_url(DIRECT_URL, intake="manual")

    tid = queue.commit_prepared(prepared)

    assert isinstance(tid, int)
    assert list(queue.tasks.keys()) == [tid]
    assert len(_rows_for(db_path)) == 1


def test_add_url_still_commits_through_the_single_path_without_a_dialog(queue_env, monkeypatch):
    """The legacy entry point keeps its exact behavior; the queue never shows UI."""
    queue, rpc, db_path = queue_env()
    committed = []
    monkeypatch.setattr(queue, "commit_prepared", lambda p: committed.append(p) or 99)

    tid = queue.add_url(DIRECT_URL, intake="manual")

    assert tid == 99
    assert len(committed) == 1
    assert committed[0].url == DIRECT_URL
    assert queue.tasks == {}
    assert rpc.added == []


def test_add_url_and_commit_agree_without_an_explicit_destination(queue_env):
    """Default destination parity for an untouched prepared request."""
    queue, _rpc, db_path = queue_env()

    legacy_tid = queue.add_url(DIRECT_URL)
    prepared = queue.prepare_url(DIRECT_URL)
    tid = queue.commit_prepared(prepared)

    assert _persisted_row(db_path, tid)["out_dir"] == _persisted_row(db_path, legacy_tid)["out_dir"]
    assert _persisted_row(db_path, tid)["filename"] == _persisted_row(db_path, legacy_tid)["filename"]


def test_task_path_resolves_the_task_local_destination(queue_env):
    """RED 30: Open File / Open Folder resolve the actual task destination,
    never the global default."""
    from pathlib import Path as _Path

    queue, _rpc, _db = queue_env()
    from dataclasses import replace as _replace

    prepared = queue.prepare_url(DIRECT_URL, intake="manual")
    prepared = _replace(prepared, out_dir="/srv/custom-dir")
    tid = queue.commit_prepared(prepared)
    task = queue.tasks[tid]

    assert task.out_dir == "/srv/custom-dir"
    # A filename that aria2 later resolves lands under the task-local dir.
    task.filename = "server-name.zip"
    assert queue._task_path(task) == _Path("/srv/custom-dir") / "server-name.zip"
    assert queue._task_path(task) != _Path(queue.settings.download_dir) / "server-name.zip"


def test_commit_prepared_persists_custom_dir_and_filename_and_restores(
    queue_env, monkeypatch
):
    """RED 29: a task-local out_dir + filename survive persist/reload and the
    aria2 launch receives dir/out exactly (dir always, out only when set)."""
    from dataclasses import replace as _replace

    queue, rpc, db_path = queue_env(intelligent_segments=False)
    prepared = queue.prepare_url(DIRECT_URL, intake="manual")
    prepared = _replace(
        prepared, out_dir="/srv/custom-dir", filename="custom-name.zip"
    )
    tid = queue.commit_prepared(prepared)
    task = queue.tasks[tid]

    assert task.out_dir == "/srv/custom-dir"
    assert task.filename == "custom-name.zip"
    row = _persisted_row(db_path, tid)
    assert row["out_dir"] == "/srv/custom-dir"
    assert row["filename"] == "custom-name.zip"

    # Restore path rebuilds the same task-local destination.
    restored = _task_from_persisted_row(row)
    assert restored.out_dir == "/srv/custom-dir"
    assert restored.filename == "custom-name.zip"

    # The launch path hands aria2 dir always and out only for a set filename.
    def _sync_spawn(fn, *args, **kwargs):
        kwargs.pop("on_done", None)
        kwargs.pop("on_fail", None)
        return fn(*args, **kwargs)

    monkeypatch.setattr(queue, "_spawn", _sync_spawn)
    queue._launch(task)
    assert rpc.added and rpc.added[0]["out_dir"] == "/srv/custom-dir"
    assert rpc.added[0]["filename"] == "custom-name.zip"


def test_commit_prepared_blank_filename_sends_no_out(queue_env, monkeypatch):
    """RED 6: a None filename must produce no forced aria2 `out`."""
    queue, rpc, _db = queue_env(intelligent_segments=False)
    prepared = queue.prepare_url(DIRECT_URL, intake="manual")
    assert prepared.filename is None
    tid = queue.commit_prepared(prepared)

    # Run the launch's add_uri call inline, then inspect what it was given.
    def _sync_spawn(fn, *args, **kwargs):
        kwargs.pop("on_done", None)
        kwargs.pop("on_fail", None)
        return fn(*args, **kwargs)

    monkeypatch.setattr(queue, "_spawn", _sync_spawn)
    queue._launch(queue.tasks[tid])

    assert rpc.added and rpc.added[0]["filename"] is None
    assert rpc.added[0]["out_dir"] == queue.settings.download_dir


def test_two_prepared_requests_are_isolated(queue_env):
    """Request-local prepared values: mutating one never leaks into another.

    The prepared value is deliberately immutable, so an override is a
    replacement (`dataclasses.replace`), never an in-place edit — there is
    no mutable state two prepared requests could share.
    """
    from dataclasses import replace

    queue, _rpc, _db = queue_env()
    a = queue.prepare_url(DIRECT_URL, intake="manual")
    b = queue.prepare_url("https://example.com/other/file.bin", intake="manual")

    a = replace(a, out_dir="/srv/custom-a", filename="custom-a.bin")

    assert b.out_dir != "/srv/custom-a"
    assert b.filename is None
    assert a.out_dir == "/srv/custom-a"
    assert a.filename == "custom-a.bin"


def test_prepare_url_takes_no_network_and_makes_no_probe(queue_env, monkeypatch):
    """Preparation is pure local classification; the probe/HEAD stays at launch."""
    queue, _rpc, _db = queue_env()
    probed = []
    monkeypatch.setattr(queue, "_probe_and_add", lambda t: probed.append(t) or "gid-x")

    prepared = queue.prepare_url(DIRECT_URL, intake="manual")
    queue.commit_prepared(prepared)

    assert probed == []  # the probe belongs to _launch, not prepare/commit


def test_prepare_url_ytdlp_and_hls_requests_are_still_committable(queue_env, monkeypatch):
    """prepare_url classifies the same backends add_url does; commit keeps the
    legacy backend selection (yt-dlp/HLS stay outside the dialog eligibility,
    but their queue path must not fork)."""
    queue, _rpc, _db = queue_env()
    for url, backend in (
        ("https://www.youtube.com/watch?v=fake", "yt-dlp"),
        ("https://example.com/live/stream.m3u8", "ffmpeg"),
    ):
        prepared = queue.prepare_url(url, filename="movie.mp4")
        assert prepared.backend == backend, url
        tid = queue.commit_prepared(prepared)
        assert queue.tasks[tid].backend == backend, url


# --- per-file selection ----------------------------------------------------
#
# The selection a task carries is 0-based, exactly like the torrent manifest;
# aria2's select-file is 1-based. These tests pin the conversion happening
# once, at the aria2 call, and pin the two things that must never happen:
# a selection quietly disappearing, and an unreadable selection quietly
# becoming "download everything".


def _bencoded_bytes(value: bytes) -> bytes:
    return str(len(value)).encode() + b":" + value


def _multi_file_torrent_bytes(name: bytes, files):
    """A bencoded multi-file `.torrent` from (length, path components) pairs."""
    entries = b""
    for length, parts in files:
        path = b"l" + b"".join(_bencoded_bytes(p) for p in parts) + b"e"
        entries += b"d6:lengthi" + str(length).encode() + b"e4:path" + path + b"e"
    info = (
        b"d5:filesl" + entries + b"e4:name" + _bencoded_bytes(name)
        + b"12:piece lengthi16384e6:pieces" + _bencoded_bytes(b"\x01" * 20) + b"e"
    )
    return b"d4:info" + info + b"e"


# index 0 Season/Episode01.mkv, index 1 Season/Episode02.mkv, index 2 Sample.mkv
def _multi_torrent_bytes():
    return _multi_file_torrent_bytes(b"Show S01", (
        (7, (b"Season", b"Episode01.mkv")),
        (9, (b"Season", b"Episode02.mkv")),
        (5, (b"Sample.mkv",)),
    ))


def _managed_torrent_task(queue_env, monkeypatch, tmp_path, selection=None):
    """A managed multi-file .torrent queued exactly as add_torrent_file does."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    queue, rpc, db_path = queue_env(**_local_settings())
    _sync_spawn(queue)
    _uncached(queue, monkeypatch)
    raw = _multi_torrent_bytes()
    meta = torrent_mod.parse_torrent(raw)
    managed = torrent_mod.store_managed_torrent(meta)
    tid = queue.add_url(
        torrent_mod.minimal_magnet(meta.info_hash),
        out_dir=str(tmp_path),
        source_type=SOURCE_TORRENT,
        info_hash=meta.info_hash,
        torrent_name=meta.name,
        torrent_path=managed,
        selected_files=selection,
    )
    _running(queue)
    return queue, rpc, db_path, tid, raw


def test_torrent_manifest_indexes_are_zero_based(tmp_path):
    """The premise the whole conversion rests on."""
    meta = torrent_mod.parse_torrent(_multi_torrent_bytes())

    assert [f.index for f in meta.files] == [0, 1, 2]
    assert meta.files[0].name == "Episode01.mkv"
    assert meta.files[2].name == "Sample.mkv"


def test_prepared_download_has_no_selection_by_default(queue_env):
    queue, _rpc, _db_path = queue_env()

    prepared = queue.prepare_url("https://example.com/a.bin")

    assert prepared.selected_files is None


def test_prepared_selection_is_canonicalised_without_reaching_aria2(
    queue_env, monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    queue, rpc, _db_path = queue_env(**_local_settings())
    _sync_spawn(queue)
    meta = torrent_mod.parse_torrent(_multi_torrent_bytes())
    managed = torrent_mod.store_managed_torrent(meta)

    prepared = queue.prepare_url(
        torrent_mod.minimal_magnet(meta.info_hash),
        out_dir=str(tmp_path),
        source_type=SOURCE_TORRENT,
        info_hash=meta.info_hash,
        torrent_path=managed,
        selected_files=[2, 0],
    )

    assert prepared.selected_files == (0, 2)
    assert rpc.torrents == []


def test_download_task_has_no_selection_by_default(queue_env):
    queue, _rpc, db_path = queue_env()

    tid = queue.add_url("https://example.com/a.bin")

    assert queue.tasks[tid].selected_files is None
    assert _persisted_row(db_path, tid)["selected_files"] == ""


def test_committed_selection_reaches_the_task_and_the_row(
    queue_env, monkeypatch, tmp_path
):
    queue, _rpc, db_path, tid, _raw = _managed_torrent_task(
        queue_env, monkeypatch, tmp_path, selection=[2, 0]
    )

    assert queue.tasks[tid].selected_files == (0, 2)
    assert _persisted_row(db_path, tid)["selected_files"] == "0,2"


def test_managed_torrent_selection_reaches_aria2_one_based(
    queue_env, monkeypatch, tmp_path
):
    queue, rpc, _db_path, tid, _raw = _managed_torrent_task(
        queue_env, monkeypatch, tmp_path, selection=(0, 2)
    )

    queue._launch(queue.tasks[tid])

    assert rpc.torrents[0]["select_file"] == "1,3"


def test_whole_torrent_still_sends_no_select_file(queue_env, monkeypatch, tmp_path):
    queue, rpc, _db_path, tid, _raw = _managed_torrent_task(
        queue_env, monkeypatch, tmp_path
    )

    queue._launch(queue.tasks[tid])

    assert rpc.torrents[0]["select_file"] is None


def test_restart_reapplies_the_same_selection_exactly_once(
    queue_env, monkeypatch, tmp_path
):
    """The regression this feature exists to prevent.

    The gid is recreated on every start, so aria2 remembers nothing. If the
    restored selection were dropped, or converted twice, Episode02 would
    start downloading after a restart that the user never asked for.
    """
    queue, rpc, db_path, tid, _raw = _managed_torrent_task(
        queue_env, monkeypatch, tmp_path, selection=(0, 2)
    )
    queue._launch(queue.tasks[tid])
    assert rpc.torrents[0]["select_file"] == "1,3"

    queue2, rpc2 = _restart(queue_env, db_path, monkeypatch)
    restored = queue2.tasks[tid]
    assert restored.selected_files == (0, 2)

    queue2._launch(restored)

    assert rpc2.torrents[0]["select_file"] == "1,3"


def test_restart_of_a_whole_torrent_gains_no_select_file(
    queue_env, monkeypatch, tmp_path
):
    queue, _rpc, db_path, tid, _raw = _managed_torrent_task(
        queue_env, monkeypatch, tmp_path
    )

    queue2, rpc2 = _restart(queue_env, db_path, monkeypatch)
    assert queue2.tasks[tid].selected_files is None

    queue2._launch(queue2.tasks[tid])

    assert rpc2.torrents[0]["select_file"] is None


@pytest.mark.parametrize("corrupt", ["0,,2", "abc", "-1", "   ", " 1",
                                     sqlite3.Binary(b""),
                                     sqlite3.Binary(b"0,2"),
                                     "9" * 5000])
def test_unreadable_persisted_selection_never_downloads_everything(
    queue_env, monkeypatch, tmp_path, corrupt
):
    queue, _rpc, db_path, tid, _raw = _managed_torrent_task(
        queue_env, monkeypatch, tmp_path, selection=(0, 2)
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE downloads SET selected_files=? WHERE id=?",
                     (corrupt, tid))

    queue2, rpc2 = _restart(queue_env, db_path, monkeypatch)

    assert tid not in queue2.tasks
    assert rpc2.torrents == []
    assert _persisted_row(db_path, tid)["status"] == "error"


def test_a_selection_cannot_be_dropped_into_a_manifest_less_magnet(queue_env):
    queue, rpc, db_path = queue_env(**_local_settings())
    _sync_spawn(queue)
    errors = []
    queue.error.connect(errors.append)

    assert queue.add_url(MAGNET, selected_files=(0,)) is None
    assert _rows(db_path) == []
    assert errors
    assert rpc.magnets == []


def test_a_selection_is_refused_for_an_ordinary_download(queue_env):
    queue, _rpc, db_path = queue_env()
    errors = []
    queue.error.connect(errors.append)

    assert queue.add_url("https://example.com/a.bin", selected_files=(0,)) is None
    assert _rows(db_path) == []
    assert errors


def test_an_ordinary_magnet_is_untouched_by_the_selection_plumbing(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(**_local_settings())
    _sync_spawn(queue)
    _uncached(queue, monkeypatch)
    _running(queue)
    tid = queue.add_url(MAGNET)

    queue._launch(queue.tasks[tid])

    assert queue.tasks[tid].selected_files is None
    assert _persisted_row(db_path, tid)["selected_files"] == ""
    assert rpc.magnets[0]["uri"] == MAGNET


def test_the_magnet_route_itself_refuses_a_selection_it_cannot_apply(queue_env, monkeypatch):
    """The launch-side backstop, independent of the prepare-time refusal.

    prepare_url already blocks this combination, so nothing in production
    reaches here today. It is asserted anyway because the failure mode it
    guards - a selection reaching a route with no manifest, and every file
    downloading as a result - is silent.
    """
    queue, rpc, _db_path = queue_env(**_local_settings())
    _sync_spawn(queue)
    _uncached(queue, monkeypatch)
    # Deliberately not _running: the queue must not auto-start the magnet,
    # or the add below would be indistinguishable from the launch under test.
    tid = queue.add_url(MAGNET)
    task = queue.tasks[tid]
    task.selected_files = (0,)

    with pytest.raises(torrent_mod.TorrentError):
        queue._add_local_magnet(task)

    assert rpc.magnets == []


def test_a_selected_torrent_still_asks_the_providers(
    queue_env, monkeypatch, tmp_path
):
    """Slice 2 replaces Slice 1's temporary "selected means local" guard.

    A selection no longer disqualifies a torrent from the cached-debrid
    route: it now narrows what that route materialises. What must never
    happen is the reason the guard existed - a cache hit quietly expanding
    an explicit subset back into the whole torrent.
    """
    queue, rpc, db_path, tid, calls = _selected_debrid_env(
        queue_env, monkeypatch, tmp_path, (0, 2), _provider_result(),
    )

    queue._launch(queue.tasks[tid])

    assert len(calls["probe"]) == 1
    assert _materialised_files(db_path) == [
        (str(tmp_path / "Show S01" / "Season"), "Episode01.mkv", SEL_LINK_EP1),
        (str(tmp_path / "Show S01"), "Sample.mkv", SEL_LINK_SAMPLE),
    ]
    # Episode02 was deselected: no row, and no link ever resolved for it.
    assert SEL_LINK_EP2 not in calls["unlock"]
    assert rpc.torrents == []
    assert rpc.magnets == []


def test_an_unselected_torrent_still_takes_the_cached_debrid_route(
    queue_env, monkeypatch, tmp_path
):
    """The compatibility half: no selection, no routing change."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    queue, rpc, db_path = queue_env(**_local_settings())
    _sync_spawn(queue)
    monkeypatch.setattr(debrid, "resolve_torrent", lambda *a, **k: _cached())
    raw = _multi_torrent_bytes()
    meta = torrent_mod.parse_torrent(raw)
    managed = torrent_mod.store_managed_torrent(meta)
    tid = queue.add_url(
        torrent_mod.minimal_magnet(meta.info_hash),
        out_dir=str(tmp_path),
        source_type=SOURCE_TORRENT,
        info_hash=meta.info_hash,
        torrent_name=meta.name,
        torrent_path=managed,
    )
    _running(queue)

    queue._launch(queue.tasks[tid])

    assert queue.tasks[tid].source_type == SOURCE_TORRENT_FILE
    assert rpc.torrents == []


# ---------------------------------------------------------------------------
# Selected files: cached-debrid materialisation
# ---------------------------------------------------------------------------
#
# A selection names files in the *torrent's* manifest. A provider hands back
# its own view of the same torrent, in its own order, and Cove has to prove
# which provider file is which chosen file before it downloads anything. The
# tests below pin the two halves of that: the exact mapping that is allowed,
# and every near-miss that must instead fall back to the local route with the
# subset intact. "Download everything" is never an acceptable answer to an
# ambiguous mapping.

SEL_LINK_EP1 = "https://alldebrid.com/f/EPISODE01"
SEL_LINK_EP2 = "https://alldebrid.com/f/EPISODE02"
SEL_LINK_SAMPLE = "https://alldebrid.com/f/SAMPLE"
SEL_LINK_EXTRA = "https://alldebrid.com/f/EXTRA"

_SHOW_NAME = "Show S01"
# The provider's view of _multi_torrent_bytes(), already root-stripped and
# path-validated the way cove.debrid hands it to the queue.
_SHOW_ENTRIES = (
    (("Season", "Episode01.mkv"), 7, SEL_LINK_EP1),
    (("Season", "Episode02.mkv"), 9, SEL_LINK_EP2),
    (("Sample.mkv",), 5, SEL_LINK_SAMPLE),
)


def _show_meta():
    return torrent_mod.parse_torrent(_multi_torrent_bytes())


def _provider_result(entries=_SHOW_ENTRIES, *, provider=ALL_DEBRID, name=_SHOW_NAME):
    """A CachedTorrent from (path components, size, locked link) triples."""
    return CachedTorrent(
        provider, _show_meta().info_hash, name,
        tuple(
            CachedTorrentFile(index, tuple(path), size, locked_link=link)
            for index, (path, size, link) in enumerate(entries)
        ),
    )


def _torbox_provider_result(entries=_SHOW_ENTRIES, *, name=_SHOW_NAME):
    """The same manifest as TorBox returns it: item/file IDs, no link."""
    return CachedTorrent(
        debrid.TORBOX, _show_meta().info_hash, name,
        tuple(
            CachedTorrentFile(
                index, tuple(path), size,
                item_id=TORBOX_TORRENT_ITEM, file_id=str(index + 1),
            )
            for index, (path, size, _link) in enumerate(entries)
        ),
    )


def _selected_debrid_env(
    queue_env, monkeypatch, tmp_path, selection, cached, raw=None, **settings
):
    """A managed `.torrent` with a selection, and a fully faked provider.

    Every provider entry point a launch can reach is recorded and faked, so
    a test can assert what was asked of the provider as well as what landed
    in the database, and nothing can touch a real account.
    """
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    queue, rpc, db_path = queue_env(**_local_settings(**settings))
    _sync_spawn(queue)
    calls = {"probe": [], "unlock": [], "requestdl": []}

    def probe(info_hash, _settings, **kw):
        calls["probe"].append(info_hash)
        return cached

    def unlock(link, provider, _settings, **kw):
        calls["unlock"].append(link)
        return Unrestricted(TORRENT_NODE_URL, "", 0, provider)

    def requestdl(item_id, file_id, _token, **kw):
        calls["requestdl"].append((item_id, file_id))
        return TORBOX_TORRENT_CDN_URL

    monkeypatch.setattr(debrid, "resolve_torrent", probe)
    monkeypatch.setattr(debrid, "unlock_torrent_file", unlock)
    monkeypatch.setattr(debrid, "torbox_refresh_torrent_file", requestdl)

    meta = torrent_mod.parse_torrent(
        _multi_torrent_bytes() if raw is None else raw
    )
    managed = torrent_mod.store_managed_torrent(meta)
    tid = queue.add_url(
        torrent_mod.minimal_magnet(meta.info_hash),
        out_dir=str(tmp_path),
        source_type=SOURCE_TORRENT,
        info_hash=meta.info_hash,
        torrent_name=meta.name,
        torrent_path=managed,
        selected_files=selection,
    )
    _running(queue)
    return queue, rpc, db_path, tid, calls


def _materialised_files(db_path):
    """(out_dir, filename, url) for every provider-backed row, in row order."""
    return [
        (r["out_dir"], r["filename"], r["url"])
        for r in _rows(db_path)
        if r["source_type"] == SOURCE_TORRENT_FILE
    ]


def _fell_back_locally(queue, rpc, db_path, tid, select_file):
    """The whole no-provider-children-then-local-fallback outcome."""
    assert _materialised_files(db_path) == []
    assert len(_rows(db_path)) == 1
    assert queue.tasks[tid].source_type == SOURCE_TORRENT
    assert queue.tasks[tid].debrid_route == ""
    assert [t["select_file"] for t in rpc.torrents] == [select_file]


# --- the mapping helper, in isolation --------------------------------------


def test_selected_provider_files_maps_canonical_paths_not_positions():
    meta = _show_meta()
    reordered = _provider_result((
        _SHOW_ENTRIES[2], _SHOW_ENTRIES[0], _SHOW_ENTRIES[1],
    ))

    picked = queue_module._selected_provider_files(meta, (1,), reordered)

    assert [f.relative_path for f in picked] == ["Season/Episode02.mkv"]
    assert [f.locked_link for f in picked] == [SEL_LINK_EP2]


def test_selected_provider_files_refuses_an_index_outside_the_manifest():
    assert queue_module._selected_provider_files(
        _show_meta(), (0, 5), _provider_result()
    ) is None


def test_selected_provider_files_refuses_a_missing_manifest():
    assert queue_module._selected_provider_files(
        None, (0,), _provider_result()
    ) is None


def test_selected_provider_files_refuses_a_collided_canonical_path():
    """Two manifest entries that sanitise to one path cannot be told apart.

    The parser refuses such a torrent outright today, so this is asserted
    against a hand-built manifest: the mapper must not depend on that being
    the only gate.
    """
    collided = torrent_mod.TorrentMetadata(
        info_hash="0" * 40, name="Show S01",
        files=(
            torrent_mod.TorrentFile(0, ("Season", "Episode01.mkv"), 7),
            torrent_mod.TorrentFile(1, ("Season", "Episode01.mkv"), 7),
        ),
        total_size=14, multi_file=True,
    )

    assert queue_module._selected_provider_files(
        collided, (0,), _provider_result()
    ) is None


def test_selected_provider_files_refuses_a_duplicated_provider_path():
    doubled = _provider_result(_SHOW_ENTRIES + (
        (("Sample.mkv",), 5, SEL_LINK_EXTRA),
    ))

    assert queue_module._selected_provider_files(
        _show_meta(), (2,), doubled
    ) is None


def test_selected_provider_files_ignores_unselected_provider_extras():
    extra = _provider_result(_SHOW_ENTRIES + (
        (("readme.nfo",), 3, SEL_LINK_EXTRA),
    ))

    picked = queue_module._selected_provider_files(_show_meta(), (0, 2), extra)

    assert [f.relative_path for f in picked] == [
        "Season/Episode01.mkv", "Sample.mkv",
    ]


def test_selected_provider_files_refuses_a_size_disagreement():
    wrong_size = _provider_result((
        _SHOW_ENTRIES[0],
        (("Season", "Episode02.mkv"), 9999, SEL_LINK_EP2),
        _SHOW_ENTRIES[2],
    ))

    assert queue_module._selected_provider_files(
        _show_meta(), (1,), wrong_size
    ) is None


def test_selected_provider_files_accepts_a_provider_that_omits_sizes():
    """`_safe_filesize` yields 0 for an absent size; 0 is "unknown", not 0 bytes."""
    sizeless = _provider_result(tuple(
        (path, 0, link) for path, _size, link in _SHOW_ENTRIES
    ))

    picked = queue_module._selected_provider_files(_show_meta(), (1,), sizeless)

    assert [f.locked_link for f in picked] == [SEL_LINK_EP2]


def test_alldebrid_root_stripping_is_the_committed_provider_transform():
    """The mapper compares root-stripped provider paths because cove.debrid
    already strips AllDebrid's repeated top folder. Pinned here so the two
    halves cannot drift apart."""
    assert debrid._strip_root(
        _SHOW_NAME, (_SHOW_NAME, "Season", "Episode01.mkv")
    ) == ("Season", "Episode01.mkv")
    assert debrid._strip_root(
        _SHOW_NAME, ("Season", "Episode01.mkv")
    ) == ("Season", "Episode01.mkv")


def test_selected_provider_files_does_not_strip_further_parents():
    """A near miss is a miss. No "drop folders until something matches"."""
    unstripped = _provider_result(tuple(
        ((_SHOW_NAME,) + tuple(path), size, link)
        for path, size, link in _SHOW_ENTRIES
    ))

    assert queue_module._selected_provider_files(
        _show_meta(), (1,), unstripped
    ) is None


def test_selected_provider_files_does_not_match_on_basename():
    dupe_raw = _multi_file_torrent_bytes(b"Movie", (
        (7, (b"Disc1", b"movie.mkv")),
        (9, (b"Disc2", b"movie.mkv")),
    ))
    meta = torrent_mod.parse_torrent(dupe_raw)
    flattened = _provider_result(
        ((("movie.mkv",), 7, SEL_LINK_EP1), (("movie.mkv",), 9, SEL_LINK_EP2)),
        name="Movie",
    )

    assert queue_module._selected_provider_files(meta, (1,), flattened) is None


def test_selected_provider_files_does_not_casefold():
    shouty = _provider_result((
        _SHOW_ENTRIES[0],
        (("Season", "episode02.MKV"), 9, SEL_LINK_EP2),
        _SHOW_ENTRIES[2],
    ))

    assert queue_module._selected_provider_files(
        _show_meta(), (1,), shouty
    ) is None


def test_selected_provider_files_refuses_a_collapsed_multi_file_result():
    """Real-Debrid's packed answer: one link standing for the whole torrent.

    It is not any single file of the torrent, so it can never satisfy a
    subset, whatever the archive happens to be called.
    """
    packed = _provider_result(
        ((("Show S01.rar",), 4096, RD_LOCKED_1),), provider=REAL_DEBRID,
    )

    assert queue_module._selected_provider_files(
        _show_meta(), (1,), packed
    ) is None


def test_a_packed_result_cannot_impersonate_the_file_it_is_named_after():
    """The archive's name is not evidence that it *is* that file.

    Real-Debrid names a packed result after the torrent, and a torrent is
    free to contain a top-level file by that same name. The provider is
    still offering the whole torrent in one piece, so it still cannot serve
    a subset - and here the paths match, so only the collapsed-result rule
    can say so. The size is left unknown on purpose, to keep the size
    cross-check out of it.
    """
    raw = _multi_file_torrent_bytes(b"Show S01", (
        (7, (b"Show S01.rar",)),
        (9, (b"Season", b"Episode02.mkv")),
    ))
    packed = _provider_result(
        ((("Show S01.rar",), 0, RD_LOCKED_1),), provider=REAL_DEBRID,
    )

    assert queue_module._selected_provider_files(
        torrent_mod.parse_torrent(raw), (0,), packed
    ) is None


def test_selected_provider_files_keeps_the_selection_order():
    reordered = _provider_result((
        _SHOW_ENTRIES[2], _SHOW_ENTRIES[1], _SHOW_ENTRIES[0],
    ))

    picked = queue_module._selected_provider_files(_show_meta(), (0, 1, 2), reordered)

    assert [f.relative_path for f in picked] == [
        "Season/Episode01.mkv", "Season/Episode02.mkv", "Sample.mkv",
    ]


# --- AllDebrid -------------------------------------------------------------


def test_selected_alldebrid_materialises_exactly_one_file(
    queue_env, monkeypatch, tmp_path
):
    queue, rpc, db_path, tid, calls = _selected_debrid_env(
        queue_env, monkeypatch, tmp_path, (1,), _provider_result(),
    )

    queue._launch(queue.tasks[tid])

    assert _materialised_files(db_path) == [
        (str(tmp_path / "Show S01" / "Season"), "Episode02.mkv", SEL_LINK_EP2),
    ]
    assert len(_rows(db_path)) == 1
    assert rpc.torrents == []


def test_selected_alldebrid_materialises_exactly_two_files(
    queue_env, monkeypatch, tmp_path
):
    queue, rpc, db_path, tid, calls = _selected_debrid_env(
        queue_env, monkeypatch, tmp_path, (0, 1), _provider_result(),
    )

    queue._launch(queue.tasks[tid])

    assert _materialised_files(db_path) == [
        (str(tmp_path / "Show S01" / "Season"), "Episode01.mkv", SEL_LINK_EP1),
        (str(tmp_path / "Show S01" / "Season"), "Episode02.mkv", SEL_LINK_EP2),
    ]


def test_selected_materialisation_survives_a_reordered_provider_manifest(
    queue_env, monkeypatch, tmp_path
):
    reordered = _provider_result((
        _SHOW_ENTRIES[2], _SHOW_ENTRIES[0], _SHOW_ENTRIES[1],
    ))
    queue, rpc, db_path, tid, calls = _selected_debrid_env(
        queue_env, monkeypatch, tmp_path, (0, 2), reordered,
    )

    queue._launch(queue.tasks[tid])

    assert _materialised_files(db_path) == [
        (str(tmp_path / "Show S01" / "Season"), "Episode01.mkv", SEL_LINK_EP1),
        (str(tmp_path / "Show S01"), "Sample.mkv", SEL_LINK_SAMPLE),
    ]


def test_a_selected_nested_file_keeps_its_provider_directory(
    queue_env, monkeypatch, tmp_path
):
    """Deselecting siblings must not flatten what is left into the root."""
    nested_raw = _multi_file_torrent_bytes(b"Show S01", (
        (7, (b"Season", b"Episode01.mkv")),
        (4, (b"Season", b"Subs", b"Episode02.srt")),
    ))
    nested = _provider_result(
        (
            (("Season", "Episode01.mkv"), 7, SEL_LINK_EP1),
            (("Season", "Subs", "Episode02.srt"), 4, SEL_LINK_EP2),
        ),
    )
    queue, rpc, db_path, tid, calls = _selected_debrid_env(
        queue_env, monkeypatch, tmp_path, (1,), nested, raw=nested_raw,
    )

    queue._launch(queue.tasks[tid])

    assert _materialised_files(db_path) == [
        (
            str(tmp_path / "Show S01" / "Season" / "Subs"),
            "Episode02.srt",
            SEL_LINK_EP2,
        ),
    ]


def test_unselected_provider_extras_are_not_materialised(
    queue_env, monkeypatch, tmp_path
):
    extra = _provider_result(_SHOW_ENTRIES + (
        (("readme.nfo",), 3, SEL_LINK_EXTRA),
    ))
    queue, rpc, db_path, tid, calls = _selected_debrid_env(
        queue_env, monkeypatch, tmp_path, (0, 2), extra,
    )

    queue._launch(queue.tasks[tid])

    assert [name for _dir, name, _url in _materialised_files(db_path)] == [
        "Episode01.mkv", "Sample.mkv",
    ]
    assert SEL_LINK_EXTRA not in calls["unlock"]


# --- Real-Debrid -----------------------------------------------------------


def _rd_result(entries=_SHOW_ENTRIES):
    return _provider_result(entries, provider=REAL_DEBRID)


def _rd_settings():
    return dict(real_debrid_enabled=True, real_debrid_api_token="rd-token-value")


def test_selected_real_debrid_file_keeps_its_own_link(
    queue_env, monkeypatch, tmp_path
):
    """The classic filtering bug: keep file B, hand it link A."""
    queue, rpc, db_path, tid, calls = _selected_debrid_env(
        queue_env, monkeypatch, tmp_path, (1,), _rd_result(), **_rd_settings(),
    )

    queue._launch(queue.tasks[tid])

    assert _materialised_files(db_path) == [
        (str(tmp_path / "Show S01" / "Season"), "Episode02.mkv", SEL_LINK_EP2),
    ]
    assert calls["unlock"] == [SEL_LINK_EP2]


def test_a_non_contiguous_real_debrid_selection_keeps_both_links(
    queue_env, monkeypatch, tmp_path
):
    """Skipping the middle file must not shift the links up by one."""
    queue, rpc, db_path, tid, calls = _selected_debrid_env(
        queue_env, monkeypatch, tmp_path, (0, 2), _rd_result(), **_rd_settings(),
    )

    queue._launch(queue.tasks[tid])

    assert [url for _dir, _name, url in _materialised_files(db_path)] == [
        SEL_LINK_EP1, SEL_LINK_SAMPLE,
    ]
    assert SEL_LINK_EP2 not in calls["unlock"]


def test_a_packed_real_debrid_result_cannot_satisfy_a_selection(
    queue_env, monkeypatch, tmp_path
):
    packed = _provider_result(
        ((("Show S01.rar",), 4096, RD_LOCKED_1),), provider=REAL_DEBRID,
    )
    queue, rpc, db_path, tid, calls = _selected_debrid_env(
        queue_env, monkeypatch, tmp_path, (1,), packed, **_rd_settings(),
    )

    queue._launch(queue.tasks[tid])

    _fell_back_locally(queue, rpc, db_path, tid, "2")
    assert calls["unlock"] == []


def test_a_packed_real_debrid_result_is_untouched_without_a_selection(
    queue_env, monkeypatch, tmp_path
):
    """The compatibility half: whole-torrent users keep the packed download."""
    packed = _provider_result(
        ((("Show S01.rar",), 4096, RD_LOCKED_1),), provider=REAL_DEBRID,
    )
    queue, rpc, db_path, tid, calls = _selected_debrid_env(
        queue_env, monkeypatch, tmp_path, None, packed, **_rd_settings(),
    )

    queue._launch(queue.tasks[tid])

    assert _materialised_files(db_path) == [
        (str(tmp_path), "Show S01.rar", RD_LOCKED_1),
    ]
    assert rpc.torrents == []


# --- TorBox ----------------------------------------------------------------


def test_selected_torbox_materialises_only_the_chosen_file_ids(
    queue_env, monkeypatch, tmp_path, torbox_available
):
    queue, rpc, db_path, tid, calls = _selected_debrid_env(
        queue_env, monkeypatch, tmp_path, (0, 2), _torbox_provider_result(),
        **_torbox_settings(),
    )

    queue._launch(queue.tasks[tid])

    rows = [r for r in _rows(db_path) if r["source_type"] == SOURCE_TORRENT_FILE]
    assert [r["debrid_file_id"] for r in rows] == ["1", "3"]
    assert [r["filename"] for r in rows] == ["Episode01.mkv", "Sample.mkv"]
    assert [r["debrid_route"] for r in rows] == [debrid.TORBOX] * 2


def test_torbox_never_requests_a_delivery_url_for_a_deselected_file(
    queue_env, monkeypatch, tmp_path, torbox_available
):
    """TorBox resolves links per launch, so a file with no row can never
    reach requestdl. Pinned because filtering later than task creation
    would quietly restore those calls."""
    queue, rpc, db_path, tid, calls = _selected_debrid_env(
        queue_env, monkeypatch, tmp_path, (1,), _torbox_provider_result(),
        **_torbox_settings(),
    )

    queue._launch(queue.tasks[tid])

    assert [file_id for _item, file_id in calls["requestdl"]] == ["2"]


# --- fail closed -----------------------------------------------------------


def test_a_selected_file_the_provider_lacks_materialises_nothing(
    queue_env, monkeypatch, tmp_path
):
    """All or nothing: Episode01 maps, Sample does not, so neither is created."""
    partial = _provider_result(_SHOW_ENTRIES[:2])
    queue, rpc, db_path, tid, calls = _selected_debrid_env(
        queue_env, monkeypatch, tmp_path, (0, 2), partial,
    )

    queue._launch(queue.tasks[tid])

    _fell_back_locally(queue, rpc, db_path, tid, "1,3")
    assert calls["unlock"] == []


def test_an_index_outside_the_manifest_materialises_nothing(
    queue_env, monkeypatch, tmp_path
):
    queue, rpc, db_path, tid, calls = _selected_debrid_env(
        queue_env, monkeypatch, tmp_path, (0, 5), _provider_result(),
    )

    queue._launch(queue.tasks[tid])

    # The subset reaches aria2 unchanged; aria2 ignores a file number the
    # torrent does not have. Cove does not get to reinterpret it.
    _fell_back_locally(queue, rpc, db_path, tid, "1,6")


def test_an_ambiguous_provider_match_materialises_nothing(
    queue_env, monkeypatch, tmp_path
):
    doubled = _provider_result(_SHOW_ENTRIES + (
        (("Sample.mkv",), 5, SEL_LINK_EXTRA),
    ))
    queue, rpc, db_path, tid, calls = _selected_debrid_env(
        queue_env, monkeypatch, tmp_path, (2,), doubled,
    )

    queue._launch(queue.tasks[tid])

    _fell_back_locally(queue, rpc, db_path, tid, "3")


def test_a_missing_managed_torrent_materialises_nothing(
    queue_env, monkeypatch, tmp_path
):
    """No authoritative manifest, no way to say which provider file is which."""
    queue, rpc, db_path, tid, calls = _selected_debrid_env(
        queue_env, monkeypatch, tmp_path, (1,), _provider_result(),
    )
    os.remove(queue.tasks[tid].torrent_path)

    queue._launch(queue.tasks[tid])

    assert _materialised_files(db_path) == []
    assert queue.tasks[tid].source_type == SOURCE_TORRENT
    assert calls["unlock"] == []


def test_a_selection_cannot_reach_debrid_through_a_manifest_less_magnet(
    queue_env, monkeypatch, tmp_path
):
    """Slice 1's magnet rule is not softened by the new provider route."""
    queue, rpc, db_path, tid, calls = _selected_debrid_env(
        queue_env, monkeypatch, tmp_path, (1,), _provider_result(),
    )
    task = queue.tasks[tid]
    task.torrent_path = ""

    queue._launch(task)

    assert _materialised_files(db_path) == []
    assert task.status == "error"
    assert rpc.magnets == []


# --- fallback --------------------------------------------------------------


def test_an_uncached_selected_torrent_keeps_the_existing_fallback(
    queue_env, monkeypatch, tmp_path
):
    queue, rpc, db_path, tid, calls = _selected_debrid_env(
        queue_env, monkeypatch, tmp_path, (0, 2), None,
    )

    queue._launch(queue.tasks[tid])

    assert len(calls["probe"]) == 1
    _fell_back_locally(queue, rpc, db_path, tid, "1,3")


def test_an_unmappable_result_does_not_start_a_local_torrent_when_forbidden(
    queue_env, monkeypatch, tmp_path
):
    """"Cancel the download" stays authoritative for a selected torrent."""
    partial = _provider_result(_SHOW_ENTRIES[:2])
    queue, rpc, db_path, tid, calls = _selected_debrid_env(
        queue_env, monkeypatch, tmp_path, (0, 2), partial,
        torrent_fallback_mode="never",
    )

    queue._launch(queue.tasks[tid])

    assert _materialised_files(db_path) == []
    assert rpc.torrents == []
    assert queue.tasks[tid].status == "error"
    assert queue.tasks[tid].error == TORRENT_CANCELLED_UNCACHED


def test_a_successful_selected_materialisation_leaves_the_selection_alone(
    queue_env, monkeypatch, tmp_path
):
    queue, rpc, db_path, tid, calls = _selected_debrid_env(
        queue_env, monkeypatch, tmp_path, (0, 2), _provider_result(),
    )

    queue._launch(queue.tasks[tid])

    assert queue.tasks[tid].selected_files == (0, 2)
    assert _persisted_row(db_path, tid)["selected_files"] == "0,2"


def test_a_restored_selection_still_drives_provider_filtering(
    queue_env, monkeypatch, tmp_path
):
    """The subset survives a restart, so the provider route must reuse it."""
    queue, rpc, db_path, tid, calls = _selected_debrid_env(
        queue_env, monkeypatch, tmp_path, (1,), _provider_result(),
    )
    queue2, rpc2, _db = queue_env(**_local_settings())
    _sync_spawn(queue2)
    monkeypatch.setattr(debrid, "resolve_torrent", lambda *a, **k: _provider_result())
    _running(queue2)

    restored = queue2.tasks[tid]
    assert restored.selected_files == (1,)
    queue2._launch(restored)

    assert _materialised_files(db_path) == [
        (str(tmp_path / "Show S01" / "Season"), "Episode02.mkv", SEL_LINK_EP2),
    ]


# --- no selection: the legacy provider path is untouched --------------------


@pytest.mark.parametrize("result", ["alldebrid", "torbox"])
def test_no_selection_materialises_every_provider_file(
    queue_env, monkeypatch, tmp_path, torbox_available, result
):
    cached = (
        _provider_result() if result == "alldebrid" else _torbox_provider_result()
    )
    extra = {} if result == "alldebrid" else _torbox_settings()
    queue, rpc, db_path, tid, calls = _selected_debrid_env(
        queue_env, monkeypatch, tmp_path, None, cached, **extra,
    )

    queue._launch(queue.tasks[tid])

    assert [name for _dir, name, _url in _materialised_files(db_path)] == [
        "Episode01.mkv", "Episode02.mkv", "Sample.mkv",
    ]
    assert len(calls["probe"]) == 1


def test_no_selection_is_not_subjected_to_manifest_mapping(
    queue_env, monkeypatch, tmp_path
):
    """A provider manifest that no selection could ever map still works.

    Paths the torrent does not contain, a size that disagrees and a missing
    managed `.torrent` are all fatal to a subset and all irrelevant without
    one.
    """
    unmappable = _provider_result((
        (("elsewhere", "one.bin"), 1234, SEL_LINK_EP1),
        (("elsewhere", "two.bin"), 5678, SEL_LINK_EP2),
    ))
    queue, rpc, db_path, tid, calls = _selected_debrid_env(
        queue_env, monkeypatch, tmp_path, None, unmappable,
    )
    os.remove(queue.tasks[tid].torrent_path)

    queue._launch(queue.tasks[tid])

    assert [name for _dir, name, _url in _materialised_files(db_path)] == [
        "one.bin", "two.bin",
    ]
    assert rpc.torrents == []
