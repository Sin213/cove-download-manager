import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import DB_FILE, MAX_CONNECTIONS_PER_SERVER

SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    filename TEXT,
    out_dir TEXT NOT NULL,
    connections INTEGER NOT NULL DEFAULT 16,
    speed_limit_kbps INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'queued',
    gid TEXT,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    completed_bytes INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at REAL NOT NULL,
    finished_at REAL,
    category TEXT NOT NULL DEFAULT 'Other',
    segments INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status);
CREATE INDEX IF NOT EXISTS idx_downloads_gid ON downloads(gid);
"""

_MIGRATIONS = [
    # v0 -> v1: add category and segments columns
    [
        "ALTER TABLE downloads ADD COLUMN category TEXT NOT NULL DEFAULT 'Other'",
        "ALTER TABLE downloads ADD COLUMN segments INTEGER NOT NULL DEFAULT 0",
    ],
    # v1 -> v2: add backend column for HLS support
    [
        "ALTER TABLE downloads ADD COLUMN backend TEXT DEFAULT 'aria2'",
    ],
    # v2 -> v3: add convert_mp3 flag for post-download MP3 conversion
    [
        "ALTER TABLE downloads ADD COLUMN convert_mp3 INTEGER DEFAULT 0",
    ],
    # v3 -> v4: stock aria2 rejects more than 16 connections to one server
    [
        "UPDATE downloads SET connections=1 WHERE connections < 1",
        f"UPDATE downloads SET connections={MAX_CONNECTIONS_PER_SERVER} "
        f"WHERE connections > {MAX_CONNECTIONS_PER_SERVER}",
    ],
    # v4 -> v5: persist browser headers for video (ffmpeg/yt-dlp) downloads
    [
        "ALTER TABLE downloads ADD COLUMN cookies TEXT DEFAULT ''",
        "ALTER TABLE downloads ADD COLUMN referrer TEXT DEFAULT ''",
        "ALTER TABLE downloads ADD COLUMN user_agent TEXT DEFAULT ''",
    ],
    # v5 -> v6: torrent support. Purely additive; every existing row keeps
    # source_type='' and behaves exactly as before.
    #
    #   source_type   ''             a normal HTTP/HLS/yt-dlp download
    #                 'torrent'      the magnet or .torrent the user added
    #                 'torrent_file' one HTTPS file materialised from a
    #                                cached debrid torrent
    #   debrid_route  '' | 'alldebrid' | 'real_debrid' -- which provider
    #                 issued the locked link stored in `url`
    #   torrent_path  local .torrent the source task came from, if any
    #   selected_files reserved for per-file selection (Slice C); always ''
    #
    # Note what is NOT here: no column holds a credential or a generated
    # delivery URL. Those stay transient, as they do for hoster links.
    [
        "ALTER TABLE downloads ADD COLUMN source_type TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE downloads ADD COLUMN info_hash TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE downloads ADD COLUMN torrent_name TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE downloads ADD COLUMN torrent_path TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE downloads ADD COLUMN debrid_route TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE downloads ADD COLUMN selected_files TEXT NOT NULL DEFAULT ''",
        "CREATE INDEX IF NOT EXISTS idx_downloads_info_hash ON downloads(info_hash)",
    ],
]


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, stmts in enumerate(_MIGRATIONS):
        if version <= i:
            for sql in stmts:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
            conn.execute(f"PRAGMA user_version = {i + 1}")
    conn.commit()


def init(path: Path = DB_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


@contextmanager
def connect(path: Path = DB_FILE):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
