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

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication, QIcon, QPalette, QColor
from PySide6.QtWidgets import QApplication, QMessageBox

from . import APP_NAME, __version__, theme
from .aria2 import Aria2Daemon, Aria2Error, Aria2InterfaceError, Aria2RPC
from .api_server import LocalApiServer
from .config import Settings
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

    # Magnets that arrive before aria2 is up (command line or IPC) are
    # buffered here and drained exactly once, in arrival order, after
    # daemon.start() succeeds and the persisted queue has resumed.
    startup_inbox: list[str] = list(launch_urls)
    queue_ready = False

    def _drain_startup_inbox() -> None:
        nonlocal queue_ready
        queue_ready = True
        pending, startup_inbox[:] = list(startup_inbox), []
        for url in pending:
            try:
                queue.add_url(url)
            except Exception:
                logging.getLogger("cove").warning("startup_inbox_add_failed")

    def _handle_open(urls: list[str]) -> None:
        if queue_ready and queue is not None:
            for url in urls:
                try:
                    queue.add_url(url)
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

    settings = Settings.load()
    app.processEvents()

    def _register_native_hosts() -> None:
        try:
            install_native_hosts()
        except Exception:
            # Non-fatal (the app still runs without the extension), but don't
            # swallow it silently — this is the usual "extension can't connect"
            # root cause.
            logging.getLogger("cove").warning(
                "native messaging host registration failed", exc_info=True
            )

    theme.set_theme(settings.theme)
    _apply_palette(app)
    app.setStyleSheet(theme.QSS)

    daemon = Aria2Daemon(settings)
    rpc = Aria2RPC(settings)
    queue = QueueManager(settings, rpc)
    scheduler = Scheduler(settings.schedule)
    app.processEvents()

    window = MainWindow(settings, queue, scheduler)
    api_server = LocalApiServer(settings, queue)
    window._single_instance_server = instance_server  # keep a strong reference
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
    threading.Thread(
        target=_register_native_hosts, name="native-host-install", daemon=True
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
