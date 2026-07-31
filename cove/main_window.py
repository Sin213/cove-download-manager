"""Cove Download Manager main window.

Layout matches the cove-screen-recorder shell:
    * frameless QMainWindow + custom Titlebar
    * Hero (h1 + subtitle + status pill)
    * StatsStrip
    * Two columns: downloads list (stage) | controls (panel)
    * Bottom action bar (Add, Add From Clipboard, Pause/Start Queue, ...)
    * Footer with hotkey hints + platform tag
"""
from __future__ import annotations

import os
import platform as _platform
import shutil
import subprocess
import sys
from math import ceil
from pathlib import Path

from PySide6.QtCore import QEvent, QMimeData, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QAction,
    QColor,
    QDrag,
    QDragEnterEvent,
    QDropEvent,
    QFont,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPainter,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizeGrip,
    QSizePolicy,
    QSpinBox,
    QSystemTrayIcon,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import APP_NAME, __version__, dedup, magnet_handler, theme
from .clipboard import extract_urls
from .config import Settings
from .dialogs import (
    AddDownloadDialog,
    ClipboardBatchDialog,
    SchedulerDialog,
    SettingsDialog,
    SourceDetailsDialog,
    torrent_file_problem,
)
from .queue import PHASE_METADATA, DownloadTask, QueueManager
from .scheduler import Scheduler
from .speed_limit import (
    SPEED_LIMIT_UNITS,
    configure_speed_spin,
    speed_value_to_kbps,
)
from .system_open import child_env
from .widgets import (
    Footer,
    FramelessResizer,
    Section,
    StatsStrip,
    StatusPill,
    Titlebar,
    _hex_to_bits,
    find_icon,
)

# Tree column indices.
COL_NAME = 0
COL_STATUS = 1
COL_PROGRESS = 2
COL_SIZE = 3
COL_SPEED = 4


def torrent_drop_paths(local_paths, enabled: bool) -> list[str]:
    """Local `.torrent` files Cove will accept from a drag-and-drop.

    Local files are otherwise ignored entirely by the drop handler; a
    `.torrent` is the single exception, and only while torrent support is
    switched on. Directories and every other local file still fail
    torrent_file_problem and stay ignored.
    """
    if not enabled:
        return []
    return [p for p in local_paths if p and not torrent_file_problem(p)]


# Shown once, immediately before Cove's first local BitTorrent transfer.
# Every sentence here is something Cove can actually stand behind: it does
# not claim anonymity, VPN protection or proxy coverage it cannot deliver.
P2P_DISCLOSURE_TITLE = "Torrent is not cached"
P2P_DISCLOSURE_TEXT = (
    "This torrent is not available through any enabled debrid service. Cove "
    "is configured to download uncached torrents directly through "
    "BitTorrent.\n\n"
    "Local BitTorrent exposes your IP address to peers and trackers. You "
    "can bind Cove to a VPN network interface or cancel uncached torrents "
    "under Settings → BitTorrent.\n\n"
    "Cove cannot verify that your VPN is active."
)


def build_p2p_consent_box(parent):
    """The uncached-torrent notice, its three buttons and its checkbox.

    Cancel is the default: a user who dismisses this dialog has not agreed
    to join a swarm. The checkbox is only ever honoured alongside the
    download button, so dismissing can never record consent either.

    Returns (box, download_button, settings_button, remember_checkbox).
    """
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle(P2P_DISCLOSURE_TITLE)
    box.setText(P2P_DISCLOSURE_TEXT)
    remember = QCheckBox("Don't show this notice again", box)
    box.setCheckBox(remember)
    settings_button = box.addButton("Open Settings", QMessageBox.ActionRole)
    cancel_button = box.addButton("Cancel download", QMessageBox.RejectRole)
    download_button = box.addButton("Download locally", QMessageBox.AcceptRole)
    box.setDefaultButton(cancel_button)
    box.setEscapeButton(cancel_button)
    return box, download_button, settings_button, remember


def _human_bytes(n: int) -> str:
    if n <= 0:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024:
            return f"{int(f)} {u}" if u == "B" else f"{f:.1f} {u}"
        f /= 1024
    return f"{f:.1f} PB"


def _human_speed(bps: int) -> str:
    if bps <= 0:
        return "—"
    return f"{_human_bytes(bps)}/s"


def _human_eta(seconds: int) -> str:
    if seconds <= 0:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours:02d}h"


def _speed_eta(task: DownloadTask, completed: int) -> str:
    if task.status != "active" or task.download_speed <= 0:
        return "—"
    speed = _human_speed(task.download_speed)
    if task.total_bytes <= 0:
        return speed
    remaining = max(0, task.total_bytes - completed)
    if remaining <= 0:
        return speed
    eta = _human_eta(ceil(remaining / task.download_speed))
    return f"{speed} · ETA {eta}"


def _human_cap(kbps: int) -> str:
    """Friendly speed-cap display: 'Off' / 'N KB/s' / 'X.Y MB/s'."""
    if kbps <= 0:
        return "Off"
    if kbps >= 1024:
        return f"{kbps / 1024:.1f} MB/s"
    return f"{kbps} KB/s"


def _truncate_path(p: str, max_chars: int = 36) -> str:
    """Shorten an absolute path for display: keep the last ~max_chars
    characters with a leading ellipsis. The full path goes in a tooltip."""
    home = str(Path.home())
    s = p
    if s.startswith(home):
        s = "~" + s[len(home):]
    if len(s) <= max_chars:
        return s
    return "…" + s[-(max_chars - 1):]


def _platform_label() -> str:
    sys = _platform.system()
    if sys != "Linux":
        return sys
    import os

    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if session == "wayland":
        return "Linux · Wayland"
    if session == "x11":
        return "Linux · X11"
    return "Linux"


def _open_path(path: Path) -> bool:
    """Open `path` with the OS default handler. Returns True on success."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
            return True
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
            return True
        # Linux / *BSD
        if shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", str(path)], env=child_env())
            return True
    except Exception:
        return False
    return False


def _reveal_in_folder(path: Path) -> bool:
    """Reveal `path` in the OS file manager (highlight it inside its
    parent folder). Falls back to opening the parent directory."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
            return True
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", str(path)])
            return True
        # Linux: most file managers don't support a portable "reveal" flag,
        # so open the containing directory. (DBus FileManager1 would work
        # but is not universal.) When `path` is itself a directory (rare:
        # someone right-clicked a folder), open it directly; otherwise
        # always hand xdg-open the parent — for in-progress downloads the
        # file may not exist on disk yet, but the parent does (the context
        # menu's enablement check guarantees that).
        target = path if path.is_dir() else path.parent
        if not target.exists():
            return False
        if shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", str(target)], env=child_env())
            return True
    except Exception:
        return False
    return False


