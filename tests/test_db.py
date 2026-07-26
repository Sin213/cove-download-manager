"""Tests for additive cove.db migrations."""

import sqlite3
import time

from cove import db


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
