"""Regression tests for cove.queue._load_persisted row restoration.

Guards against a pre-existing bug where sqlite3.Row (which has no .get())
was accessed with row.get("backend", ...), raising AttributeError whenever
a persisted queued/active/paused task was restored on startup.
"""

import sqlite3
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication

from cove import config, db, debrid
from cove.config import CategoryDirs, Settings
from cove.debrid import ALL_DEBRID, DebridError, Unrestricted
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
        queue._drop_poll.stop()


# ---------------------------------------------------------------------------
# Debrid resolution at launch time
# ---------------------------------------------------------------------------

NODE_URL = "https://s1.debrid.it/dl/SECRETNODE/movie.mkv"
ORIGINAL_URL = "https://rapidgator.net/file/abc"


class _FakeRpc:
    """Records what actually reaches aria2."""

    def __init__(self):
        self.added = []

    def add_uri(self, uris, out_dir, connections, speed_limit_kbps, filename):
        self.added.append({
            "uris": list(uris),
            "out_dir": out_dir,
            "connections": connections,
            "filename": filename,
        })
        return "gid-1"


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
        queue._drop_poll.stop()
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
        "requests.head",
        lambda url, **kw: calls.append(url) or (_ for _ in ()).throw(AssertionError("HEAD")),
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
        "requests.head",
        lambda url, **kw: SimpleNamespace(
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
        "requests.head",
        lambda url, **kw: SimpleNamespace(
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
        "requests.head",
        lambda url, **kw: SimpleNamespace(ok=True, headers={"Content-Length": "5"}),
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
