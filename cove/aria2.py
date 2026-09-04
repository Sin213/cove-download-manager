"""aria2 daemon manager + JSON-RPC client.

Spawns a local `aria2c --enable-rpc` instance and exposes the methods Cove
needs (addUri, pause, unpause, remove, tellStatus, changeOption,
changeGlobalOption). Network calls run on a background thread; the UI
should never block on these.
"""
from __future__ import annotations

import base64
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

import requests

from .config import (
    ARIA2_LOG,
    ARIA2_SESSION,
    DATA_DIR,
    MAX_CONNECTIONS_PER_SERVER,
    Settings,
)
from .netiface import interface_exists


class Aria2Error(RuntimeError):
    pass


class Aria2RpcError(Aria2Error):
    """An error aria2 itself returned, as opposed to a failure reaching it.

    The distinction is the point. A transport failure, a timeout or a
    malformed body all mean "no answer", and a caller must not read anything
    into them; only a JSON-RPC error object is aria2 stating a fact about the
    request. Measured against 1.37.0, an unknown gid answers
    `{code: 1, message: "GID <gid> is not found"}` on tellStatus, forceRemove
    and removeDownloadResult alike, while an unreachable daemon produces no
    error object at all - so cleanup can tell "this download is gone" from
    "aria2 did not answer" without guessing at message text.
    """

    def __init__(self, method: str, code, message: str):
        super().__init__(f"RPC {method} failed: {message}")
        self.code = code
        self.rpc_message = message

    def gid_not_found(self) -> bool:
        """True when aria2 said it has no such download."""
        return "is not found" in str(self.rpc_message).lower()


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


