"""Queue manager: tracks download tasks, enforces a concurrency cap, and
mediates between the UI and the aria2 RPC client.

State machine per task:
    queued -> active -> (paused -> active)* -> (completed | error | removed)

The QueueManager itself runs entirely on the Qt main thread. RPC calls
fan out to background QThreadPool workers; results come back via signals.
"""
from __future__ import annotations

import os
import re
import stat
import time
from dataclasses import dataclass, field
from typing import Optional

from PySide6.QtCore import QObject, QProcess, QRunnable, QThreadPool, QTimer, Signal

from . import db, debrid, dedup, diagnostics, netiface, torrent
from .aria2 import Aria2Error, Aria2RPC, bittorrent_enabled
from .config import MAX_CONNECTIONS_PER_SERVER, Settings
from .debrid import DebridError
from .output_paths import (
    MissingEngineOutputError,
    OutputPathError,
    WorkDirectory,
    cleanup_work_directory,
    create_work_directory,
    publish_output,
    validate_public_filename,
)
from .torrent import TorrentError

URL_RE = re.compile(r"https?://\S+|ftp://\S+|magnet:\?\S+", re.IGNORECASE)

# Suffixes yt-dlp leaves behind mid-run. None of these is ever the finished
# file, so they can never be the single legitimate publication candidate.
# Matched with a trailing word boundary rather than by equality because a
# fragmented download is written as "<target>.part-Frag3", which a plain
# ".part" comparison would let through - and publishing one fragment would
# present a broken download as a finished one. ".partial" and similar real
# extensions are left alone: the boundary only allows a non-word character
# after the known name.
_ENGINE_INTERMEDIATE_SUFFIX_RE = re.compile(r"^\.(?:part|ytdl|temp|aria2)\b", re.IGNORECASE)
# yt-dlp names per-format streams "<stem>.f137.<ext>" before merging them.
#
# This is deliberately a shape rule, not a proof: a user could in theory pick
# the basename "video.f137.mp4" themselves, and that file would be excluded
# too. That trade is intentional. Inside a yt-dlp work directory an f###-shaped
# name is not unambiguously the finished output, and the fallback exists only
# to recover ONE unambiguously legitimate final file. A false negative costs a
# failed task and leaves the file untouched - exactly what happened before this
# fallback existed. A false positive publishes an unmerged stream as the
# finished download and marks it completed. Prefer the false negative.
#
# Do not add special cases trying to tell a "real" video.f137.mp4 from an
# intermediate, and do not correlate against yt-dlp's format selection: both
# turn a cheap invariant into a guess.
_ENGINE_FRAGMENT_STEM_RE = re.compile(r"\.f\d+$")

# Sidecars yt-dlp writes alongside the media file. Cove never asks for these,
# but a user's own yt-dlp config can (--write-thumbnail, --write-info-json,
# --write-subs), and they land in the same private work directory. If the media
# output is what went missing, a lone sidecar would otherwise be the single
# "legitimate" candidate - and publishing a thumbnail as the completed download
# loses the download silently.
#
# Bounded to shapes yt-dlp actually produces, checked against the installed
# yt-dlp: .info.json/.live_chat.json/.description metadata, webp/jpg/png
# thumbnails, and the subtitle formats it writes or converts to (srt, vtt, ass,
# lrc, plus YouTube's native ttml/srv1-3/json3). Deliberately NOT a media
# allowlist: an unknown container must stay publishable. Note .webm is media
# and stays eligible - only the thumbnail's .webp is a sidecar.
_ENGINE_SIDECAR_SUFFIXES = frozenset({
    ".json", ".description",
    ".webp", ".jpg", ".jpeg", ".png",
    ".vtt", ".srt", ".ass", ".ssa", ".lrc",
    ".ttml", ".srv1", ".srv2", ".srv3", ".json3",
})


def _engine_work_files(work: WorkDirectory) -> list[str]:
    """Names of the top-level regular files in an owned private work directory.

    One directory level, no symlinks, and never anything outside the directory
    Cove created for this run.
    """
    root = str(getattr(work, "path", "") or "")
    if not root:
        return []
    names = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                try:
                    if entry.is_file(follow_symlinks=False):
                        names.append(entry.name)
                except OSError:
                    continue
    except OSError:
        return []
    return names


def _engine_output_candidates(work: WorkDirectory) -> list[str]:
    """Plausible finished media files in an owned private work directory.

    Everything yt-dlp writes that is not the finished media file is excluded:
    partials, resume state, remux scratch files, unmerged per-format streams,
    and the metadata/thumbnail/subtitle sidecars a user's own yt-dlp config may
    have asked for. What remains is either the finished file or nothing.
    """
    root = str(getattr(work, "path", "") or "")
    candidates = []
    for name in _engine_work_files(work):
        stem, suffix = os.path.splitext(name)
        suffix = suffix.lower()
        if _ENGINE_INTERMEDIATE_SUFFIX_RE.match(suffix):
            continue
        if suffix in _ENGINE_SIDECAR_SUFFIXES:
            continue
        if _ENGINE_FRAGMENT_STEM_RE.search(stem):
            continue
        candidates.append(os.path.join(root, name))
    return candidates

# Approved `source_type` values (see the v6 migration in cove/db.py).
SOURCE_PLAIN = ""
SOURCE_TORRENT = "torrent"
SOURCE_TORRENT_FILE = "torrent_file"
SOURCE_TYPES = (SOURCE_PLAIN, SOURCE_TORRENT, SOURCE_TORRENT_FILE)

# Where a URL entered Cove. Diagnostics only - never affects routing.
_INTAKE_SOURCES = ("manual", "clipboard", "extension", "api", "search", "unknown")

# Task failures on the local BitTorrent path. Every one of these is a fixed
# sentence: a torrent carries tracker passkeys and peer addresses, and none
# of that may reach a task row, a log line or an error dialog.
TORRENT_LOCAL_DISABLED = (
    "This torrent is not cached by an enabled debrid service, and local "
    "BitTorrent downloading is turned off in Settings."
)
TORRENT_PROXY_BLOCKED = (
    "Local BitTorrent is blocked while Cove's proxy is configured because "
    "peer, DHT and UDP tracker traffic may bypass the proxy. Enable the "
    "BitTorrent proxy override in Settings only after understanding this "
    "limitation."
)
TORRENT_NO_BITTORRENT = (
    "This aria2 build does not include BitTorrent support. Install or "
    "reinstall a BitTorrent-enabled aria2 build."
)
TORRENT_CONSENT_DECLINED = (
    "Local BitTorrent was declined, so this torrent was not downloaded."
)
TORRENT_CANCELLED_UNCACHED = (
    "This torrent is not cached by an enabled debrid service, and the "
    "download was cancelled."
)
TORRENT_METADATA_FAILED = "Cove could not read this torrent's metadata."
TORRENT_SUPPORT_DISABLED = (
    "BitTorrent support is turned off in Settings, so this magnet link was "
    "not added."
)
# aria2's own message for a failed torrent may quote the magnet it was
# given, a tracker announce URL (and therefore a private-tracker passkey)
# or a peer address, and a task error is persisted and shown in the UI. Only
# aria2's numeric code — which carries no torrent data — is kept.
TORRENT_ARIA2_FAILED = "Cove's BitTorrent engine could not download this torrent."

# Transient display phase for a magnet whose metadata aria2 is still
# fetching. Deliberately not a database status: it lasts seconds and a
# restart re-derives it.
PHASE_METADATA = "metadata"


def _torrent_error_text(code) -> str:
    """A torrent failure the user can report, carrying no swarm data.

    aria2's numeric code is safe (it is an enum) and is worth keeping;
    everything else aria2 says about a torrent may quote the magnet, a
    tracker URL or a peer address.
    """
    try:
        numeric = int(code)
    except (TypeError, ValueError):
        return TORRENT_ARIA2_FAILED
    return f"{TORRENT_ARIA2_FAILED} (aria2 error {numeric})"


def _clean_header(value) -> str:
    """Strip CR/LF so a persisted or drop-file value can't inject extra
    headers into the ffmpeg/yt-dlp request it is forwarded to."""
    if not isinstance(value, str):
        return ""
    return value.replace("\r", "").replace("\n", "").strip()


def _safe_torrent_name(value) -> str:
    """A magnet's `dn` reduced to a usable folder name, or ""."""
    try:
        return torrent.safe_component(value)
    except TorrentError:
        return ""


def _within(base: str, target: str) -> bool:
    """True when `target` is `base` itself or sits underneath it.

    realpath, not abspath: a symlink already sitting inside the output
    directory would otherwise let a textually-contained provider path
    resolve to somewhere else entirely. realpath on a path that does not
    exist yet still resolves the part that does, which is the part a
    symlink could be hiding in.
    """
    base_abs = os.path.realpath(base)
    target_abs = os.path.realpath(target)
    return target_abs == base_abs or target_abs.startswith(base_abs + os.sep)


def _row_get(row, key, default=None):
    """sqlite3.Row has no .get(); look up a column, falling back to
    default if it's absent (e.g. an older DB missing an additive column)."""
    return row[key] if key in row.keys() else default


def _task_from_persisted_row(row) -> "DownloadTask":
    """Rebuild a DownloadTask from a persisted 'downloads' row on startup."""
    return DownloadTask(
        id=row["id"],
        url=row["url"],
        out_dir=row["out_dir"],
        connections=row["connections"],
        speed_limit_kbps=row["speed_limit_kbps"],
        filename=row["filename"],
        gid=None,
        # "paused" is a decision the user made and must outlive the process;
        # everything else restored here represents work a shutdown interrupted,
        # which is what "queued" means. Normalising the two together silently
        # resumed downloads someone had deliberately stopped - on a metered or
        # restricted connection, the exact thing they were avoiding.
        status="paused" if _row_get(row, "status", "") == "paused" else "queued",
        total_bytes=row["total_bytes"],
        completed_bytes=row["completed_bytes"],
        created_at=row["created_at"],
        segments=row["segments"],
        backend=_row_get(row, "backend", "aria2"),
        cookies=_clean_header(_row_get(row, "cookies", "")),
        referrer=_clean_header(_row_get(row, "referrer", "")),
        user_agent=_clean_header(_row_get(row, "user_agent", "")),
        source_type=_row_get(row, "source_type", "") or "",
        info_hash=_row_get(row, "info_hash", "") or "",
        torrent_name=_row_get(row, "torrent_name", "") or "",
        torrent_path=_row_get(row, "torrent_path", "") or "",
        debrid_route=_row_get(row, "debrid_route", "") or "",
        debrid_item_id=_row_get(row, "debrid_item_id", "") or "",
        debrid_file_id=_row_get(row, "debrid_file_id", "") or "",
    )


@dataclass
class DownloadTask:
    id: int
    url: str
    out_dir: str
    connections: int = 16
    speed_limit_kbps: int = 0
    filename: Optional[str] = None
    gid: Optional[str] = None
    status: str = "queued"  # queued | active | paused | completed | error | removed
    total_bytes: int = 0
    completed_bytes: int = 0
    download_speed: int = 0
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    segments: int = 0
    bitfield: str = ""
    num_pieces: int = 0
    last_status_at: float = 0.0
    backend: str = "aria2"
    # Browser-supplied headers, used by the ffmpeg/yt-dlp backends for
    # authenticated or anti-hotlink media. Empty for plain aria2 tasks
    # (the direct extension -> aria2 path passes headers via RPC instead).
    cookies: str = ""
    referrer: str = ""
    user_agent: str = ""
    # Torrent provenance, persisted by the v6 schema.
    #   source_type == "torrent"       this row is the magnet / .torrent the
    #                                  user added; it probes the providers
    #                                  and then becomes the first file.
    #   source_type == "torrent_file"  an ordinary HTTPS download whose `url`
    #                                  is the provider's account-bound locked
    #                                  link, unlocked afresh on every launch.
    source_type: str = ""
    info_hash: str = ""
    torrent_name: str = ""
    torrent_path: str = ""
    debrid_route: str = ""
    # Third-party provider identifiers, persisted by the v7 schema. Empty
    # for AllDebrid/Real-Debrid, which have no such identity to reuse.
    #   debrid_item_id  TorBox web-download ID (T1) this task is pinned to,
    #                   so a retry/restart reuses it instead of creating
    #                   another one.
    #   debrid_file_id  reserved for TorBox torrent files (T2); always ''
    #                   for an ordinary hoster row.
    debrid_item_id: str = ""
    debrid_file_id: str = ""
    # Transient debrid state, deliberately absent from the 'downloads'
    # table and from _task_from_persisted_row. `resolved_url` is a
    # short-lived secret on the provider's delivery node: it is handed to
    # aria2 and nowhere else, and is re-derived from `url` on every
    # relaunch because the previous one expires.
    resolved_url: str = ""
    debrid_provider: str = ""
    # "metadata" while aria2 is still fetching a magnet's torrent metadata.
    # Transient by design: it is re-derived when the magnet is re-added.
    phase: str = ""

    def clear_debrid(self) -> None:
        self.resolved_url = ""
        self.debrid_provider = ""

    @property
    def progress(self) -> float:
        completed = self.interpolated_completed_bytes()
        return (completed / self.total_bytes) if self.total_bytes else 0.0

    def interpolated_completed_bytes(self) -> int:
        """Predicted byte count between aria2 polls.

        We poll aria2 a few times a second, but the UI repaints at ~30 fps;
        between samples we extrapolate `completed_bytes + speed * elapsed`
        so the progress bar moves smoothly instead of stepping.
        """
        if self.status != "active" or self.last_status_at <= 0 or self.download_speed <= 0:
            return self.completed_bytes
        elapsed = time.time() - self.last_status_at
        if elapsed <= 0:
            return self.completed_bytes
        predicted = self.completed_bytes + int(self.download_speed * elapsed)
        if self.total_bytes > 0:
            predicted = min(predicted, self.total_bytes)
        return predicted


class _RpcCall(QRunnable):
    """Run a single RPC call off the UI thread.

    autoDelete is disabled — the QueueManager pins the runnable until its
    signal lands so the QObject carrying `done`/`failed` outlives any
    queued cross-thread metacall. (Letting the pool reap a runnable whose
    Python `signals` attribute the C++ side still references segfaults.)
    """

    class _Sig(QObject):
        done = Signal(object)
        failed = Signal(str)
        finished = Signal()

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.setAutoDelete(False)
        self.signals = self._Sig()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def run(self):
        try:
            result = self._fn(*self._args, **self._kwargs)
        except (Aria2Error, DebridError, TorrentError) as e:
            # Both already carry a message written for the user; prefixing
            # the class name would only leak implementation detail into the
            # task row.
            self.signals.failed.emit(str(e))
        except Exception as e:  # pragma: no cover - defensive
            self.signals.failed.emit(f"{type(e).__name__}: {e}")
        else:
            self.signals.done.emit(result)
        self.signals.finished.emit()


