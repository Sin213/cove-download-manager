"""Regression tests for cove.queue._load_persisted row restoration.

Guards against a pre-existing bug where sqlite3.Row (which has no .get())
was accessed with row.get("backend", ...), raising AttributeError whenever
a persisted queued/active/paused task was restored on startup.
"""

import errno
import hashlib
import os
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
        if name in ("magnets", "torrents", "removed", "unpaused", "version_calls"):
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
                              "speed_limit_kbps": speed_limit_kbps})
        return "gid-file"

    def get_files(self, gid):
        return getattr(self, "files_result", [])

    def remove(self, gid, force=True):
        self.removed.append(gid)
        return gid

    def unpause(self, gid):
        self.unpaused.append(gid)
        return gid


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


def test_magnet_keeps_head_behaviour_while_the_flag_is_off(queue_env, monkeypatch):
    queue, rpc, db_path = queue_env(**_debrid_settings())
    called = []
    monkeypatch.setattr(
        debrid, "resolve_torrent",
        lambda *a, **k: called.append(a) or None,
    )
    tid = queue.add_url(MAGNET)
    task = queue.tasks[tid]

    assert task.source_type == ""
    assert task.info_hash == ""
    assert task.url == MAGNET
    assert task.backend == "aria2"
    assert _persisted_row(db_path, tid)["source_type"] == ""
    assert called == []


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


def test_fake_extractor_missing_result_does_not_complete(
    queue_env, fake_process, tmp_path
):
    queue, _rpc, _db_path = queue_env()
    task, proc = _start_extractor(queue, fake_process, tmp_path)
    _extractor_private_path(proc).write_bytes(b"unreported")

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

    monkeypatch.setattr(output_paths, "_link_pinned_fd", unsupported_link)
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


def test_output_helper_windows_retries_only_error_already_exists(
    tmp_path, monkeypatch
):
    api, work, source = _windows_test_work(
        tmp_path,
        monkeypatch,
        {"name.ext": ("rename", output_paths._WINDOWS_ERROR_ALREADY_EXISTS)},
    )

    result = publish_output(work, source, "name.ext")

    assert result.name == "name (1).ext"
    assert api.calls == ["name.ext", "name (1).ext"]


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (("rename", 80), "unexpected Windows API error"),
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
def test_output_helper_windows_real_publication(tmp_path):
    work = create_work_directory(tmp_path)
    source = work.path / "name.ext"
    source.write_bytes(b"private")
    target = tmp_path / "name.ext"
    target.write_bytes(b"existing")

    result = publish_output(work, source, "name.ext")

    assert result == tmp_path / "name (1).ext"
    assert result.read_bytes() == b"private"
    assert target.read_bytes() == b"existing"


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
    blocker = api._open_handle(
        blocked_file,
        output_paths._FILE_READ_ATTRIBUTES,
        share_mode=(
            output_paths._FILE_SHARE_READ | output_paths._FILE_SHARE_WRITE
        ),
    )
    try:
        with pytest.raises(OutputPathError):
            cleanup_work_directory(blocked)
        assert blocked.path.exists()
    finally:
        api._close(blocker)
        cleanup_work_directory(blocked)
