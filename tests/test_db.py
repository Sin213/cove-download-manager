"""Tests for additive cove.db migrations."""

import os
import sqlite3
import stat
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

from cove import config, db


@pytest.mark.skipif(os.name == "nt", reason="POSIX fcntl lock regression")
@pytest.mark.parametrize("mode", [0o600, 0o644], ids=["private", "migration"])
def test_s04_second_connection_preserves_process_write_lock(tmp_path, monkeypatch, mode):
    """C1: only an independent process can detect the silently dropped lock."""
    _, path = _s04_db_paths(tmp_path, monkeypatch)
    db.init(path)
    probe = r'''
import sqlite3, sys
connection = sqlite3.connect(sys.argv[1], timeout=0.3, isolation_level=None)
try:
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as error:
        assert "database is locked" in str(error)
        print("LOCKED")
    else:
        print("ACQUIRED")
finally:
    connection.close()
'''

    def lock_status():
        result = subprocess.run(
            [sys.executable, "-c", probe, str(path)], capture_output=True,
            text=True, check=True, timeout=3,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        )
        return result.stdout.strip()

    with closing(sqlite3.connect(path, isolation_level=None)) as holder:
        holder.execute("BEGIN IMMEDIATE")
        path.chmod(mode)
        assert lock_status() == "LOCKED", "establish the cross-process oracle first"
        with db.connect(path):
            pass
        assert lock_status() == "LOCKED", "preparing another connection must not release SQLite's locks"
        holder.rollback()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor/lock regression")
@pytest.mark.parametrize("operation", ["init", "connect"])
def test_s04_private_db_preparation_does_not_open_database(tmp_path, monkeypatch, operation):
    """C1: even a failed O_EXCL probe is unnecessary on an existing private DB."""
    _, path = _s04_db_paths(tmp_path, monkeypatch)
    db.init(path)
    opened = []
    original_open = os.open

    def observe_open(name, *args, **kwargs):
        if Path(name) == path:
            opened.append(name)
        return original_open(name, *args, **kwargs)

    monkeypatch.setattr(os, "open", observe_open)
    if operation == "init":
        db.init(path)
    else:
        with db.connect(path) as conn:
            assert conn.execute("SELECT 1").fetchone()[0] == 1
    assert not opened, "auxiliary database descriptors can release another connection's fcntl locks"


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes and symlinks")
@pytest.mark.parametrize("operation", ["init", "connect"])
@pytest.mark.parametrize("spelling", ["relative", "dotdot"])
@pytest.mark.parametrize("leaf", ["permissive", "symlink"])
def test_s04_db_alias_preserves_owned_leaf_boundary(tmp_path, monkeypatch, operation, spelling, leaf):
    """C3: normalise spelling, while preserving the final symlink for refusal."""
    data_dir = tmp_path / "data" / "cove"
    data_dir.parent.mkdir()
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.chdir(tmp_path)
    if spelling == "relative":
        path = Path("data/cove/cove.db")
    else:
        (tmp_path / "alias").mkdir()
        path = tmp_path / "alias" / ".." / "data" / "cove" / "cove.db"
    target = tmp_path / "unrelated"
    if leaf == "symlink":
        target.mkdir()
        target.chmod(0o755)
        (target / "sentinel").write_bytes(b"S04_SYNTHETIC_COOKIE")
        data_dir.symlink_to(target, target_is_directory=True)
        before = {p.name: (p.read_bytes(), stat.S_IMODE(p.stat().st_mode)) for p in target.iterdir()}
    else:
        data_dir.mkdir()
        data_dir.chmod(0o755)
    observed = []
    original_connect = sqlite3.connect

    def observe_connect(database, *args, **kwargs):
        observed.append(stat.S_IMODE(data_dir.stat().st_mode))
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(db.sqlite3, "connect", observe_connect)
    error = None
    previous = os.umask(0o022)
    try:
        try:
            if operation == "init":
                db.init(path)
            else:
                with db.connect(path) as conn:
                    assert conn.execute("SELECT 1").fetchone()[0] == 1
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            error = exc
    finally:
        os.umask(previous)
    if leaf == "symlink":
        after = {p.name: (p.read_bytes(), stat.S_IMODE(p.stat().st_mode)) for p in target.iterdir()}
        assert after == before
        assert stat.S_IMODE(target.stat().st_mode) == 0o755
        assert not observed, "refuse the aliased symlink before SQLite is called"
        assert error is not None and str(error)
        assert "S04_SYNTHETIC_COOKIE" not in str(error)
    else:
        assert error is None
        assert observed == [0o700]
        assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX private-file creation flags/modes")
