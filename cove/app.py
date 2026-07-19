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
from .aria2 import Aria2Daemon, Aria2Error, Aria2RPC
from .api_server import LocalApiServer
from .config import Settings
from .main_window import MainWindow
from .queue import QueueManager
from .scheduler import Scheduler
from .updater import UpdateController
from .native_host_install import install_native_hosts
from .widgets import find_icon

UPDATE_REPO = "Sin213/cove-download-manager"


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

    icon_path = find_icon()
    if icon_path:
        app.setWindowIcon(QIcon(str(icon_path)))

    settings = Settings.load()

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

    window = MainWindow(settings, queue, scheduler)
    api_server = LocalApiServer(settings, queue)

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
        try:
            daemon.start()
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

    app.aboutToQuit.connect(_cleanup)
    return app.exec()
