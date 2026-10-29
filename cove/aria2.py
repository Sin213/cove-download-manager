"""aria2 daemon manager + JSON-RPC client.

Spawns a local `aria2c --enable-rpc` instance and exposes the methods Cove
needs (addUri, pause, unpause, remove, tellStatus, changeOption,
changeGlobalOption). Network calls run on a background thread; the UI
should never block on these.
"""
from __future__ import annotations

import base64
import logging
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Iterable, NamedTuple
from requests.adapters import HTTPAdapter
from urllib3 import HTTPConnectionPool
from urllib3.connection import HTTPConnection
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError

import requests

from .config import (
    ARIA2_LOG,
    ARIA2_SESSION,
    DATA_DIR,
    MAX_CONNECTIONS_PER_SERVER,
    Settings,
    prepare_private_directory,
)
from .netiface import interface_exists


class Aria2Error(RuntimeError):
    pass


_PROC_ROOT = Path("/proc")
_GATE_ACCEPT_WAIT = 2.0
_RECLAIM_DEADLINE = 12.0


def _peer_identity(sock, port, cancel, deadline):
    """Bind this connected socket to its visible, same-UID accepted peer."""
    def check():
        if cancel.cancelled or time.monotonic() >= deadline: raise OSError("Reclaim interrupted")
    try:
        if not sys.platform.startswith("linux"): raise OSError("Peer evidence unavailable")
        local, remote = f"0100007F:{port:04X}", f"0100007F:{sock.getsockname()[1]:04X}"
        until = min(deadline, time.monotonic() + _GATE_ACCEPT_WAIT)
        while True:
            check()
            rows = [fields for line in (_PROC_ROOT / "net/tcp").read_text().splitlines()
                    if len(fields := line.split()) > 9 and fields[1:4] == [local, remote, "01"]]
            if len(rows) > 1: raise OSError("Ambiguous peer socket")
            if rows and rows[0][9] != "0": break
            if time.monotonic() >= until: raise OSError("Peer socket unavailable")
            cancel.wait(min(.005, max(0, until - time.monotonic())))
        # Census covers only holders visible to this UID; it is not proof of exclusivity.
        row, holders = rows[0], []
        if int(row[7]) != os.geteuid(): raise OSError("Foreign socket owner")
        target = f"socket:[{int(row[9])}]"
        for process in _PROC_ROOT.iterdir():
            check()
            if not process.name.isdecimal(): continue
            try:
                if process.stat().st_uid != os.geteuid(): continue
                links = []
                for fd in (process / "fd").iterdir():
                    check()
                    try:
                        if os.readlink(fd) == target: links.append(fd)
                    except OSError as error:
                        if error.errno not in (2, 13): raise
                if links: holders.append((process, links[0]))
            except OSError as error:
                if error.errno not in (2, 13): raise
        if len(holders) != 1: raise OSError("Ambiguous socket holders")
        process, fd = holders[0]
        def starttime(): return (process / "stat").read_text().rsplit(")", 1)[1].split()[19]
        start = starttime()
        exe = os.path.basename(os.readlink(process / "exe")).removesuffix(" (deleted)")
        args = (process / "cmdline").read_bytes().split(b"\0")
        if ("aria2" not in exe.lower() or b"--enable-rpc" not in args
                or f"--rpc-listen-port={port}".encode() not in args
                or not any(arg.startswith(b"--conf-path=") and
                    os.path.realpath(os.path.dirname(os.fsdecode(arg.split(b"=", 1)[1]))) == os.path.realpath(DATA_DIR)
                    for arg in args)): raise OSError("Peer provenance unavailable")
        check()
        if starttime() != start or os.readlink(fd) != target: raise OSError("Peer changed")
        return int(process.name), start
    except Exception:
        raise OSError("Cannot establish reclaim peer") from None


class _PeerGate:
    def __init__(self): self.pin, self.denied = None, False
    def check(self, sock, port, cancel, deadline):
        try: identity = _peer_identity(sock, port, cancel, deadline)
        except OSError:
            if not cancel.cancelled and time.monotonic() < deadline: self.denied = True
            raise
        if self.pin is None: self.pin = identity
        elif self.pin != identity:
            self.denied = True
            raise OSError("Reclaim peer changed")


@contextmanager
def _deadline_guard(cancel, deadline):
    if deadline == float("inf"):
        yield; return
    timer = threading.Timer(max(0, deadline - time.monotonic()), cancel.cancel)
    timer.daemon = False; timer.start()
    try: yield
    finally: timer.cancel(); timer.join()


def _bounded_reclaim(fn):
    @wraps(fn)
    def run(request, cancel, *args, **kwargs):
        if time.monotonic() >= request.deadline: return "deadline"
        if cancel.cancelled: return "cancelled"
        with _deadline_guard(cancel, request.deadline):
            result = fn(request, cancel, *args, **kwargs)
            if time.monotonic() >= request.deadline: return "deadline"
            return "cancelled" if cancel.cancelled else result
    return run