@pytest.mark.parametrize("owned", [True, False], ids=["cove-leaf", "custom-parent"])
@pytest.mark.parametrize("operation", ["init", "connect"])
@pytest.mark.parametrize("mask", [0o022, 0o000], ids=["umask022", "umask000"])
def test_s04_db_creation_mode_oracle(tmp_path, monkeypatch, owned, operation, mask):
    """C5: later chmod cannot conceal a permissive initial creation request."""
    if owned:
        parent, path = _s04_db_paths(tmp_path, monkeypatch)
    else:
        parent = tmp_path / "custom-parent"
        path = parent / "custom.db"
    parent.mkdir(parents=True)
    parent.chmod(0o755)
    events = []
    original_open, original_chmod, original_fchmod = os.open, os.chmod, os.fchmod

    def observe_open(name, flags, mode=0o777, **kwargs):
        fd = original_open(name, flags, mode, **kwargs)
        if Path(name) == path and flags & os.O_CREAT:
            events.append(("create", mode, flags, stat.S_IMODE(os.fstat(fd).st_mode)))
        return fd

    def observe_chmod(name, mode, **kwargs):
        if Path(name) == path:
            events.append(("chmod", mode))
        return original_chmod(name, mode, **kwargs)

    def observe_fchmod(fd, mode):
        if path.exists():
            info, target = os.fstat(fd), path.stat()
            if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
                events.append(("fchmod", mode))
        return original_fchmod(fd, mode)

    monkeypatch.setattr(os, "open", observe_open)
    monkeypatch.setattr(os, "chmod", observe_chmod)
    monkeypatch.setattr(os, "fchmod", observe_fchmod)
    previous = os.umask(mask)
    try:
        if operation == "init":
            db.init(path)
        else:
            with db.connect(path) as conn:
                assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        os.umask(previous)
    creations = [event for event in events if event[0] == "create"]
    assert len(creations) == 1, events
    _, mode, flags, initial = creations[0]
    assert mode == 0o600, events
    required = os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    assert flags & required == required, events
    assert initial == 0o600, events
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(parent.stat().st_mode) == (0o700 if owned else 0o755)


def _s04_db_paths(tmp_path, monkeypatch):
    data_dir = tmp_path / "xdg-data" / "cove"
    path = data_dir / "cove.db"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_FILE", path)
    monkeypatch.setattr(db, "DB_FILE", path)
    # db currently imports only DB_FILE. Also isolate a future DATA_DIR alias.
    monkeypatch.setattr(db, "DATA_DIR", data_dir, raising=False)
    return data_dir, path


def _s04_snapshot(conn):
    return {
        "version": conn.execute("PRAGMA user_version").fetchone()[0],
        "schema": conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall(),
        "columns": conn.execute("PRAGMA table_info(downloads)").fetchall(),
        "indexes": conn.execute("PRAGMA index_list(downloads)").fetchall(),
        "journal": conn.execute("PRAGMA journal_mode").fetchone()[0],
        "synchronous": conn.execute("PRAGMA synchronous").fetchone()[0],
        "locking": conn.execute("PRAGMA locking_mode").fetchone()[0],
        "rows": conn.execute("SELECT * FROM downloads ORDER BY id").fetchall(),
    }


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes do not describe Windows ACLs")
@pytest.mark.parametrize("operation", ["init", "connect"])
@pytest.mark.parametrize("mask", [0o022, 0o000], ids=["umask022", "umask000"])
def test_s04_fresh_db_private_before_sqlite(tmp_path, monkeypatch, mask, operation):
    """D1/D2: even connect() must securely pre-create a missing database."""
    data_dir, path = _s04_db_paths(tmp_path, monkeypatch)
    if operation == "connect":
        # connect() historically expects a parent; do not require a new API.
        data_dir.mkdir(parents=True)
        data_dir.chmod(0o755)
    observed = []
    original_connect = sqlite3.connect

    def observe_connect(database, *args, **kwargs):
        observed.append((
            stat.S_IMODE(data_dir.stat().st_mode),
            stat.S_IMODE(path.stat().st_mode) if path.exists() else None,
        ))
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(db.sqlite3, "connect", observe_connect)
    previous = os.umask(mask)
    try:
        if operation == "init":
            db.init(path)
        else:
            with db.connect(path) as conn:
                assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        os.umask(previous)
    with closing(original_connect(path)) as conn:
        if operation == "init":
            assert conn.execute("SELECT name FROM sqlite_master WHERE name='downloads'").fetchone()
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
    assert observed == [(0o700, 0o600)], "permissions must precede SQLite's first open"
    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes do not describe Windows ACLs")
