"""Regression tests for cove.queue._load_persisted row restoration.

Guards against a pre-existing bug where sqlite3.Row (which has no .get())
was accessed with row.get("backend", ...), raising AttributeError whenever
a persisted queued/active/paused task was restored on startup.
"""

import sqlite3
import time
from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication

from cove import db
from cove.config import CategoryDirs, Settings
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
