"""Cove Download Manager bootstrap.

Order:
  1. QApplication + cove theme + window icon
  2. Settings, Scheduler, MainWindow (window is shown immediately so any
     subsequent error dialogs have a real top-level parent - Wayland +
     QMessageBox(None, ...) crashes on some systems)
  3. Aria2 daemon start (deferred via QTimer). On failure, show an error
     parented to the main window and disable user actions.
"""
from __future__ import annotations

import sys
import logging
import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QIcon, QPalette, QColor
from PySide6.QtWidgets import QApplication, QMessageBox

from . import APP_NAME, __version__, theme
from .aria2 import Aria2Daemon, Aria2Error, Aria2InterfaceError, Aria2RPC
from .api_server import LocalApiServer
from .config import CONFIG_FILE, Settings
from .main_window import MainWindow
from .queue import QueueManager
from .scheduler import Scheduler
from .single_instance import (
    MAX_URLS_PER_MESSAGE,
    SingleInstanceServer,
    is_valid_launch_url,
    send_to_primary,
    server_name,
)
from .updater import UpdateController
from .browser_extension import setup_failure_text
from .native_host_install import install_native_hosts
from .widgets import find_icon

UPDATE_REPO = "Sin213/cove-download-manager"


def parse_launch_urls(argv: list[str]) -> list[str]:
    """Extract magnet URIs from GUI launch arguments.

    Uses the exact same per-URL validation policy as the IPC `open` message
    path (`single_instance.is_valid_launch_url` - length bound, control-
    character rejection, magnet-prefix gate), so the same magnet is
    accepted or rejected identically whether it arrives on the command line
    of a fresh launch or over IPC to an already-running primary - a
    malformed/oversized/control-character magnet is silently dropped
    before it can enter the startup inbox, not just later when drained into
    the queue. Anything else that isn't launch-URL-shaped (flags, file
    paths, other schemes) is likewise silently ignored rather than
    rejected with an error, matching how a double-clicked association
    normally behaves.
    """
    urls: list[str] = []
    for arg in argv[1:]:
        if is_valid_launch_url(arg):
            if len(urls) >= MAX_URLS_PER_MESSAGE:
                break
            urls.append(arg)
    return urls


class BrowserDownloadGate:
    """Decides whether *this* process may accept a browser download.

    The browser extension cancels its own transfer the moment the native host
    reports success, so success has to mean "the Cove running right now took
    it". This gate is the single place that decides that, and it answers False
    for every condition under which the download would otherwise be lost or
    deferred:

      * the queue does not exist yet, or aria2 has not finished starting
        (`ready` is set only after `resume_persisted()`), so an early request
        is refused rather than buffered - unlike command-line/IPC magnets,
        which the user explicitly asked for and which do get a startup inbox;
      * shutdown has begun, so nothing accepted now would be driven to
        completion;
      * `add_url` rejected the URL or raised.

    Nothing is written anywhere on failure, so a later launch can never
    inherit the request. The add itself is deliberately non-interactive (no
    duplicate dialog), matching how automatically captured browser downloads
    have always been added.
    """

    def __init__(self) -> None:
        self.queue = None
        self.ready = False
        self.shutting_down = False

    def accept(self, request: dict) -> bool:
        if self.shutting_down or not self.ready or self.queue is None:
            return False
        try:
            task_id = self.queue.add_url(
                request["url"],
                out_dir=request.get("directory") or None,
                filename=request.get("filename"),
                cookies=request.get("cookies") or "",
                referrer=request.get("referrer") or "",
                user_agent=request.get("user_agent") or "",
            )
        except Exception:
            # Fixed event name only - the request carries a URL, cookies and
            # a referrer, none of which may reach the log.
            logging.getLogger("cove").warning("browser_download_add_failed")
            return False
        return task_id is not None