@pytest.mark.parametrize("operation", ["init", "connect"])
def test_s04_migration_precedes_sqlite_and_preserves_history(tmp_path, monkeypatch, operation):
    """D3/M3: record modes at the actual sqlite3.connect boundary."""
    data_dir, path = _s04_db_paths(tmp_path, monkeypatch)
    db.init(path)
    with db.connect(path) as conn:
        conn.execute(
            "INSERT INTO downloads (url,out_dir,created_at,cookies,referrer,user_agent) "
            "VALUES (?,?,?,?,?,?)",
            ("http://example.invalid/s04.bin?sig=S04_SYNTHETIC_COOKIE", str(tmp_path), 1,
             "Cookie: sid=S04_SYNTHETIC_COOKIE", "http://example.invalid/ref", "S04-UA"),
        )
    original_connect = sqlite3.connect
    with closing(original_connect(path)) as conn:
        before = _s04_snapshot(conn)
    data_dir.chmod(0o755)
    path.chmod(0o644)
    observed = []

    def observe_connect(database, *args, **kwargs):
        observed.append((stat.S_IMODE(data_dir.stat().st_mode), stat.S_IMODE(path.stat().st_mode)))
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(db.sqlite3, "connect", observe_connect)
    previous = os.umask(0o022)
    try:
        if operation == "init":
            db.init(path)
        else:
            with db.connect(path) as conn:
                assert conn.execute("SELECT cookies FROM downloads").fetchone()[0] == "Cookie: sid=S04_SYNTHETIC_COOKIE"
    finally:
        os.umask(previous)
    with closing(original_connect(path)) as conn:
        assert _s04_snapshot(conn) == before
    assert observed == [(0o700, 0o600)], "late chmod leaves the initial SQLite open unsafe"


def test_s04_permission_migration_keeps_schema_and_pragmas(tmp_path, monkeypatch):
    """D11: permissions do not authorize schema or SQLite policy changes."""
    data_dir, path = _s04_db_paths(tmp_path, monkeypatch)
    db.init(path)
    with closing(sqlite3.connect(path)) as conn:
        before = _s04_snapshot(conn)
    if os.name != "nt":
        data_dir.chmod(0o755)
        path.chmod(0o644)
    db.init(path)
    with db.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
    with closing(sqlite3.connect(path)) as conn:
        assert _s04_snapshot(conn) == before


def test_v7_upgrade_adds_exactly_two_empty_ownership_columns(tmp_path):
    path = tmp_path / "v7.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(db.SCHEMA)
        for migration in db._MIGRATIONS[:7]:
            for sql in migration:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc):
                        raise
        conn.execute("PRAGMA user_version=7")
        conn.execute("INSERT INTO downloads (url,out_dir,filename,created_at) VALUES (?,?,?,?)",
                     ("https://example.com/legacy", str(tmp_path), "legacy.zip", 1.0))
        before = {r[1] for r in conn.execute("PRAGMA table_info(downloads)")}
    db.init(path)
    with db.connect(path) as conn:
        after = {r["name"] for r in conn.execute("PRAGMA table_info(downloads)")}
        assert after - before == {"work_dir", "output_path"}
        row = conn.execute("SELECT * FROM downloads").fetchone()
        assert row["filename"] == "legacy.zip"
        assert row["work_dir"] == row["output_path"] == ""
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8