class Aria2Daemon:
    """Owns the aria2c process. Idempotent start/stop."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._proc: subprocess.Popen | None = None

    @staticmethod
    def is_installed() -> bool:
        return _resolve_aria2c() is not None

    def start(self) -> None:
        if self._proc and self._proc.poll() is None:
            return
        aria2c = _resolve_aria2c()
        if aria2c is None:
            if sys.platform == "win32":
                hint = "Reinstall Cove Download Manager — the aria2 binary is missing from the bundle."
            elif sys.platform == "darwin":
                hint = "Install it: brew install aria2"
            else:
                hint = "Install it: sudo apt install aria2  (or your distro's equivalent)"
            raise Aria2Error(f"aria2c not found. {hint}")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ARIA2_SESSION.touch(exist_ok=True)
        connections = min(
            max(int(self.settings.connections_per_server), 1),
            MAX_CONNECTIONS_PER_SERVER,
        )
        # Pass the RPC secret via a 0600 conf file, not argv: command lines
        # are world-readable on many systems (/proc, process listers).
        conf_path = DATA_DIR / "aria2.rpc.conf"
        conf_path.touch(exist_ok=True)
        try:
            os.chmod(conf_path, 0o600)
        except OSError:
            pass
        conf_path.write_text(f"rpc-secret={self.settings.rpc_secret}\n")
        args = [
            aria2c,
            f"--conf-path={conf_path}",
            "--enable-rpc",
            f"--max-concurrent-downloads={MAX_CONCURRENT_DOWNLOADS}",
            f"--rpc-listen-port={self.settings.rpc_port}",
            "--rpc-listen-all=false",
            "--rpc-allow-origin-all=false",
            f"--max-connection-per-server={connections}",
            f"--split={connections}",
            "--min-split-size=1M",
            "--continue=true",
            "--allow-overwrite=false",
            "--auto-file-renaming=true",
            f"--dir={self.settings.download_dir}",
            f"--save-session={ARIA2_SESSION}",
            "--save-session-interval=10",
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
        if self.settings.proxy_type != "none" and self.settings.proxy_host:
            proxy_url = self._build_proxy_url()
            args.append(f"--all-proxy={proxy_url}")
        # Reclaim the port *before* spawning. Checking afterwards races the
        # doomed child: it needs a moment to fail its bind and exit, so
        # poll() can still read "alive" while the RPC reply is really coming
        # from the leftover daemon.
        if self._port_in_use():
            self._reclaim_stale_daemon()
        self._spawn(args)
        last_err = self._await_rpc()
        if last_err is None:
            if self._proc.poll() is None:
                return
            # RPC answered, but our own child is already dead: an aria2c
            # from a previous Cove outlived it and still holds the port. It
            # accepts our secret, so it is ours - but we cannot stop or
            # restart a process we have no handle on, and when it finally
            # exits every RPC call fails with "connection refused" mid
            # session. Shut it down and take ownership instead of driving it.
            client = Aria2RPC(self.settings)
            try:
                client.shutdown()
            except Exception:
                pass
            finally:
                client.close()
            self._wait_for_port_release()
            self._spawn(args)
            last_err = self._await_rpc()
            if last_err is None and self._proc.poll() is None:
                return
            self.stop()
            raise self._stale_daemon_error()
        our_proc_died = self._proc.poll() is not None
        self.stop()
        if our_proc_died and "unauthorized" in str(last_err).lower():
            raise Aria2Error(
                "Another aria2 process is already using port "
                f"{self.settings.rpc_port}. Quit it (check your process list "
                "for a leftover aria2c) or change the RPC port in Cove's "
                "settings, then restart Cove."
            )
        raise Aria2Error(f"aria2 RPC did not come up: {last_err}")

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
        return self._proc is not None and self._proc.poll() is None

    def _spawn(self, args: list[str]) -> None:
        try:
            self._proc = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **_hidden_console_kwargs(),
            )
        except OSError as e:
            raise Aria2Error(f"Failed to launch aria2c: {e}")

    def _await_rpc(self, timeout: float = 5.0) -> Exception | None:
        """Poll the RPC endpoint. None once it answers, else the last error."""
        deadline = time.time() + timeout
        client = Aria2RPC(self.settings)
        last_err: Exception | None = None
        try:
            while time.time() < deadline:
                try:
                    client.get_version()
                    return None
                except Exception as e:
                    last_err = e
                    time.sleep(0.1)
        finally:
            client.close()
        return last_err

    def _port_in_use(self) -> bool:
        with socket.socket() as probe:
            probe.settimeout(0.2)
            try:
                probe.connect(("127.0.0.1", int(self.settings.rpc_port)))
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
        client = Aria2RPC(self.settings)
        try:
            client.get_version()
        except Exception as e:
            if "unauthorized" in str(e).lower():
                # Someone else's aria2, not ours to shut down.
                raise self._stale_daemon_error()
            # Not an aria2 RPC we can talk to. Leave it alone and let the
            # normal startup path report why our own daemon never came up.
            return
        finally:
            client.close()
        client = Aria2RPC(self.settings)
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
        deadline = time.time() + timeout
        while time.time() < deadline:
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

    def stop(self) -> None:
        if not self._proc:
            return
        if self._proc.poll() is None:
            try:
                self._proc.send_signal(signal.SIGTERM)
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                # Reap the killed child so it can't linger as a zombie or
                # race a later startup.
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        self._proc = None


class Aria2RPC:
    """Synchronous JSON-RPC client. Call from a worker thread."""

    def __init__(self, settings: Settings, timeout: float = 5.0):
        self.url = f"http://127.0.0.1:{settings.rpc_port}/jsonrpc"
        self.secret = settings.rpc_secret
        self.timeout = timeout
        # requests.Session is not thread-safe. Calls fan out across a
        # QThreadPool, so give each thread its own session.
        self._local = threading.local()

    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
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

    def _call(self, method: str, params: Iterable[Any] = ()) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": [f"token:{self.secret}", *params],
        }
        try:
            r = self._session().post(self.url, json=payload, timeout=self.timeout)
        except requests.RequestException as e:
            raise Aria2Error(f"RPC transport error: {e}") from e
        try:
            data = r.json()
        except ValueError as e:
            raise Aria2Error(f"RPC bad response: {e}") from e
        if "error" in data:
            error = data["error"]
            if not isinstance(error, dict):
                raise Aria2Error(f"RPC {method} failed: {error}")
            # aria2 answered and said no. Subclassed so a caller can tell this
            # apart from the "no answer" cases above without parsing strings
            # that a transport failure could also produce.
            raise Aria2RpcError(
                method, error.get("code"), error.get("message", error)
            )
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
        return self._call("aria2.addUri", [uris, opts])

    def add_magnet(self, uri: str, out_dir: str, speed_limit_kbps: int = 0) -> str:
        """Start a magnet locally. Returns aria2's *metadata* gid.

        aria2 fetches the torrent metadata as its own download and then
        reports the real torrent under `followedBy`; the caller is
        responsible for following that transition.
        """
        opts: dict[str, str] = {"dir": out_dir, "seed-time": "0"}
        if speed_limit_kbps > 0:
            opts["max-download-limit"] = f"{speed_limit_kbps}K"
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
        method = "aria2.forceRemove" if force else "aria2.remove"
        try:
            return self._call(method, [gid])
        except Aria2Error:
            # Already finished/removed; clean up the result entry.
            return self._call("aria2.removeDownloadResult", [gid])

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