class NativeHostRegistration(QObject):
    """Registers the native messaging host and reports a failure to the GUI.

    Registration runs off the GUI thread (flatpak can take ~10s per browser),
    so the outcome comes back as a signal rather than a direct widget call.
    Failure is non-fatal - Cove still runs without the extension - but it is
    the usual "the extension cannot connect" root cause, so it must not stay
    log-only.
    """

    failed = Signal(str)

    def run(self) -> bool:
        try:
            install_native_hosts()
        except Exception as exc:
            logging.getLogger("cove").warning(
                "native messaging host registration failed", exc_info=True
            )
            self.failed.emit(setup_failure_text(exc))
            return False
        return True


def activate_window(window) -> None:
    """Best-effort bring-to-front. Never raises - a compositor that refuses
    focus must not block the magnet that triggered the activation."""
    try:
        if window.isMinimized():
            window.showNormal()
        window.show()
        window.raise_()
        window.activateWindow()
    except Exception:
        pass


def _apply_palette(app: QApplication) -> None:
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(theme.BG))
    pal.setColor(QPalette.WindowText, QColor(theme.TEXT))
    pal.setColor(QPalette.Base, QColor(theme.BG))
    pal.setColor(QPalette.AlternateBase, QColor(theme.SURFACE_2))
    pal.setColor(QPalette.Text, QColor(theme.TEXT))
    pal.setColor(QPalette.ToolTipBase, QColor(theme.SURFACE_2))
    pal.setColor(QPalette.ToolTipText, QColor(theme.TEXT))
    pal.setColor(QPalette.Highlight, QColor(theme.ACCENT))
    pal.setColor(QPalette.HighlightedText, QColor(theme.ACCENT_INK))
    app.setPalette(pal)


def apply_theme(app: QApplication, name: str) -> None:
    """Switch to `name` ("dark"|"light"), rebuild QSS, refresh palette,
    and re-polish every top-level widget so child widgets pick up the
    new property values."""
    theme.set_theme(name)
    _apply_palette(app)
    app.setStyleSheet(theme.QSS)
    for w in app.allWidgets():
        w.style().unpolish(w)
        w.style().polish(w)
        w.update()