def test_fresh_db_ownership_columns_round_trip(tmp_path):
    path = tmp_path / "fresh.db"
    db.init(path)
    with db.connect(path) as conn:
        columns = {r["name"]: r for r in conn.execute("PRAGMA table_info(downloads)")}
        for name in ("work_dir", "output_path"):
            assert columns[name]["type"] == "TEXT"
            assert columns[name]["notnull"] == 1
        conn.execute("INSERT INTO downloads (url,out_dir,created_at,work_dir,output_path) "
                     "VALUES (?,?,?,?,?)", ("https://example.com/f", "/scratch", 1, "/scratch/.cove-work-a", "/scratch/f"))
    with db.connect(path) as conn:
        row = conn.execute("SELECT work_dir,output_path FROM downloads").fetchone()
        assert tuple(row) == ("/scratch/.cove-work-a", "/scratch/f")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8


def _v0_db(path):
    """Create a pre-migration (v0) database with one legacy row."""
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)
    conn.execute(
        "INSERT INTO downloads (url, out_dir, created_at) VALUES (?,?,?)",
        ("https://example.com/f.zip", "/dl", time.time()),
    )
    conn.commit()
    conn.close()


def test_old_db_gains_convert_mp3_default_zero(tmp_path):
    path = tmp_path / "cove.db"
    _v0_db(path)
    db.init(path)
    with db.connect(path) as conn:
        row = conn.execute("SELECT * FROM downloads").fetchone()
        assert row["convert_mp3"] == 0
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == len(db._MIGRATIONS)


def test_init_idempotent(tmp_path):
    path = tmp_path / "cove.db"
    db.init(path)
    db.init(path)
    with db.connect(path) as conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(downloads)")]
    assert "convert_mp3" in cols


def test_convert_mp3_round_trips(tmp_path):
    path = tmp_path / "cove.db"
    db.init(path)
    with db.connect(path) as conn:
        conn.execute(
            "INSERT INTO downloads (url, out_dir, created_at, convert_mp3) "
            "VALUES (?,?,?,?)",
            ("https://example.com/a.mp4", "/dl", time.time(), 1),
        )
        conn.execute(
            "INSERT INTO downloads (url, out_dir, created_at) VALUES (?,?,?)",
            ("https://example.com/b.mp4", "/dl", time.time()),
        )
    with db.connect(path) as conn:
        rows = conn.execute(
            "SELECT url, convert_mp3 FROM downloads ORDER BY id"
        ).fetchall()
    assert bool(rows[0]["convert_mp3"]) is True
    assert bool(rows[1]["convert_mp3"]) is False


def test_migration_caps_connections_for_stock_aria2(tmp_path):
    path = tmp_path / "cove.db"
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)
    conn.execute("PRAGMA user_version = 3")
    conn.execute(
        "INSERT INTO downloads (url, out_dir, connections, created_at) VALUES (?,?,?,?)",
        ("https://example.com/large.bin", "/dl", 32, time.time()),
    )
    conn.commit()
    conn.close()

    db.init(path)

    with db.connect(path) as conn:
        row = conn.execute("SELECT connections FROM downloads").fetchone()
    assert row["connections"] == 16


# ---------------------------------------------------------------------------
# v5 -> v6: torrent columns
# ---------------------------------------------------------------------------

_TORRENT_COLUMNS = (
    "source_type", "info_hash", "torrent_name",
    "torrent_path", "debrid_route", "selected_files",
)

INFO_HASH = "0123456789abcdef0123456789abcdef01234567"
LOCKED_LINK = "https://alldebrid.com/f/LOCKEDONE"


def _v5_db(path):
    """A database at the previous schema version, with one existing row."""
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)
    for sql in (
        "ALTER TABLE downloads ADD COLUMN backend TEXT DEFAULT 'aria2'",
        "ALTER TABLE downloads ADD COLUMN convert_mp3 INTEGER DEFAULT 0",
        "ALTER TABLE downloads ADD COLUMN cookies TEXT DEFAULT ''",
        "ALTER TABLE downloads ADD COLUMN referrer TEXT DEFAULT ''",
        "ALTER TABLE downloads ADD COLUMN user_agent TEXT DEFAULT ''",
    ):
        conn.execute(sql)
    conn.execute("PRAGMA user_version = 5")
    conn.execute(
        "INSERT INTO downloads (url, out_dir, created_at, status) VALUES (?,?,?,?)",
        ("https://example.com/f.zip", "/dl", time.time(), "completed"),
    )
    conn.commit()
    conn.close()