class QueueManager(QObject):
    task_added = Signal(int)            # task id
    task_changed = Signal(int)          # task id
    task_removed = Signal(int)          # task id
    queue_running_changed = Signal(bool)
    error = Signal(str)
    # A task needs the one-time local-BitTorrent privacy disclosure before
    # it can start. Emitted on the GUI thread; the window answers with
    # torrent_consent(tid, accepted). No modal is ever raised from here.
    torrent_consent_needed = Signal(int)  # task id

    def __init__(self, settings: Settings, rpc: Aria2RPC, parent: QObject | None = None):
        super().__init__(parent)
        self.settings = settings
        self.rpc = rpc
        self.tasks: dict[int, DownloadTask] = {}
        self._running = True
        self._scheduler_allows = True
        self._pool = QThreadPool.globalInstance()
        self._inflight: set[_RpcCall] = set()
        self._auto_paused: set[int] = set()
        # Tasks whose add_uri RPC is in flight. Maps tid -> deferred actions
        # the user requested before the gid landed:
        #   {"pause": True}                — call rpc.pause(gid) on arrival
        #   {"remove": True, "delete_file": bool}
        # If "remove" is set, the task has already been hidden from the UI
        # and dropped from the DB; we keep it in self.tasks so the on_done
        # callback can still find the gid and dispatch a clean shutdown.
        self._pending_launch: dict[int, dict] = {}
        # Tasks whose cancellation aria2 has not confirmed yet. Maps tid ->
        # the removal intent captured when the user asked:
        #   {"gid": str, "path": str|None, "clean": bool, "private_run": bool}
        # The task stays in self.tasks and in the database for the whole of
        # this window, because until aria2 answers, the download is still
        # running and Cove is the only thing that knows about it.
        self._removing: dict[int, dict] = {}
        # Per-task command generation for pause/unpause. Two RPCs issued back
        # to back run on independent pool threads and can reach aria2 in the
        # opposite order, which would leave aria2 downloading a task Cove
        # shows and persists as paused. Every command captures the generation
        # it was issued under; a result whose generation has been superseded
        # means an older command won at the backend, so the current intent is
        # re-sent as a compensating command.
        self._cmd_gen: dict[int, int] = {}
        # tid -> (gid, paused). Scoped to the gid so an intent recorded for a
        # transfer a retry has since abandoned can never describe its
        # replacement.
        self._desired_paused: dict[int, tuple[str, bool]] = {}
        # tid -> (gid, paused): what aria2 was last observed to hold. Written
        # wherever that becomes known - a per-task command landing, or a
        # queue-wide pause_all, which pauses every gid at once. Reconciliation
        # compares this against `_desired_paused` rather than reasoning about
        # which command superseded which; see _converge.
        self._backend_state: dict[int, tuple[str, bool]] = {}
        # tid -> {generation: gid} for every command still in flight. A high-
        # water mark of the newest generation to resolve will not do: commands
        # complete out of order, so a newer one finishing says nothing about
        # whether older ones are still on their way. A failure removes an entry
        # just as surely as success - a command that will never land must stop
        # counting as pending. The gid is kept because serialisation is per
        # transfer: a command still outstanding against a gid a retry has
        # abandoned says nothing about its replacement and must not block it.
        self._cmd_pending: dict[int, dict[int, str]] = {}
        # tid -> (gid, paused, send) for an intent raised while a command was
        # already in flight. One slot: only the latest intent is worth issuing,
        # so a newer one replaces it. Released by _converge once the task has
        # nothing outstanding.
        self._cmd_queued: dict[int, tuple[str, bool, object, object]] = {}
        # The same idea one level up, for queue-wide transitions. Stop, Start
        # and each scheduler boundary bump this; a pause_all result carrying a
        # superseded epoch is discarded rather than applied to whatever is
        # running now.
        self._queue_epoch = 0
        # Every gid Cove has ever launched or adopted this session. External
        # downloads (browser extension) are discovered by polling aria2; this
        # guards against re-adopting one the user already cleared from the
        # list, and against adopting Cove's own downloads as "external".
        self._seen_gids: set[str] = set()
        # Whether this aria2 reports BitTorrent support. Asked once and
        # cached for the daemon's lifetime rather than on every poll.
        self._bt_capable: bool | None = None
        # Tasks parked on the one-time P2P disclosure.
        self._awaiting_consent: set[int] = set()
        # Tasks whose owner already accepted the notice this session. The
        # persisted "don't show again" flag is a global preference; this is
        # per task, so a paused-then-resumed torrent is not asked twice.
        self._consent_granted: set[int] = set()
        self._hls_procs: dict[int, QProcess] = {}
        self._hls_duration: dict[int, float] = {}
        self._hls_stderr: dict[int, str] = {}
        self._hls_work: dict[int, WorkDirectory] = {}
        self._hls_line_buffer: dict[int, str] = {}
        self._extractor_procs: dict[int, QProcess] = {}
        self._extractor_output: dict[int, str] = {}
        self._extractor_work: dict[int, WorkDirectory] = {}
        self._extractor_pause_pending: dict[int, QProcess] = {}
        self._extractor_paused_work: set[int] = set()
        self._extractor_final_path: dict[int, str] = {}
        self._extractor_line_buffer: dict[int, str] = {}
        self._poll = QTimer(self)
        self._poll.setInterval(500)
        self._poll.timeout.connect(self._poll_active)
        self._poll.start()
        self._ext_poll = QTimer(self)
        self._ext_poll.setInterval(2000)
        self._ext_poll.timeout.connect(self._check_external)
        self._ext_poll.start()
        self._purge_legacy_drop_dir()
        db.init()
        self._load_persisted()

    # ---- persistence --------------------------------------------------

    def _load_persisted(self) -> None:
        with db.connect() as conn:
            # Databases from Cove 1.8.x may have rows left in the
            # now-removed 'converting' status; normalize them to error so
            # nothing stays stuck from an old install.
            conn.execute(
                "UPDATE downloads SET status='error', "
                "error='Conversion no longer supported', "
                "finished_at=? WHERE status='converting'",
                (time.time(),),
            )
            # A removal the previous run never saw confirmed. The user asked
            # for it and the row is only still here because Cove exited inside
            # the aria2 round trip, so finish the job rather than restoring a
            # download that was already on its way out.
            conn.execute("DELETE FROM downloads WHERE status='removing'")
            rows = conn.execute(
                "SELECT * FROM downloads WHERE status IN ('queued','active','paused')"
            ).fetchall()
        for row in rows:
            t = _task_from_persisted_row(row)
            self.tasks[t.id] = t

    # aria2 download status -> Cove task status. "waiting" is omitted on
    # purpose: a waiting download has a gid but isn't polled by _poll_active,
    # so we'd never see it transition. With max-concurrent-downloads lifted,
    # extension downloads start active rather than waiting anyway. "removed"
    # is skipped so cleared downloads don't reappear.
    _ARIA2_STATUS = {
        "active": "active",
        "paused": "paused",
        "complete": "completed",
        "error": "error",
    }

    def _purge_legacy_drop_dir(self) -> None:
        """Retire browser requests left behind by the old deferred-delivery bug.

        Up to and including 3.2.0 the native messaging host wrote a durable
        request file here and told the browser the download had been accepted
        even when no Cove process was running; this queue then consumed it at
        the next launch. That is the defect being repaired: browser delivery
        is now a synchronous handoff to the live process (see
        `single_instance.send_browser_download`) and nothing is ever
        persisted, so this directory has no remaining producer or consumer.

        An installation upgraded from a buggy version may still hold files
        written before the upgrade, and those must not suddenly download.
        They are deleted, never parsed - the contents are a URL with cookies
        and a referrer, so nothing here reads or logs them, and no filename
        is logged either. Only the host's own `download-*` naming convention
        is touched, so an unrelated file a user put in this directory is left
        alone. Idempotent, and a missing directory or an unreadable entry is
        simply skipped.
        """
        from .config import DATA_DIR
        drop_dir = DATA_DIR / "drop"
        try:
            if not drop_dir.is_dir():
                return
            entries = list(drop_dir.iterdir())
        except OSError:
            return
        for f in entries:
            # `download-<ms>-<hex>.json`, its `.tmp` half-write, and the
            # `.bad` sideline the retired consumer produced.
            if not f.name.startswith("download-"):
                continue
            if not (
                f.name.endswith(".json")
                or f.name.endswith(".json.bad")
                or f.name.endswith(".tmp")
            ):
                continue
            try:
                f.unlink()
            except OSError:
                # Permission-denied or a concurrent removal: leave it be.
                # Nothing consumes this directory any more, so a survivor is
                # inert rather than a deferred download.
                continue

    def _check_external(self) -> None:
        """Pick up downloads added to aria2 outside Cove's queue (e.g. the
        browser extension), including ones that already finished."""
        known_gids = {t.gid for t in self.tasks.values() if t.gid}

        def on_done(snapshot):
            for dl in snapshot:
                gid = dl.get("gid")
                if not gid or gid in known_gids or gid in self._seen_gids:
                    continue
                status = self._ARIA2_STATUS.get(dl.get("status"))
                if status is None:
                    continue
                if dl.get("infoHash") or dl.get("bittorrent") or dl.get("following"):
                    # A torrent job, not an ordinary download. Cove's own
                    # torrents already own their gids; a stranger's torrent
                    # cannot be represented by a single-file adopted row
                    # without lying about what it is, and a metadata child
                    # adopted here would appear as a ghost second task.
                    # Remember it so the guard runs once, then leave it be.
                    self._seen_gids.add(gid)
                    continue
                # Mark seen immediately so a gid appearing in both the active
                # and stopped lists of one snapshot is only adopted once.
                self._seen_gids.add(gid)

                files = dl.get("files") or []
                url = ""
                filename = None
                out_dir = self.settings.download_dir
                if files:
                    uris = files[0].get("uris") or []
                    if uris:
                        url = uris[0].get("uri", "")
                    path = files[0].get("path", "")
                    if path:
                        from pathlib import Path
                        p = Path(path)
                        filename = p.name
                        out_dir = str(p.parent)

                def _int(v):
                    try:
                        return int(v)
                    except (TypeError, ValueError):
                        return 0

                total = _int(dl.get("totalLength"))
                completed = _int(dl.get("completedLength"))
                speed = _int(dl.get("downloadSpeed"))
                finished = time.time() if status in ("completed", "error") else None

                effective_connections = min(
                    max(int(self.settings.connections_per_server), 1),
                    MAX_CONNECTIONS_PER_SERVER,
                )
                with db.connect() as conn:
                    cur = conn.execute(
                        """INSERT INTO downloads
                            (url, filename, out_dir, connections,
                             speed_limit_kbps, status, gid, total_bytes,
                             completed_bytes, created_at, finished_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (url, filename, out_dir,
                         effective_connections, 0,
                         status, gid, total, completed, time.time(), finished),
                    )
                    tid = cur.lastrowid
                t = DownloadTask(
                    id=tid, url=url, out_dir=out_dir,
                    connections=effective_connections,
                    filename=filename, gid=gid, status=status,
                    total_bytes=total, completed_bytes=completed,
                    download_speed=speed, finished_at=finished,
                )
                self.tasks[tid] = t
                self.task_added.emit(tid)

        self._spawn(
            self.rpc.tell_external_snapshot, on_done=on_done, on_fail=lambda *_: None
        )

    def _persist(self, t: DownloadTask) -> None:
        # A task waiting on aria2 to confirm its removal stays visible and
        # interactive, so pause/resume and a pause callback landing inside that
        # window all reach this method. None of them may overwrite the durable
        # removal marker: the row has to keep saying "on its way out" until
        # aria2 answers, or a crash in between restores a download the user
        # already removed. Everything else about the row still persists, and
        # _restore_removal clears _removing before it rewrites the true status.
        status = "removing" if t.id in self._removing else t.status
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE downloads
                SET filename=?, status=?, gid=?, total_bytes=?, completed_bytes=?,
                    error=?, finished_at=?, segments=?, out_dir=?,
                    debrid_route=?, debrid_item_id=?, debrid_file_id=?
                WHERE id=?
                """,
                (
                    t.filename,
                    status,
                    t.gid,
                    t.total_bytes,
                    t.completed_bytes,
                    t.error,
                    t.finished_at,
                    t.segments,
                    t.out_dir,
                    t.debrid_route,
                    t.debrid_item_id,
                    t.debrid_file_id,
                    t.id,
                ),
            )

    # ---- public API ---------------------------------------------------

    def _resolve_category_dir(self, url: str) -> str:
        from .config import categorize
        from pathlib import Path
        category = categorize(url)
        routed = getattr(self.settings.category_dirs, category, "") or ""
        if routed:
            return routed
        if self.settings.auto_sort_by_category and category != "Other":
            return str(Path(self.settings.download_dir) / category)
        return self.settings.download_dir

    # ---- duplicate detection -------------------------------------------

    # Statuses that count as "this download already exists". `error` and
    # `removed` are deliberately absent: a failed or discarded download is
    # exactly the one a user is most likely to legitimately re-add.
    _DUP_LIVE_STATUSES = frozenset({"queued", "active", "paused"})

    # Upper bound on the completed rows a URL-identity lookup will scan.
    # Newest first, because a user re-adding something is overwhelmingly
    # re-adding something recent.
    _DUP_HISTORY_LIMIT = 5000

    @staticmethod
    def _dup_candidate_from_task(t: "DownloadTask") -> dedup.Candidate:
        return dedup.Candidate(
            url=t.url,
            source_type=t.source_type,
            info_hash=t.info_hash,
            debrid_route=t.debrid_route,
            debrid_item_id=t.debrid_item_id,
            name=t.torrent_name or (t.filename or ""),
        )

    @staticmethod
    def _dup_candidate_from_row(row) -> dedup.Candidate:
        return dedup.Candidate(
            url=_row_get(row, "url", "") or "",
            source_type=_row_get(row, "source_type", "") or "",
            info_hash=_row_get(row, "info_hash", "") or "",
            debrid_route=_row_get(row, "debrid_route", "") or "",
            debrid_item_id=_row_get(row, "debrid_item_id", "") or "",
            name=_row_get(row, "torrent_name", "") or "",
        )

    def find_duplicate(
        self,
        url: str,
        *,
        source_type: str = SOURCE_PLAIN,
        info_hash: str = "",
        debrid_route: str = "",
        debrid_item_id: str = "",
        exclude_task_id: int | None = None,
    ) -> Optional[dedup.DuplicateMatch]:
        """Report an existing download that matches this submission.

        Read-only and side-effect free: nothing is mutated, no provider is
        resolved, aria2 is not touched and no network or content check is
        performed. The caller decides what, if anything, to say about the
        result - this method never raises a dialog.

        Live in-memory tasks are checked first, then completed rows in
        SQLite (so a warning still fires after a restart). Errored and
        removed tasks never match.
        """
        cand = dedup.Candidate(
            url=url,
            source_type=source_type,
            info_hash=info_hash,
            debrid_route=debrid_route,
            debrid_item_id=debrid_item_id,
        )
        ident = dedup.identity(cand)
        if ident is None:
            return None

        completed_task: Optional[DownloadTask] = None
        for t in self.tasks.values():
            if exclude_task_id is not None and t.id == exclude_task_id:
                continue
            if t.status not in self._DUP_LIVE_STATUSES and t.status != "completed":
                continue
            if dedup.identity(self._dup_candidate_from_task(t)) != ident:
                continue
            if t.status == "completed":
                # Keep looking: a live task is the more useful answer, and
                # "Focus Existing" beats "Open Folder" when both apply.
                if completed_task is None:
                    completed_task = t
                continue
            return dedup.DuplicateMatch(
                category=dedup.LIVE,
                identity=ident[0],
                task_id=t.id,
                status=t.status,
                name=t.torrent_name or (t.filename or ""),
                out_dir=t.out_dir,
                filename=t.filename or "",
                can_duplicate=ident[0] != dedup.ID_INFO_HASH,
            )
        if completed_task is not None:
            return dedup.DuplicateMatch(
                category=dedup.COMPLETED,
                identity=ident[0],
                task_id=completed_task.id,
                status="completed",
                name=completed_task.torrent_name or (completed_task.filename or ""),
                out_dir=completed_task.out_dir,
                filename=completed_task.filename or "",
            )
        return self._find_completed_duplicate(ident, exclude_task_id)

    def _find_completed_duplicate(
        self, ident: tuple[str, str], exclude_task_id: int | None
    ) -> Optional[dedup.DuplicateMatch]:
        """Completed history, from the same table the queue already uses.

        No schema change and no new index: the info-hash and provider
        lookups narrow in SQL, and the URL lookup falls back to a bounded
        newest-first scan because a canonical URL is not a stored column.
        Rows written before the torrent/provider columns existed simply
        have them empty, which lands them on the URL rule.
        """
        kind, key = ident
        base = (
            "SELECT id, url, filename, out_dir, source_type, info_hash, "
            "torrent_name, debrid_route, debrid_item_id "
            "FROM downloads WHERE status='completed'"
        )
        try:
            with db.connect() as conn:
                if kind == dedup.ID_INFO_HASH:
                    rows = conn.execute(
                        base + " AND info_hash!='' ORDER BY id DESC", ()
                    ).fetchall()
                elif kind == dedup.ID_PROVIDER:
                    route, item_id = key.split("\x00", 1)
                    rows = conn.execute(
                        base + " AND debrid_route=? AND debrid_item_id=? "
                        "ORDER BY id DESC",
                        (route, item_id),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        base + " ORDER BY id DESC LIMIT ?",
                        (self._DUP_HISTORY_LIMIT,),
                    ).fetchall()
        except Exception:
            # History is an optimisation on top of the live queue; a
            # malformed or unreadable row must never block a new add.
            return None
        for row in rows:
            try:
                if exclude_task_id is not None and row["id"] == exclude_task_id:
                    continue
                if dedup.identity(self._dup_candidate_from_row(row)) != ident:
                    continue
                filename = _row_get(row, "filename", "") or ""
                return dedup.DuplicateMatch(
                    category=dedup.COMPLETED,
                    identity=kind,
                    task_id=row["id"],
                    status="completed",
                    name=(_row_get(row, "torrent_name", "") or "") or filename,
                    out_dir=_row_get(row, "out_dir", "") or "",
                    filename=filename,
                )
            except Exception:
                continue
        return None

    def add_url(
        self,
        url: str,
        out_dir: str | None = None,
        filename: str | None = None,
        *,
        connections: int | None = None,
        speed_limit_kbps: int = 0,
        cookies: str = "",
        referrer: str = "",
        user_agent: str = "",
        source_type: str = SOURCE_PLAIN,
        info_hash: str = "",
        torrent_name: str = "",
        torrent_path: str = "",
        debrid_route: str = "",
        intake: str = "unknown",
    ) -> Optional[int]:
        """Add one URL to the queue.

        The `source_type`/`info_hash`/... arguments are internal: they are
        set by Cove's own torrent routing below and by `add_torrent_file`,
        never by anything that carries user input (the local API and the
        native-messaging drop directory both pass explicit kwargs only).
        """
        url = url.strip()
        cookies = _clean_header(cookies)
        referrer = _clean_header(referrer)
        user_agent = _clean_header(user_agent)
        if source_type not in SOURCE_TYPES:
            return None
        if not URL_RE.match(url):
            return None
        if torrent.is_magnet(url):
            # A magnet is torrent work whatever the settings say. aria2
            # accepts one through addUri and answers with the *metadata*
            # download, which reports itself complete at 100% under a
            # "[METADATA]<hash>" file name with none of the torrent on
            # disk - indistinguishable, in the plain lifecycle, from a
            # finished file. So a magnet never reaches that lifecycle:
            # with BitTorrent support off there is nothing to route it to,
            # and the add is refused before any aria2 job exists.
            if not self._torrent_enabled():
                self.error.emit(TORRENT_SUPPORT_DISABLED)
                return None
            if source_type == SOURCE_PLAIN:
                return self._add_magnet(
                    url, out_dir=out_dir, speed_limit_kbps=speed_limit_kbps,
                    intake=intake,
                )
        import posixpath
        from urllib.parse import unquote, urlparse
        from .config import categorize
        from .extractor import is_extractor_url
        from .hls import is_hls_url
        backend = "ffmpeg" if is_hls_url(url) else (
            "yt-dlp" if is_extractor_url(url) else "aria2"
        )
        if backend in {"ffmpeg", "yt-dlp"}:
            import shutil
            executable = "ffmpeg" if backend == "ffmpeg" else "yt-dlp"
            found = shutil.which(executable)
            if backend == "yt-dlp":
                from .extractor import resolve_ytdlp
                found = resolve_ytdlp()
            if not found:
                self.error.emit(f"{executable} is required for this video download")
                return None
            requested_name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
            requested_name = "".join(c for c in requested_name if ord(c) >= 32).strip()
            if requested_name:
                stem = requested_name.rsplit(".", 1)[0]
                filename = f"{stem}.mp4"
            else:
                path_part = urlparse(url).path.rsplit("/", 1)[-1]
                stem = path_part.rsplit(".", 1)[0] if "." in path_part else "video"
                filename = f"{stem}.mp4"
            category = "Videos"
        else:
            category = categorize(url)
            requested_name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
            requested_name = "".join(c for c in requested_name if ord(c) >= 32).strip()
            filename = requested_name or None
        effective_connections = (
            self.settings.connections_per_server if connections is None else connections
        )
        effective_connections = min(
            max(int(effective_connections), 1), MAX_CONNECTIONS_PER_SERVER
        )
        if out_dir:
            dest_dir = out_dir
        else:
            dest_dir = self._resolve_category_dir(url)
        with db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO downloads
                    (url, out_dir, connections, speed_limit_kbps, status,
                     created_at, category, backend, filename,
                     cookies, referrer, user_agent,
                     source_type, info_hash, torrent_name, torrent_path,
                     debrid_route)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    url,
                    dest_dir,
                    effective_connections,
                    speed_limit_kbps,
                    "queued",
                    time.time(),
                    category,
                    backend,
                    filename,
                    cookies,
                    referrer,
                    user_agent,
                    source_type,
                    info_hash,
                    torrent_name,
                    torrent_path,
                    debrid_route,
                ),
            )
            tid = cur.lastrowid
        t = DownloadTask(
            id=tid,
            url=url,
            out_dir=dest_dir,
            connections=effective_connections,
            speed_limit_kbps=speed_limit_kbps,
            backend=backend,
            filename=filename,
            cookies=cookies,
            referrer=referrer,
            user_agent=user_agent,
            source_type=source_type,
            info_hash=info_hash,
            torrent_name=torrent_name,
            torrent_path=torrent_path,
            debrid_route=debrid_route,
        )
        self.tasks[tid] = t
        self._diag_url_added(t, intake)
        self.task_added.emit(tid)
        self._maybe_start_next()
        return tid

    # ---- diagnostics ---------------------------------------------------
    #
    # Observation only. Every helper below is best effort: it records what
    # happened and never influences routing, validation or task state.

    def _diag(self, component: str, event: str, level: str = "INFO", **kw) -> None:
        try:
            diagnostics.emit(component, event, level, **kw)
        except Exception:
            pass

    def _diag_url_added(self, t: DownloadTask, intake: str) -> None:
        facts = diagnostics.url_facts(t.url)
        self._diag(
            "queue", "url_added", "INFO", task_id=t.id,
            intake=intake if intake in _INTAKE_SOURCES else "unknown",
            scheme=facts["scheme"], host=facts["host"],
            classification=facts["classification"], backend=t.backend,
            source_type=t.source_type,
        )

    def _diag_task_failed(self, t: DownloadTask, reason: str) -> None:
        self._diag("queue", "task_failed", "ERROR", task_id=t.id,
                   backend=t.backend, reason=reason)

    def _diag_engine_output_rejected(self, t, reported, work, exc) -> None:
        """Record why a publication was refused, without echoing the path.

        Everything here is derived from values the queue already holds. The
        validation decision itself was made in cove/output_paths.py and is
        not re-run, re-interpreted or influenced by this call.
        """
        try:
            reported_text = str(reported or "")
            work_root = str(getattr(work, "path", "") or "")
            facts = diagnostics.path_facts(reported_text, expected_root=work_root)
            exists = is_file = None
            if reported_text:
                try:
                    exists = os.path.exists(reported_text)
                    is_file = os.path.isfile(reported_text) if exists else None
                except OSError:
                    exists = is_file = None
            self._diag(
                "extractor.publish", "engine_output_rejected", "ERROR",
                task_id=t.id, exc=exc,
                engine="yt-dlp",
                stage="validate_engine_output",
                rule=diagnostics.classify_output_path_error(str(exc)),
                path=diagnostics.sanitize_path(reported_text) if reported_text else None,
                expected_root=diagnostics.sanitize_path(work_root) if work_root else None,
                absolute=facts["absolute"],
                drive=facts["drive"],
                depth=facts["depth"],
                ext=facts["ext"],
                same_drive=facts["same_drive"],
                within_expected_root=facts["within_expected_root"],
                exists=exists,
                is_file=is_file,
            )
        except Exception:
            pass

    def _diag_engine_work_shape(self, t, work, reported) -> None:
        """Record the shape of the private work directory before it is cleaned.

        Counts and extensions only. The names inside that directory come from
        the page title, so they never reach the log.
        """
        try:
            names = _engine_work_files(work)
            candidates = _engine_output_candidates(work)
            reported_exists = None
            if reported:
                try:
                    reported_exists = os.path.exists(str(reported))
                except OSError:
                    reported_exists = None
            self._diag(
                "extractor.publish", "work_shape", "INFO",
                task_id=t.id, engine="yt-dlp",
                entries=len(names),
                candidates=len(candidates),
                exts=sorted({os.path.splitext(name)[1].lower() for name in names}),
                reported_exists=reported_exists,
                single_candidate=len(candidates) == 1,
            )
        except Exception:
            pass

    def _publish_extractor_output(
        self, work: WorkDirectory, reported: str, requested: str
    ):
        """Publish the run's finished file, tolerating a stale reported name.

        yt-dlp exits 0 and prints the path it believes it produced. On Windows
        that name is sometimes not the file actually left behind, which failed
        an otherwise complete download. When the run's own work directory holds
        exactly one legitimate finished file, publish that instead; ambiguity
        and emptiness both still fail closed, and the fallback candidate goes
        through the same validation as any reported path.
        """
        if reported:
            try:
                return publish_output(work, reported, requested)
            except MissingEngineOutputError as missing:
                rejection = missing
        else:
            rejection = OutputPathError("yt-dlp did not report a final output path")
        candidates = _engine_output_candidates(work)
        if len(candidates) != 1:
            raise rejection
        published_suffix = os.path.splitext(candidates[0])[1]
        fallback = os.path.splitext(requested)[0] + published_suffix
        return publish_output(work, candidates[0], validate_public_filename(fallback))

    def _debrid_credential_facts(self) -> dict:
        """Configured-or-not, as booleans. Never the credential itself."""
        try:
            return {
                "rd_enabled": bool(getattr(self.settings, "real_debrid_enabled", False)),
                "rd_authenticated": bool(
                    getattr(self.settings, "real_debrid_api_token", "")
                ),
            }
        except Exception:
            return {"rd_enabled": False, "rd_authenticated": False}

    def add_urls(
        self, urls: list[str], out_dir: str | None = None, intake: str = "unknown"
    ) -> list[int]:
        return [
            tid
            for u in urls
            if (tid := self.add_url(u, out_dir, intake=intake)) is not None
        ]

    # ---- torrent input ------------------------------------------------

    def _torrent_enabled(self) -> bool:
        return getattr(self.settings, "torrent_support_enabled", False) is True

    def _live_torrent(self, info_hash: str) -> bool:
        """True when this info hash is already represented in the queue.

        Completed rows are deliberately not counted: re-adding a torrent
        you finished last month is a legitimate thing to do, and matches
        how the queue already treats a finished HTTP download.
        """
        if not info_hash:
            return False
        return any(
            t.info_hash == info_hash
            and t.source_type in (SOURCE_TORRENT, SOURCE_TORRENT_FILE)
            and t.status in {"queued", "active", "paused", "error"}
            for t in self.tasks.values()
        )

    def _add_magnet(
        self, url: str, *, out_dir: str | None = None, speed_limit_kbps: int = 0,
        intake: str = "unknown",
    ) -> Optional[int]:
        """Route a magnet to a torrent source task.

        Only the info hash survives parsing, but the original magnet is
        still what gets persisted as the task's URL: it is local-only, and
        the trackers in it are what Slice B's local downloader will need.
        """
        try:
            magnet = torrent.parse_magnet(url)
        except TorrentError as exc:
            self.error.emit(str(exc))
            return None
        if self._live_torrent(magnet.info_hash):
            self.error.emit("That torrent is already in Cove's queue.")
            return None
        return self.add_url(
            url,
            out_dir=out_dir,
            speed_limit_kbps=speed_limit_kbps,
            source_type=SOURCE_TORRENT,
            info_hash=magnet.info_hash,
            torrent_name=_safe_torrent_name(magnet.display_name),
            intake=intake,
        )

    def add_torrent_file(
        self,
        path: str,
        out_dir: str | None = None,
        *,
        duplicate_check=None,
    ) -> None:
        """Queue a local `.torrent`.

        Reading, bencode parsing, the info-dictionary SHA-1 and the copy
        into Cove's own store all happen on a worker; the GUI thread only
        ever sees the finished metadata and the managed path.

        `duplicate_check` is how an interactive caller gets a say: the info
        hash is only known after parsing, so the check cannot happen before
        the call. It is invoked on the GUI thread with the match and the
        torrent's name, and returning False abandons the add. Automation
        passes nothing and is never prompted.
        """
        if not self._torrent_enabled():
            return
        dest = out_dir or self.settings.download_dir
        source = str(path)

        def on_done(result) -> None:
            meta, managed_path = result
            if self._live_torrent(meta.info_hash):
                self.error.emit("That torrent is already in Cove's queue.")
                return
            if duplicate_check is not None:
                match = self.find_duplicate(
                    torrent.minimal_magnet(meta.info_hash),
                    source_type=SOURCE_TORRENT,
                    info_hash=meta.info_hash,
                )
                if match is not None and not duplicate_check(match, meta.name):
                    return
            # The minimal magnet is the task URL: it identifies the torrent
            # without persisting anything the .torrent might carry. The
            # persisted path is Cove's own copy, not the user's file, so a
            # restart or a retry cannot depend on where they put it.
            self.add_url(
                torrent.minimal_magnet(meta.info_hash),
                out_dir=dest,
                source_type=SOURCE_TORRENT,
                info_hash=meta.info_hash,
                torrent_name=meta.name,
                torrent_path=managed_path,
            )

        self._spawn(
            self._read_and_store_torrent, source,
            on_done=on_done, on_fail=self.error.emit,
        )

    @staticmethod
    def _read_and_store_torrent(source: str):
        """Parse the user's `.torrent` and copy it into Cove's store.

        Runs on a QThreadPool worker; never call it from the GUI thread.
        """
        meta = torrent.read_torrent_file(source)
        return meta, torrent.store_managed_torrent(meta)

    def _launch_torrent(self, t: DownloadTask) -> None:
        self._spawn(
            self._probe_torrent,
            t,
            on_done=lambda cached, tid=t.id: self._on_torrent_probed(tid, cached),
            on_fail=lambda msg, tid=t.id: self._fail_task(tid, msg),
        )

    def _probe_torrent(self, t: DownloadTask):
        """Ask the providers whether they already hold this torrent.

        Runs on a QThreadPool worker; never call it from the GUI thread.
        """
        torrent_bytes = None
        info_hash = t.info_hash
        if t.torrent_path:
            try:
                meta = torrent.read_torrent_file(t.torrent_path)
            except TorrentError:
                # The user moved or deleted the .torrent since adding it.
                # The info hash is enough to ask with, so don't fail here.
                meta = None
            if meta is not None and info_hash and meta.info_hash != info_hash:
                # Something else lives at that path now. The persisted hash
                # is the task's durable identity, so a replaced file must
                # not be able to redirect the row at a different torrent.
                meta = None
            if meta is not None:
                info_hash = meta.info_hash
                # Never the user's original file: it carries the announce
                # URLs, and a private-tracker announce URL carries their
                # passkey. The info-only document hashes identically.
                torrent_bytes = meta.info_only_document()
        return debrid.resolve_torrent(
            info_hash, self.settings, torrent_bytes=torrent_bytes,
            session=self._bound_session(),
        )

    def _on_torrent_probed(self, tid: int, cached) -> None:
        t = self.tasks.get(tid)
        if t is None or t.source_type != SOURCE_TORRENT:
            return
        if cached is None:
            # No enabled provider holds it. Cove downloads it itself rather
            # than handing the user off to another torrent client.
            self._start_local_torrent(tid)
            return
        try:
            self._materialize_cached_torrent(t, cached)
        except TorrentError as exc:
            self._fail_task(tid, str(exc))

    def _materialize_cached_torrent(self, t: DownloadTask, cached) -> None:
        """Turn a cached provider torrent into ordinary HTTPS tasks.

        The source row becomes the torrent's first file and the remaining
        files are inserted next to it, all inside one transaction. That is
        what makes the step idempotent: either every row exists or none
        does, and a second probe of the same info hash finds the rows and
        drops the redundant source instead of expanding twice.

        Only the provider's account-bound *locked* link is stored for
        AllDebrid/Real-Debrid. TorBox has no such link: its rows persist
        `debrid_item_id`/`debrid_file_id` instead, and `url` is set to a
        stable, non-secret `https://` reference built from those IDs --
        never a magnet, because `_launch`'s http(s) check is what routes a
        SOURCE_TORRENT_FILE row through debrid resolution at all; a magnet
        `url` would instead be handed to aria2 as a raw BitTorrent add.
        Either way the short-lived delivery URL is generated per launch
        and never reaches the database.
        """
        from .config import categorize

        def link_for(f) -> str:
            if f.locked_link:
                return f.locked_link
            return f"https://torbox.app/torrent/{f.item_id}/{f.file_id}"

        base = t.out_dir
        rows = []
        for f in cached.files:
            parts = cached.destination_parts(f)
            dest_dir = os.path.join(base, *parts[:-1]) if len(parts) > 1 else base
            # The components are already validated, so this is a second
            # gate rather than the only one — but the only one that knows
            # the actual destination.
            if not _within(base, dest_dir):
                raise TorrentError(
                    "This torrent contains a file path Cove will not write to."
                )
            rows.append((dest_dir, parts[-1], f))

        now = time.time()
        first_dir, first_name, first_file = rows[0]
        new_rows: list[tuple] = []
        with db.connect() as conn:
            # Terminal rows are excluded on purpose, matching _live_torrent:
            # idempotence has to stop a duplicate expansion that is still in
            # flight, not turn finished history into a permanent block on
            # re-downloading the same torrent.
            existing = conn.execute(
                "SELECT id FROM downloads WHERE source_type=? AND info_hash=? "
                "AND status NOT IN ('completed','removed') LIMIT 1",
                (SOURCE_TORRENT_FILE, cached.info_hash),
            ).fetchone()
            if existing is not None:
                # Already expanded by an earlier run. Drop the redundant
                # source row rather than leave a permanently stuck task.
                conn.execute("DELETE FROM downloads WHERE id=?", (t.id,))
            else:
                conn.execute(
                    """
                    UPDATE downloads
                    SET url=?, filename=?, out_dir=?, total_bytes=?,
                        completed_bytes=0, status='queued', error=NULL,
                        gid=NULL, finished_at=NULL, category=?,
                        source_type=?, info_hash=?, torrent_name=?,
                        torrent_path='', debrid_route=?,
                        debrid_item_id=?, debrid_file_id=?
                    WHERE id=?
                    """,
                    (
                        link_for(first_file), first_name, first_dir,
                        first_file.size, categorize(first_name),
                        SOURCE_TORRENT_FILE, cached.info_hash, cached.name,
                        cached.provider, first_file.item_id, first_file.file_id,
                        t.id,
                    ),
                )
                for dest_dir, name, f in rows[1:]:
                    cur = conn.execute(
                        """
                        INSERT INTO downloads
                            (url, out_dir, connections, speed_limit_kbps,
                             status, created_at, category, backend, filename,
                             total_bytes, source_type, info_hash,
                             torrent_name, debrid_route,
                             debrid_item_id, debrid_file_id)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            link_for(f), dest_dir, t.connections,
                            t.speed_limit_kbps, "queued", now,
                            categorize(name), "aria2", name, f.size,
                            SOURCE_TORRENT_FILE, cached.info_hash,
                            cached.name, cached.provider,
                            f.item_id, f.file_id,
                        ),
                    )
                    new_rows.append((cur.lastrowid, dest_dir, name, f))

        if existing is not None:
            self.tasks.pop(t.id, None)
            self.task_removed.emit(t.id)
            self._maybe_start_next()
            return

        t.url = link_for(first_file)
        t.filename = first_name
        t.out_dir = first_dir
        t.total_bytes = first_file.size
        t.completed_bytes = 0
        t.status = "queued"
        t.error = None
        t.gid = None
        t.finished_at = None
        t.source_type = SOURCE_TORRENT_FILE
        t.info_hash = cached.info_hash
        t.torrent_name = cached.name
        t.torrent_path = ""
        t.debrid_route = cached.provider
        t.clear_debrid()
        t.debrid_item_id = first_file.item_id
        t.debrid_file_id = first_file.file_id
        self.task_changed.emit(t.id)

        for tid, dest_dir, name, f in new_rows:
            nt = DownloadTask(
                id=tid,
                url=link_for(f),
                out_dir=dest_dir,
                connections=t.connections,
                speed_limit_kbps=t.speed_limit_kbps,
                filename=name,
                total_bytes=f.size,
                created_at=now,
                source_type=SOURCE_TORRENT_FILE,
                info_hash=cached.info_hash,
                torrent_name=cached.name,
                debrid_route=cached.provider,
                debrid_item_id=f.item_id,
                debrid_file_id=f.file_id,
            )
            self.tasks[tid] = nt
            self.task_added.emit(tid)
        self._maybe_start_next()

    # ---- local BitTorrent ---------------------------------------------
    #
    # Reached only when no enabled provider has the torrent cached. Three
    # gates stand in front of the first byte of peer traffic — the user's
    # fallback preference, the proxy honesty check, and the one-time IP
    # disclosure — and a fourth (aria2's own BitTorrent support) in front
    # of the RPC call itself.

    def _proxy_configured(self) -> bool:
        return (
            getattr(self.settings, "proxy_type", "none") != "none"
            and bool(getattr(self.settings, "proxy_host", ""))
        )

    def _local_fallback_allowed(self) -> bool:
        from .config import TORRENT_FALLBACK_AUTOMATIC

        mode = getattr(self.settings, "torrent_fallback_mode", TORRENT_FALLBACK_AUTOMATIC)
        return mode == TORRENT_FALLBACK_AUTOMATIC

    def _start_local_torrent(self, tid: int) -> None:
        t = self.tasks.get(tid)
        if t is None or t.source_type != SOURCE_TORRENT:
            return
        if not self._local_fallback_allowed():
            # "Cancel the download" in Settings. The task stays visible as a
            # failure with a reason rather than vanishing, and the notice
            # below is skipped entirely: the user already answered it.
            self._fail_task(tid, TORRENT_CANCELLED_UNCACHED)
            return
        if self._proxy_configured() and not getattr(
            self.settings, "torrent_allow_with_proxy", False
        ):
            # aria2's --all-proxy covers HTTP(S) trackers and web seeds; it
            # says nothing about peer connections, DHT or UDP announces. A
            # user who set a proxy did not agree to leak around it.
            self._fail_task(tid, TORRENT_PROXY_BLOCKED)
            return
        if (
            not getattr(self.settings, "torrent_ip_disclosure_shown", False)
            and tid not in self._consent_granted
        ):
            # Park the task and ask the window. Nothing has been sent to
            # aria2 yet, so declining costs the swarm nothing.
            self._awaiting_consent.add(tid)
            self.torrent_consent_needed.emit(tid)
            return
        self._verify_bittorrent_then_add(tid)

    def torrent_consent(self, tid: int, accepted: bool, remember: bool = False) -> None:
        """The user's answer to the uncached-torrent notice.

        Called from the GUI thread by the window that showed the modal.
        Consent is persisted *before* anything reaches aria2, and only when
        the user both proceeded and asked not to be shown the notice again:
        cancelling or dismissing the dialog must never record consent.
        """
        if tid not in self._awaiting_consent:
            return
        self._awaiting_consent.discard(tid)
        if not accepted:
            self._fail_task(tid, TORRENT_CONSENT_DECLINED)
            return
        self._consent_granted.add(tid)
        if remember:
            self.settings.torrent_ip_disclosure_shown = True
            self.settings.save()
        self._verify_bittorrent_then_add(tid)

    def torrent_consent_reevaluate(self, tid: int) -> None:
        """Re-run the local-torrent checks after the user visited Settings.

        The task stayed parked while Settings was open — nothing reached
        aria2 — so the freshly saved settings decide its fate: the notice
        may reappear, or "Cancel the download" may now fail it outright.
        """
        if tid not in self._awaiting_consent:
            return
        self._awaiting_consent.discard(tid)
        self._start_local_torrent(tid)

    def _verify_bittorrent_then_add(self, tid: int) -> None:
        if self._bt_capable is True:
            self._add_local_torrent(tid)
            return
        if self._bt_capable is False:
            self._fail_task(tid, TORRENT_NO_BITTORRENT)
            return
        self._spawn(
            self._bittorrent_capable,
            on_done=lambda ok, tid=tid: self._on_bittorrent_capability(tid, ok),
            # A capability check that cannot complete is not permission to
            # start a torrent anyway.
            on_fail=lambda _msg, tid=tid: self._fail_task(tid, TORRENT_NO_BITTORRENT),
        )

    def _bittorrent_capable(self) -> bool:
        """Runs on a QThreadPool worker; never call it from the GUI thread."""
        return bittorrent_enabled(self.rpc.get_version())

    def _on_bittorrent_capability(self, tid: int, ok: bool) -> None:
        self._bt_capable = bool(ok)
        if ok:
            self._add_local_torrent(tid)
        else:
            self._fail_task(tid, TORRENT_NO_BITTORRENT)

    def _add_local_torrent(self, tid: int) -> None:
        t = self.tasks.get(tid)
        if t is None or t.source_type != SOURCE_TORRENT:
            return
        # The disclosure and the capability check both take time, and the
        # user may have stopped the queue (or the scheduler window may have
        # closed, or they may have paused this task) while they ran. This
        # RPC is where peer and tracker exposure begins, so it stays behind
        # the same lifecycle gates as any other download: defer, and let
        # start_queue / the scheduler / resume relaunch it.
        queue_held = not self._running or not self._scheduler_allows
        if queue_held or t.status == "paused":
            # No add RPC is being issued, so nothing may be left claiming
            # one is in flight: pause() records that intent for any active
            # gid-less aria2 task, including one parked on the disclosure,
            # and a stale entry here would make resume() wait forever for a
            # gid callback that never comes.
            self._pending_launch.pop(tid, None)
            t.status = "paused"
            if queue_held:
                # Queue-driven, not user-driven, so start_queue and the
                # scheduler resume it the way they resume anything else.
                self._auto_paused.add(tid)
            self._persist(t)
            self.task_changed.emit(tid)
            return
        t.status = "active"
        t.error = None
        if not t.filename and t.torrent_name:
            # One row stands for the whole torrent, so the row is named
            # after the torrent rather than after a file inside it.
            t.filename = t.torrent_name
        if t.torrent_path:
            t.phase = ""
            self._pending_launch.setdefault(tid, {})
            self._spawn(
                self._add_managed_torrent,
                t,
                on_done=lambda gid, tid=tid: self._on_local_torrent_gid(tid, gid),
                on_fail=lambda msg, tid=tid: self._fail_task(tid, msg),
            )
            return
        # A magnet has no metadata yet: aria2 fetches it as its own download
        # and reports the real torrent through followedBy.
        t.phase = PHASE_METADATA
        self._persist(t)
        self.task_changed.emit(tid)
        self._pending_launch.setdefault(tid, {})
        self._spawn(
            self._add_local_magnet,
            t,
            on_done=lambda gid, tid=tid: self._on_local_torrent_gid(tid, gid),
            on_fail=lambda msg, tid=tid: self._fail_task(tid, msg),
        )

    def _add_local_magnet(self, t: DownloadTask) -> str:
        """Hand the magnet to aria2, keeping its failures unquotable.

        Runs on a QThreadPool worker. aria2's error text for a bad magnet
        tends to include the magnet, so it is replaced with a fixed sentence
        before it can reach a task row.
        """
        try:
            return self.rpc.add_magnet(t.url, t.out_dir, t.speed_limit_kbps)
        except Aria2Error:
            raise TorrentError(TORRENT_ARIA2_FAILED) from None

    def _add_managed_torrent(self, t: DownloadTask) -> str:
        """Re-read Cove's own `.torrent` copy and hand it to aria2.

        Runs on a QThreadPool worker. The stored copy is re-hashed every
        time: a replaced or corrupted file must fail the task, never start
        a different torrent.
        """
        # A TorrentError from the read is one of Cove's own fixed sentences
        # and is the more useful diagnosis, so it is left alone; only
        # aria2's message is replaced.
        data = torrent.read_managed_torrent(t.torrent_path, t.info_hash)
        try:
            return self.rpc.add_torrent(data, t.out_dir, t.speed_limit_kbps)
        except Aria2Error:
            raise TorrentError(TORRENT_ARIA2_FAILED) from None

    def _on_local_torrent_gid(self, tid: int, gid: str) -> None:
        # Record the gid before anything else so the external-download poll
        # can never adopt Cove's own torrent as a stranger's.
        self._seen_gids.add(gid)
        pending = self._pending_launch.pop(tid, {})
        t = self.tasks.get(tid)
        if t is None:
            # Removed while the add was in flight; don't leak the download,
            # and honour a remove-and-delete the user already chose.
            self._finish_inflight_torrent_removal(
                gid,
                pending.get("out_dir", ""),
                bool(pending.get("delete_file")),
            )
            return
        t.gid = gid
        if (
            pending.get("pause")
            or t.status == "paused"
            or not self._running
            or not self._scheduler_allows
        ):
            # Paused, or the queue stopped, while the add was in flight.
            t.status = "paused"
            self._spawn(self.rpc.pause, gid, on_fail=lambda *_: None)
        else:
            # Covers a pause-then-resume during the add: resume left the
            # task "queued", which _maybe_start_next skips once a gid
            # exists and _poll_active ignores entirely. The gid callback is
            # where Cove reconciles with aria2, so put it back in a
            # pollable state.
            t.status = "active"
            t.error = None
        self._persist(t)
        self.task_changed.emit(tid)

    def _on_torrent_metadata(self, t: DownloadTask, status: dict) -> bool:
        """Follow a magnet's metadata gid onto the real torrent gid.

        Returns True when this status was the metadata transition and the
        caller must not treat it as ordinary progress — in particular, the
        metadata download completing is not the task completing.
        """
        followed = status.get("followedBy")
        children = [
            g for g in followed if isinstance(g, str) and g
        ] if isinstance(followed, list) else []
        if children:
            child = children[0]
            self._seen_gids.add(child)
            t.gid = child
            t.phase = ""
            t.error = None
            t.finished_at = None
            t.completed_bytes = 0
            # The child gid is the actual transfer, and aria2 starts it
            # running. A pause taken during the metadata fetch — by the
            # user, by stop_queue or by the scheduler — has to be re-applied
            # here, or the swarm would resume behind the user's back.
            queue_held = not self._running or not self._scheduler_allows
            if t.status == "paused" or queue_held:
                t.status = "paused"
                if queue_held:
                    self._auto_paused.add(t.id)
                self._spawn(self.rpc.pause, child, on_fail=lambda *_: None)
            else:
                t.status = "active"
            self._persist(t)
            self.task_changed.emit(t.id)
            return True
        if t.phase == PHASE_METADATA and status.get("status") in ("complete", "error"):
            # aria2 finished with the metadata download but named no
            # torrent to follow. There is nothing to download.
            self._fail_task(t.id, TORRENT_METADATA_FAILED)
            return True
        return False

    def _apply_torrent_status(self, t: DownloadTask, status: dict) -> None:
        """Torrent-shaped fields of a status poll: name and info hash."""
        info_hash = status.get("infoHash")
        if isinstance(info_hash, str) and info_hash:
            try:
                t.info_hash = torrent.normalize_info_hash(info_hash)
            except TorrentError:
                pass
        bt = status.get("bittorrent") if isinstance(status.get("bittorrent"), dict) else {}
        info = bt.get("info") if isinstance(bt.get("info"), dict) else {}
        safe = _safe_torrent_name(info.get("name"))
        if safe:
            t.torrent_name = safe
            t.filename = safe

    def _fail_task(self, tid: int, msg: str) -> None:
        t = self.tasks.get(tid)
        if not t:
            return
        # Nothing is in flight for a failed task; a leftover entry would
        # make _maybe_start_next skip it forever on retry.
        self._pending_launch.pop(tid, None)
        t.status = "error"
        t.error = msg
        t.finished_at = time.time()
        self._persist(t)
        self.task_changed.emit(tid)
        self._maybe_start_next()

    def _cleanup_engine_work(
        self, work: WorkDirectory | None, task_id: int | None = None
    ) -> None:
        if work is None:
            return
        try:
            cleanup_work_directory(work)
        except (OSError, OutputPathError) as exc:
            self._diag("extractor.publish", "work_cleanup", "WARNING",
                       task_id=task_id, result="failure", exc=exc)
            self.error.emit(f"Could not clean private output directory: {exc}")
        else:
            self._diag("extractor.publish", "work_cleanup", "INFO",
                       task_id=task_id, result="success")

    def _stop_engine_process(
        self,
        proc: QProcess | tuple[QProcess | None, ...] | None,
        work: WorkDirectory | None,
        on_stopped=None,
    ) -> None:
        completed = False
        processes = proc if isinstance(proc, tuple) else (proc,)
        remaining: list[QProcess] = []
        for candidate in processes:
            if candidate is not None and all(candidate is not item for item in remaining):
                remaining.append(candidate)

        def _complete(*_args) -> None:
            nonlocal completed
            if completed:
                return
            completed = True
            self._cleanup_engine_work(work)
            if on_stopped is not None:
                on_stopped()

        if not remaining:
            _complete()
            return

        def _process_stopped(candidate: QProcess) -> None:
            for index, item in enumerate(remaining):
                if item is candidate:
                    remaining.pop(index)
                    break
            if not remaining:
                _complete()

        for candidate in tuple(remaining):
            candidate.finished.connect(
                lambda *_args, stopped=candidate: _process_stopped(stopped)
            )
            if candidate.state() == QProcess.NotRunning:
                _process_stopped(candidate)
            else:
                candidate.terminate()

        def _force_kill() -> None:
            for candidate in tuple(remaining):
                try:
                    if candidate.state() != QProcess.NotRunning:
                        candidate.kill()
                    else:
                        _process_stopped(candidate)
                except RuntimeError:
                    _process_stopped(candidate)

        QTimer.singleShot(5000, _force_kill)

    def _retire_hls_run(self, tid: int) -> None:
        proc = self._hls_procs.pop(tid, None)
        self._hls_duration.pop(tid, None)
        self._hls_stderr.pop(tid, None)
        self._hls_line_buffer.pop(tid, None)
        work = self._hls_work.pop(tid, None)
        self._stop_engine_process(proc, work)

    def _retire_extractor_run(self, tid: int) -> None:
        self._extractor_paused_work.discard(tid)
        proc = self._extractor_procs.pop(tid, None)
        pending_proc = self._extractor_pause_pending.get(tid)
        self._extractor_output.pop(tid, None)
        self._extractor_final_path.pop(tid, None)
        self._extractor_line_buffer.pop(tid, None)
        work = self._extractor_work.pop(tid, None)

        def _retired() -> None:
            if self._extractor_pause_pending.get(tid) is pending_proc:
                self._extractor_pause_pending.pop(tid, None)
            self._maybe_start_next()

        self._stop_engine_process((proc, pending_proc), work, _retired)

    def pause(self, tid: int) -> None:
        t = self.tasks.get(tid)
        if not t or t.status not in {"active", "queued"}:
            return
        proc = None
        work = None
        if tid in self._hls_procs or tid in self._hls_work:
            proc = self._hls_procs.pop(tid, None)
            self._hls_duration.pop(tid, None)
            self._hls_stderr.pop(tid, None)
            self._hls_line_buffer.pop(tid, None)
            work = self._hls_work.pop(tid, None)
        elif tid in self._extractor_procs or tid in self._extractor_work:
            proc = self._extractor_procs.pop(tid, None)
            self._extractor_output.pop(tid, None)
            self._extractor_final_path.pop(tid, None)
            self._extractor_line_buffer.pop(tid, None)
            work = self._extractor_work.get(tid)
            if proc is not None and work is not None:
                self._extractor_pause_pending[tid] = proc

                def _paused_after_stop():
                    if self._extractor_pause_pending.get(tid) is not proc:
                        return
                    if tid not in self.tasks or self._extractor_work.get(tid) is not work:
                        return
                    self._extractor_pause_pending.pop(tid, None)
                    self._extractor_paused_work.add(tid)
                    self._mark_paused(tid)

                self._stop_engine_process(proc, None, _paused_after_stop)
                return
        if proc is not None or work is not None:
            # ffmpeg can't be suspended: pause kills the process and resume
            # relaunches from scratch.
            self._stop_engine_process(proc, work)
            self._mark_paused(tid)
            return
        if t.status == "active" and not t.gid and t.backend == "aria2":
            # add_uri is mid-flight; remember the intent so on_done can
            # send rpc.pause() once it knows the gid. Reflect it locally
            # right away so the UI doesn't lie about state.
            self._pending_launch.setdefault(tid, {})["pause"] = True
            self._mark_paused(tid)
            return
        if t.gid and t.status == "active":
            def _send(gen, gid=t.gid):
                def _paused(_result):
                    # Local state follows the intent, not the generation. A
                    # pause landing after the user has resumed must not persist
                    # "paused" over that newer wish - and the newer wish may be
                    # held rather than issued, in which case it has no
                    # generation of its own to compare against yet.
                    if self._desired_for(tid, gid) is True:
                        self._mark_paused(tid)
                    self._on_cmd_landed(tid, gen, gid, True)

                self._spawn(
                    self.rpc.pause, gid,
                    on_done=_paused,
                    on_fail=lambda msg: self._on_pause_failed(tid, gen, gid, msg),
                )

            self._issue_state(
                tid, t.gid, True, _send,
                settle=lambda: self._mark_paused(tid),
            )
        else:
            self._mark_paused(tid)

    # ---- pause/unpause command ordering --------------------------------

    def _issue_state(self, tid: int, gid: str, paused: bool, send, settle=None) -> None:
        """Put one state-changing command per task in flight, and only one.

        Two commands running against the same gid are independent HTTP requests
        on independent worker threads: aria2 can apply them in one order and
        their callbacks can arrive in the other. Nothing downstream can then
        say which command the backend actually ended up applying, and
        `_note_backend_state` would record the wrong answer - after which
        `_converge` sees false agreement and never corrects it.

        Serialising removes the ambiguity at the source. While a command is
        outstanding the newer intent is recorded and its send is held; it
        replaces any previously held send, because only the latest intent is
        worth issuing. `_converge` releases it once the outstanding command
        resolves.

        `send(gen)` spawns the RPC with its own callbacks, so each caller keeps
        its own success and failure handling. `settle` carries whatever local
        state that command would have applied on success, for the case where
        the wish is later dropped as redundant: aria2 needs no command then,
        but Cove still has to agree with it.
        """
        self._desired_paused[tid] = (gid, paused)
        if self._in_flight(tid, gid):
            self._cmd_queued[tid] = (gid, paused, send, settle)
            return
        send(self._next_cmd_gen(tid, gid, paused=paused))

    def _state_sender(self, tid: int, gid: str, paused: bool):
        """A send for a command Cove issued itself rather than the user.

        Failures are repairs that did not take: reported, and the belief about
        aria2 dropped rather than kept as if it had worked.
        """
        def _send(gen):
            fn = self.rpc.pause if paused else self.rpc.unpause
            self._spawn(
                fn, gid,
                on_done=lambda _: self._on_cmd_landed(tid, gen, gid, paused),
                on_fail=lambda msg: self._on_compensation_failed(
                    tid, gen, gid, paused, msg
                ),
            )

        return _send

    def _release_queued(self, tid: int, gid: str) -> bool:
        """Send the wish held back while a command was in flight.

        Held for the same transfer only, and only when aria2 is not already in
        the state it asks for. A user who clicks pause twice raises the same
        wish twice; once the first command has landed, the second describes
        what the backend already holds and sending it would be the duplicate
        this whole mechanism exists to avoid.
        """
        entry = self._cmd_queued.get(tid)
        if entry is None or entry[0] != gid:
            return False
        self._cmd_queued.pop(tid, None)
        _gid, paused, send, settle = entry
        t = self.tasks.get(tid)
        if t is None or t.gid != gid or tid in self._removing:
            return False
        if self._backend_for(tid, gid) == paused:
            # No RPC needed, but the wish was still granted: aria2 is already
            # in the state it asked for. Its local effect has to happen anyway,
            # or the UI and the persisted row keep describing the state the
            # user moved away from.
            self._desired_paused[tid] = (gid, paused)
            if settle is not None:
                settle()
            return False
        send(self._next_cmd_gen(tid, gid, paused=paused))
        return True

    def _next_cmd_gen(self, tid: int, gid: str, paused: bool) -> int:
        """Record the newest pause/unpause intent and return its generation.

        The intent is scoped to the gid it was issued for. A retry abandons a
        gid and relaunches under a new one, and an intent left over from the
        old transfer says nothing about the new one - carrying it forward would
        both suppress reconciliation and describe a download that no longer
        exists.
        """
        gen = self._cmd_gen.get(tid, 0) + 1
        self._cmd_gen[tid] = gen
        self._desired_paused[tid] = (gid, paused)
        # Every caller spawns the command immediately, so issuing it and
        # putting it in flight are the same moment.
        self._cmd_pending.setdefault(tid, {})[gen] = gid
        return gen

    def _desired_for(self, tid: int, gid: str):
        """The recorded intent for `gid`, or None if there is none for it."""
        record = self._desired_paused.get(tid)
        if not record or record[0] != gid:
            return None
        return record[1]

    def _backend_for(self, tid: int, gid: str):
        """What aria2 is known to hold for `gid`, or None if unobserved."""
        record = self._backend_state.get(tid)
        if not record or record[0] != gid:
            return None
        return record[1]

    def _note_backend_state(self, tid: int, gid: str, paused: bool) -> None:
        """Record what a command that just reached aria2 left behind.

        Callback order is the only evidence of the order commands landed in, so
        the most recent callback describes the backend. No generation check
        belongs here: an older command whose result arrives last is precisely
        the one aria2 applied last.
        """
        self._backend_state[tid] = (gid, paused)

    def _note_resolved(self, tid: int, gen: int) -> None:
        """Mark a command as no longer in flight, whether it landed or failed.

        Failures count. A command that will never land must stop being treated
        as pending, or convergence waits forever for a result that is not
        coming.
        """
        pending = self._cmd_pending.get(tid)
        if pending is None:
            return
        pending.pop(gen, None)
        if not pending:
            self._cmd_pending.pop(tid, None)

    def _in_flight(self, tid: int, gid: str) -> bool:
        """Whether a command is outstanding against this exact transfer."""
        return gid in self._cmd_pending.get(tid, {}).values()

    def _on_cmd_landed(self, tid: int, gen: int, gid: str, paused: bool) -> None:
        """A pause/unpause reached aria2."""
        self._note_resolved(tid, gen)
        self._note_backend_state(tid, gid, paused)
        self._converge(tid, gid)

    def _converge(self, tid: int, gid: str) -> None:
        """Send the current intent if aria2 is known to hold something else.

        This is the whole reconciliation rule. Rather than reasoning about
        which command superseded which, it compares two facts: the state the
        user asked for (`_desired_paused`) and the state aria2 was last
        observed in (`_backend_state`). If they disagree and no command is
        still in flight to close the gap, the intent is sent again.

        Waiting for in-flight commands is what keeps this from racing itself: a
        command already on its way to aria2 targets the current intent, so
        compensating before it lands would just issue a duplicate. Once every
        command has resolved, one of two things is true - either the backend
        matches the intent and there is nothing to do, or it does not and
        exactly one corrective command is issued. That command's own landing
        re-runs this check, which then finds agreement and stops.

        This is also where a command held back by `_issue_state` is released,
        since that is the same moment: the task now has nothing outstanding.
        """
        if tid in self._removing:
            return
        t = self.tasks.get(tid)
        if t is None or t.gid != gid:
            return
        if self._in_flight(tid, gid):
            return
        if self._release_queued(tid, gid):
            # The intent held during the last command is now on its way, and it
            # is by definition the current one. Its own landing runs this again.
            return
        desired = self._desired_for(tid, gid)
        if desired is None:
            return
        known = self._backend_for(tid, gid)
        if known is None or known == desired:
            # Unobserved is not the same as wrong. Asserting a state over a
            # backend nothing has reported on would be a guess, and the next
            # explicit command or queue transition will settle it anyway.
            return
        self._state_sender(tid, gid, desired)(
            self._next_cmd_gen(tid, gid, paused=desired)
        )

    def _on_compensation_failed(
        self, tid: int, gen: int, gid: str, paused: bool, msg: str
    ) -> None:
        """A corrective pause/unpause did not reach aria2.

        Discarding a stale result is not enough if the command meant to repair
        it also fails, so this must not be swallowed. The error is surfaced and
        the recorded intent for this gid is dropped: Cove no longer claims to
        know what aria2 holds, which stops that stale belief from suppressing
        the next reconciliation. Convergence then comes from the next explicit
        pause/resume or queue transition - nothing here pretends it succeeded.

        A repair the user has already overtaken is not their problem, so it is
        neither reported nor allowed to clear the newer intent. The test is
        against the intent rather than the generation, because the newer wish
        may be held rather than issued and so have no generation yet.
        """
        self._note_resolved(tid, gen)
        if self._desired_for(tid, gid) != paused:
            self._converge(tid, gid)
            return
        self.error.emit(msg)
        if self._desired_paused.get(tid, (None,))[0] == gid:
            self._desired_paused.pop(tid, None)
        # Still release anything held behind this command, or a wish raised
        # while it was in flight would never be sent.
        self._converge(tid, gid)

    def _on_pause_failed(self, tid: int, gen: int, gid: str, msg: str) -> None:
        """Retract a pause intent aria2 refused to act on.

        `_desired_paused` records the state aria2 is believed to be converging
        on. A failed pause never reached it, so leaving the intent set would
        claim a pause is still pending forever - and anything that defers to a
        pending pause (queue-wide reconciliation in particular) would keep
        skipping this task while it is in fact still downloading.

        Retracting is only half of it. Something else may already have put
        aria2 into the paused state this command failed to reach - a queue-wide
        pause, or an earlier pause of this same task that did succeed. The
        retraction is therefore followed by a convergence check, which sends
        the unpause if aria2 is known to be holding a pause nobody wants now.
        """
        self.error.emit(msg)
        self._note_resolved(tid, gen)
        if self._desired_for(tid, gid) is not True or tid in self._cmd_queued:
            # Something newer wants a different state, or a wish is held behind
            # this command and is about to be sent. Either way a pause is not
            # what is outstanding any more, so there is nothing to retract -
            # and retracting would overwrite the newer wish. Neither has a
            # generation to compare against yet, which is why the test is on
            # the intent rather than the generation.
            self._converge(tid, gid)
            return
        self._desired_paused[tid] = (gid, False)
        self._converge(tid, gid)

    def resume(self, tid: int) -> None:
        t = self.tasks.get(tid)
        if not t or t.status not in {"paused", "error"}:
            return
        if t.gid and t.status == "paused":
            # Optimistic flip to active — unpause is just telling aria2 to
            # resume the existing gid, no new add_uri needed.
            t.status = "active"
            t.error = None
            self._persist(t)
            self.task_changed.emit(tid)
            def _send(gen, gid=t.gid):
                self._spawn(
                    self.rpc.unpause, gid,
                    on_done=lambda _: self._on_cmd_landed(tid, gen, gid, False),
                    on_fail=lambda msg: self._on_unpause_failed(tid, msg, gen, gid),
                )

            self._issue_state(tid, t.gid, False, _send)
        elif not t.gid and tid in self._pending_launch:
            # The add RPC is still in flight, so there is nothing to unpause
            # and nothing to relaunch — a relaunch would add it to aria2
            # twice. Cancel the deferred pause instead and let the gid
            # callback finish the transition back to active.
            self._pending_launch[tid].pop("pause", None)
            t.status = "active"
            t.error = None
            self._persist(t)
            self.task_changed.emit(tid)
        else:
            if t.gid:
                # Errored aria2 download: _maybe_start_next skips tasks that
                # already have a gid, so drop the dead one from aria2 and
                # relaunch fresh — otherwise Retry leaves it stuck "queued".
                self._spawn(self.rpc.remove, t.gid, on_fail=lambda *_: None)
                t.gid = None
            t.status = "queued"
            t.error = None
            self._persist(t)
            self.task_changed.emit(tid)
            self._maybe_start_next()

    def force_start(self, tid: int) -> None:
        t = self.tasks.get(tid)
        if not t or t.status != "queued":
            return
        self._launch(t)

    def _cleans_incomplete_data(
        self,
        t: DownloadTask,
        delete_file: bool,
        keep_incomplete: bool,
        private_run: bool,
    ) -> bool:
        """Whether removing `t` should also clean what it left on disk.

        Explicitly removing an unfinished aria2 download means "abandon this
        download", so its partial payload and matching .aria2 resume file go
        with the row - otherwise the only thing that knew about those files is
        deleted and they stay behind forever, unresumable.

        A finished file is never touched without an explicit delete, private
        engine runs (yt-dlp/ffmpeg) keep their own work-directory cleanup, and
        callers that promise on-disk files are kept - Clear all, Clear
        completed, the API - opt out with `keep_incomplete`.
        """
        if delete_file:
            return True
        if keep_incomplete or private_run:
            return False
        return t.backend == "aria2" and t.status != "completed"

    def remove(
        self, tid: int, delete_file: bool = False, keep_incomplete: bool = False
    ) -> None:
        t = self.tasks.get(tid)
        if not t:
            return

        # Torrents first: a torrent has its own removal path, and it also
        # covers the two states the generic in-flight handling below cannot
        # see — an add_magnet/addTorrent RPC still on its way back, and a
        # task parked on the privacy disclosure.
        if t.source_type == SOURCE_TORRENT:
            self._remove_torrent(t, delete_file)
            return

        # Special case: add_uri RPC is in flight. We can't ask aria2 to
        # remove a gid we don't have yet, so keep the task alive in
        # self.tasks but hide it from the UI/DB. on_done will dispatch the
        # actual remove once it learns the gid. Only aria2 tasks qualify:
        # active ffmpeg/yt-dlp tasks never get a gid and must instead go
        # through the normal path that terminates their process.
        if t.status == "active" and not t.gid and t.backend == "aria2":
            # Resolve the cleanup decision now, while the task is still here to
            # ask; on_done only sees the tombstone. A task in this state has no
            # private engine run by definition.
            self._pending_launch.setdefault(tid, {}).update(
                {
                    "remove": True,
                    "delete_file": self._cleans_incomplete_data(
                        t, delete_file, keep_incomplete, private_run=False
                    ),
                }
            )
            with db.connect() as conn:
                conn.execute("DELETE FROM downloads WHERE id=?", (tid,))
            self.task_removed.emit(tid)
            return

        # A second click while aria2 is still deciding must not start a
        # second teardown for the same task.
        if tid in self._removing:
            return

        # Normal path: stop any private engine run, then ask aria2 to forget
        # the gid. Local state, the database row and anything on disk are
        # only touched once aria2 has confirmed - see _finish_removal.
        private_run = False
        if tid in self._hls_procs or tid in self._hls_work:
            private_run = True
            self._retire_hls_run(tid)
        if tid in self._extractor_procs or tid in self._extractor_work:
            private_run = True
            self._retire_extractor_run(tid)
        gid = t.gid
        path = None if private_run else self._task_path(t)
        clean = self._cleans_incomplete_data(t, delete_file, keep_incomplete, private_run)

        if not gid:
            # Nothing to cancel: there is no backend transfer to outlive us.
            self._finish_removal(tid, path if clean else None)
            return

        # A gid means aria2 may still be writing. Until it answers, the task
        # is still real: it stays visible, keeps its gid mapping and keeps
        # occupying a concurrency slot. Deleting files here would race aria2
        # into recreating them.
        self._removing[tid] = {"gid": gid, "path": path, "clean": clean}
        # The row does have to record that it is on its way out, though. Before
        # this was two-phase the delete was synchronous, so a crash could never
        # resurrect a removed download; leaving the row untouched until aria2
        # answers would restore an active task on the next launch and start it
        # downloading again. The marker is durable and deliberately not applied
        # to `t.status`, which the UI and _restore_removal still need - being
        # in `_removing` is what makes _persist write it.
        self._persist(t)
        self.task_changed.emit(tid)

        def _confirmed(*_args):
            # Ordering below is load-bearing: aria2 has forgotten the gid, so
            # the payload and control file can no longer be recreated.
            self._finish_removal(tid, path if clean else None)

        def _refused(*args):
            self.error.emit(*args)
            self._restore_removal(tid)

        self._spawn(self.rpc.remove, gid, on_done=_confirmed, on_fail=_refused)

    def is_pending_removal(self, tid: int) -> bool:
        """Whether `tid` is still tracked only so its cancellation can finish.

        True both while aria2 has yet to confirm a removal and while a task
        removed before its add_uri returned is waiting for the gid. Callers
        that hide removed tasks need this to tell "on its way out" from "the
        backend refused, so it is live again".
        """
        if tid in self._removing:
            return True
        return bool(self._pending_launch.get(tid, {}).get("remove"))

    def _finish_removal(self, tid: int, unlink_path: str | None) -> None:
        """Let go of a task aria2 has confirmed it no longer owns."""
        self._removing.pop(tid, None)
        self.tasks.pop(tid, None)
        self._cmd_gen.pop(tid, None)
        self._desired_paused.pop(tid, None)
        self._backend_state.pop(tid, None)
        self._cmd_pending.pop(tid, None)
        self._cmd_queued.pop(tid, None)
        with db.connect() as conn:
            conn.execute("DELETE FROM downloads WHERE id=?", (tid,))
        if unlink_path:
            self._make_unlinker(unlink_path)()
        self.task_removed.emit(tid)
        self._maybe_start_next()

    def _restore_removal(self, tid: int) -> None:
        """Put a task back after aria2 refused to cancel it.

        The transfer is still running and Cove is still the only thing that
        knows about it, so nothing was deleted and the row never left the
        database. All that is needed is to stop treating it as doomed - and to
        clear the durable removal marker, or the next launch would drop a task
        aria2 is still running.
        """
        self._removing.pop(tid, None)
        t = self.tasks.get(tid)
        if t is None:
            return
        self._persist(t)
        self.task_changed.emit(tid)
        self._reassert_intent(t)

    def _reassert_intent(self, t: DownloadTask) -> None:
        """Re-send this task's pause/unpause intent to aria2.

        Reconciliation is deliberately suppressed while a removal is in flight,
        so a pause or unpause that completed during that window was never
        checked against the current intent. A refused removal leaves the
        transfer live, which means aria2 may now hold the opposite of what Cove
        shows - so the intent is asserted again rather than assumed.
        """
        gid = t.gid
        if not gid:
            return
        desired = self._desired_for(t.id, gid)
        if desired is None:
            # No per-task command to replay, but the task was also excluded
            # from any queue-wide pause that ran during the removal window, so
            # aria2's state for it is simply unknown. Assert what the queue
            # wants for it now rather than assuming the two agree.
            desired = (
                t.status == "paused"
                or not (self._running and self._scheduler_allows)
            )
            if desired and t.status == "active":
                # Only the stopped-queue branch reaches here, and this is the
                # auto-pause the task missed by being mid-removal when
                # _mark_all_active_paused ran. Recording it is what makes Start
                # resume the task: start_queue only resumes members of
                # _auto_paused that are locally paused, so leaving the status
                # at "active" would strand the download paused in aria2 behind
                # a queue the UI shows as running.
                t.status = "paused"
                self._persist(t)
                self._auto_paused.add(t.id)
                self.task_changed.emit(t.id)
        self._issue_state(t.id, gid, desired, self._state_sender(t.id, gid, desired))

    # ---- torrent removal ----------------------------------------------

    def _remove_torrent(self, t: DownloadTask, delete_file: bool) -> None:
        """Drop a local torrent task, optionally deleting what it wrote.

        A torrent is a tree, not a file, so the paths to delete come from
        aria2 rather than from anything Cove reconstructed — and even those
        are re-checked against the task's destination before anything is
        unlinked. The torrent's own folder is never handed to rmtree: only
        the files aria2 named, their `.aria2` control files, and directories
        that those deletions left empty.
        """
        tid = t.id
        gid = t.gid
        base = t.out_dir
        self.tasks.pop(tid, None)
        self._awaiting_consent.discard(tid)
        self._consent_granted.discard(tid)
        with db.connect() as conn:
            conn.execute("DELETE FROM downloads WHERE id=?", (tid,))
        self.task_removed.emit(tid)
        # The managed .torrent belongs to this torrent, not to this row;
        # another live task for the same info hash still needs it.
        if t.torrent_path and not self._info_hash_in_use(t.info_hash):
            torrent.discard_managed_torrent(t.torrent_path)

        if not gid:
            if tid in self._pending_launch:
                # The add RPC is still on its way back. Leave a tombstone so
                # the gid callback can finish the job — including the delete
                # the user asked for, which would otherwise be dropped along
                # with any partial data aria2 wrote in the meantime.
                self._pending_launch[tid] = {
                    "remove": True,
                    "delete_file": bool(delete_file),
                    "out_dir": base,
                }
            self._maybe_start_next()
            return

        def _drop_gid(paths=()):
            def _after(*_args):
                if delete_file:
                    self._delete_torrent_files(paths, base)
                self._maybe_start_next()

            self._spawn(self.rpc.remove, gid, on_done=_after, on_fail=_after)

        if not delete_file:
            _drop_gid()
            return
        # Ask aria2 what it wrote *before* removing the gid; afterwards it
        # no longer knows.
        self._spawn(
            self.rpc.get_files,
            gid,
            on_done=lambda files: _drop_gid(self._torrent_file_paths(files)),
            on_fail=lambda *_: _drop_gid(),
        )

    def _finish_inflight_torrent_removal(
        self, gid: str, base: str, delete_file: bool
    ) -> None:
        """Drop a gid that landed after its task was removed.

        Reuses the same bounded deletion path as an ordinary removal, so
        anything aria2 wrote during the race is cleaned up under exactly the
        same containment checks.
        """
        def _drop(paths=()):
            def _after(*_args):
                if paths and base:
                    self._delete_torrent_files(paths, base)
                self._maybe_start_next()

            self._spawn(self.rpc.remove, gid, on_done=_after, on_fail=_after)

        if not (delete_file and base):
            _drop()
            return
        self._spawn(
            self.rpc.get_files,
            gid,
            on_done=lambda files: _drop(self._torrent_file_paths(files)),
            on_fail=lambda *_: _drop(),
        )

    def _info_hash_in_use(self, info_hash: str) -> bool:
        return bool(info_hash) and any(
            t.info_hash == info_hash for t in self.tasks.values()
        )

    @staticmethod
    def _torrent_file_paths(files) -> tuple[str, ...]:
        if not isinstance(files, list):
            return ()
        return tuple(
            f["path"] for f in files
            if isinstance(f, dict) and isinstance(f.get("path"), str) and f["path"]
        )

    @staticmethod
    def _delete_torrent_files(paths, base: str) -> None:
        """Unlink aria2's files for a torrent, staying under `base`.

        Every path is checked with realpath containment, which also rejects
        a symlink that points out of the tree: deleting the link itself
        would be harmless, but a link Cove followed would not be, so
        anything whose real location escapes is skipped entirely.
        """
        from pathlib import Path

        deleted_dirs: set[str] = set()
        for raw in paths:
            if not _within(base, raw):
                continue
            p = Path(raw)
            for candidate in (p, p.with_name(p.name + ".aria2")):
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass
            deleted_dirs.add(str(p.parent))

        # Deepest first, so a directory emptied by its children's removal is
        # itself considered. rmdir only ever removes an empty directory, so
        # an unrelated file left in one keeps it.
        base_real = os.path.realpath(base)
        for directory in sorted(deleted_dirs, key=lambda d: -d.count(os.sep)):
            current = Path(directory)
            while _within(base, str(current)) and os.path.realpath(current) != base_real:
                try:
                    current.rmdir()
                except OSError:
                    break
                current = current.parent

    @staticmethod
    def _make_unlinker(path):
        """Return a callable that deletes `path` and its `.aria2` control
        file, ignoring missing files. No-op if path is None."""
        def _unlink() -> None:
            if not path:
                return
            ctrl = path.with_name(path.name + ".aria2")
            for p in (path, ctrl):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
        return _unlink

    def resume_persisted(self) -> None:
        """Kick off any tasks restored from SQLite.

        Call this once aria2's RPC is confirmed up. Without it, items left
        queued/active/paused at the previous shutdown would sit forever
        until the user touched the queue.
        """
        if self._running and self._scheduler_allows:
            self._maybe_start_next()

    def clear_completed(self, delete_files: bool = False) -> None:
        # Completed rows only, and explicitly never the incomplete-data
        # cleanup: this is a list operation, not an abandon-download one.
        for tid in [t.id for t in self.tasks.values() if t.status == "completed"]:
            self.remove(tid, delete_file=delete_files, keep_incomplete=True)

    def start_queue(self) -> None:
        if self._running:
            return
        self._running = True
        # Supersede any queue-wide pause still in flight from the Stop this
        # is undoing, so its result cannot pause what is about to resume.
        self._queue_epoch += 1
        self.queue_running_changed.emit(True)
        # Resume only items that stop_queue paused (not user-paused ones).
        for tid in list(self._auto_paused):
            t = self.tasks.get(tid)
            if t and t.status == "paused":
                self.resume(tid)
        self._auto_paused.clear()
        self._maybe_start_next()

    def stop_queue(self) -> None:
        if not self._running:
            return
        self._running = False
        self._queue_epoch += 1
        self.queue_running_changed.emit(False)
        self._auto_paused = {t.id for t in self.tasks.values() if t.status == "active"}
        self._pause_everything()

    def set_overall_speed_limit(self, kbps: int) -> None:
        """Push the *active* aria2 cap. Settings persistence is the
        caller's responsibility — this method must not write the configured
        kbps back to settings, otherwise toggling the limiter off would
        clobber the user's chosen value.
        """
        self._spawn(self.rpc.set_overall_speed_limit_kbps, kbps)

    def set_max_concurrent(self, n: int) -> None:
        self.settings.max_concurrent = max(1, n)
        self.settings.save()
        self._maybe_start_next()

    def set_scheduler_allowed(self, allowed: bool) -> None:
        if allowed == self._scheduler_allows:
            return
        self._scheduler_allows = allowed
        # Same reasoning as start_queue/stop_queue: crossing a schedule
        # boundary supersedes any queue-wide pause still in flight.
        self._queue_epoch += 1
        if allowed:
            for tid in list(self._auto_paused):
                t = self.tasks.get(tid)
                if t and t.status == "paused":
                    self.resume(tid)
            self._auto_paused.clear()
            if self._running:
                self._maybe_start_next()
        else:
            self._auto_paused |= {t.id for t in self.tasks.values() if t.status == "active"}
            self._pause_everything()

    def _pause_everything(self) -> None:
        """Queue-wide pause: terminate ffmpeg/yt-dlp processes and pause all
        aria2 downloads. Active tasks are marked paused even if the pause_all
        RPC fails - the video processes are already gone by then, so leaving
        their tasks "active" would persist a state that cannot resume."""
        for tid in set(self._hls_procs) | set(self._hls_work):
            self._retire_hls_run(tid)
        for tid in set(self._extractor_procs) | set(self._extractor_work):
            self._retire_extractor_run(tid)
        self._hls_duration.clear()
        self._hls_stderr.clear()
        self._hls_line_buffer.clear()
        self._extractor_output.clear()
        self._extractor_final_path.clear()
        self._extractor_line_buffer.clear()

        epoch = self._queue_epoch

        def _settled(landed: bool) -> None:
            if landed:
                # pause_all pauses every transfer aria2 holds, so this is the
                # one place a queue-wide command teaches the per-task backend
                # record what the backend now contains. Without it, a task the
                # stale-result path steps over has no observed state at all,
                # and nothing can tell that aria2 is holding a pause.
                for t in self.tasks.values():
                    if t.gid:
                        self._note_backend_state(t.id, t.gid, True)
            if not self._settle_pause_all(epoch):
                self._mark_all_active_paused()

        def _on_fail(msg: str) -> None:
            self.error.emit(msg)
            _settled(False)

        self._spawn(
            self.rpc.pause_all,
            on_done=lambda _result=None: _settled(True),
            on_fail=_on_fail,
        )

    def _settle_pause_all(self, epoch: int) -> bool:
        """Handle a pause_all result that a later decision superseded.

        Returns True when the result was stale and has been dealt with here.

        A Stop immediately followed by a Start leaves a pause_all in flight
        across the restart. Marking tasks paused then would stop downloads the
        UI shows as running. The RPC still reaches aria2 whenever it reaches
        it, though, so discarding the result is only half the job: anything
        Cove currently considers active has to be unpaused again, or aria2
        stays paused behind a running-looking queue.
        """
        if epoch == self._queue_epoch:
            return False
        if not (self._running and self._scheduler_allows):
            return False
        for t in self.tasks.values():
            if t.status != "active" or not t.gid or t.id in self._removing:
                continue
            # A task the user paused while this queue-wide result was in
            # flight is still "active" locally until its own pause callback
            # lands. Compensating it here would overwrite that newer, more
            # specific intent and resume a download the user just stopped.
            if self._desired_for(t.id, t.gid) is True:
                # Stepping aside is safe now: the backend state recorded above
                # means that if this pause fails, _on_pause_failed's own
                # convergence check will send the unpause withheld here.
                continue
            self._issue_state(
                t.id, t.gid, False, self._state_sender(t.id, t.gid, False)
            )
        return True

    @property
    def is_running(self) -> bool:
        return self._running

    # ---- internals ----------------------------------------------------

    def _spawn(self, fn, *args, on_done=None, on_fail=None, **kwargs):
        call = _RpcCall(fn, *args, **kwargs)
        if on_done is not None:
            call.signals.done.connect(on_done)
        if on_fail is not None:
            call.signals.failed.connect(on_fail)
        else:
            call.signals.failed.connect(self.error.emit)
        self._inflight.add(call)
        call.signals.finished.connect(lambda c=call: self._inflight.discard(c))
        self._pool.start(call)

    def _active_count(self) -> int:
        return sum(1 for t in self.tasks.values() if t.status == "active")

    def _maybe_start_next(self) -> None:
        if not self._running or not self._scheduler_allows:
            return
        slots = max(0, self.settings.max_concurrent - self._active_count())
        if slots <= 0:
            return
        ready = sorted(
            # `not t.gid` guards against relaunching a task that already has
            # an aria2 gid (e.g. an adopted external download) — doing so
            # would start a duplicate download. A task whose add RPC is
            # still in flight has no gid *yet* and needs the same guard:
            # resuming it before the gid lands would otherwise add it to
            # aria2 twice.
            (
                t for t in self.tasks.values()
                if t.status == "queued"
                and not t.gid
                and t.id not in self._pending_launch
                and t.id not in self._extractor_pause_pending
            ),
            key=lambda t: t.created_at,
        )
        for t in ready[:slots]:
            self._launch(t)

    def _launch_hls(self, t: DownloadTask) -> None:
        from .hls import ffmpeg_command, parse_ffmpeg_progress

        requested = t.filename or "stream.mp4"
        try:
            requested = validate_public_filename(requested)
        except OutputPathError as exc:
            self._fail_task(t.id, f"Could not prepare private output directory: {exc}")
            return
        self._retire_hls_run(t.id)
        try:
            os.makedirs(t.out_dir, exist_ok=True)
            run_work = create_work_directory(t.out_dir)
        except (OSError, OutputPathError) as exc:
            self._fail_task(t.id, f"Could not prepare private output directory: {exc}")
            return

        output_path = run_work.path / requested
        cmd = ffmpeg_command(
            t.url,
            str(output_path),
            cookies=t.cookies,
            referrer=t.referrer,
            user_agent=t.user_agent,
        )

        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        self._hls_procs[t.id] = proc
        self._hls_duration[t.id] = 0.0
        self._hls_stderr[t.id] = ""
        self._hls_work[t.id] = run_work

        def on_read():
            if self._hls_procs.get(t.id) is not proc:
                return
            data = proc.readAllStandardOutput().data().decode("utf-8", errors="replace")
            self._hls_stderr[t.id] = (self._hls_stderr.get(t.id, "") + data)[-12000:]
            # QProcess delivers whatever bytes have arrived, so one ffmpeg
            # progress record can straddle two readyRead events. Splitting each
            # chunk on its own parsed the halves as two malformed records and
            # dropped the update; the incomplete tail is carried to the next
            # chunk instead. Same pattern as the extractor reader below.
            #
            # Both delimiters count. ffmpeg rewrites its status line in place
            # and ends each one with a bare carriage return, so splitting on
            # "\n" alone would hold every progress update in the buffer until
            # some unrelated log line arrived - and the regexes below, which
            # search rather than match, would then report the oldest record in
            # the accumulated blob rather than the newest.
            pending = self._hls_line_buffer.get(t.id, "") + data
            lines = re.split(r"[\r\n]", pending)
            self._hls_line_buffer[t.id] = lines.pop()
            for line in lines:
                info = parse_ffmpeg_progress(line, self._hls_duration.get(t.id, 0.0))
                if "duration_secs" in info:
                    self._hls_duration[t.id] = info["duration_secs"]
                if "time_secs" in info:
                    t.completed_bytes = int(info["time_secs"])
                    t.download_speed = 0
                    t.error = info.get("speed", "")
                    dur = self._hls_duration.get(t.id, 0.0)
                    if dur > 0:
                        t.total_bytes = int(dur)
                    self.task_changed.emit(t.id)

        def on_finished(exit_code, _exit_status):
            if self._hls_procs.get(t.id) is not proc:
                proc.deleteLater()
                self._cleanup_engine_work(run_work)
                return
            proc.deleteLater()
            self._hls_procs.pop(t.id, None)
            self._hls_duration.pop(t.id, None)
            self._hls_line_buffer.pop(t.id, None)
            stderr = self._hls_stderr.pop(t.id, "")
            work = self._hls_work.pop(t.id, None) or run_work
            if t.id not in self.tasks:
                self._cleanup_engine_work(work)
                return
            if exit_code == 0:
                try:
                    published = publish_output(work, output_path, requested)
                except (OSError, OutputPathError) as exc:
                    self._cleanup_engine_work(work)
                    t.status = "error"
                    t.error = f"Could not publish HLS output: {exc}"
                    t.finished_at = time.time()
                else:
                    t.filename = published.name
                    t.status = "completed"
                    t.finished_at = time.time()
                    t.error = None
            else:
                self._cleanup_engine_work(work)
                t.status = "error"
                last_lines = "\n".join(stderr.splitlines()[-5:])
                t.error = last_lines or f"ffmpeg exited with code {exit_code}"
                t.finished_at = time.time()
            self._persist(t)
            self.task_changed.emit(t.id)
            self._maybe_start_next()

        def on_error(err):
            # FailedToStart never emits finished; without this the task
            # would sit "active" forever.
            if err != QProcess.FailedToStart:
                return
            if self._hls_procs.get(t.id) is not proc:
                proc.deleteLater()
                self._cleanup_engine_work(run_work)
                return
            proc.deleteLater()
            self._hls_procs.pop(t.id, None)
            self._hls_duration.pop(t.id, None)
            self._hls_stderr.pop(t.id, None)
            work = self._hls_work.pop(t.id, None) or run_work
            self._cleanup_engine_work(work)
            if t.id not in self.tasks:
                return
            t.status = "error"
            t.error = f"{cmd[0]} failed to start"
            t.finished_at = time.time()
            self._persist(t)
            self.task_changed.emit(t.id)
            self._maybe_start_next()

        proc.readyReadStandardOutput.connect(on_read)
        proc.finished.connect(on_finished)
        proc.errorOccurred.connect(on_error)
        proc.start(cmd[0], cmd[1:])

    def _launch_extractor(self, t: DownloadTask) -> None:
        from .extractor import (
            parse_ytdlp_final_path,
            parse_ytdlp_progress,
            ytdlp_command,
        )

        requested = t.filename or "video.mp4"
        try:
            requested = validate_public_filename(requested)
        except OutputPathError as exc:
            self._fail_task(t.id, f"Could not prepare private output directory: {exc}")
            return
        run_work = None
        if t.id in self._extractor_paused_work:
            candidate = self._extractor_work.get(t.id)
            try:
                work_info = candidate.path.lstat() if candidate else None
                destination_info = candidate.destination.stat() if candidate else None
                requested_destination = os.path.realpath(t.out_dir)
                reusable = bool(
                    candidate
                    and work_info
                    and destination_info
                    and stat.S_ISDIR(work_info.st_mode)
                    and stat.S_ISDIR(destination_info.st_mode)
                    and (work_info.st_dev, work_info.st_ino) == (candidate.device, candidate.inode)
                    and (destination_info.st_dev, destination_info.st_ino)
                    == (candidate.destination_device, candidate.destination_inode)
                    and requested_destination == str(candidate.destination)
                )
            except OSError:
                reusable = False
            self._extractor_paused_work.discard(t.id)
            if reusable:
                run_work = candidate

        if run_work is None:
            self._retire_extractor_run(t.id)
            try:
                os.makedirs(t.out_dir, exist_ok=True)
                run_work = create_work_directory(t.out_dir)
            except (OSError, OutputPathError) as exc:
                self._fail_task(t.id, f"Could not prepare private output directory: {exc}")
                return

        stem = os.path.splitext(requested)[0]
        # Literal % in the directory or stem would be parsed as yt-dlp
        # output-template fields; %% is yt-dlp's escape for a literal %.
        escaped_dir = str(run_work.path).replace("%", "%%")
        escaped_stem = stem.replace("%", "%%")
        output_template = os.path.join(escaped_dir, f"{escaped_stem}.%(ext)s")
        cmd = ytdlp_command(
            t.url,
            output_template,
            cookies=t.cookies,
            referrer=t.referrer,
            user_agent=t.user_agent,
        )

        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        self._extractor_procs[t.id] = proc
        self._extractor_output[t.id] = ""
        self._extractor_work[t.id] = run_work
        self._extractor_final_path[t.id] = ""
        self._extractor_line_buffer[t.id] = ""

        def on_read():
            if self._extractor_procs.get(t.id) is not proc:
                return
            data = proc.readAllStandardOutput().data().decode("utf-8", errors="replace")
            self._extractor_output[t.id] = (self._extractor_output.get(t.id, "") + data)[-12000:]
            pending = self._extractor_line_buffer.get(t.id, "") + data
            lines = pending.splitlines(keepends=True)
            if lines and not lines[-1].endswith(("\n", "\r")):
                self._extractor_line_buffer[t.id] = lines.pop()
            else:
                self._extractor_line_buffer[t.id] = ""
            for raw_line in lines:
                line = raw_line.rstrip("\r\n")
                final_path = parse_ytdlp_final_path(line)
                if final_path:
                    self._extractor_final_path[t.id] = final_path
                info = parse_ytdlp_progress(line)
                if info:
                    t.total_bytes = 1000
                    t.completed_bytes = int(info["percent"] * 10)
                    t.download_speed = int(info.get("speed_bps", 0))
                    self.task_changed.emit(t.id)

        def on_finished(exit_code, _exit_status):
            if self._extractor_procs.get(t.id) is not proc:
                proc.deleteLater()
                return
            on_read()
            proc.deleteLater()
            self._extractor_procs.pop(t.id, None)
            self._extractor_pause_pending.pop(t.id, None)
            self._extractor_paused_work.discard(t.id)
            output = self._extractor_output.pop(t.id, "")
            reported = self._extractor_final_path.pop(t.id, "")
            pending = self._extractor_line_buffer.pop(t.id, "")
            if not reported and pending:
                reported = parse_ytdlp_final_path(pending.rstrip("\r\n")) or ""
            work = self._extractor_work.pop(t.id, None) or run_work
            if t.id not in self.tasks:
                self._cleanup_engine_work(work)
                return
            if exit_code == 0:
                self._diag("extractor.publish", "publish_begin", "INFO",
                           task_id=t.id, engine="yt-dlp",
                           reported=bool(reported))
                try:
                    published = self._publish_extractor_output(work, reported, requested)
                except (OSError, OutputPathError) as exc:
                    self._diag_engine_work_shape(t, work, reported)
                    self._diag_engine_output_rejected(t, reported, work, exc)
                    self._cleanup_engine_work(work, task_id=t.id)
                    t.status = "error"
                    t.error = f"Could not publish extractor output: {exc}"
                    t.finished_at = time.time()
                    self._diag_task_failed(t, "extractor_publish_failed")
                else:
                    t.filename = published.name
                    t.status = "completed"
                    t.finished_at = time.time()
                    t.total_bytes = max(t.total_bytes, 1000)
                    t.completed_bytes = t.total_bytes
                    t.error = None
                    self._diag("extractor.publish", "publish_success", "INFO",
                               task_id=t.id, engine="yt-dlp",
                               ext=os.path.splitext(published.name)[1].lower() or None)
                    self._diag("queue", "task_completed", "INFO", task_id=t.id,
                               backend=t.backend)
            else:
                self._cleanup_engine_work(work)
                t.status = "error"
                last_lines = "\n".join(output.splitlines()[-5:])
                t.error = last_lines or f"yt-dlp exited with code {exit_code}"
                t.finished_at = time.time()
            self._persist(t)
            self.task_changed.emit(t.id)
            self._maybe_start_next()

        def on_error(err):
            # FailedToStart never emits finished; without this the task
            # would sit "active" forever.
            if err != QProcess.FailedToStart:
                return
            if self._extractor_procs.get(t.id) is not proc:
                proc.deleteLater()
                return
            proc.deleteLater()
            self._extractor_procs.pop(t.id, None)
            self._extractor_pause_pending.pop(t.id, None)
            self._extractor_paused_work.discard(t.id)
            self._extractor_output.pop(t.id, None)
            self._extractor_final_path.pop(t.id, None)
            self._extractor_line_buffer.pop(t.id, None)
            work = self._extractor_work.pop(t.id, None) or run_work
            self._cleanup_engine_work(work)
            if t.id not in self.tasks:
                return
            t.status = "error"
            t.error = f"{cmd[0]} failed to start"
            t.finished_at = time.time()
            self._persist(t)
            self.task_changed.emit(t.id)
            self._maybe_start_next()

        proc.readyReadStandardOutput.connect(on_read)
        proc.finished.connect(on_finished)
        proc.errorOccurred.connect(on_error)
        proc.start(cmd[0], cmd[1:])

    def _launch(self, t: DownloadTask) -> None:
        t.status = "active"
        t.error = None
        # Any previously generated debrid link has expired by now; a fresh
        # one is resolved from t.url below.
        t.clear_debrid()
        self._persist(t)
        self.task_changed.emit(t.id)
        self._diag("queue", "task_launched", "INFO", task_id=t.id,
                   backend=t.backend, source_type=t.source_type)
        if t.backend == "ffmpeg":
            self._launch_hls(t)
            return
        if t.backend == "yt-dlp":
            self._launch_extractor(t)
            return
        if t.source_type == SOURCE_TORRENT:
            self._launch_torrent(t)
            return
        # An account-bound provider share link would otherwise "succeed" by
        # saving the provider's forbidden HTML page as the file. Fail the
        # task instead so the row carries a reason the user can act on.
        #
        # A materialised torrent file row is the one exception: its URL is
        # an account-bound provider link *by construction*, issued to this
        # user's own account and unlocked through the API below. The bypass
        # is keyed on source_type, which only Cove's own materialisation
        # sets — a pasted share link still lands in the branch below.
        share_reason = (
            "" if t.source_type == SOURCE_TORRENT_FILE
            else debrid.share_link_reason(t.url, self.settings)
        )
        if share_reason:
            facts = diagnostics.url_facts(t.url)
            self._diag(
                "debrid", "share_link_rejected", "WARNING", task_id=t.id,
                host=facts["host"], route=facts["route"],
                provider=facts["provider"] or "unknown",
                classification=facts["classification"],
                resolver="unsupported_share_link",
                **self._debrid_credential_facts(),
            )
            t.status = "error"
            t.error = share_reason
            t.finished_at = time.time()
            self._diag_task_failed(t, "share_link_rejected")
            self._persist(t)
            self.task_changed.emit(t.id)
            self._maybe_start_next()
            return
        self._pending_launch[t.id] = {}

        def on_done(gid: str, tid: int = t.id) -> None:
            pending = self._pending_launch.pop(tid, {})
            tt = self.tasks.get(tid)
            # Record our own gid immediately (before any remove/pause branch)
            # so the external-download poll never re-adopts it.
            self._seen_gids.add(gid)

            # Deferred remove wins over deferred pause: the user said
            # "drop this", so do that and don't bother pausing first.
            if pending.get("remove"):
                # Best-effort file cleanup: the filename may not be known yet
                # (set by the first status poll, which we skipped), in which
                # case the unlinker is a no-op. Unlink only after aria2 drops
                # the gid so it can't recreate the partial/.aria2 files.
                path = self._task_path(tt) if tt else None
                unlink = self._make_unlinker(path) if pending.get("delete_file") else None
                self._spawn(
                    self.rpc.remove,
                    gid,
                    on_done=(lambda *_: unlink()) if unlink else None,
                    on_fail=(lambda *_: unlink()) if unlink else None,
                )
                # Pop the still-tracked task so the polling loop forgets it.
                self.tasks.pop(tid, None)
                self._maybe_start_next()
                return

            if tt is None:
                # Task vanished some other way; clean up the gid in aria2
                # so we don't leak it and bail.
                self._spawn(self.rpc.remove, gid)
                return

            tt.gid = gid

            if pending.get("pause") or tt.status == "paused":
                # User paused before gid landed (or stop_queue's pause_all
                # already ran and marked it) — local state is already
                # "paused"; tell aria2 to actually pause the download.
                self._spawn(self.rpc.pause, gid)
                self._persist(tt)
                self._maybe_start_next()
                return

            if not self._running or not self._scheduler_allows:
                # stop_queue/scheduler sent pause_all while add_uri was in
                # flight; that couldn't see this gid, so pause it now and
                # mark it auto-paused so start_queue resumes it.
                tt.status = "paused"
                self._auto_paused.add(tid)
                self._spawn(self.rpc.pause, gid)
                self._persist(tt)
                self.task_changed.emit(tid)
                return

            self._persist(tt)
            self.task_changed.emit(tid)

        def on_fail(msg: str, tid: int = t.id) -> None:
            pending = self._pending_launch.pop(tid, {})
            tt = self.tasks.get(tid)

            # If the user already removed the task, the failure is moot —
            # the local row and DB entry are already gone.
            if pending.get("remove"):
                self.tasks.pop(tid, None)
                self._maybe_start_next()
                return

            if not tt:
                return
            tt.status = "error"
            tt.error = msg
            self._persist(tt)
            self.task_changed.emit(tid)
            self._maybe_start_next()

        is_http = t.url.startswith("http://") or t.url.startswith("https://")
        # _probe_and_add is the only off-GUI-thread path that can do network
        # work, so debrid resolution has to route through it too — otherwise
        # turning off intelligent segments would silently disable debrid.
        # A torrent file row always needs the worker: its stored link has to
        # be unlocked before aria2 can be handed anything.
        needs_worker = (
            self.settings.intelligent_segments
            or self._debrid_enabled()
            or t.source_type == SOURCE_TORRENT_FILE
        )
        if is_http and needs_worker:
            self._spawn(
                self._probe_and_add,
                t,
                on_done=on_done,
                on_fail=on_fail,
            )
        else:
            self._spawn(
                self.rpc.add_uri,
                [t.url],
                t.out_dir,
                t.connections,
                t.speed_limit_kbps,
                t.filename,
                on_done=on_done,
                on_fail=on_fail,
            )

    @staticmethod
    def _compute_segments(supports_range: bool, content_length: int, max_conn: int) -> int:
        if not supports_range:
            return 1
        if content_length < 1_048_576:
            return 1
        if content_length < 10_485_760:
            return min(4, max_conn)
        if content_length < 104_857_600:
            return min(8, max_conn)
        return max_conn

    def _debrid_enabled(self) -> bool:
        return debrid.is_enabled(self.settings)

    def _bound_session(self):
        """requests.Session for the debrid/probe calls aria2 never sees.

        Lazily built and cached: the interface setting only takes effect on
        restart (see Settings), so there is no need to rebuild this per
        call. Bound to the same interface aria2 is launched with, so a VPN
        binding covers this traffic too, not only aria2-managed downloads.
        """
        if getattr(self, "_debrid_session", None) is None:
            self._debrid_session = netiface.bound_requests_session(
                str(getattr(self.settings, "torrent_network_interface", "") or "")
            )
        return self._debrid_session

    def _resolve_debrid(self, t: DownloadTask) -> str:
        """Swap the original hoster URL for a provider node URL, if any.

        Returns the URL aria2 should actually fetch. Raises DebridError
        when a configured provider should have handled the link but
        couldn't — that reaches the user through the normal task-failure
        path rather than being papered over with a direct download.

        Runs on a QThreadPool worker; never call it from the GUI thread.
        """
        if t.source_type == SOURCE_TORRENT_FILE:
            if (
                t.debrid_route == debrid.TORBOX
                and t.debrid_item_id
                and t.debrid_file_id
                and debrid.TORBOX_FEATURE_AVAILABLE
            ):
                # TorBox has no account-bound per-file link to unlock: the
                # persisted identity is the item/file ID pair, and every
                # launch asks requestdl for a fresh delivery URL from them.
                # t.url (the synthetic https reference) is left untouched.
                #
                # The availability-gate check keeps this authoritative,
                # matching the pinned TorBox hoster branch below: with the
                # gate off, a row materialised during earlier development
                # testing must not silently keep calling into a hidden,
                # unsupported provider.
                token = getattr(self.settings, "torbox_api_token", "")
                download = debrid.torbox_refresh_torrent_file(
                    t.debrid_item_id, t.debrid_file_id, token,
                    session=self._bound_session(),
                )
                t.resolved_url = download
                t.debrid_provider = debrid.TORBOX
                return download
            # The persisted URL is the provider's stable account-bound link
            # for this file. It is not fetchable as-is and is not a hoster
            # link either, so it bypasses both the share-link guard and the
            # provider-domain exclusion and goes straight to the provider
            # that issued it. t.url is left untouched: the generated node
            # URL expires, this link doesn't.
            result = debrid.unlock_torrent_file(
                t.url, t.debrid_route, self.settings,
                session=self._bound_session(),
            )
            t.resolved_url = result.download
            t.debrid_provider = result.provider
            if result.filesize > 0:
                t.total_bytes = result.filesize
            return result.download
        if (
            t.source_type == SOURCE_PLAIN
            and t.debrid_route == debrid.TORBOX
            and t.debrid_item_id
            and debrid.TORBOX_FEATURE_AVAILABLE
        ):
            # Pinned to a TorBox web-download item from an earlier launch:
            # reuse/refresh it instead of asking resolve() to create
            # another one. A missing remote item soft-recreates once inside
            # torbox_refresh_web_download and comes back with a replacement
            # item_id, which is persisted here exactly like the original.
            #
            # The availability-gate check keeps this authoritative: with the
            # gate off (the shipped T1 default), a row pinned during earlier
            # development testing falls through to the branches below
            # instead of calling into a hidden, unsupported provider.
            result = debrid.torbox_refresh_web_download(
                t.debrid_item_id, t.url, self.settings,
                session=self._bound_session(),
            )
            t.resolved_url = result.download
            t.debrid_provider = result.provider
            if result.item_id:
                t.debrid_item_id = result.item_id
            if not t.filename and result.filename:
                t.filename = result.filename
            if result.filesize > 0:
                t.total_bytes = result.filesize
            return result.download
        if not self._debrid_enabled():
            return t.url
        result = debrid.resolve(t.url, self.settings, session=self._bound_session())
        if result is None:
            return t.url
        # t.url stays the original hoster link: it is the task's identity,
        # it is what gets persisted, and it is what a later relaunch
        # re-resolves. Only the transient fields learn about the node URL.
        t.resolved_url = result.download
        t.debrid_provider = result.provider
        if result.provider == debrid.TORBOX and result.item_id:
            # First-time TorBox hoster success: pin this task to the
            # created item so a retry/restart reuses it instead of creating
            # another one. t.url (the original hoster link) is untouched.
            t.debrid_route = debrid.TORBOX
            t.debrid_item_id = result.item_id
            t.debrid_file_id = ""
        if not t.filename and result.filename:
            # Empty means nobody chose a name: add_url stores None unless the
            # user (or the extension) explicitly supplied one, so a non-empty
            # filename here is always an explicit choice and is left alone.
            t.filename = result.filename
        if result.filesize > 0:
            t.total_bytes = result.filesize
        return result.download

    def _probe_and_add(self, t: DownloadTask) -> str:
        target = self._resolve_debrid(t)
        probed = False
        supports_range = False
        content_length = 0
        # A provider that reported a size has already told us everything the
        # probe would; skip it rather than send the node URL a second time.
        if not (t.resolved_url and t.total_bytes > 0):
            try:
                resp = self._bound_session().head(target, timeout=5, allow_redirects=True)
                if resp.ok:
                    probed = True
                    supports_range = resp.headers.get("Accept-Ranges", "").lower() == "bytes"
                    try:
                        content_length = int(resp.headers.get("Content-Length", 0))
                    except (TypeError, ValueError):
                        content_length = 0
            except Exception:
                pass
        if probed:
            segments = self._compute_segments(supports_range, content_length, t.connections)
        else:
            segments = t.connections
        t.segments = segments
        # Seed the progress denominator so the bar moves before aria2's first
        # status poll. Never overwrite a size the provider or user already set.
        if content_length > 0 and t.total_bytes <= 0:
            t.total_bytes = content_length
        gid = self.rpc.add_uri(
            [target], t.out_dir, segments,
            t.speed_limit_kbps, t.filename,
        )
        # Runs on the probe worker thread; DiagLogger.emit is thread safe.
        # Host class and segment count only - never the URL handed to aria2.
        self._diag("aria2", "add", "INFO", task_id=t.id, gid=gid,
                   segments=segments,
                   target=diagnostics.url_facts(target)["classification"],
                   provider=t.debrid_provider or None)
        return gid

    def _on_unpause_failed(
        self, tid: int, msg: str, gen: int | None = None, gid: str | None = None
    ) -> None:
        """Recover a task whose unpause aria2 refused, by relaunching it.

        This is destructive - it drops the gid and requeues - so it must only
        ever run for the command it belongs to. A failure arriving after the
        user has paused again, or after a retry replaced the gid, would
        otherwise detach and restart a task nobody asked to restart.
        """
        t = self.tasks.get(tid)
        if not t:
            return
        if gen is not None:
            # Ends this command's flight either way, so convergence stops
            # waiting on it even when the recovery below is declined.
            self._note_resolved(tid, gen)
        if gid is not None and t.gid != gid:
            return
        if tid in self._removing:
            return
        if gen is not None and gid is not None and self._desired_for(tid, gid) is not False:
            # The user has asked for something else since - possibly a pause
            # held behind this very command. Relaunching is destructive, so it
            # only ever runs for a resume that is still wanted; releasing the
            # newer wish is the right recovery instead.
            self._converge(tid, gid)
            return
        if t.gid:
            # Clean the dead download out of aria2 before relaunching, or
            # it lingers there and can be re-adopted as an "external" one.
            self._spawn(self.rpc.remove, t.gid, on_fail=lambda *_: None)
        t.gid = None
        t.status = "queued"
        t.error = None
        self._persist(t)
        self.task_changed.emit(tid)
        self._maybe_start_next()

    def _mark_paused(self, tid: int) -> None:
        t = self.tasks.get(tid)
        if not t:
            return
        t.status = "paused"
        self._persist(t)
        self.task_changed.emit(tid)
        self._maybe_start_next()

    def _mark_all_active_paused(self) -> None:
        for t in self.tasks.values():
            # A task whose cancellation is in flight was excluded from the
            # pause_all itself, so recording it as paused would describe a
            # backend state nothing ever asked for. If the removal is refused,
            # _restore_removal asserts its state explicitly instead.
            if t.id in self._removing:
                continue
            if t.status == "active":
                t.status = "paused"
                self._persist(t)
                self.task_changed.emit(t.id)

    def _poll_active(self) -> None:
        active = [t for t in self.tasks.values() if t.status in {"active", "paused"} and t.gid]
        if not active:
            return
        for t in active:
            if t.id in self._removing:
                continue
            self._spawn(
                self.rpc.tell_status,
                t.gid,
                on_done=lambda status, tid=t.id, gid=t.gid: self._on_poll_status(
                    tid, gid, status
                ),
                on_fail=lambda *_: None,
            )

    def _on_poll_status(self, tid: int, gid: str, status: dict) -> None:
        """Apply a poll result only if it still describes the live transfer.

        A retry replaces a task's gid while a tellStatus for the old one may
        still be in flight. The answer is addressed to the task by id, so
        without this check the old transfer's progress, error or completion
        would be written onto its replacement.
        """
        t = self.tasks.get(tid)
        if t is None or t.gid != gid:
            return
        self._apply_status(tid, status)

    def _apply_status(self, tid: int, status: dict) -> None:
        t = self.tasks.get(tid)
        if not t:
            return
        if tid in self._removing:
            # Cancellation is already in flight. Completing, failing or
            # cleaning up here would act on a task that is on its way out.
            return
        is_torrent = t.source_type == SOURCE_TORRENT
        if is_torrent:
            # The metadata gid completing is a transition, not a finished
            # download, so this has to run before anything below can read
            # status == "complete" as success.
            if self._on_torrent_metadata(t, status):
                return
            self._apply_torrent_status(t, status)
        try:
            # aria2 reports totalLength=0 until it has read the response
            # headers, and permanently for servers that send no length.
            # Overwriting unconditionally wiped out a size seeded from the
            # debrid provider or the HEAD probe, leaving the progress bar
            # stuck at 0%.
            total = int(status.get("totalLength", 0))
            if total > 0:
                t.total_bytes = total
            t.completed_bytes = int(status.get("completedLength", 0))
            t.download_speed = int(status.get("downloadSpeed", 0))
            t.last_status_at = time.time()
        except (TypeError, ValueError):
            pass
        t.bitfield = status.get("bitfield", "")
        t.num_pieces = int(status.get("numPieces", 0) or 0)
        files = status.get("files") or []
        if files and not t.filename and not is_torrent:
            path = files[0].get("path") or ""
            if path:
                from pathlib import Path
                t.filename = Path(path).name
        a2_status = status.get("status")
        if a2_status == "complete":
            if t.status == "completed":
                return
            t.status = "completed"
            t.finished_at = time.time()
            t.clear_debrid()
            self._diag("aria2", "final_success", "INFO", task_id=tid, gid=t.gid)
            self._diag("queue", "task_completed", "INFO", task_id=tid,
                       backend=t.backend)
            self._persist(t)
            self.task_changed.emit(tid)
            self._maybe_start_next()
        elif a2_status == "error":
            t.status = "error"
            if is_torrent:
                # Never aria2's own text for a torrent: see TORRENT_ARIA2_FAILED.
                t.error = _torrent_error_text(status.get("errorCode"))
            else:
                t.error = status.get("errorMessage") or f"aria2 error {status.get('errorCode')}"
            t.finished_at = time.time()
            t.clear_debrid()
            # aria2's own error code and message: no URL, no command line.
            self._diag("aria2", "final_error", "ERROR", task_id=tid, gid=t.gid,
                       code=status.get("errorCode"),
                       message=status.get("errorMessage"))
            self._diag_task_failed(t, "aria2_error")
            self._persist(t)
            self.task_changed.emit(tid)
            self._maybe_start_next()
        else:
            # Progress-only update. Don't let poll responses overwrite local
            # pause/active intent — Cove drives those transitions via explicit
            # RPC calls and waits for the on_done callback.
            self.task_changed.emit(tid)

    def _task_path(self, t: DownloadTask):
        if not t.filename:
            return None
        from pathlib import Path
        return Path(t.out_dir) / t.filename
