import errno
import os
import sqlite3
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .config import DATA_DIR, DB_FILE, MAX_CONNECTIONS_PER_SERVER, prepare_private_directory

COOKIE_CLEANUP_BUDGET_S = 3.0


@dataclass(frozen=True)
class CookieCleanup:
    status: str
    reason: str = ""
    rows: int = 0
    error_type: str = ""


def clear_legacy_cookies(path=None):
    """Clear the active logical cookie column and verify on a later connection.

    This budget bounds this helper's own accumulated lock waiting only. It is
    not a startup deadline: real SQL and I/O time, stalled storage, db.init and
    the later restoration queries are not covered.
    """
    deadline = time.monotonic() + COOKIE_CLEANUP_BUDGET_S
    committed = False
    detect = "SELECT EXISTS(SELECT 1 FROM downloads WHERE cookies IS NOT NULL AND cookies <> '')"

    def arm(conn):
        remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
        conn.execute(f"PRAGMA busy_timeout={remaining_ms}")

    def connection():
        return connect() if path is None else connect(path)

    try:
        with connection() as conn:
            try:
                arm(conn)
                conn.execute("PRAGMA secure_delete=ON")
                if not conn.execute(detect).fetchone()[0]:
                    return CookieCleanup("not_needed")
                arm(conn)
                conn.execute("BEGIN IMMEDIATE")
                arm(conn)
                rows = conn.execute("UPDATE downloads SET cookies='' WHERE cookies IS NOT NULL AND cookies <> ''").rowcount
                arm(conn)
                conn.commit()
                committed = True
            except Exception:
                conn.rollback()
                raise
        with connection() as conn:
            arm(conn)
            if conn.execute(detect).fetchone()[0]:
                return CookieCleanup("incomplete", "unverified")
        return CookieCleanup("complete", rows=rows) if rows else CookieCleanup("not_needed")
    except sqlite3.Error as exc:
        if committed:
            return CookieCleanup("incomplete", "unverified")
        if hasattr(exc, "sqlite_errorcode"):
            reason = {5: "locked", 6: "locked", 13: "disk_full", 10: "io_error",
                      8: "readonly", 14: "unwritable", 11: "corrupt", 26: "corrupt"}.get(
                          exc.sqlite_errorcode & 255, "unexpected")
        else:
            text = str(exc).lower()  # Python 3.10 only; never emitted.
            reason = next((reason for needle, reason in (
                ("locked", "locked"), ("busy", "locked"), ("readonly", "readonly"),
                ("full", "disk_full"), ("disk i/o", "io_error"), ("malformed", "corrupt"),
                ("not a database", "corrupt"), ("unable to open", "unwritable"),
            ) if needle in text), "unexpected")
        return CookieCleanup("incomplete" if reason in ("locked", "disk_full", "io_error") else "blocked", reason)
    except OSError:
        return CookieCleanup("incomplete", "unverified") if committed else CookieCleanup("blocked", "unwritable")
    except Exception as exc:
        if not committed:
            raise
        name = type(exc).__name__
        return CookieCleanup("incomplete", "unverified", error_type=(
            name if name.isidentifier() and len(name) <= 64 else "Exception"))

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
    # v6 -> v7: third-party debrid provider identifiers (TorBox T1). Purely
    # additive; every existing row keeps both columns empty and behaves
    # exactly as before.
    #
    #   debrid_item_id  TorBox web-download ID (T1) or torrent ID (T2)
    #   debrid_file_id  generally empty for T1 hoster rows; TorBox torrent
    #                   file ID in T2
    #
    # Note what is NOT here: no token, no generated CDN URL, no requestdl
    # URL. Those stay transient, exactly as for existing debrid routes.
    [
        "ALTER TABLE downloads ADD COLUMN debrid_item_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE downloads ADD COLUMN debrid_file_id TEXT NOT NULL DEFAULT ''",
    ],
    # v7 -> v8: task-private staging and committed public output. Additive;
    # legacy rows have no deletion authority and are never inferred from disk.
    [
        "ALTER TABLE downloads ADD COLUMN work_dir TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE downloads ADD COLUMN output_path TEXT NOT NULL DEFAULT ''",
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


def _prepare_database(path: Path) -> None:
    """Create or tighten the regular DB file before SQLite can touch it."""
    if os.path.abspath(path.parent) == os.path.abspath(DATA_DIR):
        prepare_private_directory(DATA_DIR)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        info = path.lstat()
    except FileNotFoundError:
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            info = path.lstat()  # Another creator won; use the no-descriptor path below.
        else:
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise OSError(errno.EINVAL, "Cove database must be a regular file")
                if os.name != "nt":
                    fchmod = getattr(os, "fchmod", None)
                    if fchmod is not None:
                        fchmod(fd, 0o600)
                    else:
                        os.chmod(path, 0o600, follow_symlinks=False)
                    if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
                        raise OSError(errno.EACCES, "Cove database is not private")
            finally:
                os.close(fd)
            return
    if not stat.S_ISREG(info.st_mode):
        raise OSError(errno.EINVAL, "Cove database must be a regular file")
    # Closing ANY auxiliary fd on an existing DB drops this process's SQLite
    # fcntl locks. Inspect and tighten existing files without opening them.
    if os.name == "nt" or stat.S_IMODE(info.st_mode) == 0o600:
        return
    try:
        os.chmod(path, 0o600, follow_symlinks=False)
    except NotImplementedError:
        raise OSError(errno.ENOTSUP, "No-follow database permission repair is unavailable") from None
    tightened = path.lstat()
    if not stat.S_ISREG(tightened.st_mode) or (tightened.st_dev, tightened.st_ino) != (
        info.st_dev, info.st_ino,
    ):
        raise OSError(errno.EINVAL, "Cove database changed during permission repair")
    if stat.S_IMODE(tightened.st_mode) != 0o600:
        raise OSError(errno.EACCES, "Cove database is not private")


def init(path: Path = DB_FILE) -> None:
    _prepare_database(path)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


@contextmanager
def connect(path: Path = DB_FILE):
    _prepare_database(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
