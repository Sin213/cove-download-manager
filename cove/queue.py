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
import time
from dataclasses import dataclass, field
from typing import Optional

from PySide6.QtCore import QObject, QProcess, QRunnable, QThreadPool, QTimer, Signal

from . import db, debrid
from .aria2 import Aria2Error, Aria2RPC
from .config import MAX_CONNECTIONS_PER_SERVER, Settings
from .debrid import DebridError

URL_RE = re.compile(r"https?://\S+|ftp://\S+|magnet:\?\S+", re.IGNORECASE)


def _clean_header(value) -> str:
    """Strip CR/LF so a persisted or drop-file value can't inject extra
    headers into the ffmpeg/yt-dlp request it is forwarded to."""
    if not isinstance(value, str):
        return ""
    return value.replace("\r", "").replace("\n", "").strip()


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
        status="queued",
        total_bytes=row["total_bytes"],
        completed_bytes=row["completed_bytes"],
        created_at=row["created_at"],
        segments=row["segments"],
        backend=_row_get(row, "backend", "aria2"),
        cookies=_clean_header(_row_get(row, "cookies", "")),
        referrer=_clean_header(_row_get(row, "referrer", "")),
        user_agent=_clean_header(_row_get(row, "user_agent", "")),
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
    # Transient debrid state, deliberately absent from the 'downloads'
    # table and from _task_from_persisted_row. `resolved_url` is a
    # short-lived secret on the provider's delivery node: it is handed to
    # aria2 and nowhere else, and is re-derived from `url` on every
    # relaunch because the previous one expires.
    resolved_url: str = ""
    debrid_provider: str = ""

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
        except (Aria2Error, DebridError) as e:
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
        # Every gid Cove has ever launched or adopted this session. External
        # downloads (browser extension) are discovered by polling aria2; this
        # guards against re-adopting one the user already cleared from the
        # list, and against adopting Cove's own downloads as "external".
        self._seen_gids: set[str] = set()
        self._hls_procs: dict[int, QProcess] = {}
        self._hls_duration: dict[int, float] = {}
        self._hls_stderr: dict[int, str] = {}
        self._extractor_procs: dict[int, QProcess] = {}
        self._extractor_output: dict[int, str] = {}
        self._poll = QTimer(self)
        self._poll.setInterval(500)
        self._poll.timeout.connect(self._poll_active)
        self._poll.start()
        self._ext_poll = QTimer(self)
        self._ext_poll.setInterval(2000)
        self._ext_poll.timeout.connect(self._check_external)
        self._ext_poll.start()
        self._drop_poll = QTimer(self)
        self._drop_poll.setInterval(1000)
        self._drop_poll.timeout.connect(self._check_drop_dir)
        self._drop_poll.start()
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

    def _check_drop_dir(self) -> None:
        """Pick up video downloads queued by the native messaging process."""
        from .config import DATA_DIR
        drop_dir = DATA_DIR / "drop"
        if not drop_dir.is_dir():
            return
        import json as _json
        try:
            entries = sorted(drop_dir.iterdir())
        except OSError:
            return
        for f in entries:
            if not f.name.endswith(".json"):
                continue
            try:
                data = _json.loads(f.read_text())
                url = data.get("url", "")
                if url:
                    tid = self.add_url(
                        url,
                        out_dir=data.get("dir") or None,
                        filename=data.get("filename"),
                        cookies=data.get("cookies") or "",
                        referrer=data.get("referrer") or "",
                        user_agent=data.get("userAgent") or "",
                    )
                    if tid is None:
                        # The queue rejected the URL; sideline the file
                        # instead of silently discarding the request.
                        raise ValueError("queue rejected drop request")
                f.unlink(missing_ok=True)
            except Exception:
                # Sideline the file instead of retrying it every second or
                # deleting the URL outright; .bad files are skipped above.
                try:
                    f.rename(f.with_name(f.name + ".bad"))
                except OSError:
                    f.unlink(missing_ok=True)

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
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE downloads
                SET filename=?, status=?, gid=?, total_bytes=?, completed_bytes=?,
                    error=?, finished_at=?, segments=?, out_dir=?
                WHERE id=?
                """,
                (
                    t.filename,
                    t.status,
                    t.gid,
                    t.total_bytes,
                    t.completed_bytes,
                    t.error,
                    t.finished_at,
                    t.segments,
                    t.out_dir,
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
    ) -> Optional[int]:
        url = url.strip()
        cookies = _clean_header(cookies)
        referrer = _clean_header(referrer)
        user_agent = _clean_header(user_agent)
        if not URL_RE.match(url):
            return None
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
                     cookies, referrer, user_agent)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
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
        )
        self.tasks[tid] = t
        self.task_added.emit(tid)
        self._maybe_start_next()
        return tid

    def add_urls(
        self, urls: list[str], out_dir: str | None = None
    ) -> list[int]:
        return [
            tid
            for u in urls
            if (tid := self.add_url(u, out_dir)) is not None
        ]

    def pause(self, tid: int) -> None:
        t = self.tasks.get(tid)
        if not t or t.status not in {"active", "queued"}:
            return
        proc = None
        if tid in self._hls_procs:
            proc = self._hls_procs.pop(tid)
            self._hls_duration.pop(tid, None)
            self._hls_stderr.pop(tid, None)
        elif tid in self._extractor_procs:
            proc = self._extractor_procs.pop(tid)
            self._extractor_output.pop(tid, None)
        if proc is not None:
            # ffmpeg/yt-dlp can't be suspended: pause kills the process and
            # resume relaunches from scratch (yt-dlp continues from .part
            # files). Popping the proc first makes on_finished treat this
            # run as superseded instead of marking the task errored.
            if proc.state() != QProcess.NotRunning:
                proc.terminate()
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
            self._spawn(self.rpc.pause, t.gid, on_done=lambda _: self._mark_paused(tid))
        else:
            self._mark_paused(tid)

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
            self._spawn(
                self.rpc.unpause,
                t.gid,
                on_fail=lambda msg, tid=tid: self._on_unpause_failed(tid, msg),
            )
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

    def remove(self, tid: int, delete_file: bool = False) -> None:
        t = self.tasks.get(tid)
        if not t:
            return

        # Special case: add_uri RPC is in flight. We can't ask aria2 to
        # remove a gid we don't have yet, so keep the task alive in
        # self.tasks but hide it from the UI/DB. on_done will dispatch the
        # actual remove once it learns the gid. Only aria2 tasks qualify:
        # active ffmpeg/yt-dlp tasks never get a gid and must instead go
        # through the normal path that terminates their process.
        if t.status == "active" and not t.gid and t.backend == "aria2":
            self._pending_launch.setdefault(tid, {}).update(
                {"remove": True, "delete_file": bool(delete_file)}
            )
            with db.connect() as conn:
                conn.execute("DELETE FROM downloads WHERE id=?", (tid,))
            self.task_removed.emit(tid)
            return

        # Normal path: drop from local state, ask aria2 to forget the gid
        # if it had one, optionally unlink the file on disk.
        if tid in self._hls_procs:
            proc = self._hls_procs.pop(tid)
            self._hls_duration.pop(tid, None)
            self._hls_stderr.pop(tid, None)
            if proc.state() != QProcess.NotRunning:
                proc.terminate()
        if tid in self._extractor_procs:
            proc = self._extractor_procs.pop(tid)
            self._extractor_output.pop(tid, None)
            if proc.state() != QProcess.NotRunning:
                proc.terminate()
        self.tasks.pop(tid, None)
        gid = t.gid
        path = self._task_path(t)
        with db.connect() as conn:
            conn.execute("DELETE FROM downloads WHERE id=?", (tid,))

        unlink = self._make_unlinker(path) if delete_file else None
        if gid:
            def _after_remove(*_args):
                if unlink:
                    unlink()
                self._maybe_start_next()

            def _after_remove_fail(*args):
                self.error.emit(*args)
                _after_remove()

            self._spawn(
                self.rpc.remove,
                gid,
                on_done=_after_remove,
                on_fail=_after_remove_fail,
            )
        else:
            if unlink:
                unlink()
            self._maybe_start_next()

        self.task_removed.emit(tid)

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
        for tid in [t.id for t in self.tasks.values() if t.status == "completed"]:
            self.remove(tid, delete_file=delete_files)

    def start_queue(self) -> None:
        if self._running:
            return
        self._running = True
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
        for proc in list(self._hls_procs.values()):
            if proc.state() != QProcess.NotRunning:
                proc.terminate()
        self._hls_procs.clear()
        self._hls_duration.clear()
        self._hls_stderr.clear()
        for proc in list(self._extractor_procs.values()):
            if proc.state() != QProcess.NotRunning:
                proc.terminate()
        self._extractor_procs.clear()
        self._extractor_output.clear()

        def _on_fail(msg: str) -> None:
            self.error.emit(msg)
            self._mark_all_active_paused()

        self._spawn(
            self.rpc.pause_all,
            on_done=lambda _: self._mark_all_active_paused(),
            on_fail=_on_fail,
        )

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
            # would start a duplicate download.
            (t for t in self.tasks.values() if t.status == "queued" and not t.gid),
            key=lambda t: t.created_at,
        )
        for t in ready[:slots]:
            self._launch(t)

    def _launch_hls(self, t: DownloadTask) -> None:
        from .hls import ffmpeg_command, parse_ffmpeg_progress
        # ffmpeg does not create missing directories (aria2 and yt-dlp do).
        try:
            os.makedirs(t.out_dir, exist_ok=True)
        except OSError as e:
            t.status = "error"
            t.error = f"Could not create download directory: {e}"
            t.finished_at = time.time()
            self._persist(t)
            self.task_changed.emit(t.id)
            self._maybe_start_next()
            return
        output_path = os.path.join(t.out_dir, t.filename or "stream.mp4")
        cmd = ffmpeg_command(
            t.url,
            output_path,
            cookies=t.cookies,
            referrer=t.referrer,
            user_agent=t.user_agent,
        )

        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        self._hls_procs[t.id] = proc
        self._hls_duration[t.id] = 0.0
        self._hls_stderr[t.id] = ""

        def on_read():
            data = proc.readAllStandardOutput().data().decode("utf-8", errors="replace")
            self._hls_stderr[t.id] = (self._hls_stderr.get(t.id, "") + data)[-12000:]
            for line in data.splitlines():
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
            proc.deleteLater()
            if self._hls_procs.get(t.id) is not proc:
                # Superseded: paused/stopped/removed terminated this run and
                # already cleaned up; its exit must not touch the task.
                return
            self._hls_procs.pop(t.id, None)
            self._hls_duration.pop(t.id, None)
            stderr = self._hls_stderr.pop(t.id, "")
            if t.id not in self.tasks:
                return
            if exit_code == 0:
                t.status = "completed"
                t.finished_at = time.time()
                t.error = None
            else:
                t.status = "error"
                last_lines = "\n".join(stderr.splitlines()[-5:])
                t.error = last_lines or f"ffmpeg exited with code {exit_code}"
            self._persist(t)
            self.task_changed.emit(t.id)
            self._maybe_start_next()

        def on_error(err):
            # FailedToStart never emits finished; without this the task
            # would sit "active" forever.
            if err != QProcess.FailedToStart:
                return
            proc.deleteLater()
            if self._hls_procs.get(t.id) is not proc:
                return
            self._hls_procs.pop(t.id, None)
            self._hls_duration.pop(t.id, None)
            self._hls_stderr.pop(t.id, None)
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
        from .extractor import parse_ytdlp_progress, ytdlp_command
        stem = os.path.splitext(t.filename or "video.mp4")[0]
        # Literal % in the directory or stem would be parsed as yt-dlp
        # output-template fields; %% is yt-dlp's escape for a literal %.
        escaped_dir = t.out_dir.replace("%", "%%")
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

        def on_read():
            data = proc.readAllStandardOutput().data().decode("utf-8", errors="replace")
            self._extractor_output[t.id] = (self._extractor_output.get(t.id, "") + data)[-12000:]
            for line in data.splitlines():
                info = parse_ytdlp_progress(line)
                if info:
                    t.total_bytes = 1000
                    t.completed_bytes = int(info["percent"] * 10)
                    t.download_speed = int(info.get("speed_bps", 0))
                    self.task_changed.emit(t.id)

        def on_finished(exit_code, _exit_status):
            proc.deleteLater()
            if self._extractor_procs.get(t.id) is not proc:
                # Superseded: paused/stopped/removed terminated this run and
                # already cleaned up; its exit must not touch the task.
                return
            self._extractor_procs.pop(t.id, None)
            output = self._extractor_output.pop(t.id, "")
            if t.id not in self.tasks:
                return
            if exit_code == 0:
                t.status = "completed"
                t.finished_at = time.time()
                t.total_bytes = max(t.total_bytes, 1000)
                t.completed_bytes = t.total_bytes
                t.error = None
            else:
                t.status = "error"
                last_lines = "\n".join(output.splitlines()[-5:])
                t.error = last_lines or f"yt-dlp exited with code {exit_code}"
            self._persist(t)
            self.task_changed.emit(t.id)
            self._maybe_start_next()

        def on_error(err):
            # FailedToStart never emits finished; without this the task
            # would sit "active" forever.
            if err != QProcess.FailedToStart:
                return
            proc.deleteLater()
            if self._extractor_procs.get(t.id) is not proc:
                return
            self._extractor_procs.pop(t.id, None)
            self._extractor_output.pop(t.id, None)
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
        if t.backend == "ffmpeg":
            self._launch_hls(t)
            return
        if t.backend == "yt-dlp":
            self._launch_extractor(t)
            return
        # An account-bound provider share link would otherwise "succeed" by
        # saving the provider's forbidden HTML page as the file. Fail the
        # task instead so the row carries a reason the user can act on.
        share_reason = debrid.share_link_reason(t.url)
        if share_reason:
            t.status = "error"
            t.error = share_reason
            t.finished_at = time.time()
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
        needs_worker = self.settings.intelligent_segments or self._debrid_enabled()
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

    def _resolve_debrid(self, t: DownloadTask) -> str:
        """Swap the original hoster URL for a provider node URL, if any.

        Returns the URL aria2 should actually fetch. Raises DebridError
        when a configured provider should have handled the link but
        couldn't — that reaches the user through the normal task-failure
        path rather than being papered over with a direct download.

        Runs on a QThreadPool worker; never call it from the GUI thread.
        """
        if not self._debrid_enabled():
            return t.url
        result = debrid.resolve(t.url, self.settings)
        if result is None:
            return t.url
        # t.url stays the original hoster link: it is the task's identity,
        # it is what gets persisted, and it is what a later relaunch
        # re-resolves. Only the transient fields learn about the node URL.
        t.resolved_url = result.download
        t.debrid_provider = result.provider
        if not t.filename and result.filename:
            # Empty means nobody chose a name: add_url stores None unless the
            # user (or the extension) explicitly supplied one, so a non-empty
            # filename here is always an explicit choice and is left alone.
            t.filename = result.filename
        if result.filesize > 0:
            t.total_bytes = result.filesize
        return result.download

    def _probe_and_add(self, t: DownloadTask) -> str:
        import requests as _requests
        target = self._resolve_debrid(t)
        probed = False
        supports_range = False
        content_length = 0
        # A provider that reported a size has already told us everything the
        # probe would; skip it rather than send the node URL a second time.
        if not (t.resolved_url and t.total_bytes > 0):
            try:
                resp = _requests.head(target, timeout=5, allow_redirects=True)
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
        return self.rpc.add_uri(
            [target], t.out_dir, segments,
            t.speed_limit_kbps, t.filename,
        )

    def _on_unpause_failed(self, tid: int, msg: str) -> None:
        t = self.tasks.get(tid)
        if not t:
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
            if t.status == "active":
                t.status = "paused"
                self._persist(t)
                self.task_changed.emit(t.id)

    def _poll_active(self) -> None:
        active = [t for t in self.tasks.values() if t.status in {"active", "paused"} and t.gid]
        if not active:
            return
        for t in active:
            self._spawn(
                self.rpc.tell_status,
                t.gid,
                on_done=lambda status, tid=t.id: self._apply_status(tid, status),
                on_fail=lambda *_: None,
            )

    def _apply_status(self, tid: int, status: dict) -> None:
        t = self.tasks.get(tid)
        if not t:
            return
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
        if files and not t.filename:
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
            self._persist(t)
            self.task_changed.emit(tid)
            self._maybe_start_next()
        elif a2_status == "error":
            t.status = "error"
            t.error = status.get("errorMessage") or f"aria2 error {status.get('errorCode')}"
            t.finished_at = time.time()
            t.clear_debrid()
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