def _columns(path):
    with db.connect(path) as conn:
        return [r["name"] for r in conn.execute("PRAGMA table_info(downloads)")]


def test_v5_database_gains_the_torrent_columns(tmp_path):
    path = tmp_path / "cove.db"
    _v5_db(path)
    db.init(path)
    cols = _columns(path)
    for name in _TORRENT_COLUMNS:
        assert name in cols
    with db.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db._MIGRATIONS)


def test_existing_rows_keep_working_after_v6(tmp_path):
    path = tmp_path / "cove.db"
    _v5_db(path)
    db.init(path)
    with db.connect(path) as conn:
        row = conn.execute("SELECT * FROM downloads").fetchone()
    assert row["url"] == "https://example.com/f.zip"
    assert row["status"] == "completed"
    for name in _TORRENT_COLUMNS:
        assert row[name] == ""


def test_fresh_database_has_the_torrent_columns(tmp_path):
    path = tmp_path / "cove.db"
    db.init(path)
    cols = _columns(path)
    for name in _TORRENT_COLUMNS:
        assert name in cols


def test_v6_migration_is_idempotent(tmp_path):
    path = tmp_path / "cove.db"
    _v5_db(path)
    db.init(path)
    db.init(path)
    db.init(path)
    cols = _columns(path)
    for name in _TORRENT_COLUMNS:
        assert cols.count(name) == 1


def test_torrent_source_row_round_trips(tmp_path):
    path = tmp_path / "cove.db"
    db.init(path)
    magnet = f"magnet:?xt=urn:btih:{INFO_HASH}"
    with db.connect(path) as conn:
        conn.execute(
            "INSERT INTO downloads (url, out_dir, created_at, source_type, "
            "info_hash, torrent_name, torrent_path) VALUES (?,?,?,?,?,?,?)",
            (magnet, "/dl", time.time(), "torrent", INFO_HASH,
             "Season 1", "/home/u/s1.torrent"),
        )
    with db.connect(path) as conn:
        row = conn.execute("SELECT * FROM downloads").fetchone()
    assert row["source_type"] == "torrent"
    assert row["info_hash"] == INFO_HASH
    assert row["torrent_name"] == "Season 1"
    assert row["torrent_path"] == "/home/u/s1.torrent"
    assert row["debrid_route"] == ""
    # Reserved for per-file selection in a later slice.
    assert row["selected_files"] == ""


def test_torrent_file_row_persists_the_locked_link_only(tmp_path):
    path = tmp_path / "cove.db"
    db.init(path)
    with db.connect(path) as conn:
        conn.execute(
            "INSERT INTO downloads (url, out_dir, created_at, filename, "
            "total_bytes, source_type, info_hash, torrent_name, debrid_route) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (LOCKED_LINK, "/dl/Season 1", time.time(), "ep1.mkv", 10,
             "torrent_file", INFO_HASH, "Season 1", "alldebrid"),
        )
    with db.connect(path) as conn:
        row = conn.execute("SELECT * FROM downloads").fetchone()
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(downloads)")]
    assert row["url"] == LOCKED_LINK
    assert row["source_type"] == "torrent_file"
    assert row["debrid_route"] == "alldebrid"
    assert row["total_bytes"] == 10
    # No column exists that could hold a generated delivery URL or a
    # credential; both stay transient by construction.
    for forbidden in ("resolved_url", "download_url", "api_key", "api_token"):
        assert forbidden not in cols
    joined = " ".join(str(row[k]) for k in row.keys())
    assert "debrid.it" not in joined


# ---------------------------------------------------------------------------
# v6 -> v7: third-party debrid provider identifiers (TorBox T1)
# ---------------------------------------------------------------------------

_PROVIDER_ID_COLUMNS = ("debrid_item_id", "debrid_file_id")

TORBOX_ITEM_ID = "42"


def _v6_db(path):
    """A database at the previous schema version, with one existing row."""
    _v5_db(path)
    conn = sqlite3.connect(path)
    for sql in (
        "ALTER TABLE downloads ADD COLUMN source_type TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE downloads ADD COLUMN info_hash TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE downloads ADD COLUMN torrent_name TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE downloads ADD COLUMN torrent_path TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE downloads ADD COLUMN debrid_route TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE downloads ADD COLUMN selected_files TEXT NOT NULL DEFAULT ''",
    ):
        conn.execute(sql)
    conn.execute("PRAGMA user_version = 6")
    conn.commit()
    conn.close()


