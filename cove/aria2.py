"""aria2 daemon manager + JSON-RPC client.

Spawns a local `aria2c --enable-rpc` instance and exposes the methods Cove
needs (addUri, pause, unpause, remove, tellStatus, changeOption,
changeGlobalOption). Network calls run on a background thread; the UI
should never block on these.
"""
from __future__ import annotations

import os
import shutil
import signal
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


class Aria2Error(RuntimeError):
    pass


# aria2 defaults max-concurrent-downloads to 5. Downloads added via the
# browser extension go straight to aria2 (bypassing Cove's own queue), so
# that default silently caps extension downloads at 5 and also throttles the
# GUI queue below its configurable "up to 16 parallel". Lift it well above
# both; downloads beyond this still queue inside aria2 but are uncommon.
MAX_CONCURRENT_DOWNLOADS = 20


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
            f"--log={ARIA2_LOG}",
            "--log-level=warn",
            "--summary-interval=0",
            "--quiet=true",
        ]
        if self.settings.speed_limiter_enabled and self.settings.overall_speed_limit_kbps > 0:
            args.append(
                f"--max-overall-download-limit={self.settings.overall_speed_limit_kbps}K"
            )
        if self.settings.proxy_type != "none" and self.settings.proxy_host:
            proxy_url = self._build_proxy_url()
            args.append(f"--all-proxy={proxy_url}")
        try:
            self._proc = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **_hidden_console_kwargs(),
            )
        except OSError as e:
            raise Aria2Error(f"Failed to launch aria2c: {e}")
        # Wait briefly for RPC to come up.
        deadline = time.time() + 5.0
        client = Aria2RPC(self.settings)
        last_err: Exception | None = None
        try:
            while time.time() < deadline:
                try:
                    client.get_version()
                    return
                except Exception as e:
                    last_err = e
                    time.sleep(0.1)
        finally:
            client.close()
        self.stop()
        raise Aria2Error(f"aria2 RPC did not come up: {last_err}")

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
            raise Aria2Error(
                f"RPC {method} failed: {data['error'].get('message', data['error'])}"
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
                ],
            ],
        )

    _EXTERNAL_KEYS = [
        "gid", "status", "totalLength", "completedLength", "downloadSpeed", "files",
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
