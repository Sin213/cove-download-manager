"""Auto-updater backed by GitHub Releases.

Philosophy: never silently replace the user's binary. A background thread
polls the releases API on startup; when a newer version is published, the
user gets a dialog and chooses whether to install.

AppImage installs can do download → verify → swap → relaunch end-to-end
(the kernel keeps the running mmap alive across an overwrite, so replacing
the file on disk and re-execing works). Other distributions just open the
GitHub release page — the user runs the installer themselves.

**Integrity:** before the swap, the downloaded asset is verified against a
SHA-256 manifest published as a sibling release asset (`SHA256SUMS`,
`SHA256SUMS.txt`, `checksums.txt`, or `<asset>.sha256`). If no manifest is
present in the release, or the digest doesn't match, the auto-install path
refuses to run and the user is sent to the release page. Cove never
executes binaries it can't verify.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog

from . import netiface
from .system_open import open_url


# Kept as a module-local name: the same opener is now shared with the rest
# of the app (see cove.system_open.open_url).
_open_url = open_url


@dataclass
class UpdateInfo:
    latest_version: str
    release_url: str
    asset_name: str | None = None
    asset_url: str | None = None
    asset_size: int = 0
    checksum_url: str | None = None  # SHA256SUMS (or .sha256) sibling asset
    checksum_name: str | None = None


# Accept extra numeric components (e.g. 1.5.0.1) and ignore them for
# ordering, rather than failing to match and treating the version as 0.0.0.
_VERSION_RE = re.compile(
    r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.\d+)*(?:-([a-zA-Z0-9._-]+))?(?:\+.+)?$",
    re.I,
)

_PRE_PART_RE = re.compile(r"(\d+|\D+)")


def _pre_key(pre: str) -> tuple:
    """Comparable key for a pre-release suffix like 'rc10' or 'beta.2'."""
    parts: list[tuple[int, int | str]] = []
    for seg in pre.split("."):
        for tok in _PRE_PART_RE.findall(seg):
            if tok.isdigit():
                parts.append((0, int(tok)))
            else:
                parts.append((1, tok.lower()))
    return tuple(parts)


def _version_key(v: str) -> tuple:
    """Comparable key for a version string.

    Build metadata (+suffix) is ignored per semver. A plain release (no
    pre-release suffix) sorts ABOVE any pre-release of the same x.y.z.
    Pre-release suffixes are split into alpha/numeric tokens so rc10 > rc9.
    """
    m = _VERSION_RE.match((v or "").strip())
    if not m:
        return (0, 0, 0, 1, ())
    major = int(m.group(1) or 0)
    minor = int(m.group(2) or 0)
    patch = int(m.group(3) or 0)
    pre = m.group(4) or ""
    if pre:
        return (major, minor, patch, 0, _pre_key(pre))
    return (major, minor, patch, 1, ())


def version_newer(latest: str, current: str) -> bool:
    return _version_key(latest) > _version_key(current)


def bundle_kind() -> str:
    """Detect how this instance was packaged so we can pick the right asset."""
    if os.environ.get("APPIMAGE"):
        return "appimage"
    if sys.platform == "win32":
        if not getattr(sys, "frozen", False):
            return "source"
        exe_str = str(Path(sys.executable).resolve())
        if "Program Files" in exe_str or r"AppData\Local" in exe_str:
            return "win-setup"
        return "win-portable"
    if sys.platform.startswith("linux") and getattr(sys, "frozen", False):
        return "deb"
    return "source"


def preferred_asset(kind: str, assets: list[dict]) -> dict | None:
    def first_match(predicate) -> dict | None:
        return next((a for a in assets if predicate(a["name"].lower())), None)

    if kind == "appimage":
        return first_match(lambda n: n.endswith(".appimage"))
    if kind == "deb":
        return first_match(lambda n: n.endswith(".deb"))
    if kind == "win-setup":
        return first_match(lambda n: "setup" in n and n.endswith(".exe"))
    if kind == "win-portable":
        return first_match(lambda n: "portable" in n and n.endswith(".exe"))
    return None


def find_checksum_asset(asset_name: str, assets: list[dict]) -> dict | None:
    """Locate a SHA-256 manifest in the release asset list.

    Recognises:
      * SHA256SUMS / SHA256SUMS.txt / checksums.txt   (multi-line manifests)
      * <asset_name>.sha256                           (single-file digest)
    Names are matched case-insensitively.
    """
    sibling = f"{asset_name}.sha256".lower()
    multi = {"sha256sums", "sha256sums.txt", "checksums.txt"}
    for a in assets:
        n = a["name"].lower()
        if n == sibling or n in multi:
            return a
    return None


def parse_sha256_manifest(text: str, target_name: str) -> str | None:
    """Find target_name's hex digest in a SHA256SUMS-style manifest.

    Tolerates lines like:
        <hex>  filename
        <hex> *filename
        <hex>=filename
    Returns the lowercase digest string or None if not found / malformed.
    """
    target = target_name.strip()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Single-file format: just <hex>. Only a 64-char digest is SHA-256;
        # 40/56 would be SHA-1/SHA-224 from a misnamed manifest.
        if " " not in line and "=" not in line and len(line) == 64:
            return line.lower()
        # Multi-file: <hex>  name  (two-space classic, or single-space, or = name)
        digest, _, rest = line.partition(" ")
        if not rest:
            digest, _, rest = line.partition("=")
        name = rest.strip().lstrip("*").strip()
        if name == target and len(digest) == 64:
            return digest.lower()
    return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_text(url: str, repo: str, timeout: float = 8.0, iface: str = "") -> str | None:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": f"{repo.split('/')[-1]}-updater"},
    )
    try:
        with netiface.bound_urlopen(req, name=iface, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def fetch_latest_release(repo: str, timeout: float = 8.0, iface: str = "") -> dict | None:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"{repo.split('/')[-1]}-updater",
        },
    )
    try:
        with netiface.bound_urlopen(req, name=iface, timeout=timeout) as resp:
            return json.load(resp)
    except Exception:
        return None


class UpdateCheckWorker(QObject):
    updateAvailable = Signal(object)
    noUpdate = Signal()
    failed = Signal(str)

    def __init__(self, current_version: str, repo: str, iface: str = ""):
        super().__init__()
        self._current = current_version
        self._repo = repo
        self._iface = iface

    def run(self) -> None:
        # Every exit path must emit a signal: run() is driven by
        # thread.started, and an escaping exception would leave the thread
        # spinning and the controller's _thread reference set forever,
        # blocking all future update checks.
        try:
            self._run()
        except Exception as exc:
            self.failed.emit(f"malformed release data: {exc}")

    def _run(self) -> None:
        data = fetch_latest_release(self._repo, iface=self._iface)
        if data is None:
            self.failed.emit("could not reach the releases API")
            return
        tag = data.get("tag_name") or ""
        if not tag:
            self.failed.emit("release had no tag_name")
            return
        latest = tag.lstrip("vV")
        if not version_newer(latest, self._current):
            self.noUpdate.emit()
            return
        assets = data.get("assets") or []
        asset = preferred_asset(bundle_kind(), assets)
        checksum = find_checksum_asset(asset["name"], assets) if asset else None
        info = UpdateInfo(
            latest_version=latest,
            release_url=(
                data.get("html_url")
                or f"https://github.com/{self._repo}/releases/tag/{tag}"
            ),
            asset_name=asset["name"] if asset else None,
            asset_url=asset["browser_download_url"] if asset else None,
            asset_size=int(asset["size"]) if asset else 0,
            checksum_name=checksum["name"] if checksum else None,
            checksum_url=checksum["browser_download_url"] if checksum else None,
        )
        self.updateAvailable.emit(info)


class DownloadWorker(QObject):
    """Fetches the release manifest, then the asset, on one worker thread.

    The manifest used to be fetched synchronously from the GUI thread, which
    froze the whole interface for the length of its timeout and - because it
    took a different code path from the asset download - ignored the user's
    configured network interface. Both requests now go through this one
    interface-aware worker, so they cannot diverge.
    """

    progress = Signal(int)
    finished = Signal(str)
    failed = Signal(str)
    # The release does not ship a usable digest: "unreachable" (the manifest
    # could not be fetched) or "no_digest" (it holds none for our asset).
    # Reported separately from `failed` because neither is a download error and
    # each has its own user-facing outcome.
    manifestFailed = Signal(str)
    digestResolved = Signal(str)

    def __init__(
        self,
        url: str,
        dest: Path,
        repo: str,
        iface: str = "",
        checksum_url: str = "",
        asset_name: str = "",
    ):
        super().__init__()
        self._url = url
        self._dest = dest
        self._repo = repo
        self._iface = iface
        self._checksum_url = checksum_url
        self._asset_name = asset_name
        # Shared state, not a queued slot call: this worker's thread is busy
        # inside run() for the whole transfer, so anything delivered through
        # its event queue could not be seen until the transfer had finished -
        # which is precisely when cancelling stops being useful.
        self._cancel = threading.Event()
        # The response currently being read, so cancel() can close it. Setting
        # the event alone only takes effect between reads; a worker blocked
        # inside one - waiting on a stalled manifest or a dead connection -
        # would otherwise ignore Cancel for the whole socket timeout.
        self._active_response = None
        self._response_lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self) -> None:
        self._cancel.set()
        self._interrupt_active_response()

    def _interrupt_active_response(self) -> None:
        """Break the worker out of a blocked read, from the GUI thread.

        `close()` cannot do this job here. It takes the buffered reader's lock,
        which the worker holds for the whole of a read - so calling it from the
        GUI thread freezes the interface until that read returns or the socket
        times out, which is 8 or 20 seconds of exactly the hang Cancel exists
        to avoid. `shutdown()` takes no lock and is what actually makes the
        blocked read return.

        The private attribute walk is deliberate: urllib exposes no supported
        route to the socket, and every step is guarded so a stack that does not
        match simply falls through to the close below.
        """
        with self._response_lock:
            response, self._active_response = self._active_response, None
        if response is None:
            return
        try:
            raw = getattr(getattr(response, "fp", None), "raw", None)
            sock = getattr(raw, "_sock", None)
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        # The close can still block behind the read the shutdown just broke,
        # so it never runs on the caller's thread.
        threading.Thread(
            target=self._quietly_close, args=(response,), daemon=True
        ).start()

    @staticmethod
    def _quietly_close(response) -> None:
        try:
            response.close()
        except Exception:
            pass

    def _close_active_response(self) -> None:
        """Release the response from the worker thread, where blocking is fine."""
        with self._response_lock:
            response, self._active_response = self._active_response, None
        if response is not None:
            self._quietly_close(response)

    def _open(self, url: str, timeout: float):
        """Open a request, refusing to start one that is already cancelled.

        Note the residual gap: once `bound_urlopen` is entered, a cancel
        arriving during DNS, connect or TLS cannot interrupt it - there is no
        socket to close yet - so it takes effect when the socket timeout
        expires. Closing that gap means replacing urllib with a stack that
        exposes the socket during establishment, which is more machinery than
        a Cancel button warrants. Every phase is bounded by `timeout`.
        """
        if self._cancel.is_set():
            raise RuntimeError("cancelled")
        req = urllib.request.Request(
            url, headers={"User-Agent": f"{self._repo.split('/')[-1]}-updater"},
        )
        return self._track(
            netiface.bound_urlopen(req, name=self._iface, timeout=timeout)
        )

    def _track(self, response):
        """Register a response so cancel() can interrupt a read on it."""
        with self._response_lock:
            if self._cancel.is_set():
                response.close()
                raise RuntimeError("cancelled")
            self._active_response = response
        return response

    def run(self) -> None:
        if self._checksum_url and not self._resolve_digest():
            return
        try:
            if self._cancel.is_set():
                raise RuntimeError("cancelled")
            with self._open(self._url, 20) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                written = 0
                self._dest.parent.mkdir(parents=True, exist_ok=True)
                with open(self._dest, "wb") as f:
                    while True:
                        if self._cancel.is_set():
                            raise RuntimeError("cancelled")
                        chunk = resp.read(262144)
                        if not chunk:
                            break
                        f.write(chunk)
                        written += len(chunk)
                        if total > 0:
                            self.progress.emit(int(written * 100 / total))
            if self._cancel.is_set():
                # A Cancel that lands during the final read finds the loop
                # already leaving through its normal exit, with nothing left to
                # interrupt. Without this the worker reports success and the
                # controller installs the update the user just called off.
                raise RuntimeError("cancelled")
            self.finished.emit(str(self._dest))
        except Exception as exc:
            try:
                self._dest.unlink(missing_ok=True)
            except Exception:
                pass
            self.failed.emit(str(exc))
        finally:
            self._close_active_response()

    def _fetch_manifest(self) -> str | None:
        """The checksum manifest, read so that Cancel can interrupt it.

        Deliberately not `fetch_text()`: that owns its response, so a Cancel
        arriving mid-request could not be acted on until the socket timeout
        expired. Same interface binding and headers, just a response this
        worker can close from the GUI thread.
        """
        response = self._open(self._checksum_url, 8.0)
        try:
            return response.read().decode("utf-8", errors="replace")
        finally:
            self._close_active_response()

    def _resolve_digest(self) -> bool:
        """Recover this asset's expected SHA-256 before transferring anything.

        Returns False when the release cannot be verified, having reported why.
        Nothing is downloaded in that case: an unverifiable binary is one Cove
        would refuse to install anyway.
        """
        try:
            manifest = self._fetch_manifest()
        except Exception:
            manifest = None
        if self._cancel.is_set():
            self.failed.emit("cancelled")
            return False
        if manifest is None:
            self.manifestFailed.emit("unreachable")
            return False
        digest = parse_sha256_manifest(manifest, self._asset_name)
        if not digest:
            self.manifestFailed.emit("no_digest")
            return False
        self.digestResolved.emit(digest)
        return True


def swap_in_appimage(new_path: Path) -> Path:
    """Install `new_path` next to the running AppImage under its own
    versioned filename, remove the old file, and return the new path.

    Keeping the release asset's filename (instead of overwriting the old
    file in place) matches electron-updater semantics and keeps the
    on-disk name truthful - external launchers like Cove Nexus derive the
    installed version from it."""
    current = os.environ.get("APPIMAGE")
    if not current:
        raise RuntimeError("APPIMAGE env var not set - not an AppImage install")
    old = Path(current).resolve()
    target = old.parent / new_path.name
    tmp = target.with_name(target.name + ".part")
    try:
        shutil.move(str(new_path), str(tmp))
        mode = os.stat(tmp).st_mode
        os.chmod(tmp, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(tmp, target)
    except Exception:
        # A cross-filesystem move that dies midway leaves a partial .part.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    if target != old:
        try:
            old.unlink()  # unlinking the running file is fine on Linux
        except OSError:
            pass
    os.environ["APPIMAGE"] = str(target)
    return target


def relaunch(path: Path) -> None:
    subprocess.Popen(
        [str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


class UpdateController(QObject):
    """Attach to a QMainWindow. Call .check() to kick off a background poll;
    on a newer release it drives the prompt → download → swap → relaunch flow."""

    def __init__(
        self,
        parent,
        current_version: str,
        repo: str,
        app_display_name: str,
        cache_subdir: str,
        iface: str = "",
    ):
        super().__init__(parent)
        self._parent = parent
        self._current = current_version
        self._repo = repo
        self._display_name = app_display_name
        self._cache_subdir = cache_subdir
        self._iface = iface
        self._thread: QThread | None = None
        self._worker: UpdateCheckWorker | None = None
        self._download_thread: QThread | None = None
        self._download_worker: DownloadWorker | None = None
        self._progress: QProgressDialog | None = None
        self._prompt_shown = False
        self._expected_digest: str | None = None
        self._pending_info: UpdateInfo | None = None

    def check(self) -> None:
        if self._thread is not None:
            return
        thread = QThread(self)
        worker = UpdateCheckWorker(self._current, self._repo, iface=self._iface)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.updateAvailable.connect(thread.quit)
        worker.noUpdate.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.updateAvailable.connect(self._on_update_available, Qt.QueuedConnection)
        thread.finished.connect(self._on_check_done, Qt.QueuedConnection)
        self._thread = thread
        self._worker = worker
        thread.start()

    def _on_check_done(self) -> None:
        self._thread = None
        self._worker = None

    def _on_update_available(self, info: UpdateInfo) -> None:
        if self._prompt_shown:
            return
        self._prompt_shown = True
        self._prompt(info)

    def _prompt(self, info: UpdateInfo) -> None:
        kind = bundle_kind()
        can_auto_install = kind == "appimage" and bool(info.asset_url)

        msg = QMessageBox(self._parent)
        msg.setIcon(QMessageBox.Information)
        msg.setWindowTitle(f"{self._display_name} — update available")
        msg.setText(
            f"{self._display_name} v{info.latest_version} is available.\n"
            f"You're running v{self._current}.",
        )
        if can_auto_install:
            mb = info.asset_size // (1024 * 1024) if info.asset_size else 0
            msg.setInformativeText(
                f"{info.asset_name}{f' ({mb} MB)' if mb else ''}. "
                "The app will restart after the update.",
            )
            install_btn = msg.addButton("Update now", QMessageBox.AcceptRole)
            open_btn = msg.addButton("View release", QMessageBox.HelpRole)
            msg.addButton("Later", QMessageBox.RejectRole)
        else:
            msg.setInformativeText(
                "Open the release page to download the latest installer.",
            )
            install_btn = None
            open_btn = msg.addButton("View release", QMessageBox.AcceptRole)
            msg.addButton("Later", QMessageBox.RejectRole)
        msg.exec()
        clicked = msg.clickedButton()
        if install_btn is not None and clicked is install_btn:
            self._install(info)
        elif open_btn is not None and clicked is open_btn:
            _open_url(info.release_url)

    def _install(self, info: UpdateInfo) -> None:
        if not info.asset_url or not info.asset_name:
            _open_url(info.release_url)
            return

        # Refuse to install anything we can't verify. If the release
        # doesn't ship a SHA-256 manifest, send the user to the page so
        # they can decide for themselves.
        if not info.checksum_url:
            QMessageBox.warning(
                self._parent,
                "Update needs manual verification",
                f"This release of {self._display_name} doesn't include a "
                f"SHA256SUMS file, so Cove can't auto-install it. Opening "
                f"the release page so you can download it manually.",
            )
            _open_url(info.release_url)
            return

        cache_root = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
        cache = Path(cache_root) / self._cache_subdir
        cache.mkdir(parents=True, exist_ok=True)
        dest = cache / info.asset_name
        self._pending_info = info

        # The manifest is fetched by the worker, not here: doing it on this
        # thread froze the window for the length of its timeout and bypassed
        # the configured network interface. The digest arrives via
        # digestResolved before any bytes of the asset are written.
        self._expected_digest = None

        self._progress = QProgressDialog(
            f"Downloading {info.asset_name}…", "Cancel", 0, 100, self._parent,
        )
        self._progress.setWindowTitle(f"Updating {self._display_name}")
        self._progress.setAutoClose(False)
        self._progress.setAutoReset(False)
        self._progress.setMinimumDuration(0)
        self._progress.setValue(0)

        thread = QThread(self)
        worker = DownloadWorker(
            info.asset_url, dest, self._repo, iface=self._iface,
            checksum_url=info.checksum_url, asset_name=info.asset_name,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.manifestFailed.connect(thread.quit)
        # Direct, deliberately: the worker owns `thread`, whose event loop is
        # occupied by run() for the whole transfer. A queued call would sit
        # behind the very download it is meant to interrupt. cancel() only
        # sets a threading.Event, which is safe to touch from this thread.
        self._progress.canceled.connect(worker.cancel, Qt.DirectConnection)
        worker.digestResolved.connect(self._on_digest_resolved, Qt.QueuedConnection)
        worker.manifestFailed.connect(self._on_manifest_failed, Qt.QueuedConnection)
        worker.progress.connect(self._progress.setValue, Qt.QueuedConnection)
        worker.finished.connect(self._on_downloaded, Qt.QueuedConnection)
        worker.failed.connect(self._on_download_failed, Qt.QueuedConnection)
        thread.finished.connect(self._on_download_thread_done, Qt.QueuedConnection)
        self._download_thread = thread
        self._download_worker = worker
        thread.start()

    def _on_digest_resolved(self, digest: str) -> None:
        self._expected_digest = digest

    def _on_manifest_failed(self, reason: str) -> None:
        """The release cannot be verified, so nothing was downloaded."""
        if self._progress is not None:
            self._progress.close()
        info = self._pending_info
        if reason == "no_digest":
            QMessageBox.warning(
                self._parent,
                "Update aborted",
                f"The release manifest doesn't contain a digest for "
                f"{info.asset_name if info else 'this release'}. Cove won't "
                f"install unverified binaries.",
            )
            if info is not None:
                _open_url(info.release_url)
            return
        QMessageBox.warning(
            self._parent,
            "Update aborted",
            "Couldn't download the checksum manifest. Try again later.",
        )

    def _on_downloaded(self, path: str) -> None:
        if self._progress is not None:
            self._progress.close()
        downloaded = Path(path)

        # Second gate on cancellation, at the boundary that matters: past here
        # the executable is replaced and Cove relaunches. The worker checks
        # too, but this signal is queued, so a Cancel can arrive after it was
        # emitted and before it is delivered.
        worker = self._download_worker
        if worker is not None and worker.cancelled:
            try:
                downloaded.unlink(missing_ok=True)
            except Exception:
                pass
            return

        # Integrity gate. The asset must hash to the digest we recovered
        # from the release's SHA256SUMS manifest, or we delete it and bail.
        expected = (self._expected_digest or "").lower()
        if not expected:
            try: downloaded.unlink(missing_ok=True)
            except Exception: pass
            QMessageBox.warning(
                self._parent,
                "Update failed",
                "Lost the expected digest before verification — aborting.",
            )
            return
        try:
            actual = sha256_file(downloaded)
        except Exception as exc:
            QMessageBox.warning(
                self._parent,
                "Update failed",
                f"Couldn't read the downloaded file for hashing:\n{exc}",
            )
            return
        if actual != expected:
            try: downloaded.unlink(missing_ok=True)
            except Exception: pass
            QMessageBox.critical(
                self._parent,
                "Update rejected",
                "The downloaded AppImage didn't match the expected SHA-256 "
                "from the release manifest, so Cove deleted it and won't "
                "install it.\n\n"
                f"expected: {expected}\nactual:   {actual}",
            )
            return

        try:
            new_path = swap_in_appimage(downloaded)
        except Exception as exc:
            QMessageBox.warning(
                self._parent,
                "Update failed",
                f"Couldn't swap in the new AppImage:\n{exc}",
            )
            return
        relaunch(new_path)
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _on_download_failed(self, msg: str) -> None:
        if self._progress is not None:
            self._progress.close()
        worker = self._download_worker
        if worker is not None and worker.cancelled:
            # User-initiated cancel is not a failure; no error dialog.
            return
        QMessageBox.warning(
            self._parent,
            "Update failed",
            f"The download didn't complete:\n{msg}",
        )

    def _on_download_thread_done(self) -> None:
        self._download_thread = None
        self._download_worker = None