class DownloadTree(QTreeWidget):
    """QTreeWidget that paints a centered placeholder when empty."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._empty_title = "No downloads yet"
        self._empty_sub = (
            "Press Ctrl+N to add a URL, or drop a link onto this window."
        )
        self._get_task = None  # set by MainWindow after construction
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragOnly)

    def startDrag(self, supported_actions):
        if not self._get_task:
            return
        file_paths = []
        for item in self.selectedItems():
            tid = item.data(0, Qt.UserRole)
            task = self._get_task(tid)
            if task and task.status == "completed" and task.filename:
                p = Path(task.out_dir) / task.filename
                if p.exists():
                    file_paths.append(p)
        if not file_paths:
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(p)) for p in file_paths])
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.topLevelItemCount() != 0:
            return
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = self.viewport().rect()
        title_color = QColor(theme.TEXT_DIM)
        sub_color = QColor(theme.TEXT_FAINT)

        title_font = self.font()
        title_font.setPointSizeF(12.0)
        title_font.setWeight(QFont.Medium)
        sub_font = self.font()
        sub_font.setPointSizeF(9.5)

        cy = rect.center().y() - 14
        p.setFont(title_font)
        p.setPen(title_color)
        title_rect = rect.adjusted(0, 0, 0, 0)
        title_rect.setHeight(rect.height())
        title_metrics = p.fontMetrics()
        tw = title_metrics.horizontalAdvance(self._empty_title)
        p.drawText(int(rect.center().x() - tw / 2), cy, self._empty_title)

        p.setFont(sub_font)
        p.setPen(sub_color)
        sub_metrics = p.fontMetrics()
        sw = sub_metrics.horizontalAdvance(self._empty_sub)
        p.drawText(int(rect.center().x() - sw / 2), cy + 24, self._empty_sub)
        p.end()


def _ask_magnet_offer(parent) -> bool:
    """Ask whether Cove should handle magnet links. True when accepted."""
    answer = QMessageBox.question(
        parent,
        "Magnet links",
        "Open magnet links with Cove from now on?\n\n"
        "You can change this later in Settings.",
        QMessageBox.Yes | QMessageBox.No,
        QMessageBox.Yes,
    )
    return answer == QMessageBox.Yes


class MainWindow(QMainWindow):
    def __init__(
        self,
        settings: Settings,
        queue: QueueManager,
        scheduler: Scheduler,
    ):
        super().__init__()
        self.settings = settings
        self.queue = queue
        self.scheduler = scheduler

        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setWindowTitle(f"{APP_NAME} Download Manager")
        icon_path = find_icon()
        if icon_path:
            self.setWindowIcon(QIcon(str(icon_path)))
        self.resize(1180, 720)
        self.setMinimumSize(880, 540)
        self._resizer = FramelessResizer(self)
        # Visible SE-corner resize grip so the user has a discoverable
        # affordance to grab. FramelessResizer handles invisible edge drag.
        self._size_grip = QSizeGrip(self)
        self._size_grip.setFixedSize(16, 16)
        self._size_grip.raise_()
        self.setAcceptDrops(True)

        self._build_ui()
        self._wire_signals()

        # The scheduler may have already settled into "outside window"
        # before we connected its signal — push the current state into
        # the queue so launches respect the schedule from boot, not just
        # from the next transition.
        self.queue.set_scheduler_allowed(self.scheduler.allowed)

        for tid in self.queue.tasks:
            self._on_task_added(tid)

        # 1 Hz: stats strip + status pill (low-frequency overview).
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._slow_tick)
        self._tick.start()
        # ~30 Hz: re-render only the active rows so progress bars
        # interpolate smoothly between aria2 status samples.
        self._smooth = QTimer(self)
        self._smooth.setInterval(33)
        self._smooth.timeout.connect(self._smooth_tick)
        self._smooth.start()
        self._refresh_stats()
        self._refresh_status_pill()
        self._refresh_schedule_section()

        # System tray for download-outcome notifications.
        self._tray: QSystemTrayIcon | None = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            icon = self.windowIcon()
            if icon.isNull():
                icon_path = find_icon()
                if icon_path:
                    icon = QIcon(str(icon_path))
            self._tray = QSystemTrayIcon(icon, self)
            self._tray.setToolTip(APP_NAME)
            self._tray.show()
        self._notified_status: dict[int, str] = {}
        # Set by an explicit Quit (tray menu or application quit) so a close
        # event raised during that quit is never turned back into a hide -
        # otherwise close-to-tray would trap the app in a hide loop.
        self._force_quit = False
        self._install_tray_menu()

    # ---- system tray ------------------------------------------------------

    def _system_tray_available(self) -> bool:
        """True only if this process owns a tray icon the platform still
        shows. Both halves matter: the icon may have been created at startup
        on a desktop whose tray has since gone away, and hiding the window
        with no icon to restore it from would strand the user."""
        return self._tray is not None and QSystemTrayIcon.isSystemTrayAvailable()

    def _install_tray_menu(self) -> None:
        """Attach Open/Quit to the existing notification tray icon.

        Reuses `self._tray` rather than creating a second QSystemTrayIcon -
        Cove has shown one for download notifications since long before
        close-to-tray existed, and a second icon would just duplicate it.
        Idempotent, so re-running it never installs a second menu.
        """
        if self._tray is None or getattr(self, "_tray_menu", None) is not None:
            return
        menu = QMenu(self)
        open_action = menu.addAction("Open Cove")
        open_action.triggered.connect(self.show_from_tray)
        menu.addSeparator()
        quit_action = menu.addAction("Quit Cove")
        quit_action.triggered.connect(self.request_quit)
        self._tray_menu = menu
        self._tray.setContextMenu(menu)
        # A plain click (or double-click, which some platforms send instead)
        # restores the existing window; neither ever constructs a new one.
        try:
            self._tray.activated.connect(self._on_tray_activated)
        except (AttributeError, TypeError):
            pass

    def _on_tray_activated(self, reason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_from_tray()

    def show_from_tray(self) -> None:
        """Restore and raise the one existing main window."""
        # Imported here, not at module scope: cove.app imports this
        # module, so a top-level import would be circular.
        from .app import activate_window

        activate_window(self)

    def request_quit(self) -> None:
        """Explicit Quit: leave for good, whatever close-to-tray says.

        Sets the bypass flag first so the close event Qt delivers while
        quitting cannot be intercepted back into a hide, drops the tray icon,
        then quits - which runs the app's single `aboutToQuit` cleanup
        (API server, aria2, queue timers, single-instance IPC) exactly once.
        Future browser downloads are then the browser's own business.
        """
        self._force_quit = True
        if self._tray is not None:
            self._tray.hide()
        QApplication.quit()

    def closeEvent(self, event) -> None:  # noqa: N802
        """Hide to the tray instead of exiting, only when opted in.

        Default (`close_to_tray` off), an explicit Quit, or a platform with
        no usable tray all fall through to the normal full shutdown, so X
        behaves exactly as it always has unless the user asked otherwise.
        Hiding runs no cleanup at all: aria2, the queue, the single-instance
        endpoint and browser interception must all keep working.
        """
        if (
            not self._force_quit
            and getattr(self.settings, "close_to_tray", False) is True
            and self._system_tray_available()
        ):
            event.ignore()
            self.hide()
            return
        if self._tray is not None:
            self._tray.hide()
        QMainWindow.closeEvent(self, event)

    # ---- UI construction ------------------------------------------------

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # Reposition the SE-corner QSizeGrip on every resize so it stays
        # pinned to the bottom-right of the window.
        s = self._size_grip.sizeHint()
        self._size_grip.move(self.width() - s.width(), self.height() - s.height())

    def _build_ui(self) -> None:
        chrome = QWidget()
        chrome.setObjectName("chrome")
        outer = QVBoxLayout(chrome)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Titlebar
        self.titlebar = Titlebar(
            self,
            f"{APP_NAME} Download Manager",
            __version__,
            theme_name=self.settings.theme,
        )
        outer.addWidget(self.titlebar)

        # Body
        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(28, 24, 28, 16)
        body_lay.setSpacing(18)

        # Hero — single tagline + status pill (titlebar already shows the
        # product name; a duplicate H1 here just steals vertical space).
        hero = QHBoxLayout()
        hero.setSpacing(16)
        sub = QLabel(
            "Multi-connection downloads with a queue, schedule, and a global speed cap."
        )
        sub.setProperty("role", "hero-sub")
        hero.addWidget(sub, 1, Qt.AlignVCenter)
        self.status_pill = StatusPill("Idle")
        hero.addWidget(self.status_pill, 0, Qt.AlignVCenter)
        body_lay.addLayout(hero)

        # Stats strip
        self.stats = StatsStrip()
        self.stats.add_cell("Active", "0")
        self.stats.add_cell("Queued", "0")
        self.stats.add_cell("Total", "—")
        self.stats.add_cell("Speed limit", "Off", last=True)
        body_lay.addWidget(self.stats)

        # Downloads label above the two-column area so left/right columns align.
        dl_label = QLabel("Downloads")
        dl_label.setProperty("role", "section-label")
        body_lay.addWidget(dl_label)

        # Two-column area
        cols = QHBoxLayout()
        cols.setSpacing(16)
        cols.addLayout(self._build_stage(), 7)
        cols.addLayout(self._build_panel(), 4)
        body_lay.addLayout(cols, 1)

        # Bottom action bar
        body_lay.addLayout(self._build_actionbar())

        outer.addWidget(body, 1)

        # Footer
        self.footer = Footer()
        self.footer.add_hotkey("Add", "Ctrl + N")
        self.footer.add_hotkey("Paste", "Ctrl + V")
        self.footer.add_hotkey("Pause/Resume", "Space")
        self.footer.add_hotkey("Toggle Queue", "Ctrl + P")
        self.footer.set_platform(_platform_label())
        self.footer.folder_clicked.connect(self._open_downloads_folder)
        outer.addWidget(self.footer)
        self._refresh_folder_chip()

        self.setCentralWidget(chrome)

        # Shortcuts
        self._add_shortcut("Ctrl+N", self._add_download)
        self._add_shortcut("Ctrl+Shift+V", self._add_from_clipboard)
        self._add_shortcut("Ctrl+P", self._toggle_queue)
        self._add_shortcut("Ctrl+V", self._paste_urls)
        self._add_shortcut("Ctrl+A", self.tree.selectAll)

    def _build_stage(self) -> QVBoxLayout:
        stage = QVBoxLayout()
        stage.setContentsMargins(0, 0, 0, 0)
        stage.setSpacing(0)

        self.tree = DownloadTree()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["Name", "Status", "Progress", "Size", "Speed"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._open_context_menu)

        header = self.tree.header()
        header.setStretchLastSection(False)
        for col in range(self.tree.columnCount()):
            header.setSectionResizeMode(col, QHeaderView.Interactive)
        header.resizeSection(COL_NAME, 380)
        header.resizeSection(COL_STATUS, 100)
        header.resizeSection(COL_PROGRESS, 220)
        header.resizeSection(COL_SIZE, 160)
        header.resizeSection(COL_SPEED, 160)
        stage.addWidget(self.tree, 1)
        self.tree._get_task = lambda tid: self.queue.tasks.get(tid)

        self._items: dict[int, QTreeWidgetItem] = {}
        self._bars: dict[int, QProgressBar] = {}

        # Delete key removes the selected rows. WidgetShortcut so it only
        # fires when the tree has focus.
        del_sc = QShortcut(QKeySequence(Qt.Key_Delete), self.tree)
        del_sc.setContext(Qt.WidgetShortcut)
        del_sc.activated.connect(lambda: self._remove_selected(delete_file=False))
        space_sc = QShortcut(QKeySequence(Qt.Key_Space), self.tree)
        space_sc.setContext(Qt.WidgetShortcut)
        space_sc.activated.connect(self._toggle_selected)
        return stage

    def _build_panel(self) -> QVBoxLayout:
        panel = QVBoxLayout()
        panel.setContentsMargins(0, 0, 0, 0)
        panel.setSpacing(10)

        # Concurrent
        sec_conc = Section("Concurrent downloads")
        self.concurrent_spin = QSpinBox()
        self.concurrent_spin.setRange(1, 16)
        self.concurrent_spin.setValue(self.settings.max_concurrent)
        self.concurrent_spin.valueChanged.connect(self.queue.set_max_concurrent)
        hint_conc = QLabel("How many files run at once.")
        hint_conc.setProperty("role", "muted")
        sec_conc.body().addWidget(self.concurrent_spin)
        sec_conc.body().addWidget(hint_conc)
        panel.addWidget(sec_conc)

        # Speed cap
        sec_speed = Section("Global speed limit")
        # Header row: value, display unit, and small (i) info badge.
        head = QHBoxLayout()
        head.setSpacing(8)
        self.speed_spin = QDoubleSpinBox()
        self.speed_unit = QComboBox()
        self.speed_unit.addItems(SPEED_LIMIT_UNITS)
        self.speed_unit.setCurrentText(self.settings.speed_limit_unit)
        configure_speed_spin(
            self.speed_spin,
            self.settings.speed_limit_unit,
            self.settings.overall_speed_limit_kbps,
        )
        self.speed_spin.valueChanged.connect(self._on_speed_value_changed)
        self.speed_unit.currentTextChanged.connect(self._on_speed_unit_changed)
        head.addWidget(self.speed_spin, 1)
        head.addWidget(self.speed_unit, 0)

        info = QLabel("i")
        info.setObjectName("infoBadge")
        info.setAlignment(Qt.AlignCenter)
        info.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        info.setFixedSize(20, 20)
        info.setToolTip(
            "Internet servers may break the connection when speed is too "
            "limited. It's not recommended to use the Speed Limiter to "
            "download from servers that don't support resume."
        )
        head.addWidget(info, 0)
        sec_speed.body().addLayout(head)

        self.speed_always_on = QCheckBox("Enable speed limiter")
        self.speed_always_on.setChecked(self.settings.speed_limiter_enabled)
        self.speed_always_on.toggled.connect(self._on_speed_enabled_toggled)
        sec_speed.body().addWidget(self.speed_always_on)

        speed_hint = QLabel("Total downstream cap across all files.")
        speed_hint.setProperty("role", "muted")
        sec_speed.body().addWidget(speed_hint)
        panel.addWidget(sec_speed)

        # Schedule
        sec_sched = Section("Schedule")
        self.schedule_state_label = QLabel("Off")
        self.schedule_state_label.setProperty("role", "mono")
        self.schedule_window_label = QLabel("Downloads run any time.")
        self.schedule_window_label.setProperty("role", "muted")
        self.schedule_window_label.setWordWrap(True)
        edit = QPushButton("Edit schedule")
        edit.clicked.connect(self._open_scheduler)
        sec_sched.body().addWidget(self.schedule_state_label)
        sec_sched.body().addWidget(self.schedule_window_label)
        sec_sched.body().addWidget(edit)
        panel.addWidget(sec_sched)

        panel.addStretch(1)
        return panel

    def _build_actionbar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 2, 0, 2)
        row.setSpacing(12)

        self.add_btn = QPushButton("Add download")
        self.add_btn.setProperty("kind", "accent")
        self.add_btn.clicked.connect(self._add_download)
        row.addWidget(self.add_btn)

        self.clip_btn = QPushButton("Add from clipboard")
        self.clip_btn.clicked.connect(self._add_from_clipboard)
        row.addWidget(self.clip_btn)

        self.open_folder_btn = QPushButton("Open downloads folder")
        self.open_folder_btn.setToolTip("Open where new downloads are saved")
        self.open_folder_btn.clicked.connect(self._open_downloads_folder)
        row.addWidget(self.open_folder_btn)

        self.settings_btn = QPushButton("Settings")
        self.settings_btn.clicked.connect(self._open_settings)
        row.addWidget(self.settings_btn)

        self.clear_btn = QPushButton("Clear completed")
        self.clear_btn.clicked.connect(lambda: self._clear_completed(False))
        row.addWidget(self.clear_btn)

        row.addStretch(1)

        self.queue_btn = QPushButton("Pause queue")
        self.queue_btn.clicked.connect(self._toggle_queue)
        row.addWidget(self.queue_btn)
        return row

    def _add_shortcut(self, key: str, slot) -> None:
        act = QAction(self)
        act.setShortcut(QKeySequence(key))
        act.triggered.connect(slot)
        self.addAction(act)

    def _wire_signals(self) -> None:
        self.queue.task_added.connect(self._on_task_added)
        self.queue.task_changed.connect(self._on_task_changed)
        self.queue.task_removed.connect(self._on_task_removed)
        self.queue.queue_running_changed.connect(self._on_queue_running_changed)
        self.queue.error.connect(self._on_error)
        self.queue.torrent_consent_needed.connect(self._on_torrent_consent_needed)
        self.scheduler.allowed_changed.connect(self._on_scheduler_changed)

    # ---- duplicate-aware adding -----------------------------------------
    #
    # Every user-initiated add funnels through `add_urls_checked` (or, for
    # a `.torrent`, through the queue's `duplicate_check` callback, since
    # the info hash only exists after the file has been parsed). Automated
    # paths - the local API, native messaging, queue restore, retry/resume,
    # `_check_external`, `_materialize_cached_torrent` - keep calling the
    # queue directly and are never prompted.

    @staticmethod
    def _candidate(url: str) -> dedup.Candidate:
        text = (url or "").strip()
        return dedup.Candidate(url=text, info_hash=dedup.magnet_info_hash(text))

    def _focus_task(self, tid: int | None) -> None:
        item = self._items.get(tid) if tid is not None else None
        if item is None:
            return
        self.tree.setCurrentItem(item)
        self.tree.scrollToItem(item)
        self.tree.setFocus()

    @staticmethod
    def _duplicate_target(match: dedup.DuplicateMatch) -> Path | None:
        """Where "Open Folder" should point, or None if nothing survives.

        The file itself when it is still there, otherwise the directory it
        was saved into - a user who moved or deleted the file still wants
        to land somewhere useful.
        """
        if not match.out_dir:
            return None
        try:
            folder = Path(match.out_dir)
            if match.filename:
                target = folder / match.filename
                if target.exists():
                    return target
            return folder if folder.is_dir() else None
        except OSError:
            return None

    def _confirm_duplicate(
        self, match: dedup.DuplicateMatch, label: str = ""
    ) -> bool:
        """Warn about one duplicate. True only if the user chose to proceed.

        Cancel is both the default and the Escape button, so dismissing the
        dialog can never start a download. "Download Anyway" applies to this
        add and nothing else: no suppression is stored anywhere.
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        folder: Path | None = None
        focus_btn = None
        open_btn = None
        proceed_btn = None
        if match.category == dedup.COMPLETED:
            box.setWindowTitle("Already downloaded")
            box.setText("This download appears to have already been completed.")
            folder = self._duplicate_target(match)
            if folder is not None:
                open_btn = box.addButton("Open Folder", QMessageBox.ActionRole)
            proceed_btn = box.addButton("Download Again", QMessageBox.AcceptRole)
        else:
            live_torrent = match.identity == dedup.ID_INFO_HASH
            box.setWindowTitle("Already in queue")
            box.setText(
                "This torrent is already in your queue."
                if live_torrent
                else "This download is already in your queue."
            )
            focus_btn = box.addButton("Focus Existing", QMessageBox.ActionRole)
            if not live_torrent:
                # A live torrent cannot be offered "Download Anyway": the
                # engine's own info-hash guard would refuse it, and an
                # action that cannot be honoured is worse than no action.
                proceed_btn = box.addButton(
                    "Download Anyway", QMessageBox.AcceptRole
                )
        detail = label or match.name
        if detail:
            box.setInformativeText(detail)
        cancel_btn = box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(cancel_btn)
        box.setEscapeButton(cancel_btn)
        box.exec()
        clicked = box.clickedButton()
        if focus_btn is not None and clicked is focus_btn:
            self._focus_task(match.task_id)
            return False
        if open_btn is not None and clicked is open_btn and folder is not None:
            _reveal_in_folder(folder)
            return False
        return proceed_btn is not None and clicked is proceed_btn

    def _confirm_duplicate_batch(
        self, checked: list[tuple[dedup.Candidate, dedup.DuplicateMatch | None]]
    ) -> list[str]:
        """One summary for the whole batch; never one modal per item.

        Returns the URLs to add, in the order they were submitted. Only
        short labels are shown - a signed link's query carries its token
        and a private-tracker magnet carries its passkey, so no full URL
        is ever rendered here.
        """
        dups = [(c, m) for c, m in checked if m is not None]
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Duplicate downloads")
        box.setText(
            f"{len(dups)} of {len(checked)} downloads already exist in your "
            "queue or completed history."
        )
        shown = [dedup.safe_label(c) for c, _ in dups[:8]]
        if len(dups) > len(shown):
            shown.append(f"...and {len(dups) - len(shown)} more")
        box.setInformativeText("\n".join(shown))
        skip_btn = box.addButton("Skip Duplicates", QMessageBox.AcceptRole)
        all_btn = box.addButton("Add All Anyway", QMessageBox.ActionRole)
        cancel_btn = box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(skip_btn)
        box.setEscapeButton(cancel_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is all_btn:
            # Live same-info-hash torrents stay skipped even here: the
            # engine cannot run one twice, whatever the user picks.
            return [c.url for c, m in checked if m is None or m.can_duplicate]
        if clicked is skip_btn:
            return [c.url for c, m in checked if m is None]
        return []

    def add_urls_checked(
        self, urls: list[str], out_dir: str | None = None
    ) -> list[int]:
        """Add URLs interactively, warning about anything already present."""
        cands = [self._candidate(u) for u in urls if (u or "").strip()]
        if not cands:
            return []
        checked: list[tuple[dedup.Candidate, dedup.DuplicateMatch | None]] = []
        seen: dict[tuple[str, str], dedup.Candidate] = {}
        for cand in cands:
            ident = dedup.identity(cand)
            if ident is not None and ident in seen:
                # A repeat inside this very batch, which the queue cannot
                # know about yet because nothing has been added.
                match = dedup.DuplicateMatch(
                    category=dedup.LIVE,
                    identity=ident[0],
                    name=dedup.safe_label(seen[ident]),
                    can_duplicate=ident[0] != dedup.ID_INFO_HASH,
                )
            else:
                match = self.queue.find_duplicate(
                    cand.url, info_hash=cand.info_hash
                )
                if ident is not None:
                    seen[ident] = cand
            checked.append((cand, match))
        if all(m is None for _, m in checked):
            return self.queue.add_urls([c.url for c, _ in checked], out_dir)
        if len(checked) == 1:
            cand, match = checked[0]
            if not self._confirm_duplicate(match, dedup.safe_label(cand)):
                return []
            tid = self.queue.add_url(cand.url, out_dir)
            return [] if tid is None else [tid]
        chosen = self._confirm_duplicate_batch(checked)
        return self.queue.add_urls(chosen, out_dir) if chosen else []

    def add_url_interactive(self, url: str) -> None:
        """Entry point for command-line and second-instance magnets."""
        self.add_urls_checked([url])

    # ---- actions --------------------------------------------------------

    def _add_download(self) -> None:
        dlg = AddDownloadDialog(self.settings, self)
        if dlg.exec() != AddDownloadDialog.Accepted:
            return
        if dlg.torrent_path:
            self.settings.download_dir = dlg.get_dir()
            self.settings.save()
            self._refresh_folder_chip()
            self.queue.add_torrent_file(
                dlg.torrent_path,
                dlg.get_dir(),
                duplicate_check=self._confirm_duplicate,
            )
            self._maybe_offer_magnet_handler()
            return
        urls = dlg.get_urls()
        if not urls:
            QMessageBox.information(self, "Nothing to add", "No URLs detected.")
            return
        self.settings.download_dir = dlg.get_dir()
        self.settings.save()
        self._refresh_folder_chip()
        self.add_urls_checked(urls)

        from . import torrent

        if any(torrent.is_magnet(u) for u in urls):
            self._maybe_offer_magnet_handler()

    def _maybe_offer_magnet_handler(self) -> bool:
        """Offer once, the first time the user adds a magnet or torrent.

        A first-run prompt would arrive before the user knows what Cove does.
        Someone who just pasted a magnet has demonstrated the exact need.
        Returns True when the offer was shown.
        """
        settings = self.settings
        if getattr(settings, "magnet_prompt_shown", False):
            return False
        try:
            state = magnet_handler.status()
        except Exception:
            return False
        if not state.supported or state.is_default:
            return False

        accepted = _ask_magnet_offer(self)
        settings.magnet_prompt_shown = True
        # The question has now been asked directly, so the startup migration
        # heuristic (which only exists to infer an answer nobody gave) must
        # never fire after this, whichever way the user answered.
        settings.magnet_setting_missing = False
        if accepted:
            try:
                magnet_handler.enable()
            except Exception:
                # The user already answered; a broken enable() must not
                # surface into the Add-dialog accept path or re-ask later.
                pass
            # Set regardless of whether enable() could confirm the default,
            # or raised outright. On Windows it never can: the user still
            # has to choose Cove in Settings. The preference means "keep the
            # registration repaired", which is what an accepting user wants
            # either way.
            settings.magnet_handler_enabled = True
        try:
            settings.save()
        except Exception:
            pass
        return True

    def _on_torrent_consent_needed(self, tid: int) -> None:
        """Ask once, before Cove's first local BitTorrent transfer.

        The queue emits this signal from the GUI thread and starts nothing
        until the answer comes back, so no worker ever waits on a modal and
        no peer connection is opened before the user has said yes.
        """
        box, download_button, settings_button, remember = build_p2p_consent_box(self)
        box.exec()
        clicked = box.clickedButton()
        if clicked is settings_button:
            # The task stays parked in the queue while Settings is open, so
            # the notice's checkbox is discarded here on purpose: the user
            # has not chosen to download yet.
            self._open_settings()
            self.queue.torrent_consent_reevaluate(tid)
            return
        self.queue.torrent_consent(
            tid,
            clicked is download_button,
            remember=remember.isChecked(),
        )

    def _add_from_clipboard(self) -> None:
        text = QGuiApplication.clipboard().text() or ""
        urls = extract_urls(text)
        if not urls:
            QMessageBox.information(
                self, "Clipboard empty", "No URLs found on the clipboard."
            )
            return
        dlg = ClipboardBatchDialog(urls, self.settings, self)
        if dlg.exec() == ClipboardBatchDialog.Accepted:
            chosen = dlg.selected()
            if chosen:
                selected_dir = dlg.get_dir()
                self.add_urls_checked(chosen, selected_dir)

    def _paste_urls(self) -> None:
        text = QGuiApplication.clipboard().text() or ""
        urls = extract_urls(text)
        if urls:
            self.add_urls_checked(urls)

    def _toggle_selected(self) -> None:
        for tid in self._selected_tids():
            t = self.queue.tasks.get(tid)
            if not t:
                continue
            if t.status in {"queued", "active"}:
                self.queue.pause(tid)
            elif t.status in {"paused", "error"}:
                self.queue.resume(tid)

    def _toggle_queue(self) -> None:
        if self.queue.is_running:
            self.queue.stop_queue()
        else:
            self.queue.start_queue()

    def _open_scheduler(self) -> None:
        dlg = SchedulerDialog(self.settings.schedule, self.settings, self)
        if dlg.exec() == SchedulerDialog.Accepted:
            self.settings.schedule = dlg.result_window()
            self.settings.time_format_24h = dlg.use_24h_format()
            self.settings.save()
            self.scheduler.update_window(self.settings.schedule)
            self._refresh_status_pill()
            self._refresh_schedule_section()

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec() == SettingsDialog.Accepted:
            self.concurrent_spin.blockSignals(True)
            self.concurrent_spin.setValue(self.settings.max_concurrent)
            self.concurrent_spin.blockSignals(False)
            self.speed_unit.blockSignals(True)
            self.speed_unit.setCurrentText(self.settings.speed_limit_unit)
            self.speed_unit.blockSignals(False)
            configure_speed_spin(
                self.speed_spin,
                self.settings.speed_limit_unit,
                self.settings.overall_speed_limit_kbps,
            )
            self.speed_always_on.blockSignals(True)
            self.speed_always_on.setChecked(self.settings.speed_limiter_enabled)
            self.speed_always_on.blockSignals(False)
            self.queue.set_max_concurrent(self.settings.max_concurrent)
            self._apply_speed_limit()
            self._refresh_schedule_section()
            self._refresh_folder_chip()

    def _clear_completed(self, delete_files: bool) -> None:
        completed = [t for t in self.queue.tasks.values() if t.status == "completed"]
        if not completed:
            return
        if delete_files:
            ans = QMessageBox.question(
                self,
                "Delete completed files?",
                f"This permanently deletes {len(completed)} file(s) from disk.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if ans != QMessageBox.Yes:
                return
        self.queue.clear_completed(delete_files=delete_files)

    def _on_speed_value_changed(self, value: float) -> None:
        self.settings.overall_speed_limit_kbps = speed_value_to_kbps(
            value, self.speed_unit.currentText()
        )
        self.settings.save()
        self._apply_speed_limit()

    def _on_speed_unit_changed(self, unit: str) -> None:
        self.settings.speed_limit_unit = unit
        configure_speed_spin(
            self.speed_spin, unit, self.settings.overall_speed_limit_kbps
        )
        self.settings.save()

    def _on_speed_enabled_toggled(self, checked: bool) -> None:
        self.settings.speed_limiter_enabled = checked
        self.settings.save()
        self._apply_speed_limit()

    def _apply_speed_limit(self) -> None:
        """Push the effective limit (kbps if enabled, else 0) to aria2."""
        kbps = self.settings.overall_speed_limit_kbps
        effective = kbps if self.settings.speed_limiter_enabled else 0
        self.queue.set_overall_speed_limit(effective)
        cap = _human_cap(effective)
        self.stats.set_value("Speed limit", cap)

    # ---- context menu --------------------------------------------------

    def _selected_tids(self) -> list[int]:
        return [int(it.data(0, Qt.UserRole)) for it in self.tree.selectedItems()]

    def _remove_selected(self, *, delete_file: bool) -> None:
        tids = self._selected_tids()
        if not tids:
            return
        if delete_file:
            ans = QMessageBox.question(
                self,
                "Delete files?",
                f"This permanently deletes {len(tids)} file(s) from disk.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if ans != QMessageBox.Yes:
                return
        for tid in tids:
            self.queue.remove(tid, delete_file=delete_file)

    def _clear_all(self) -> None:
        tids = list(self.queue.tasks.keys())
        if not tids:
            return
        ans = QMessageBox.question(
            self,
            "Clear all downloads?",
            f"Remove all {len(tids)} downloads from the list? Files on disk are kept.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        for tid in tids:
            self.queue.remove(tid, delete_file=False)

    def _add_source_action(self, menu, task) -> None:
        """Add "View source" for `task`. Available in every status: the
        origin of a download is just as worth checking when it failed."""
        act = menu.addAction("View source")
        act.triggered.connect(
            lambda _=False, t=task: self._show_source_details(t)
        )

    def _show_source_details(self, task) -> None:
        SourceDetailsDialog(task, self).exec()

    def _open_context_menu(self, pos) -> None:
        menu = QMenu(self)
        item = self.tree.itemAt(pos)

        if item is not None:
            tid = item.data(0, Qt.UserRole)
            task = self.queue.tasks.get(tid)
            selected = self._selected_tids()
            if task is not None:
                # If the right-clicked row isn't part of the existing
                # selection, treat the click as selecting just that row.
                if tid not in selected:
                    self.tree.setCurrentItem(item)
                    selected = [tid]
                # Open / reveal — for finished or in-progress files.
                file_path = self._task_path(task)
                if task.status == "completed" and file_path is not None:
                    open_a = menu.addAction("Open file")
                    open_a.setEnabled(file_path.exists())
                    open_a.triggered.connect(
                        lambda _=False, p=file_path: _open_path(p)
                    )
                if file_path is not None:
                    reveal_a = menu.addAction("Show in folder")
                    reveal_a.setEnabled(
                        file_path.exists() or file_path.parent.exists()
                    )
                    reveal_a.triggered.connect(
                        lambda _=False, p=file_path: _reveal_in_folder(p)
                    )
                if task.status == "completed" or file_path is not None:
                    menu.addSeparator()
                if task.status in {"queued", "active"} and task.backend != "ffmpeg":
                    menu.addAction("Pause").triggered.connect(
                        lambda: [self.queue.pause(t) for t in selected]
                    )
                if task.status == "queued":
                    menu.addAction("Start now").triggered.connect(
                        lambda: [self.queue.force_start(t) for t in selected]
                    )
                if task.status == "paused" and task.backend != "ffmpeg":
                    menu.addAction("Resume").triggered.connect(
                        lambda: [self.queue.resume(t) for t in selected]
                    )
                if task.status == "error":
                    retry_a = menu.addAction("Retry")
                    retry_a.triggered.connect(
                        lambda: [self.queue.resume(t) for t in selected]
                    )
                menu.addSeparator()
                self._add_source_action(menu, task)
                menu.addSeparator()
                menu.addAction("Remove\tDel").triggered.connect(
                    lambda: self._remove_selected(delete_file=False)
                )
                menu.addAction("Remove and delete file").triggered.connect(
                    lambda: self._remove_selected(delete_file=True)
                )
                menu.addSeparator()

        # Always-available bulk actions (shown on rows and on empty space).
        completed_count = sum(
            1 for t in self.queue.tasks.values() if t.status == "completed"
        )
        a = menu.addAction(f"Clear completed ({completed_count})")
        a.setEnabled(completed_count > 0)
        a.triggered.connect(lambda: self._clear_completed(False))
        b = menu.addAction("Clear all")
        b.setEnabled(bool(self.queue.tasks))
        b.triggered.connect(self._clear_all)

        menu.exec(self.tree.viewport().mapToGlobal(pos))

    # ---- queue handlers ------------------------------------------------

    def _on_task_added(self, tid: int) -> None:
        task = self.queue.tasks.get(tid)
        if not task:
            return
        item = QTreeWidgetItem(["", "", "", "", ""])
        item.setData(0, Qt.UserRole, tid)
        self.tree.addTopLevelItem(item)
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setTextVisible(True)
        self.tree.setItemWidget(item, COL_PROGRESS, bar)
        self._items[tid] = item
        self._bars[tid] = bar
        self._render(task)
        self._refresh_status_pill()

    def _on_task_changed(self, tid: int) -> None:
        task = self.queue.tasks.get(tid)
        if task:
            self._maybe_notify(task)
            self._render(task)
            self._refresh_stats()
            self._refresh_status_pill()

    def _maybe_notify(self, task: DownloadTask) -> None:
        if task.status not in ("error", "completed"):
            # Task left a terminal state (e.g. retried) — allow re-notification.
            self._notified_status.pop(task.id, None)
            return
        if self._notified_status.get(task.id) == task.status:
            return
        self._notified_status[task.id] = task.status
        if self._tray is None:
            return
        name = task.filename or task.url
        if task.status == "error" and self.settings.notify_on_error:
            self._tray.showMessage(
                "Download failed",
                f"{name}\n{task.error or 'Unknown error'}",
                QSystemTrayIcon.MessageIcon.Critical,
                8000,
            )
        elif task.status == "completed" and self.settings.notify_on_complete:
            self._tray.showMessage(
                "Download complete",
                name,
                QSystemTrayIcon.MessageIcon.Information,
                5000,
            )

    def _on_task_removed(self, tid: int) -> None:
        self._notified_status.pop(tid, None)
        item = self._items.pop(tid, None)
        self._bars.pop(tid, None)
        if item:
            idx = self.tree.indexOfTopLevelItem(item)
            if idx >= 0:
                self.tree.takeTopLevelItem(idx)
        self._refresh_stats()
        self._refresh_status_pill()

    def _on_queue_running_changed(self, running: bool) -> None:
        self.queue_btn.setText("Pause queue" if running else "Start queue")
        self.queue_btn.setProperty("kind", "" if running else "accent")
        self.queue_btn.style().unpolish(self.queue_btn)
        self.queue_btn.style().polish(self.queue_btn)
        self._refresh_status_pill()

    def _on_scheduler_changed(self, allowed: bool) -> None:
        self.queue.set_scheduler_allowed(allowed)
        self._refresh_status_pill()
        self._refresh_schedule_section()

    def _on_error(self, msg: str) -> None:
        # Don't be intrusive — flash it in the status pill briefly.
        self.status_pill.set_state("error", "Error")
        QTimer.singleShot(4000, self._refresh_status_pill)

    # ---- rendering -----------------------------------------------------

    def _render(self, task: DownloadTask) -> None:
        item = self._items.get(task.id)
        bar = self._bars.get(task.id)
        if not item or not bar:
            return
        name = task.filename or task.url
        item.setText(COL_NAME, name)
        item.setToolTip(COL_NAME, task.error or task.url)
        item.setText(COL_STATUS, task_status_label(task))
        # Persistent state coloring on the Status column so an error (or
        # paused) row stays visually distinct after the StatusPill flash
        # has reverted.
        if task.status == "error":
            item.setForeground(COL_STATUS, QColor(theme.REC))
            if task.error:
                item.setToolTip(COL_STATUS, task.error)
        elif task.status == "paused":
            item.setForeground(COL_STATUS, QColor(theme.WARN))
            item.setToolTip(COL_STATUS, "")
        elif task.status == "completed":
            item.setForeground(COL_STATUS, QColor(theme.ACCENT))
            item.setToolTip(COL_STATUS, "")
        else:
            item.setForeground(COL_STATUS, QColor(theme.TEXT_DIM))
            item.setToolTip(COL_STATUS, "")
        completed = task.interpolated_completed_bytes()
        if task.total_bytes > 0:
            pct = int(completed * 100 / task.total_bytes)
            bar.setValue(pct)
            seg_hint = f" [{task.segments}x]" if task.segments > 1 and task.backend != "ffmpeg" else ""
            bar.setFormat(f"{pct}%{seg_hint}")
        else:
            bar.setValue(100 if task.status == "completed" else 0)
            bar.setFormat("100%" if task.status == "completed" else "—")
        if task.num_pieces > 0 and task.bitfield:
            done = sum(1 for b in _hex_to_bits(task.bitfield, task.num_pieces) if b)
            bar.setToolTip(f"Pieces: {done}/{task.num_pieces}")
        if task.backend == "ffmpeg":
            if task.total_bytes > 0:
                e, d = task.completed_bytes, task.total_bytes
                item.setText(COL_SIZE, f"{e // 60}:{e % 60:02d} / {d // 60}:{d % 60:02d}")
            else:
                item.setText(COL_SIZE, "--")
        else:
            size_text = (
                f"{_human_bytes(completed)} / {_human_bytes(task.total_bytes)}"
                if task.total_bytes
                else _human_bytes(completed)
            )
            item.setText(COL_SIZE, size_text)
        if task.backend == "ffmpeg" and task.status == "active":
            item.setText(COL_SPEED, task.error or "")
        else:
            item.setText(COL_SPEED, _speed_eta(task, completed))

    def _slow_tick(self) -> None:
        self._refresh_stats()
        self._refresh_status_pill()

    def _smooth_tick(self) -> None:
        # Re-render only active rows; everything else is static between
        # aria2 polls so re-rendering it would just be wasted work.
        for tid, task in list(self.queue.tasks.items()):
            if task.status == "active":
                self._render(task)

    def stop_ui_timers(self) -> None:
        """Stop the repaint timers before teardown so they can't fire on
        already-destroyed progress-bar widgets during shutdown."""
        self._tick.stop()
        self._smooth.stop()

    def changeEvent(self, event) -> None:
        # Keep the titlebar maximize glyph in sync when the window state
        # changes by any path (OS shortcut, snapping), not just our button.
        if event.type() == QEvent.WindowStateChange:
            self.titlebar.sync_max_glyph()
        super().changeEvent(event)

    def _refresh_stats(self) -> None:
        active = 0
        queued = 0
        speed = 0
        for t in self.queue.tasks.values():
            if t.status == "active":
                active += 1
                speed += t.download_speed
            elif t.status == "queued":
                queued += 1
        kbps = self.settings.overall_speed_limit_kbps
        if kbps == 0 or not self.settings.speed_limiter_enabled:
            cap_text = "Off"
        else:
            cap_text = _human_cap(kbps)
        self.stats.set_value("Active", str(active))
        self.stats.set_value("Queued", str(queued))
        self.stats.set_value("Total", _human_speed(speed) if speed > 0 else "—")
        self.stats.set_value("Speed limit", cap_text)

    def _refresh_status_pill(self) -> None:
        if not self.queue.is_running:
            self.status_pill.set_state("paused", "Queue paused")
            return
        if self.settings.schedule.enabled and not self.scheduler.allowed:
            self.status_pill.set_state("off", "Outside schedule")
            return
        active = any(t.status == "active" for t in self.queue.tasks.values())
        queued = any(t.status == "queued" for t in self.queue.tasks.values())
        if active:
            self.status_pill.set_state("ok", "Running")
        elif queued:
            self.status_pill.set_state("ok", "Queued")
        else:
            self.status_pill.set_state("ok", "Idle")

    def _refresh_schedule_section(self) -> None:
        s = self.settings.schedule
        if not s.enabled:
            self.schedule_state_label.setText("Off")
            self.schedule_window_label.setText("Downloads run any time.")
            return
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        on_days = ", ".join(days[d] for d in sorted(s.days)) or "no days"
        start = _fmt_time(s.start_hour, s.start_minute, self.settings.time_format_24h)
        end = _fmt_time(s.end_hour, s.end_minute, self.settings.time_format_24h)
        self.schedule_state_label.setText(f"{start} – {end}")
        tag = "within window" if self.scheduler.allowed else "outside window"
        self.schedule_window_label.setText(f"{on_days} · {tag}")

    # ── Output folder / file actions ──────────────────────────────

    def _task_path(self, task: DownloadTask) -> Path | None:
        """Best-effort path to a task's destination file. Returns None if
        the filename hasn't been resolved by aria2 yet."""
        if not task.filename:
            return None
        return Path(task.out_dir) / task.filename

    def _open_downloads_folder(self) -> None:
        """Open the configured default download folder. If it doesn't
        exist yet, fall back to the user's home directory."""
        path = Path(self.settings.download_dir)
        if not path.exists():
            path = Path.home()
        _open_path(path)

    def _refresh_folder_chip(self) -> None:
        path = self.settings.download_dir
        self.footer.set_folder(path, _truncate_path(path))

    # ── Drag-and-drop URL ingest ──────────────────────────────────

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # type: ignore[override]
        md = event.mimeData()
        if md.hasUrls() or md.hasText():
            event.acceptProposedAction()
        else:
            event.ignore()

    def _torrent_enabled(self) -> bool:
        return getattr(self.settings, "torrent_support_enabled", False) is True

    def dropEvent(self, event: QDropEvent) -> None:  # type: ignore[override]
        md = event.mimeData()
        urls: list[str] = []
        locals_: list[str] = []
        if md.hasUrls():
            for u in md.urls():
                s = u.toString()
                if s:
                    urls.append(s)
                locals_.append(u.toLocalFile())
        torrents = torrent_drop_paths(locals_, self._torrent_enabled())
        # Browsers usually also include text/plain — and on some Linux
        # setups that's the only thing they hand over. Fall back to it.
        if md.hasText():
            for line in md.text().splitlines():
                s = line.strip()
                if s and s not in urls:
                    urls.append(s)
        urls = [u for u in urls if not u.startswith("file://")]
        if not urls and not torrents:
            event.ignore()
            return
        for path in torrents:
            self.queue.add_torrent_file(
                path,
                self.settings.download_dir,
                duplicate_check=self._confirm_duplicate,
            )
        added = self.add_urls_checked(urls) if urls else []
        if added or torrents:
            event.acceptProposedAction()
        else:
            event.ignore()


def _fmt_time(hour: int, minute: int, use_24h: bool) -> str:
    if use_24h:
        return f"{hour:02d}:{minute:02d}"
    period = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    return f"{h12}:{minute:02d} {period}"


def _status_label(s: str) -> str:
    return {
        "queued": "Queued",
        "active": "Downloading",
        "paused": "Paused",
        "completed": "Done",
        "error": "Error",
    }.get(s, s)


def task_status_label(task) -> str:
    """The status column for one task.

    A magnet whose metadata aria2 is still fetching is active but has no
    byte count yet; showing it as "Downloading" against an empty progress
    bar reads as a stall, and showing 0 of 0 bytes reads as finished.
    """
    if task.status == "active" and getattr(task, "phase", "") == PHASE_METADATA:
        return "Fetching metadata"
    return _status_label(task.status)