class _AuthCancel:
    def __init__(self):
        self.lock, self.sockets, self.event = threading.RLock(), set(), threading.Event()
    @property
    def cancelled(self):
        return self.event.is_set()
    def wait(self, timeout):
        return self.event.wait(timeout)
    def register(self, sock):
        with self.lock:
            if self.cancelled: raise OSError("Authentication cancelled")
            self.sockets.add(sock)
    def cancel(self):
        with self.lock:
            self.event.set()
            for sock in tuple(self.sockets):
                try: sock.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                if sock.fileno() == -1: self.sockets.discard(sock)
    def session(self, verify=None):
        gate = self
        class Sock(socket.socket):
            def close(self):
                with gate.lock:
                    super().close()
                    if self.fileno() == -1: gate.sockets.discard(self)
        class Connection(HTTPConnection):
            def _new_conn(self):
                sock = Sock(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    gate.register(sock)
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    sock.settimeout(self.timeout)
                    if gate.cancelled: raise OSError("Authentication cancelled")
                    sock.connect(("127.0.0.1", self.port))
                    if gate.cancelled: raise OSError("Authentication cancelled")
                    if verify is not None: verify(sock)
                except socket.timeout as error:
                    sock.close(); raise ConnectTimeoutError(self, "connect timeout") from error
                except OSError as error:
                    sock.close(); raise NewConnectionError(self, "connect failed") from error
                return sock
        class Pool(HTTPConnectionPool):
            ConnectionCls = Connection
        class Adapter(HTTPAdapter):
            def init_poolmanager(self, *args, **kwargs):
                super().init_poolmanager(*args, **kwargs)
                self.poolmanager.pool_classes_by_scheme = {"http": Pool}
        session = requests.Session(); session.trust_env = False
        session.mount("http://", Adapter(max_retries=0))
        return session


class _LaunchTicket(NamedTuple):
    proc: object
    secret: str
    port: int


class _AuthInput(NamedTuple):
    settings: object
    secret: str
    @property
    def _secret(self): return self.secret
    def _stale_daemon_error(self): return Aria2Daemon._stale_daemon_error(self)


class _RpcSettings(NamedTuple):
    rpc_port: int
    rpc_secret: None = None


_SYNC_RECLAIM = object()
_PORT_RELEASE_TIMEOUT = 5.0


class _ReclaimRequest(NamedTuple):
    port: int
    secret: str | None
    deadline: float = float("inf")
    gate: object = None
    def __repr__(self): return f"_ReclaimRequest(port={self.port})"


class _Preflight(NamedTuple):
    port: int
    kind: str


def _probe_port(port):
    with socket.socket() as probe:
        probe.settimeout(0.2)
        try:
            probe.connect(("127.0.0.1", int(port)))
        except ConnectionRefusedError:
            return "refused"
        except OSError:
            return "free"
    return "busy"


STOP_TERM_TIMEOUT = 5
STOP_KILL_TIMEOUT = 5
_owned_pending = None  # Strong, single-slot ownership of incomplete construction.
_lifecycle_lock = threading.RLock()  # Serialize probes and the unreaped-leader proof.
_UNRESOLVED = "Aria2 child cleanup is unresolved."


def _release_child(proc, graceful=False):
    """Only positive poll/wait evidence releases ownership; every wait is bounded."""
    def exited():
        with _lifecycle_lock:
            returncode = proc.poll()
        if returncode is not None:
            if (sys.platform != "win32" and returncode == 0
                    and signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN):
                logging.getLogger("cove").info("aria2_child_status-unavailable")
            return True
        return False
    try:
        proc._cove_phase = "poll"
        if exited():
            return True
        if graceful:
            try:
                with _lifecycle_lock:
                    proc._cove_phase = "term"
                    proc.terminate()
                if proc.wait(timeout=STOP_TERM_TIMEOUT) is not None:
                    return True
            except Exception:
                pass
        with _lifecycle_lock:
            if exited():
                return True
            group = (sys.platform != "win32" and getattr(proc, "_cove_new_session", False) is True
                    and os.getpgid(proc.pid) == proc.pid and proc.pid != os.getpgrp()
                    and getattr(proc, "returncode", None) is None)
            proc._cove_phase = "kill"
            if group:
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        if proc.wait(timeout=STOP_KILL_TIMEOUT) is not None:
            return True
        return exited()
    except Exception:
        # A failed signal or wait is not evidence; poll may still prove exit.
        try:
            return exited()
        except Exception:
            return False


def _unresolved_child(proc):
    if not getattr(proc, "_cove_unresolved_logged", False):
        try:
            logging.getLogger("cove").error("aria2_child_unresolved pid=%s platform=%s phase=%s",
                                          proc.pid, sys.platform, getattr(proc, "_cove_phase", "poll"))
            proc._cove_unresolved_logged = True
        except BaseException:
            pass  # Retain ownership and leave the diagnostic retryable.


class _OwnedPopen(subprocess.Popen):
    def __new__(cls, *args, **kwargs):
        if _owned_pending is not None:
            raise Aria2Error(_UNRESOLVED)
        return super().__new__(cls)

    def __init__(self, *args, **kwargs):
        global _owned_pending
        self._cove_new_session = kwargs.get("start_new_session", False) is True
        try:
            super().__init__(*args, **kwargs)
        except BaseException:
            if (getattr(self, "returncode", None) is None
                    and ((sys.platform != "win32" and getattr(self, "pid", None) is not None)
                         or (sys.platform == "win32" and getattr(self, "_handle", None) is not None))):
                _owned_pending = self
                try:
                    released = _release_child(self)
                except BaseException:
                    released = self.returncode is not None
                if released:
                    _owned_pending = None
                else:
                    try:
                        _unresolved_child(self)
                    except BaseException:
                        pass  # Diagnostics must never replace the construction exception.
            raise


def read_rpc_record() -> tuple[str, str | None]:
    """Read only a bounded regular auth record; absence alone permits upgrade."""
    path = DATA_DIR / "aria2.rpc.auth"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "ABSENT", None
    except OSError:
        return "PRESENT-INVALID", None
    try:
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024:
            return "PRESENT-INVALID", None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as record:
            opened = os.fstat(record.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                return "PRESENT-INVALID", None
            match = re.fullmatch(rb"rpc-secret=([A-Za-z0-9_-]{16,128})\n", record.read(1025))
        if match:
            return "VALID", match[1].decode("ascii")
    except (OSError, ValueError):
        pass
    return "PRESENT-INVALID", None


def validate_gid(gid: str) -> None:
    """Reject invalid caller identities before any RPC can admit a download."""
    if not isinstance(gid, str) or re.fullmatch(r"[0-9a-f]{16}", gid) is None or gid == "0" * 16:
        raise Aria2Error("Invalid download GID: expected 16 lowercase hexadecimal characters, not all zero.")


def generate_gid() -> str:
    """Generate a fresh aria2 identity, excluding its reserved all-zero GID."""
    while True:
        gid = secrets.token_hex(8)
        if gid != "0" * 16:
            return gid


class Aria2RpcError(Aria2Error):
    """An error aria2 itself returned, as opposed to a failure reaching it.

    The distinction is the point. A transport failure, a timeout or a
    malformed body all mean "no answer", and a caller must not read anything
    into them; only a JSON-RPC error object is aria2 stating a fact about the
    request. An unreachable daemon produces no error object at all, so cleanup
    can tell "this download is gone" from "aria2 did not answer".
    """

    # aria2 1.37.0 has more than one phrasing for an unknown gid, and which one
    # it uses depends on the method and on whether a download result is still
    # held. Both observed live: removal methods answer "GID <gid> is not
    # found", while tellStatus answers "No such download for GID#<gid>" once
    # the result has been purged.
    _GID_ABSENT = ("is not found", "no such download")

    def __init__(self, method: str, code, message: str):
        super().__init__(f"RPC {method} failed: {message}")
        self.code = code
        self.rpc_message = message

    def gid_not_found(self) -> bool:
        """True when aria2 said it has no such download.

        Matching only the removal-method phrasing made a purged parent look
        like aria2 declining to answer, which refused the removal and left the
        payload child running. Found by the live harness, not by reasoning
        about the text.
        """
        if hasattr(self, "_gid_absent"):
            return self._gid_absent
        message = str(self.rpc_message).lower()
        return any(phrase in message for phrase in self._GID_ABSENT)


class Aria2InterfaceError(Aria2Error):
    """The configured network interface is not present on this machine.

    Distinct from a fatal aria2 failure on purpose: downloads must stay
    blocked, but the user has to keep access to Settings to clear or change
    the binding, otherwise the only fix is hand-editing the config file.
    """


# aria2 defaults max-concurrent-downloads to 5. Downloads added via the
# browser extension go straight to aria2 (bypassing Cove's own queue), so
# that default silently caps extension downloads at 5 and also throttles the
# GUI queue below its configurable "up to 16 parallel". Lift it well above
# both; downloads beyond this still queue inside aria2 but are uncommon.
MAX_CONCURRENT_DOWNLOADS = 20

# The aria2 feature name for BitTorrent support, as reported by
# aria2.getVersion's "enabledFeatures" list.
_BITTORRENT_FEATURE = "bittorrent"


def bittorrent_enabled(version: object) -> bool:
    """True when this aria2 build reports BitTorrent in enabledFeatures.

    Anything unexpected (missing key, wrong type, non-string entries) is
    treated as "no": Cove must not start a local torrent on a guess.
    """
    if not isinstance(version, dict):
        return False
    features = version.get("enabledFeatures")
    if not isinstance(features, list):
        return False
    return any(
        isinstance(f, str) and f.strip().lower() == _BITTORRENT_FEATURE
        for f in features
    )


def _bundled_aria2c() -> str | None:
    """Look for aria2c shipped alongside the running bundle.

    On Windows the installer ships aria2c.exe inside the PyInstaller
    bundle so the app doesn't need a system aria2. Linux AppImage / .deb
    builds use the system one (declared as a Depends or installed via
    the user's package manager); they fall through to PATH.
    """
    exe_name = "aria2c.exe" if sys.platform == "win32" else "aria2c"
    search: list[Path] = []

    # PyInstaller one-file: assets are extracted to _MEIPASS at runtime.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        search.append(Path(meipass) / exe_name)

    # PyInstaller one-dir / "frozen" launcher: next to the exe (and under
    # _internal/, where modern PyInstaller stows binaries).
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        search.extend([exe_dir / exe_name, exe_dir / "_internal" / exe_name])

    # AppImage layout (we don't currently ship aria2 in the AppImage, but
    # leave the path open in case we do later).
    appdir = os.environ.get("APPDIR")
    if appdir:
        search.append(Path(appdir) / "usr" / "bin" / exe_name)

    for p in search:
        if p.is_file():
            return str(p)
    return None


def _file_allocation() -> str:
    """falloc reserves space without writing it on ext4/xfs/APFS. On Windows
    it needs SeManageVolumePrivilege (admin) and otherwise falls back to
    zero-filling the whole file, so Windows uses "none" and the queue
    pre-creates the output as a sparse file instead."""
    return "none" if sys.platform == "win32" else "falloc"


def _resolve_aria2c() -> str | None:
    return _bundled_aria2c() or shutil.which("aria2c")


def _hidden_console_kwargs() -> dict:
    """subprocess.Popen kwargs to spawn a child without a console window
    (Windows) and detached from our process group (POSIX)."""
    if sys.platform == "win32":
        # CREATE_NO_WINDOW = 0x08000000 — suppresses the console that
        # would otherwise pop up when a windowed PyInstaller launcher
        # spawns a console-subsystem child like aria2c.exe.
        flags = subprocess.CREATE_NO_WINDOW
        # Also detach so closing the parent doesn't drag the child along
        # before our cleanup hook runs.
        flags |= subprocess.CREATE_NEW_PROCESS_GROUP
        return {"creationflags": flags}
    return {"start_new_session": True}


def _remove_legacy_session_file() -> None:
    # Name-based ownership does not prove Cove created the file.
    # Substituting the final component or an ancestor between lstat and unlink
    # can target aria2.session in another directory; at most one such entry is
    # unlinked, without recursion.
    # Cleanup requires explicit connection refusal from the local RPC probe;
    # timeouts and other errors skip. Refusal does not prove no aria2 writer exists.
    # No process-ownership check is implemented: writers on other RPC ports
    # or binding between the port check and spawn cannot be detected.
    # Hard-linked copies, backups, snapshots and free space are not addressed.
    path = ARIA2_SESSION
    if path.name != "aria2.session" or path.parent != DATA_DIR:
        return
    log = logging.getLogger("cove")
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            log.info("legacy_aria2_session_cleanup_skipped: reason=not_regular_file")
            return
        if getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            log.info("legacy_aria2_session_cleanup_skipped: reason=reparse_point")
            return
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        log.warning("legacy_aria2_session_cleanup_failed: errno=%s", exc.errno)


class Aria2Daemon:
    """Owns the aria2c process. Idempotent start/stop."""

    _rpc_refused = False  # An unprobed daemon has no explicit refusal.
    preflight = _SYNC_RECLAIM

    def __init__(self, settings: Settings):
        self.settings = settings
        self._proc: subprocess.Popen | None = None
        self._unreleased = False
        self._secret = self._active_secret = self._launch_conf = None

    @staticmethod
    def is_installed() -> bool:
        return _resolve_aria2c() is not None

    def start(self) -> None:
        if self._unreleased or _owned_pending is not None: raise Aria2Error(_UNRESOLVED)
        if self.is_running(): return
        ticket = self.begin_start()
        try:
            error = self._await_rpc()
        except BaseException:
            try: self.stop()
            except BaseException: pass
            raise
        self.commit_ready(ticket, error)

    def begin_start(self):
        if self._unreleased or _owned_pending is not None:
            raise Aria2Error(_UNRESOLVED)
        if self.is_running():
            if self._active_secret is not None: return None
            raise Aria2Error("aria2 startup already in progress.")
        self._active_secret = self._secret = None
        aria2c = _resolve_aria2c()
        if aria2c is None:
            if sys.platform == "win32":
                hint = "Reinstall Cove Download Manager — the aria2 binary is missing from the bundle."
            elif sys.platform == "darwin":
                hint = "Install it: brew install aria2"
            else:
                hint = "Install it: sudo apt install aria2  (or your distro's equivalent)"
            raise Aria2Error(f"aria2c not found. {hint}")
        connections = min(
            max(int(self.settings.connections_per_server), 1),
            MAX_CONNECTIONS_PER_SERVER,
        )
        port = self.settings.rpc_port
        args = [
            aria2c,
            "--enable-rpc",
            f"--max-concurrent-downloads={MAX_CONCURRENT_DOWNLOADS}",
            f"--rpc-listen-port={port}",
            "--rpc-listen-all=false",
            "--rpc-allow-origin-all=false",
            f"--max-connection-per-server={connections}",
            f"--split={connections}",
            "--min-split-size=1M",
            "--continue=true",
            # aria2's default "prealloc" writes zeros for the whole file
            # before downloading. On a multi-GB file that pegs the disk,
            # blocks aria2's single-threaded RPC loop and lags the machine.
            f"--file-allocation={_file_allocation()}",
            "--allow-overwrite=false",
            "--auto-file-renaming=true",
            f"--dir={self.settings.download_dir}",
            # Cove participates in the swarm only while a torrent is still
            # downloading. Once aria2 reports the download complete it must
            # stop seeding, so the UI's "done" is the truth. Everything else
            # about BitTorrent (DHT, PEX, ports, encryption) stays on aria2's
            # defaults on purpose.
            "--seed-time=0",
            f"--log={ARIA2_LOG}",
            "--log-level=warn",
            "--summary-interval=0",
            "--quiet=true",
        ]
        # Interface binding. Cove refuses to launch rather than let traffic
        # out over an adapter the user did not choose, so a missing
        # interface is a hard error and never a silent fall back to "any".
        iface = str(getattr(self.settings, "torrent_network_interface", "") or "")
        if iface:
            if not interface_exists(iface):
                raise Aria2InterfaceError(
                    f"Network interface '{iface}' was not found. Cove is configured "
                    "to bind all downloads to it and will not fall back to another "
                    "interface. Reconnect it, or pick a different interface under "
                    "Settings → BitTorrent."
                )
            args.append(f"--interface={iface}")
        if self.settings.speed_limiter_enabled and self.settings.overall_speed_limit_kbps > 0:
            args.append(
                f"--max-overall-download-limit={self.settings.overall_speed_limit_kbps}K"
            )
        # Reclaim the port *before* spawning. Checking afterwards races the
        # doomed child: it needs a moment to fail its bind and exit, so
        # poll() can still read "alive" while the RPC reply is really coming
        # from the leftover daemon.
        self._rpc_refused = False
        if self.preflight is _SYNC_RECLAIM:
            if self._port_in_use():
                self._reclaim_stale_daemon()
                logging.getLogger("cove").info("legacy_aria2_session_cleanup_skipped: reason=rpc_port_busy")
            elif self._rpc_refused:
                _remove_legacy_session_file()
            else:
                logging.getLogger("cove").info("legacy_aria2_session_cleanup_skipped: reason=rpc_port_not_refused")
        elif self.preflight is None or self.preflight.port != port:
            state, secret = read_rpc_record()
            return _ReclaimRequest(port, self.settings.rpc_secret if state == "ABSENT" else secret, gate=_PeerGate())
        else:
            preflight, self.preflight = self.preflight, None
            if preflight.kind == "stale":
                raise self._stale_daemon_error()
            if preflight.kind == "refused":
                self._rpc_refused = True
                _remove_legacy_session_file()
            elif preflight.kind == "released":
                logging.getLogger("cove").info("legacy_aria2_session_cleanup_skipped: reason=rpc_port_busy")
            else:
                logging.getLogger("cove").info("legacy_aria2_session_cleanup_skipped: reason=rpc_port_not_refused")
        try:
            self._secret = secrets.token_urlsafe(32)
            conf_path = self._write_rpc_conf()
            self._write_rpc_conf(record=True)
            args.insert(1, f"--conf-path={conf_path}")
            self._spawn(args)
            return _LaunchTicket(self._proc, self._secret, port)
        except BaseException:
            try:
                self.stop()
            except BaseException:
                pass  # Cleanup failure cannot replace the original startup exception.
            raise
    @staticmethod
    @_bounded_reclaim
    def reclaim_probe(request, cancel):
        gate = request.gate or _PeerGate()
        verify = lambda sock: gate.check(sock, request.port, cancel, request.deadline)
        kind = _probe_port(request.port)
        if kind != "busy": return kind
        if request.secret is not None:
            error = Aria2Daemon._await_rpc(
                _AuthInput(_RpcSettings(request.port), request.secret),
                secret=request.secret, retry=False, cancel=cancel, verify=verify, limit=request.deadline)
            if time.monotonic() >= request.deadline: return "deadline"
            if cancel.cancelled: return "cancelled"
            if error is None: return "eligible"
        if time.monotonic() >= request.deadline: return "deadline"
        if cancel.cancelled: return "cancelled"
        return "stale" if _probe_port(request.port) == "busy" else "free"

    @staticmethod
    @_bounded_reclaim
    def reclaim_release(request, cancel, timeout=None):
        gate = request.gate or _PeerGate()
        verify = lambda sock: gate.check(sock, request.port, cancel, request.deadline)
        client = Aria2RPC(_RpcSettings(request.port), secret_provider=lambda: request.secret,
                          session_factory=lambda: cancel.session(verify))
        try:
            client.shutdown()
        except Exception:
            pass
        finally:
            client.close()
        if gate.denied: return "busy"
        deadline = min(request.deadline, time.monotonic() + (_PORT_RELEASE_TIMEOUT if timeout is None else timeout))
        while not cancel.cancelled and time.monotonic() < deadline:
            if _probe_port(request.port) != "busy": break
            cancel.wait(0.1)
        if time.monotonic() >= request.deadline: return "deadline"
        if cancel.cancelled: return "cancelled"
        return "busy" if _probe_port(request.port) == "busy" else "released"

    @staticmethod
    def authenticate(ticket, cancel):
        return Aria2Daemon._await_rpc(_AuthInput(_RpcSettings(ticket.port), ticket.secret), cancel=cancel)

    def commit_ready(self, ticket, error):
        try:
            if error is None and self._proc is ticket.proc and self._secret == ticket.secret and self.is_running():
                self._unlink_launch_conf(); self._active_secret = ticket.secret
                return
        except BaseException:
            try: self.stop()
            except BaseException: pass
            raise
        self.stop()
        if error is None or str(error) == str(self._stale_daemon_error()):
            raise self._stale_daemon_error()
        raise Aria2Error("aria2 RPC did not come up.")

    def _write_rpc_conf(self, record: bool = False) -> Path:
        """Publish a complete private conf; no credential belongs in argv."""
        s = self.settings
        for value in (s.proxy_host, s.proxy_type, s.proxy_port):
            if any(ord(char) < 0x20 or 0x7f <= ord(char) <= 0x9f for char in str(value)):
                raise Aria2Error("Invalid control character in aria2 configuration.")
        text = f"rpc-secret={self._secret}\n"
        if not record and s.proxy_type != "none" and s.proxy_host:
            text += f"all-proxy={self._build_proxy_url()}\n"
        conf_path = DATA_DIR / ("aria2.rpc.auth" if record else f".aria2.launch.{secrets.token_hex(12)}.conf")
        temporary = None
        fd = None
        try:
            prepare_private_directory(DATA_DIR)
            candidate = DATA_DIR / f".aria2.auth.{secrets.token_hex(12)}.tmp" if record else conf_path
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
            temporary = candidate  # Never unlink a candidate we did not create.
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as conf:
                fd = None  # The file object now owns the descriptor.
                conf.write(text)
                conf.flush()
                os.fsync(conf.fileno())
            if record:
                os.replace(temporary, conf_path)
            else:
                self._launch_conf = conf_path
            temporary = None
            if os.name != "nt":
                if not stat.S_ISREG(conf_path.lstat().st_mode):
                    raise OSError("aria2 configuration must be a regular file")
                os.chmod(conf_path, 0o600, follow_symlinks=False)
                info = conf_path.lstat()
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                    raise OSError("aria2 configuration is not private")
            return conf_path
        except (OSError, ValueError):
            pass
        finally:
            if fd is not None:
                os.close(fd)
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass
        raise Aria2Error("Unable to prepare private aria2 configuration.")

    def is_running(self) -> bool:
        """Whether our aria2c child is alive. Cheap enough for a GUI timer.

        `start()` consults `poll()` only on the way in, as its first-line
        guard, and nothing re-reads it afterwards. So a child that exits
        after boot used to go unnoticed for the rest of the session: every
        download failed with a generic error while the window still looked
        idle. This is the periodic check that closes that window.

        Deliberately a probe and not a repair. Restarting aria2c from here
        would leave Cove holding gids issued by a process that no longer
        exists, and unpicking that reaches into every asynchronous add in
        the queue - a much larger change than the outage it would paper
        over. Saying "aria2 is gone, restart Cove" is honest, immediate,
        and cannot corrupt the queue.

        `poll()` is also the call that reaps a child that has exited, so
        asking this periodically is what stops a dead aria2c lingering as a
        zombie for the rest of the session.
        """
        proc = self._proc
        with _lifecycle_lock:
            return proc is not None and proc.poll() is None

    def _spawn(self, args: list[str]) -> None:
        child_env = None
        if self.settings.proxy_type != "none" and self.settings.proxy_host:
            # aria2 1.37.0 reads all_proxy after conf but before argv. Only this
            # option lost precedence when moved to conf; protocol proxy options
            # and no_proxy retain their pre-S04 behaviour. Uppercase names are
            # ignored on POSIX, but Windows environment names ignore case.
            child_env = os.environ.copy()
            child_env.pop("all_proxy", None)
            if os.name == "nt":
                child_env.pop("ALL_PROXY", None)
        try:
            self._proc = _OwnedPopen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=child_env,
                **_hidden_console_kwargs(),
            )
        except OSError:
            pass
        else:
            return
        raise Aria2Error("Failed to launch aria2c.")

    def _await_rpc(self, timeout: float = 5.0, secret=None, retry=True, cancel=None, verify=None, limit=None) -> Exception | None:
        """Observe acceptance, then one exact rejection (aria2 delays it ~2s)."""
        deadline = time.monotonic() + timeout
        if limit is not None: deadline = min(deadline, limit)
        secret = self._secret if secret is None else secret
        client = Aria2RPC(self.settings, secret_provider=lambda: secret, session_factory=(lambda: cancel.session(verify)) if cancel else None)
        last_err = Aria2Error("aria2 RPC did not come up.")
        try:
            while (remaining := deadline - time.monotonic()) > 0:
                if cancel and cancel.cancelled: return Aria2Error("aria2 authentication cancelled.")
                try:
                    client.timeout = min(timeout, remaining)
                    version = client.get_version()
                    if cancel and cancel.cancelled: return Aria2Error("aria2 authentication cancelled.")
                    if not isinstance(version, dict) or not isinstance(version.get("version"), str):
                        return last_err
                    break
                except Aria2RpcError:
                    if cancel and cancel.cancelled: return Aria2Error("aria2 authentication cancelled.")
                    return self._stale_daemon_error()
                except Aria2Error:
                    if cancel and cancel.cancelled: return Aria2Error("aria2 authentication cancelled.")
                    if not retry:
                        return last_err
                    cancel.wait(0.1) if cancel else time.sleep(0.1)
            else:
                if cancel and cancel.cancelled: return Aria2Error("aria2 authentication cancelled.")
                return last_err
            wrong = secrets.token_urlsafe(32)
            secret = wrong if wrong != secret else wrong + "_"
            client.timeout = max(4.0, timeout)
            if limit is not None:
                if time.monotonic() >= limit: return last_err
                client.timeout = min(client.timeout, limit - time.monotonic())
            try:
                client.get_version()
            except Aria2RpcError as error:
                if cancel and cancel.cancelled: return Aria2Error("aria2 authentication cancelled.")
                if error.http_status == 400 and type(error.code) is int and error.code == 1 and error.rpc_message == "Unauthorized":
                    return None
            except Aria2Error:
                if cancel and cancel.cancelled: return Aria2Error("aria2 authentication cancelled.")
                return last_err
            if cancel and cancel.cancelled: return Aria2Error("aria2 authentication cancelled.")
            return self._stale_daemon_error()
        finally:
            client.close()
        return last_err

    def _port_in_use(self) -> bool:
        self._rpc_refused = False
        with socket.socket() as probe:
            probe.settimeout(0.2)
            try:
                probe.connect(("127.0.0.1", int(self.settings.rpc_port)))
            except ConnectionRefusedError:
                self._rpc_refused = True
                return False
            except OSError:
                return False
        return True

    def _stale_daemon_error(self) -> "Aria2Error":
        return Aria2Error(
            "Another aria2 process is already using port "
            f"{self.settings.rpc_port}. Quit it (check your process list "
            "for a leftover aria2c) or change the RPC port in Cove's "
            "settings, then restart Cove."
        )

    def _reclaim_stale_daemon(self) -> None:
        """Clear an aria2 that already holds our RPC port.

        A previous Cove's aria2c can outlive it (a crash, a killed session)
        and keep listening. It accepts our secret, so a naive health check
        passes and Cove ends up driving a process it cannot stop or restart
        - then every call fails with "connection refused" once that process
        finally exits. Shut it down and take ownership instead.
        """
        state, secret = read_rpc_record()
        if state == "ABSENT":
            secret = self.settings.rpc_secret
        if secret is None or self._await_rpc(secret=secret, retry=False) is not None:
            raise self._stale_daemon_error()
        client = Aria2RPC(self.settings, secret_provider=lambda: secret)
        try:
            client.shutdown()
        except Exception:
            pass
        finally:
            client.close()
        self._wait_for_port_release()
        if self._port_in_use():
            raise self._stale_daemon_error()

    def _wait_for_port_release(self, timeout: float = 5.0) -> None:
        """Give a shut-down daemon time to let go of the RPC port."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with socket.socket() as probe:
                probe.settimeout(0.2)
                try:
                    probe.connect(("127.0.0.1", int(self.settings.rpc_port)))
                except OSError:
                    return
            time.sleep(0.1)

    def _build_proxy_url(self) -> str:
        from urllib.parse import quote
        s = self.settings
        scheme = s.proxy_type if s.proxy_type in ("http", "https", "socks5") else "http"
        auth = ""
        if s.proxy_username:
            user = quote(s.proxy_username, safe="")
            pwd = quote(s.proxy_password, safe="") if s.proxy_password else ""
            auth = f"{user}:{pwd}@" if pwd else f"{user}@"
        port = f":{s.proxy_port}" if s.proxy_port else ""
        return f"{scheme}://{auth}{s.proxy_host}{port}"

    def stop(self, *, kill_only=False) -> bool:
        global _owned_pending
        self._active_secret = self._secret = None
        self._unlink_launch_conf()
        procs = [self._proc] + ([_owned_pending] if _owned_pending is not self._proc else [])
        failed = False
        for proc in procs:
            if proc is None:
                continue
            if _release_child(proc, graceful=not (kill_only or self._unreleased or proc is _owned_pending)):
                if self._proc is proc:
                    self._proc = None
                if _owned_pending is proc:
                    _owned_pending = None
            else:
                failed = self._unreleased = True
                _unresolved_child(proc)
        self._unreleased = failed
        return not failed

    def _unlink_launch_conf(self) -> None:
        if self._launch_conf is not None:
            try:
                self._launch_conf.unlink()
                self._launch_conf = None
            except OSError:
                pass

    def active_rpc_secret(self) -> str | None:
        if self._active_secret is None: return None
        if not self.is_running():
            self._active_secret = None
        return self._active_secret


class Aria2RPC:
    """Synchronous JSON-RPC client. Call from a worker thread."""

    def __init__(self, settings: Settings, timeout: float = 5.0, secret_provider=None, session_factory=None):
        self.url = f"http://127.0.0.1:{settings.rpc_port}/jsonrpc"
        self.secret = settings.rpc_secret
        self._secret_provider = secret_provider
        self.timeout = timeout
        self._session_factory = session_factory
        # requests.Session is not thread-safe. Calls fan out across a
        # QThreadPool, so give each thread its own session.
        self._local = threading.local()

    @property
    def secret(self):
        provider = getattr(self, "_secret_provider", None)
        return provider() if provider is not None else self._legacy_secret

    @secret.setter
    def secret(self, value):
        self._legacy_secret = value

    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = (getattr(self, "_session_factory", None) or requests.Session)()
            # The RPC token must never go to an environment proxy.
            s.trust_env = False
            self._local.session = s
        return s

    def close(self) -> None:
        """Release the calling thread's HTTP connection pool, if any."""
        s = getattr(self._local, "session", None)
        if s is not None:
            s.close()
            self._local.session = None

    def __del__(self) -> None:
        # Best-effort; may run during interpreter shutdown.
        try:
            self.close()
        except Exception:
            pass

    def _call(
        self, method: str, params: Iterable[Any] = (), *, with_token: bool = True
    ) -> Any:
        # `system.multicall` takes exactly one argument - the batch - and each
        # sub-call carries its own token, so it is the one method that must not
        # have one prepended here.
        secret = self.secret
        if secret is None:
            raise Aria2Error("aria2 is not ready")
        def sanitize(value):
            if isinstance(value, dict):
                return {str(sanitize(k)): sanitize(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [sanitize(v) for v in value]
            if isinstance(value, (int, float)) and not isinstance(value, bool) and secret and (secret in str(value) or secret in repr(value)):
                return "[redacted]"
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                try:
                    if float(value) == float(secret):
                        return "[redacted]"
                except (ValueError, OverflowError):
                    pass
            return value.replace(secret, "[redacted]") if isinstance(value, str) and secret else value
        def redact(value):
            return str(sanitize(value)).replace(secret, "[redacted]") if secret else str(value)
        params = list(params)
        if method == "system.multicall" and params:
            for call in params[0]:
                if isinstance(call, dict):
                    call["params"][0] = f"token:{secret}"
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": [f"token:{secret}", *params] if with_token
            else [*params],
        }
        r = None
        failure = None
        try:
            r = self._session().post(self.url, json=payload, timeout=self.timeout, allow_redirects=False)
        except requests.RequestException as e:
            failure = f"RPC transport error: {redact(e)}"
        except ValueError:
            pass  # Discard parsing failures before raising a sanitized error.
        if failure is not None:
            raise Aria2Error(failure)
        if r is None:
            raise Aria2Error("RPC message could not be processed")
        if 300 <= r.status_code < 400:
            raise Aria2Error(f"RPC unexpected redirect: HTTP {r.status_code}")
        try:
            data = r.json()
        except (ValueError, RecursionError):
            data = None
        if not isinstance(data, dict):
            raise Aria2Error("RPC bad response: invalid JSON object")
        if "error" in data and "result" in data:
            raise Aria2Error("RPC bad response: contradictory envelope")
        try:
            if "error" in data:
                error = data["error"]
                if not isinstance(error, dict):
                    raise Aria2Error(f"RPC {method} failed: {redact(error)}")
                code = error.get("code")
                message = error.get("message", error)
                failure = Aria2RpcError(method, sanitize(code), redact(message))
                failure._gid_absent = any(phrase in str(message).lower() for phrase in Aria2RpcError._GID_ABSENT)
                failure.http_status = sanitize(r.status_code)
                raise failure
            if method == "system.multicall" and isinstance(data.get("result"), list):
                for answer in data["result"]:
                    if isinstance(answer, dict):
                        answer["_gid_absent"] = any(phrase in str(answer.get("faultString", answer.get("message", answer))).lower() for phrase in Aria2RpcError._GID_ABSENT)
                        answer["faultString"] = redact(answer.get("faultString", answer.get("message", answer)))
                        for key in ("message", "faultString", "code", "faultCode"):
                            if key in answer:
                                answer[key] = sanitize(answer[key])
        except RecursionError:
            data = None
        if data is None:
            raise Aria2Error("RPC bad response: invalid JSON object")
        return data.get("result")

    # ---- Lifecycle -----------------------------------------------------

    def get_version(self) -> dict:
        return self._call("aria2.getVersion")

    def shutdown(self) -> None:
        try:
            self._call("aria2.shutdown")
        except Aria2Error:
            pass

    # ---- Downloads -----------------------------------------------------

    def add_uri(
        self,
        uris: list[str],
        out_dir: str,
        connections: int,
        speed_limit_kbps: int = 0,
        filename: str | None = None,
        headers: list[str] | None = None,
        gid: str | None = None,
    ) -> str:
        connections = min(max(int(connections), 1), MAX_CONNECTIONS_PER_SERVER)
        opts: dict[str, str] = {
            "dir": out_dir,
            "split": str(connections),
            "max-connection-per-server": str(connections),
            "continue": "true",
        }
        if speed_limit_kbps > 0:
            opts["max-download-limit"] = f"{speed_limit_kbps}K"
        if filename:
            opts["out"] = filename
        if headers:
            opts["header"] = headers
        if gid is not None:
            validate_gid(gid)
            opts["gid"] = gid
        return self._call("aria2.addUri", [uris, opts])

    def add_magnet(
        self, uri: str, out_dir: str, speed_limit_kbps: int = 0,
        gid: str | None = None,
    ) -> str:
        """Start a magnet locally. Returns aria2's *metadata* gid.

        aria2 fetches the torrent metadata as its own download and then
        reports the real torrent under `followedBy`; the caller is
        responsible for following that transition.
        """
        opts: dict[str, str] = {"dir": out_dir, "seed-time": "0"}
        if speed_limit_kbps > 0:
            opts["max-download-limit"] = f"{speed_limit_kbps}K"
        if gid is not None:
            validate_gid(gid)
            opts["gid"] = gid
        return self._call("aria2.addUri", [[uri], opts])

    def add_magnet_metadata(self, uri: str, out_dir: str) -> str:
        """Fetch one magnet's metainfo and nothing else. Returns the gid.

        The difference from `add_magnet` is `bt-metadata-only`, and it is the
        whole difference: measured against aria2 1.37.0, a magnet added
        without it completes its metadata download and immediately reports a
        payload child through `followedBy`, writing the torrent's files and
        an `.aria2` control file into `out_dir`. With it, the gid completes
        with no `followedBy` at all and `out_dir` holds exactly one file.
        (`follow-torrent` does not substitute: it governs a downloaded
        `.torrent` *file*, and neither `mem` nor `false` suppresses the
        payload child of a magnet.)

        `bt-save-metadata` is what makes the result readable, and it names
        the file for us: aria2 writes `<lowercase hex info hash>.torrent`
        into the download directory, so the caller can name the artifact it
        expects instead of guessing at whatever appeared.

        `out_dir` must be a directory the caller owns and is prepared to
        delete. It is never the user's download directory: this is a
        temporary metadata job, and nothing it writes is a download.

        Every option here is per request. Nothing in this method changes
        aria2's global configuration, so an ordinary transfer running
        alongside is unaffected.
        """
        opts: dict[str, str] = {
            "dir": out_dir,
            "bt-metadata-only": "true",
            "bt-save-metadata": "true",
            "seed-time": "0",
        }
        return self._call("aria2.addUri", [[uri], opts])

    def add_torrent(
        self,
        data: bytes,
        out_dir: str,
        speed_limit_kbps: int = 0,
        select_file: str | None = None,
        gid: str | None = None,
    ) -> str:
        """Start a validated `.torrent` locally via aria2.addTorrent.

        `data` is base64-encoded for JSON-RPC. Neither the raw bytes nor
        their encoding appear in any exception raised here: an aria2 error
        message is already user-facing, and the payload is not.
        """
        if not isinstance(data, (bytes, bytearray)):
            raise Aria2Error("Cove could not read this torrent file.")
        encoded = base64.b64encode(bytes(data)).decode("ascii")
        opts: dict[str, str] = {"dir": out_dir, "seed-time": "0"}
        if speed_limit_kbps > 0:
            opts["max-download-limit"] = f"{speed_limit_kbps}K"
        if select_file:
            # Forward-compatible plumbing only; Slice B always downloads
            # every file, so nothing passes this today.
            opts["select-file"] = select_file
        if gid is not None:
            validate_gid(gid)
            opts["gid"] = gid
        return self._call("aria2.addTorrent", [encoded, [], opts])

    def get_files(self, gid: str) -> list[dict]:
        """The files aria2 actually writes for `gid`.

        Used by removal: a torrent's real paths come from aria2, never from
        a path Cove reconstructed itself.
        """
        return self._call("aria2.getFiles", [gid])

    def pause(self, gid: str) -> str:
        return self._call("aria2.pause", [gid])

    def unpause(self, gid: str) -> str:
        return self._call("aria2.unpause", [gid])

    def pause_all(self) -> str:
        return self._call("aria2.pauseAll")

    def unpause_all(self) -> str:
        return self._call("aria2.unpauseAll")

    def remove(self, gid: str, force: bool = True) -> str:
        """Stop `gid`, or establish that aria2 no longer has it.

        Returning normally is a promise the caller acts on: it deletes files
        and forgets the gid. So only aria2's own refusal justifies the
        removeDownloadResult fallback - that answer means the download is not
        active, and the stopped-result entry is all there is left to clean.
        A transport failure, a timeout or a malformed body are "no answer",
        and retrying them as a result cleanup that happens to succeed is how
        a live transfer outlives the row that owned it. They stay failures.
        """
        method = "aria2.forceRemove" if force else "aria2.remove"
        try:
            return self._call(method, [gid])
        except Aria2RpcError as refused:
            try:
                return self._call("aria2.removeDownloadResult", [gid])
            except Aria2RpcError as absent:
                if absent.gid_not_found():
                    # Both calls were answered and both said there is no such
                    # download. Nothing is left that could still be writing,
                    # which is exactly what was asked for.
                    return "OK"
                raise absent from refused

    def remove_download_result(self, gid: str) -> str:
        return self._call("aria2.removeDownloadResult", [gid])

    def tell_status(self, gid: str) -> dict:
        return self._call(
            "aria2.tellStatus",
            [
                gid,
                [
                    "gid",
                    "status",
                    "totalLength",
                    "completedLength",
                    "downloadSpeed",
                    "files",
                    "errorCode",
                    "errorMessage",
                    "connections",
                    "dir",
                    "bitfield",
                    "numPieces",
                    # Torrent lifecycle: a magnet's metadata download points
                    # at the real torrent through followedBy/following, and
                    # infoHash/bittorrent identify what aria2 is actually on.
                    "followedBy",
                    "following",
                    "infoHash",
                    "bittorrent",
                ],
            ],
        )

    _EXTERNAL_KEYS = [
        "gid", "status", "totalLength", "completedLength", "downloadSpeed", "files",
        # Adoption guard: a torrent child gid names its metadata parent in
        # `following`, and any torrent job at all carries infoHash/bittorrent.
        "following", "followedBy", "infoHash", "bittorrent",
    ]

    def tell_active(self) -> list[dict]:
        return self._call("aria2.tellActive", [self._EXTERNAL_KEYS])

    def tell_all_downloads(self, num: int = 1000) -> list[dict]:
        """Every download aria2 knows, as one point-in-time snapshot.

        aria2 splits these across three methods: `tellActive` for what is
        running, `tellWaiting` for what is queued or paused, and `tellStopped`
        for what has finished, errored or been removed. Calling them one after
        another yields separate snapshots, and a download that changes state in
        a gap can be absent from every answer - which is precisely what freeing
        a slot, or a child completing quickly, provokes. `system.multicall` is
        handled as a single batch, so nothing can cross between the parts and
        be lost by all of them.

        The two paged listings ask for `num` entries. If the queued listing
        comes back exactly full this is no longer a whole snapshot, and since
        the caller uses it to decide that a download has no children, it raises
        rather than answer with a picture it cannot vouch for. Paginating
        instead would reintroduce the split snapshot this method exists to
        avoid. A full stopped listing is not treated the same way: it sits at
        the cap routinely on a busy daemon, anything found there is already
        terminal, and it is read from the newest end where a boundary-created
        child lands, so a full page is not evidence of having missed one.
        """
        token = None  # Resolved once by _call, including every nested token.
        batch = [
            {"methodName": "aria2.tellActive",
             "params": [token, self._EXTERNAL_KEYS]},
            {"methodName": "aria2.tellWaiting",
             "params": [token, 0, num, self._EXTERNAL_KEYS]},
            # Offset -1, not 0: aria2 orders stopped results oldest-first, and
            # a child that finished during the removal is the newest of them.
            # A negative offset walks back from the end, so this is the page
            # the boundary case actually lands in. Verified against 1.37.0.
            {"methodName": "aria2.tellStopped",
             "params": [token, -1, num, self._EXTERNAL_KEYS]},
        ]
        answers = self._call("system.multicall", [batch], with_token=False)
        # Every sub-call must have answered. A truncated or non-list batch
        # result is not a daemon with nothing to report: skipping the parts
        # that did not parse would turn a missing queued listing into "no
        # child" and retire a task whose payload is still there.
        if not isinstance(answers, list) or len(answers) != len(batch):
            raise Aria2Error(
                f"RPC system.multicall answered for {len(batch)} sub-calls "
                "with something else entirely"
            )
        jobs: list[dict] = []
        for index, answer in enumerate(answers):
            # A sub-call that failed is reported in-band, as a fault struct
            # rather than an error response. Reading that as "no downloads"
            # would be the same unproven leap as reading a transport failure
            # as "no child", so it is surfaced as aria2 declining to answer.
            if isinstance(answer, dict):
                error = Aria2RpcError(
                    "system.multicall",
                    answer.get("faultCode", answer.get("code")),
                    answer.get("faultString", answer.get("message", answer)),
                )
                error._gid_absent = answer.get("_gid_absent", error.gid_not_found())
                raise error
            # Each successful sub-call answers with its result wrapped in a
            # one-element array. Anything else is not a listing, and is not
            # read as an empty one.
            if not (isinstance(answer, list) and len(answer) == 1
                    and isinstance(answer[0], list)):
                raise Aria2Error(
                    f"RPC {batch[index]['methodName']} answered in a shape "
                    "that is not a download listing"
                )
            part = [job for job in answer[0] if isinstance(job, dict)]
            if (batch[index]["methodName"] == "aria2.tellWaiting"
                    and len(part) >= num):
                # A queued listing at the cap may not be all of it, and this
                # answer is used to conclude that a download has no children.
                # Refusing to answer keeps the caller's task recoverable; a
                # short answer would retire it.
                raise Aria2Error(
                    f"RPC aria2.tellWaiting returned a full page ({num}); "
                    "the snapshot of queued downloads is not complete"
                )
            jobs += part
        return jobs

    def tell_stopped(self, offset: int = 0, num: int = 1000) -> list[dict]:
        return self._call("aria2.tellStopped", [offset, num, self._EXTERNAL_KEYS])

    def tell_external_snapshot(self) -> list[dict]:
        """Active + recently-stopped downloads, for discovering downloads
        added outside Cove's queue (e.g. by the browser extension).

        Includes stopped/completed entries so downloads that finish faster
        than the discovery poll interval are still picked up. Runs both RPC
        calls on one worker thread to avoid extra concurrent use of the
        shared HTTP session.
        """
        return self.tell_active() + self.tell_stopped()

    # ---- Global options ------------------------------------------------

    def set_overall_speed_limit_kbps(self, kbps: int) -> None:
        value = f"{kbps}K" if kbps > 0 else "0"
        self._call("aria2.changeGlobalOption", [{"max-overall-download-limit": value}])

    def set_per_download_speed_limit_kbps(self, gid: str, kbps: int) -> None:
        value = f"{kbps}K" if kbps > 0 else "0"
        self._call("aria2.changeOption", [gid, {"max-download-limit": value}])