def run() -> int:
    # Safety net: never open the GUI when launched as a native messaging
    # host. A browser respawns the host on failure, so a GUI here loops into
    # endless windows. Primary dispatch is cove.entry; this guards any direct
    # caller of run() too.
    from .entry import NATIVE_MESSAGING_FLAG

    if NATIVE_MESSAGING_FLAG in sys.argv:
        from .native_messaging import main as nm_main
        nm_main()
        return 0

    QApplication.setAttribute(Qt.AA_DontUseNativeDialogs, False)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("cove")

    launch_urls = parse_launch_urls(sys.argv)

    from .config import DATA_DIR

    instance_name = server_name(DATA_DIR)
    instance_server = SingleInstanceServer()
    if not instance_server.try_become_primary(instance_name):
        # Not primary. Forward and exit before touching Settings, aria2, the
        # queue, or the window - a second aria2 daemon fights the first one
        # for the same RPC port.
        sent = send_to_primary(instance_name, launch_urls)
        if not sent:
            sent = send_to_primary(instance_name, launch_urls)  # one bounded retry
        if not sent:
            logging.getLogger("cove").error(
                "single_instance_forward_failed: could not reach the running Cove instance"
            )
            return 1
        return 0

    # We are primary and already listening, but nothing pumps the Qt event
    # loop until app.exec() at the very end of this function - a secondary
    # that connects during the synchronous construction below would sit
    # unacknowledged and could time out even though we are alive. Wire up
    # the IPC handlers and a few processEvents() calls through construction
    # so a racing secondary still gets a prompt ack, without deferring
    # construction itself into the event loop.
    #
    # `window` and `queue` are assigned further down in this same function
    # scope; `_handle_open`/`_handle_activate`/`_drain_startup_inbox` close
    # over them and read whatever value each name holds *at call time* (not
    # at definition time), so declaring them `None` here and guarding each
    # use is enough to make an early-arriving IPC request safe: it still
    # buffers into `startup_inbox` and gets acked, it just can't raise the
    # window or touch the queue until those objects actually exist.
    queue = None
    window = None

    # Browser downloads are delivered synchronously to this process and are
    # never buffered: the extension cancels the browser's own transfer on the
    # strength of our answer, so an answer we can't honour right now would
    # lose the download. Installed before anything else so a request arriving
    # mid-startup is refused (browser keeps it) rather than racing an
    # uninitialised queue.
    browser_gate = BrowserDownloadGate()
    instance_server.browser_download_handler = browser_gate.accept

    # Magnets that arrive before aria2 is up (command line or IPC) are
    # buffered here and drained exactly once, in arrival order, after
    # daemon.start() succeeds and the persisted queue has resumed.
    startup_inbox: list[str] = list(launch_urls)
    queue_ready = False

    def _add_interactive(url: str) -> None:
        """Add one externally-delivered magnet the way the GUI would.

        A command-line or second-instance magnet is as user-initiated as
        one typed into the Add dialog, so it goes through the window's
        duplicate check whenever the window exists. Before it does (an IPC
        request that beats the GUI up), the queue is all there is.
        """
        if window is not None:
            window.add_url_interactive(url)
        else:
            queue.add_url(url)

    def _drain_startup_inbox() -> None:
        nonlocal queue_ready
        queue_ready = True
        # Same moment for the browser path: only now can an add actually be
        # driven to completion, so only now may we tell a browser we took one.
        browser_gate.ready = True
        pending, startup_inbox[:] = list(startup_inbox), []
        for url in pending:
            try:
                _add_interactive(url)
            except Exception:
                logging.getLogger("cove").warning("startup_inbox_add_failed")

    def _handle_open(urls: list[str]) -> None:
        if queue_ready and queue is not None:
            for url in urls:
                try:
                    _add_interactive(url)
                except Exception:
                    logging.getLogger("cove").warning("ipc_add_failed")
        else:
            startup_inbox.extend(urls)
        if window is not None:
            activate_window(window)

    def _handle_activate() -> None:
        if window is not None:
            activate_window(window)

    instance_server.open_requested.connect(_handle_open)
    instance_server.activate_requested.connect(_handle_activate)
    app.processEvents()

    icon_path = find_icon()
    if icon_path:
        app.setWindowIcon(QIcon(str(icon_path)))

    try:
        settings = Settings.load()
    except OSError as e:
        # settings.json exists but could not be read - typically a backup or
        # antivirus agent holding it open past the sharing-retry window on
        # Windows, or an ACL that denies us. Settings.load() deliberately fails
        # closed here instead of falling back to defaults, because that would
        # rotate rpc_secret and api_token and discard every stored setting.
        # Exiting keeps the file intact and recoverable.
        #
        # No window exists yet and QMessageBox(None, ...) crashes on some
        # Wayland systems (see module docstring), so report this the way the
        # other pre-window failures above do.
        logging.getLogger("cove").error(
            "settings_unreadable: could not read %s (%s). Cove stopped rather "
            "than resetting it. Close whatever is holding the file open, or fix "
            "its permissions, then start Cove again.",
            CONFIG_FILE,
            e,
        )
        return 1
    app.processEvents()

    theme.set_theme(settings.theme)
    _apply_palette(app)
    app.setStyleSheet(theme.QSS)

    daemon = Aria2Daemon(settings)
    rpc = Aria2RPC(settings)
    queue = QueueManager(settings, rpc)
    browser_gate.queue = queue
    scheduler = Scheduler(settings.schedule)
    app.processEvents()

    window = MainWindow(settings, queue, scheduler)
    api_server = LocalApiServer(settings, queue)
    window._single_instance_server = instance_server  # keep a strong reference
    # Presence only: a heartbeat or a forwarded download proves the extension
    # is installed and can reach this process. Connected after the window
    # exists, so an early ping simply leaves the indicator at "not detected".
    instance_server.extension_seen.connect(window.note_extension_seen)
    app.processEvents()

    def _start_api_server() -> None:
        try:
            api_server.start()
        except (OSError, ValueError) as exc:
            message = f"Cove local API could not start on 127.0.0.1:{settings.api_port}: {exc}"
            logging.getLogger("cove").warning(message)
            queue.error.emit(message)

    def _on_theme_toggled(name: str) -> None:
        settings.theme = name
        settings.save()
        apply_theme(app, name)
        window.titlebar.theme_btn.set_theme(name)

    window.titlebar.theme_btn.toggled_theme.connect(_on_theme_toggled)

    window.show()
    app.processEvents()

    # Registration shells out to flatpak (up to 10 s per browser); run it
    # off the GUI thread so a slow or hung flatpak can't freeze the window.
    # The window keeps a strong reference: the thread outlives this scope.
    native_host_registration = NativeHostRegistration()
    native_host_registration.failed.connect(window.note_extension_setup_failed)
    window._native_host_registration = native_host_registration
    threading.Thread(
        target=native_host_registration.run, name="native-host-install", daemon=True
    ).start()

    # Same reasoning as the native-host thread above: registry and xdg-mime
    # work can block, and a magnet association must never delay the window.
    def _heal_magnet_handler() -> None:
        from .magnet_startup import migrate_and_repair

        migrate_and_repair(settings)

    threading.Thread(
        target=_heal_magnet_handler, name="magnet-self-heal", daemon=True
    ).start()

    def _boot_daemon() -> None:
        while True:
            try:
                daemon.start()
                break
            except Aria2InterfaceError as e:
                # A bound interface that is gone blocks every download, but
                # the window stays usable: disabling it would trap the user
                # with no way to reach the very setting at fault.
                box = QMessageBox(window)
                box.setIcon(QMessageBox.Warning)
                box.setWindowTitle(f"{APP_NAME} - network interface unavailable")
                box.setText(str(e))
                settings_button = box.addButton("Open Settings", QMessageBox.ActionRole)
                close_button = box.addButton("Close", QMessageBox.RejectRole)
                box.setDefaultButton(settings_button)
                box.setEscapeButton(close_button)
                box.exec()
                if box.clickedButton() is settings_button:
                    window._open_settings()
                    continue
                # Nothing was started, so no traffic leaves over another
                # interface. Downloads simply stay blocked until aria2 runs.
                return
            except Aria2Error as e:
                QMessageBox.critical(window, f"{APP_NAME} - aria2 missing", str(e))
                window.setEnabled(False)
                return
        # Apply the effective speed limit (kbps if the limiter is on, else 0).
        effective = settings.overall_speed_limit_kbps if settings.speed_limiter_enabled else 0
        try:
            rpc.set_overall_speed_limit_kbps(effective)
        except Aria2Error:
            pass
        # Now that aria2 is reachable, drive any persisted-queued tasks.
        queue.resume_persisted()
        # Only now drain command-line/IPC magnets buffered before aria2 was
        # ready. If daemon.start() raised above, we return before this and
        # the startup inbox is never drained into a broken daemon.
        _drain_startup_inbox()
        # Accept API downloads only once aria2 is up, so a request that
        # races app startup cannot be persisted straight into "error".
        if settings.api_enabled:
            _start_api_server()

    QTimer.singleShot(0, _boot_daemon)

    # Update check - opt-in by default, always prompts before installing.
    if settings.auto_update_check:
        updater = UpdateController(
            parent=window,
            current_version=__version__,
            repo=UPDATE_REPO,
            app_display_name=f"{APP_NAME} Download Manager",
            cache_subdir="cove-download-manager",
            iface=str(getattr(settings, "torrent_network_interface", "") or ""),
        )
        # Defer a few seconds so the window has fully painted before any
        # network or dialog work happens.
        QTimer.singleShot(4000, updater.check)
        window._updater = updater  # keep a reference

    def _cleanup() -> None:
        # Refuse browser downloads from the first instant of shutdown: an add
        # accepted now would never be driven to completion, and the browser
        # would already have cancelled its own copy.
        browser_gate.shutting_down = True
        # Stop UI repaint timers first so they don't fire on widgets being
        # torn down during shutdown.
        try:
            window.stop_ui_timers()
        except Exception:
            pass
        try:
            api_server.stop()
        except Exception:
            pass
        try:
            rpc.shutdown()
        except Exception:
            pass
        daemon.stop()
        instance_server.shutdown()

    app.aboutToQuit.connect(_cleanup)
    return app.exec()