def test_fresh_database_has_the_provider_id_columns(tmp_path):
    path = tmp_path / "cove.db"
    db.init(path)
    cols = _columns(path)
    for name in _PROVIDER_ID_COLUMNS:
        assert name in cols


def test_v6_database_gains_the_provider_id_columns(tmp_path):
    path = tmp_path / "cove.db"
    _v6_db(path)
    db.init(path)
    cols = _columns(path)
    for name in _PROVIDER_ID_COLUMNS:
        assert name in cols
    with db.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db._MIGRATIONS)


def test_v7_migration_is_idempotent(tmp_path):
    path = tmp_path / "cove.db"
    _v6_db(path)
    db.init(path)
    db.init(path)
    db.init(path)
    cols = _columns(path)
    for name in _PROVIDER_ID_COLUMNS:
        assert cols.count(name) == 1


def test_existing_rows_gain_empty_provider_id_defaults(tmp_path):
    path = tmp_path / "cove.db"
    _v6_db(path)
    db.init(path)
    with db.connect(path) as conn:
        row = conn.execute("SELECT * FROM downloads").fetchone()
    for name in _PROVIDER_ID_COLUMNS:
        assert row[name] == ""


def test_torbox_hoster_item_id_round_trips(tmp_path):
    path = tmp_path / "cove.db"
    db.init(path)
    with db.connect(path) as conn:
        conn.execute(
            "INSERT INTO downloads (url, out_dir, created_at, debrid_route, "
            "debrid_item_id) VALUES (?,?,?,?,?)",
            ("https://rapidgator.net/f", "/dl", time.time(), "torbox", TORBOX_ITEM_ID),
        )
    with db.connect(path) as conn:
        row = conn.execute("SELECT * FROM downloads").fetchone()
    assert row["debrid_route"] == "torbox"
    assert row["debrid_item_id"] == TORBOX_ITEM_ID
    assert row["debrid_file_id"] == ""


def test_alldebrid_and_real_debrid_rows_keep_empty_provider_ids(tmp_path):
    path = tmp_path / "cove.db"
    db.init(path)
    with db.connect(path) as conn:
        conn.execute(
            "INSERT INTO downloads (url, out_dir, created_at, source_type, "
            "info_hash, torrent_name, debrid_route) VALUES (?,?,?,?,?,?,?)",
            (LOCKED_LINK, "/dl", time.time(), "torrent_file", INFO_HASH,
             "Season 1", "alldebrid"),
        )
        conn.execute(
            "INSERT INTO downloads (url, out_dir, created_at, debrid_route) "
            "VALUES (?,?,?,?)",
            ("https://real-debrid.com/d/x", "/dl", time.time(), "real_debrid"),
        )
    with db.connect(path) as conn:
        rows = conn.execute(
            "SELECT debrid_route, debrid_item_id, debrid_file_id "
            "FROM downloads ORDER BY id"
        ).fetchall()
    for row in rows:
        assert row["debrid_item_id"] == ""
        assert row["debrid_file_id"] == ""


def test_no_transient_delivery_url_column_exists(tmp_path):
    path = tmp_path / "cove.db"
    db.init(path)
    cols = _columns(path)
    for forbidden in ("resolved_url", "download_url", "cdn_url", "requestdl_url"):
        assert forbidden not in cols


def test_no_torbox_token_column_or_content_exists(tmp_path):
    path = tmp_path / "cove.db"
    db.init(path)
    cols = _columns(path)
    for forbidden in ("torbox_api_token", "api_token", "token"):
        assert forbidden not in cols
    with db.connect(path) as conn:
        conn.execute(
            "INSERT INTO downloads (url, out_dir, created_at, debrid_route, "
            "debrid_item_id) VALUES (?,?,?,?,?)",
            ("https://rapidgator.net/f", "/dl", time.time(), "torbox", TORBOX_ITEM_ID),
        )
        row = conn.execute("SELECT * FROM downloads").fetchone()
    joined = " ".join(str(row[k]) for k in row.keys())
    assert "torbox-token" not in joined
