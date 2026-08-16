"""Dialogs in the cove-screen-recorder visual idiom: dialog title, optional
subtitle, sections / form rows, accent OK / ghost Cancel.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTime, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from . import debrid
from .clipboard import extract_urls
from .config import (
    CATEGORY_NAMES,
    CONNECTION_CHOICES,
    DEBRID_ALL_DEBRID,
    DEBRID_REAL_DEBRID,
    DEBRID_TORBOX,
    TORRENT_FALLBACK_AUTOMATIC,
    TORRENT_FALLBACK_NEVER,
    ScheduleWindow,
    Settings,
)
from .debrid import DebridError
from .indexer_editor import IndexerEditorDialog
from .netiface import ANY_INTERFACE, ANY_INTERFACE_LABEL, list_interfaces
from .search.indexers import CustomTorznabIndexer, new_custom_indexer_id
from .source_info import redact_url, source_details
from .speed_limit import (
    SPEED_LIMIT_UNITS,
    configure_speed_spin,
    speed_value_to_kbps,
)

ALL_DEBRID_KEY_URL = "https://alldebrid.com/apikeys/"
REAL_DEBRID_TOKEN_URL = "https://real-debrid.com/apitoken"
TORBOX_TOKEN_URL = "https://torbox.app/settings"

# Account tests are pinned here rather than on the dialog so a runnable
# still in flight survives the dialog closing. The queue module documents
# the same hazard: letting the pool reap a runnable whose signal object the
# C++ side still references crashes the process.
_INFLIGHT_ACCOUNT_TESTS: set = set()


class _AccountTest(QRunnable):
    """Run one provider account check off the GUI thread.

    Emits the provider name alongside the result so the dialog can route
    it without a closure — connecting bound methods lets Qt drop the
    connection automatically if the dialog is destroyed mid-flight.
    """

    class _Sig(QObject):
        done = Signal(str, object)   # provider, sanitized account dict
        failed = Signal(str, str)    # provider, displayable message
        finished = Signal()

    def __init__(self, provider: str, fn):
        super().__init__()
        self.setAutoDelete(False)
        self.provider = provider
        self.signals = self._Sig()
        self._fn = fn

    def run(self):
        try:
            account = self._fn()
        except DebridError as e:
            self.signals.failed.emit(self.provider, str(e))
        except Exception:
            # Never surface the raw exception: it may quote the request,
            # and the request carries the API credential.
            self.signals.failed.emit(
                self.provider,
                f"{debrid.provider_label(self.provider)}: the account test "
                f"could not be completed.",
            )
        else:
            self.signals.done.emit(self.provider, account)
        self.signals.finished.emit()


class _MagnetProbe(QRunnable):
    """Run one magnet-handler call off the GUI thread.

    `status()`, `enable()` and `disable()` all shell out to xdg-mime and the
    desktop-database tools, each bounded at several seconds. Called directly
    from the GUI thread - which is where the Settings dialog and its buttons
    live - that is several seconds of a window that does not repaint or accept
    input. Same shape as _AccountTest above, including autoDelete(False) so the
    signal carrier outlives any queued cross-thread call.
    """

    class _Sig(QObject):
        done = Signal(object)
        finished = Signal()

    def __init__(self, fn):
        super().__init__()
        self.setAutoDelete(False)
        self.signals = self._Sig()
        self._fn = fn

    def run(self):
        try:
            result = self._fn()
        except Exception:
            # Best-effort by design: a magnet association is never worth
            # surfacing a traceback into Settings.
            result = None
        self.signals.done.emit(result)
        self.signals.finished.emit()


# Pins in-flight probes for the same reason _INFLIGHT_ACCOUNT_TESTS does.
_INFLIGHT_MAGNET_PROBES = set()


def _run_magnet_probe(fn, on_done) -> None:
    call = _MagnetProbe(fn)
    _INFLIGHT_MAGNET_PROBES.add(call)
    call.signals.done.connect(on_done)
    call.signals.finished.connect(lambda c=call: _INFLIGHT_MAGNET_PROBES.discard(c))
    QThreadPool.globalInstance().start(call)


def _make_buttons(parent: QDialog, ok_text: str = "Save") -> QDialogButtonBox:
    bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    ok = bb.button(QDialogButtonBox.Ok)
    ok.setText(ok_text)
    ok.setProperty("kind", "accent")
    bb.accepted.connect(parent.accept)
    bb.rejected.connect(parent.reject)
    return bb


def _link_label(text: str, url: str) -> QLabel:
    label = QLabel(f'<a href="{url}">{text}</a>')
    label.setProperty("role", "muted")
    label.setOpenExternalLinks(True)
    label.setTextInteractionFlags(Qt.TextBrowserInteraction)
    return label


def _account_summary(provider: str, account: object) -> str:
    """One line describing a verified account.

    Only the whitelisted fields the debrid module already sanitized are
    read, so no provider payload can reach the label verbatim.
    """
    label = debrid.provider_label(provider)
    if not isinstance(account, dict):
        return f"{label}: connected."
    parts = []
    username = account.get("username")
    if isinstance(username, str) and username:
        parts.append(username)
    if provider == DEBRID_ALL_DEBRID:
        if account.get("is_premium") is True:
            parts.append("Premium")
        elif account.get("is_trial") is True:
            parts.append("Trial")
        else:
            parts.append("Free")
        expires = _format_epoch(account.get("premium_until"))
        if expires:
            parts.append(f"until {expires}")
    elif provider == DEBRID_TORBOX:
        email = account.get("email")
        if isinstance(email, str) and email:
            parts.append(email)
        parts.append("Subscribed" if account.get("is_subscribed") is True else "Free")
        expires = _format_iso_date(account.get("expiration"))
        if expires:
            parts.append(f"until {expires}")
    else:
        account_type = account.get("type")
        if isinstance(account_type, str) and account_type:
            parts.append(account_type)
        expires = _format_iso_date(account.get("expiration"))
        if expires:
            parts.append(f"until {expires}")
    return f"{label}: connected as " + ", ".join(parts) if parts else f"{label}: connected."


def _format_epoch(value) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return ""
    from datetime import datetime

    try:
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return ""


def _format_iso_date(value) -> str:
    if not isinstance(value, str) or not value:
        return ""
    date_part = value.split("T", 1)[0]
    return date_part if len(date_part) == 10 else ""


def _title_block(layout: QVBoxLayout, title: str, subtitle: str | None = None) -> None:
    t = QLabel(title)
    t.setObjectName("dialogTitle")
    layout.addWidget(t)
    if subtitle:
        s = QLabel(subtitle)
        s.setObjectName("dialogSubtitle")
        layout.addWidget(s)


def _make_connections_combo(current: int) -> QComboBox:
    """Connections dropdown capped to stock aria2's per-server maximum."""
    combo = QComboBox()
    for n in CONNECTION_CHOICES:
        combo.addItem(str(n), n)
    closest = min(CONNECTION_CHOICES, key=lambda v: abs(v - current))
    combo.setCurrentIndex(CONNECTION_CHOICES.index(closest))
    return combo


def _time_format(use_24h: bool) -> str:
    return "HH:mm" if use_24h else "hh:mm AP"


def torrent_file_problem(path) -> str:
    """Why this path can't be added as a `.torrent`, or "" if it can.

    These are the cheap checks — extension, is-a-file, size — so the GUI
    thread can reject an obviously wrong drop or pick without reading
    anything. The real parse still happens on a worker.
    """
    import os

    from .torrent import MAX_TORRENT_BYTES

    if not isinstance(path, str) or not path.lower().endswith(".torrent"):
        return "Cove can only add .torrent files here."
    try:
        if not os.path.isfile(path):
            return "That is not a .torrent file."
        if os.path.getsize(path) > MAX_TORRENT_BYTES:
            return "That .torrent file is larger than Cove will read (10 MiB)."
    except OSError:
        return "That .torrent file could not be opened."
    return ""


class AddDownloadDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add download")
        self.setMinimumWidth(560)
        self.settings = settings

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        _title_block(layout, "Add download", "Paste one or more URLs, one per line.")

        self.urls = QPlainTextEdit()
        self.urls.setPlaceholderText("https://example.com/file.zip")
        self.urls.setMinimumHeight(140)
        layout.addWidget(self.urls)

        form = QFormLayout()
        form.setSpacing(10)
        self.dir_edit = QLineEdit(settings.download_dir)
        browse = QPushButton("Browse")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.dir_edit, 1)
        row.addWidget(browse)
        form.addRow("Save to", row)
        layout.addLayout(form)

        # Torrent input is hidden entirely until the local BitTorrent
        # fallback ships: with only the cached-debrid route available, an
        # uncached torrent would have nowhere to go.
        self.torrent_path = ""
        self.torrent_enabled = getattr(settings, "torrent_support_enabled", False) is True
        self.torrent_button = QPushButton("Add torrent file...")
        self.torrent_button.clicked.connect(self._pick_torrent)
        self.torrent_button.setVisible(self.torrent_enabled)
        self.torrent_button.setEnabled(self.torrent_enabled)
        layout.addWidget(self.torrent_button)

        layout.addWidget(_make_buttons(self, ok_text="Add"))

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Save downloads to", self.dir_edit.text())
        if path:
            self.dir_edit.setText(path)

    def _pick_torrent(self) -> None:
        if not self.torrent_enabled:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Add torrent file", self.dir_edit.text(), "Torrent files (*.torrent)"
        )
        if not path:
            return
        problem = torrent_file_problem(path)
        if problem:
            QMessageBox.warning(self, "Cannot add torrent", problem)
            return
        self.torrent_path = path
        self.accept()

    def get_urls(self) -> list[str]:
        text = self.urls.toPlainText()
        urls = extract_urls(text)
        if urls:
            return urls
        return [ln.strip() for ln in text.splitlines() if ln.strip()]

    def get_dir(self) -> str:
        return self.dir_edit.text().strip() or self.settings.download_dir


class SourceDetailsDialog(QDialog):
    """Read-only "View source" sheet for one task.

    Renders `source_info.source_details`, which has already masked
    credentials and dropped anything private. The unmasked URL exists only
    behind the explicit "Copy original URL" button, so nothing sensitive
    is ever on screen or on the clipboard without the user asking for it.
    """

    def __init__(self, task, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Source details")
        self.setMinimumWidth(560)
        self._task = task
        self._redacted_url = redact_url(task.url)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        _title_block(
            layout,
            "Source details",
            "Cookies are never shown. Credentials and signed-link tokens are "
            "masked wherever they can be recognised.",
        )

        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMinimumHeight(180)
        self.details.setPlainText(
            "\n".join(f"{label}: {value}" for label, value in source_details(task))
        )
        layout.addWidget(self.details)

        copy_row = QHBoxLayout()
        copy_url = QPushButton("Copy URL")
        copy_url.setToolTip(
            "Copy the URL exactly as shown above, with recognised secrets masked."
        )
        copy_url.clicked.connect(self.copy_url)
        copy_row.addWidget(copy_url)

        copy_original = QPushButton("Copy original URL")
        copy_original.setToolTip(
            "Copy the unmasked URL, including any credentials it carries."
        )
        copy_original.clicked.connect(self.copy_original_url)
        copy_row.addWidget(copy_original)

        copy_details = QPushButton("Copy details")
        copy_details.clicked.connect(self.copy_details)
        copy_row.addWidget(copy_details)
        copy_row.addStretch(1)
        layout.addLayout(copy_row)

        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.button(QDialogButtonBox.Close).clicked.connect(self.reject)
        layout.addWidget(bb)

    def details_text(self) -> str:
        return self.details.toPlainText()

    def _set_clipboard(self, text: str) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)

    def copy_url(self) -> None:
        self._set_clipboard(self._redacted_url)

    def copy_original_url(self) -> None:
        self._set_clipboard(self._task.url or "")

    def copy_details(self) -> None:
        self._set_clipboard(self.details_text())


class ClipboardBatchDialog(QDialog):
    def __init__(self, urls: list[str], settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add from clipboard")
        self.setMinimumWidth(560)
        self.settings = settings

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        _title_block(
            layout,
            "Add from clipboard",
            f"Found {len(urls)} URL(s). Pick which to queue.",
        )

        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.NoSelection)
        for u in urls:
            item = QListWidgetItem(u)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self.list.addItem(item)
        layout.addWidget(self.list, 1)

        form = QFormLayout()
        form.setSpacing(10)
        self.dir_edit = QLineEdit(settings.download_dir)
        browse = QPushButton("Browse")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.dir_edit, 1)
        row.addWidget(browse)
        form.addRow("Save to", row)
        layout.addLayout(form)

        controls = QHBoxLayout()
        select_all = QPushButton("Select all")
        none_btn = QPushButton("Select none")
        select_all.clicked.connect(lambda: self._set_all(True))
        none_btn.clicked.connect(lambda: self._set_all(False))
        controls.addWidget(select_all)
        controls.addWidget(none_btn)
        controls.addStretch(1)
        layout.addLayout(controls)

        layout.addWidget(_make_buttons(self, ok_text="Queue"))

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Save downloads to", self.dir_edit.text())
        if path:
            self.dir_edit.setText(path)

    def _set_all(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(state)

    def selected(self) -> list[str]:
        out: list[str] = []
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.checkState() == Qt.Checked:
                out.append(it.text())
        return out

    def get_dir(self) -> str:
        return self.dir_edit.text().strip() or self.settings.download_dir


_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class SchedulerDialog(QDialog):
    def __init__(self, window: ScheduleWindow, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Schedule")
        self.setMinimumWidth(440)
        self.settings = settings

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        _title_block(layout, "Schedule", "Restrict downloads to a daily time window.")

        self.enabled = QCheckBox("Enable scheduled window")
        self.enabled.setChecked(window.enabled)
        layout.addWidget(self.enabled)

        form = QFormLayout()
        form.setSpacing(10)
        fmt = _time_format(settings.time_format_24h)
        self.start = QTimeEdit(QTime(window.start_hour, window.start_minute))
        self.start.setDisplayFormat(fmt)
        self.end = QTimeEdit(QTime(window.end_hour, window.end_minute))
        self.end.setDisplayFormat(fmt)
        form.addRow("Start", self.start)
        form.addRow("End", self.end)
        layout.addLayout(form)

        # Time format toggle.
        self.use_24h = QCheckBox("24-hour format")
        self.use_24h.setChecked(settings.time_format_24h)
        self.use_24h.toggled.connect(self._on_format_toggled)
        layout.addWidget(self.use_24h)

        days_group = QGroupBox("Days")
        grid = QGridLayout(days_group)
        grid.setSpacing(8)
        self._day_boxes: list[QCheckBox] = []
        for i, name in enumerate(_DAYS):
            box = QCheckBox(name)
            box.setChecked(i in window.days)
            self._day_boxes.append(box)
            grid.addWidget(box, i // 4, i % 4)
        layout.addWidget(days_group)

        hint = QLabel("If End is on or before Start, the window wraps past midnight.")
        hint.setProperty("role", "muted")
        layout.addWidget(hint)

        layout.addWidget(_make_buttons(self, ok_text="Save"))

    def _on_format_toggled(self, checked: bool) -> None:
        fmt = _time_format(checked)
        self.start.setDisplayFormat(fmt)
        self.end.setDisplayFormat(fmt)

    def result_window(self) -> ScheduleWindow:
        return ScheduleWindow(
            enabled=self.enabled.isChecked(),
            start_hour=self.start.time().hour(),
            start_minute=self.start.time().minute(),
            end_hour=self.end.time().hour(),
            end_minute=self.end.time().minute(),
            days=[i for i, b in enumerate(self._day_boxes) if b.isChecked()],
        )

    def use_24h_format(self) -> bool:
        return self.use_24h.isChecked()


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(540)
        self.settings = settings

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        _title_block(layout, "Settings", "Defaults applied to new downloads.")

        # The settings form is taller than the usable desktop on many laptop
        # and scaled displays. Keep the title and action buttons visible while
        # allowing the form itself to shrink and scroll.
        self.settings_scroll = QScrollArea(self)
        self.settings_scroll.setObjectName("settingsScroll")
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setFrameShape(QFrame.NoFrame)
        self.settings_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(14)

        form = QFormLayout()
        form.setSpacing(10)

        self.dir_edit = QLineEdit(settings.download_dir)
        browse = QPushButton("Browse")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.dir_edit, 1)
        row.addWidget(browse)
        form.addRow("Default download folder", row)

        self.connections = _make_connections_combo(settings.connections_per_server)
        form.addRow("Connections per file", self.connections)

        self.max_concurrent = QSpinBox()
        self.max_concurrent.setRange(1, 16)
        self.max_concurrent.setValue(settings.max_concurrent)
        form.addRow("Concurrent downloads", self.max_concurrent)

        speed_row = QHBoxLayout()
        self.speed_limit = QDoubleSpinBox()
        self.speed_unit = QComboBox()
        self.speed_unit.addItems(SPEED_LIMIT_UNITS)
        self.speed_unit.setCurrentText(settings.speed_limit_unit)
        self._speed_display_unit = settings.speed_limit_unit
        self._speed_limit_kbps = settings.overall_speed_limit_kbps
        configure_speed_spin(
            self.speed_limit,
            settings.speed_limit_unit,
            settings.overall_speed_limit_kbps,
        )
        self.speed_limit.valueChanged.connect(self._on_speed_value_changed)
        self.speed_unit.currentTextChanged.connect(self._on_speed_unit_changed)
        speed_row.addWidget(self.speed_limit, 1)
        speed_row.addWidget(self.speed_unit)
        form.addRow("Global speed limit", speed_row)

        self.speed_enabled = QCheckBox("Enable speed limiter")
        self.speed_enabled.setChecked(settings.speed_limiter_enabled)
        form.addRow("", self.speed_enabled)

        self.use_24h = QCheckBox("24-hour clock in scheduler")
        self.use_24h.setChecked(settings.time_format_24h)
        form.addRow("Time format", self.use_24h)

        self.auto_update = QCheckBox("Check for updates on startup")
        self.auto_update.setChecked(settings.auto_update_check)
        self.auto_update.setToolTip(
            "When enabled, Cove pings GitHub Releases on launch and prompts "
            "you if a newer version is available. Updates are never installed "
            "silently - you'll always be asked first."
        )
        form.addRow("Updates", self.auto_update)

        self.close_to_tray = QCheckBox("Close to system tray")
        self.close_to_tray.setChecked(settings.close_to_tray)
        form.addRow("Window", self.close_to_tray)
        tray_note = QLabel(
            "Keeps Cove running so browser downloads can still be sent to it. "
            "Use Quit from the tray menu to exit completely."
        )
        tray_note.setProperty("role", "muted")
        tray_note.setWordWrap(True)
        self.close_to_tray.setToolTip(tray_note.text())
        if not QSystemTrayIcon.isSystemTrayAvailable():
            # Without a tray there would be no icon to restore Cove from, so
            # the close handler ignores the setting entirely. Say so and take
            # the control away rather than offering a switch that does nothing.
            self.close_to_tray.setChecked(False)
            self.close_to_tray.setEnabled(False)
            tray_note.setText(
                "This system has no notification tray, so closing Cove always "
                "exits it completely."
            )
        form.addRow("", tray_note)

        # Magnet links. Actions plus live status, deliberately not a
        # checkbox: on Windows a checkbox would stay ticked after the user
        # closed Settings without choosing Cove, stating something false.
        from . import magnet_handler
        from . import magnet_identity
        from .magnet_identity import WINDOWS_PORTABLE, WINDOWS_SETUP

        self._magnet_handler = magnet_handler
        # Serialises the magnet operations and identifies which one owns the
        # controls; a callback carrying a stale token is dropped.
        self._magnet_op = 0
        # Probed off the GUI thread: status() shells out with a five-second
        # timeout, which is five seconds of a frozen Settings window if it runs
        # here. The controls stay disabled until the answer lands, so nothing
        # can be acted on before its state is known.
        self.magnet_status_label = QLabel("Status: checking\u2026")
        self.magnet_status_label.setProperty("role", "muted")
        self.magnet_status_label.setWordWrap(True)

        # build_identity() only inspects the environment and the executable
        # path - no subprocess - so the button label needs no probe.
        is_windows = magnet_identity.build_identity() in (WINDOWS_SETUP, WINDOWS_PORTABLE)
        self.magnet_action_btn = QPushButton(
            "Choose Cove as default" if is_windows else "Make Cove default"
        )
        self.magnet_remove_btn = QPushButton("Remove Cove registration")
        self.magnet_repair_check = QCheckBox(
            "Repair Cove's magnet registration after updates"
        )
        self.magnet_repair_check.setChecked(
            bool(getattr(settings, "magnet_handler_enabled", False))
        )

        magnet_buttons = QHBoxLayout()
        magnet_buttons.addWidget(self.magnet_action_btn)
        magnet_buttons.addWidget(self.magnet_remove_btn)
        magnet_buttons.addStretch(1)

        magnet_box = QVBoxLayout()
        magnet_box.addWidget(self.magnet_status_label)
        magnet_box.addLayout(magnet_buttons)
        magnet_box.addWidget(self.magnet_repair_check)
        form.addRow("Magnet links", magnet_box)

        self.magnet_action_btn.setEnabled(False)
        self.magnet_remove_btn.setEnabled(False)
        self.magnet_repair_check.setEnabled(False)

        self.magnet_action_btn.clicked.connect(self._on_magnet_enable)
        self.magnet_remove_btn.clicked.connect(self._on_magnet_disable)
        self._refresh_magnet_status()

        self.smart_segments = QCheckBox("Auto-tune connections based on server support")
        self.smart_segments.setChecked(settings.intelligent_segments)
        self.smart_segments.setToolTip(
            "Probes the server before downloading to check Range header support "
            "and adjusts the number of connections based on file size."
        )
        form.addRow("Smart segments", self.smart_segments)

        self.notify_complete = QCheckBox("Notify when a download completes")
        self.notify_complete.setChecked(settings.notify_on_complete)
        form.addRow("Notifications", self.notify_complete)

        self.notify_error = QCheckBox("Notify when a download fails")
        self.notify_error.setChecked(settings.notify_on_error)
        form.addRow("", self.notify_error)

        scroll_layout.addLayout(form)

        # Proxy
        proxy_group = QGroupBox("Proxy")
        proxy_lay = QFormLayout(proxy_group)
        proxy_lay.setSpacing(8)
        self.proxy_type = QComboBox()
        for label, val in [("None", "none"), ("HTTP", "http"),
                           ("HTTPS", "https"), ("SOCKS5", "socks5")]:
            self.proxy_type.addItem(label, val)
        idx = self.proxy_type.findData(settings.proxy_type)
        if idx >= 0:
            self.proxy_type.setCurrentIndex(idx)
        self.proxy_type.currentIndexChanged.connect(self._on_proxy_type_changed)
        proxy_lay.addRow("Type", self.proxy_type)
        self.proxy_host = QLineEdit(settings.proxy_host)
        self.proxy_host.setPlaceholderText("proxy.example.com")
        proxy_lay.addRow("Host", self.proxy_host)
        self.proxy_port = QSpinBox()
        self.proxy_port.setRange(0, 65535)
        self.proxy_port.setSpecialValueText("Default")
        self.proxy_port.setValue(settings.proxy_port)
        proxy_lay.addRow("Port", self.proxy_port)
        self.proxy_user = QLineEdit(settings.proxy_username)
        self.proxy_user.setPlaceholderText("Optional")
        proxy_lay.addRow("Username", self.proxy_user)
        self.proxy_pass = QLineEdit(settings.proxy_password)
        self.proxy_pass.setPlaceholderText("Optional")
        self.proxy_pass.setEchoMode(QLineEdit.Password)
        proxy_lay.addRow("Password", self.proxy_pass)
        self.proxy_note = QLabel("Restart Cove to apply proxy changes.")
        self.proxy_note.setProperty("role", "muted")
        proxy_lay.addRow(self.proxy_note)
        scroll_layout.addWidget(proxy_group)
        self._on_proxy_type_changed()

        # Debrid services
        debrid_group = QGroupBox("Debrid services")
        debrid_lay = QFormLayout(debrid_group)
        debrid_lay.setSpacing(8)

        self.ad_enabled = QCheckBox("Enable AllDebrid")
        self.ad_enabled.setChecked(settings.all_debrid_enabled)
        self.ad_enabled.toggled.connect(self._on_debrid_toggled)
        debrid_lay.addRow(self.ad_enabled)
        self.ad_key = QLineEdit(settings.all_debrid_api_key)
        self.ad_key.setEchoMode(QLineEdit.Password)
        self.ad_key.setPlaceholderText("API key")
        self.ad_test = QPushButton("Test")
        self.ad_test.clicked.connect(self._test_all_debrid)
        ad_row = QHBoxLayout()
        ad_row.addWidget(self.ad_key, 1)
        ad_row.addWidget(self.ad_test)
        debrid_lay.addRow("AllDebrid API key", ad_row)
        self.ad_result = QLabel("")
        self.ad_result.setProperty("role", "muted")
        self.ad_result.setWordWrap(True)
        # Account names come from the provider. QLabel auto-detects rich
        # text, so a markup-shaped username could restyle or spoof this row.
        self.ad_result.setTextFormat(Qt.PlainText)
        debrid_lay.addRow("", self.ad_result)
        debrid_lay.addRow("", _link_label("Get an AllDebrid API key", ALL_DEBRID_KEY_URL))

        self.rd_enabled = QCheckBox("Enable Real-Debrid")
        self.rd_enabled.setChecked(settings.real_debrid_enabled)
        self.rd_enabled.toggled.connect(self._on_debrid_toggled)
        debrid_lay.addRow(self.rd_enabled)
        self.rd_token = QLineEdit(settings.real_debrid_api_token)
        self.rd_token.setEchoMode(QLineEdit.Password)
        self.rd_token.setPlaceholderText("API token")
        self.rd_test = QPushButton("Test")
        self.rd_test.clicked.connect(self._test_real_debrid)
        rd_row = QHBoxLayout()
        rd_row.addWidget(self.rd_token, 1)
        rd_row.addWidget(self.rd_test)
        debrid_lay.addRow("Real-Debrid API token", rd_row)
        self.rd_result = QLabel("")
        self.rd_result.setProperty("role", "muted")
        self.rd_result.setWordWrap(True)
        self.rd_result.setTextFormat(Qt.PlainText)
        debrid_lay.addRow("", self.rd_result)
        debrid_lay.addRow("", _link_label("Get a Real-Debrid API token", REAL_DEBRID_TOKEN_URL))

        # TorBox: hidden/disabled for ordinary users until the T2 slice
        # ships (cove.debrid.TORBOX_FEATURE_AVAILABLE). Kept as one
        # container so the whole block toggles together while still living
        # inside this same Debrid services group, matching AD/RD.
        self.torbox_container = QWidget()
        torbox_form = QFormLayout(self.torbox_container)
        torbox_form.setContentsMargins(0, 0, 0, 0)
        torbox_form.setSpacing(8)
        self.torbox_enabled_cb = QCheckBox("Enable TorBox")
        self.torbox_enabled_cb.setChecked(getattr(settings, "torbox_enabled", False) is True)
        self.torbox_enabled_cb.toggled.connect(self._on_debrid_toggled)
        torbox_form.addRow(self.torbox_enabled_cb)
        self.torbox_token = QLineEdit(getattr(settings, "torbox_api_token", ""))
        self.torbox_token.setEchoMode(QLineEdit.Password)
        self.torbox_token.setPlaceholderText("API token")
        self.torbox_test = QPushButton("Test")
        self.torbox_test.clicked.connect(self._test_torbox)
        torbox_row = QHBoxLayout()
        torbox_row.addWidget(self.torbox_token, 1)
        torbox_row.addWidget(self.torbox_test)
        torbox_form.addRow("TorBox API token", torbox_row)
        self.torbox_result = QLabel("")
        self.torbox_result.setProperty("role", "muted")
        self.torbox_result.setWordWrap(True)
        self.torbox_result.setTextFormat(Qt.PlainText)
        torbox_form.addRow("", self.torbox_result)
        torbox_form.addRow("", _link_label("Get a TorBox API token", TORBOX_TOKEN_URL))
        debrid_lay.addRow(self.torbox_container)
        self.torbox_container.setVisible(debrid.TORBOX_FEATURE_AVAILABLE)

        self.debrid_preferred = QComboBox()
        self.debrid_preferred.addItem("AllDebrid first", DEBRID_ALL_DEBRID)
        self.debrid_preferred.addItem("Real-Debrid first", DEBRID_REAL_DEBRID)
        if debrid.TORBOX_FEATURE_AVAILABLE:
            self.debrid_preferred.addItem("TorBox first", DEBRID_TORBOX)
        idx = self.debrid_preferred.findData(settings.debrid_preferred_provider)
        self.debrid_preferred.setCurrentIndex(idx if idx >= 0 else 0)
        debrid_lay.addRow("Try first", self.debrid_preferred)
        debrid_note = QLabel(
            "Supported hoster links are resolved through your account before "
            "downloading. Other links download normally."
        )
        debrid_note.setProperty("role", "muted")
        debrid_note.setWordWrap(True)
        debrid_lay.addRow(debrid_note)
        scroll_layout.addWidget(debrid_group)
        self._on_debrid_toggled()

        # BitTorrent — deliberately its own group, not part of Debrid
        # services: a cached debrid torrent is an ordinary HTTPS download,
        # while this section is about joining a swarm directly.
        self.torrent_group = QGroupBox("BitTorrent")
        torrent_lay = QFormLayout(self.torrent_group)
        torrent_lay.setSpacing(8)
        self.torrent_enabled = QCheckBox("Enable torrent support")
        self.torrent_enabled.setChecked(
            getattr(settings, "torrent_support_enabled", False) is True
        )
        self.torrent_enabled.toggled.connect(self._on_torrent_toggled)
        torrent_lay.addRow(self.torrent_enabled)

        self.torrent_fallback = QComboBox()
        self.torrent_fallback.addItem(
            "Download locally with BitTorrent", TORRENT_FALLBACK_AUTOMATIC
        )
        # Still the stored value "never": relabelling the option must not
        # invalidate settings files written by earlier versions.
        self.torrent_fallback.addItem("Cancel the download", TORRENT_FALLBACK_NEVER)
        idx = self.torrent_fallback.findData(
            getattr(settings, "torrent_fallback_mode", TORRENT_FALLBACK_AUTOMATIC)
        )
        self.torrent_fallback.setCurrentIndex(idx if idx >= 0 else 0)
        torrent_lay.addRow("When a torrent is not cached", self.torrent_fallback)

        # Interface binding. This lives under BitTorrent because that is the
        # traffic users come here to control, but it is honest about the
        # fact that one shared aria2 daemon means it binds everything.
        self.torrent_interface = QComboBox()
        self._reload_interfaces(
            str(getattr(settings, "torrent_network_interface", "") or "")
        )
        torrent_lay.addRow("Network interface", self.torrent_interface)
        interface_note = QLabel(
            "Binds all downloads handled by aria2, plus debrid resolution, "
            "queue probes, and update checks, to the selected network "
            "interface. Restart Cove to apply changes."
        )
        interface_note.setProperty("role", "muted")
        interface_note.setWordWrap(True)
        torrent_lay.addRow(interface_note)

        self.torrent_proxy_override = QCheckBox(
            "Allow local BitTorrent while proxy settings are enabled"
        )
        self.torrent_proxy_override.setChecked(
            getattr(settings, "torrent_allow_with_proxy", False) is True
        )
        torrent_lay.addRow(self.torrent_proxy_override)

        torrent_note = QLabel(
            "Torrents cached by an enabled debrid service download over HTTPS "
            "and never join the torrent swarm.\n"
            "Downloading locally exposes your IP address to peers and "
            "trackers. Cove stops seeding as soon as a download completes.\n"
            "Cove's ordinary HTTP proxy settings cannot guarantee that peer, "
            "DHT or UDP tracker traffic is proxied."
        )
        torrent_note.setProperty("role", "muted")
        torrent_note.setWordWrap(True)
        torrent_lay.addRow(torrent_note)
        scroll_layout.addWidget(self.torrent_group)
        self._on_torrent_toggled()

        # Custom Torznab indexers (Search v2). A draft list kept separate from
        # the live Settings object: every row operation edits this draft, and
        # only _on_accept copies it onto Settings. Cancel therefore discards
        # every add/edit/remove/toggle with no rollback bookkeeping.
        self._indexer_draft = [
            CustomTorznabIndexer(
                id=record.id,
                enabled=record.enabled,
                name=record.name,
                url=record.url,
                api_key=record.api_key,
            )
            for record in settings.custom_indexers
        ]
        indexer_group = QGroupBox("Custom Torznab indexers")
        indexer_lay = QVBoxLayout(indexer_group)
        indexer_lay.setSpacing(8)
        self.indexer_table = QTableWidget(0, 3)
        self.indexer_table.setHorizontalHeaderLabels(["Enabled", "Name", "Endpoint"])
        self.indexer_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.indexer_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.indexer_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.indexer_table.verticalHeader().setVisible(False)
        self.indexer_table.setSortingEnabled(False)
        header = self.indexer_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.indexer_table.itemSelectionChanged.connect(self._refresh_indexer_buttons)
        indexer_lay.addWidget(self.indexer_table)
        indexer_buttons = QHBoxLayout()
        self.indexer_add_btn = QPushButton("Add")
        self.indexer_edit_btn = QPushButton("Edit")
        self.indexer_remove_btn = QPushButton("Remove")
        self.indexer_add_btn.clicked.connect(self._add_indexer)
        self.indexer_edit_btn.clicked.connect(self._edit_indexer)
        self.indexer_remove_btn.clicked.connect(self._remove_indexer)
        indexer_buttons.addWidget(self.indexer_add_btn)
        indexer_buttons.addWidget(self.indexer_edit_btn)
        indexer_buttons.addWidget(self.indexer_remove_btn)
        indexer_buttons.addStretch(1)
        indexer_lay.addLayout(indexer_buttons)
        indexer_note = QLabel(
            "Add, edit, remove or enable/disable your Torznab indexers here. "
            "Changes apply to the next search."
        )
        indexer_note.setProperty("role", "muted")
        indexer_note.setWordWrap(True)
        indexer_lay.addWidget(indexer_note)
        scroll_layout.addWidget(indexer_group)
        self._refresh_indexer_table()

        # Category folders
        cat_group = QGroupBox("Category folders")
        cat_lay = QFormLayout(cat_group)
        cat_lay.setSpacing(8)
        self._cat_edits: dict[str, QLineEdit] = {}
        for name in CATEGORY_NAMES:
            current = getattr(settings.category_dirs, name, "")
            edit = QLineEdit(current)
            edit.setPlaceholderText(f"Use default download folder")
            btn = QPushButton("Browse")
            btn.clicked.connect(lambda _=False, e=edit, n=name: self._browse_category(e, n))
            row_h = QHBoxLayout()
            row_h.addWidget(edit, 1)
            row_h.addWidget(btn)
            cat_lay.addRow(name, row_h)
            self._cat_edits[name] = edit
        self.auto_sort = QCheckBox("Create category subfolders automatically")
        self.auto_sort.setChecked(settings.auto_sort_by_category)
        self.auto_sort.setToolTip(
            "When enabled and a category folder is not set, Cove creates a "
            "subfolder under the default download folder (e.g. Downloads/Videos)."
        )
        cat_lay.addRow(self.auto_sort)
        cat_note = QLabel("Leave blank to use the default download folder.")
        cat_note.setProperty("role", "muted")
        cat_lay.addRow(cat_note)
        scroll_layout.addWidget(cat_group)

        self.settings_scroll.setWidget(scroll_content)
        layout.addWidget(self.settings_scroll, 1)

        # Keep a direct reference to the button box rather than fishing it
        # back out of the layout by index (which breaks if layout order
        # changes). Route Save through _on_accept instead of the default.
        bb = _make_buttons(self, ok_text="Save")
        layout.addWidget(bb)
        bb.accepted.disconnect()
        bb.accepted.connect(self._on_accept)

        screen = self.screen()
        if screen is not None:
            available = screen.availableGeometry()
            width = max(320, min(900, available.width() - 80))
            if width < self.minimumWidth():
                self.setMinimumWidth(width)
            height = min(
                720,
                max(self.minimumSizeHint().height(), available.height() - 80),
            )
            self.resize(width, height)
        else:  # Defensive fallback for unusual headless Qt platforms.
            self.resize(900, 640)

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Default download folder", self.dir_edit.text())
        if path:
            self.dir_edit.setText(path)

    def _browse_category(self, edit: QLineEdit, name: str) -> None:
        start = edit.text() or self.dir_edit.text()
        path = QFileDialog.getExistingDirectory(self, f"{name} folder", start)
        if path:
            edit.setText(path)

    def _on_proxy_type_changed(self, _index: int = 0) -> None:
        enabled = self.proxy_type.currentData() != "none"
        self.proxy_host.setEnabled(enabled)
        self.proxy_port.setEnabled(enabled)
        self.proxy_user.setEnabled(enabled)
        self.proxy_pass.setEnabled(enabled)

    # ---- debrid -------------------------------------------------------

    def _on_debrid_toggled(self, _checked: bool = False) -> None:
        """Mirror the proxy section: the credential row follows its switch."""
        for enabled_box, edit, test in (
            (self.ad_enabled, self.ad_key, self.ad_test),
            (self.rd_enabled, self.rd_token, self.rd_test),
            (self.torbox_enabled_cb, self.torbox_token, self.torbox_test),
        ):
            on = enabled_box.isChecked()
            edit.setEnabled(on)
            # Don't re-enable a Test button that is currently mid-request.
            test.setEnabled(on and test.property("testing") is not True)

    # ---- torrents -----------------------------------------------------

    def _reload_interfaces(self, current: str) -> None:
        """Fill the interface combo, preserving an unavailable saved name.

        A saved interface that has gone away stays selected and is marked
        as missing rather than being dropped: silently reverting it to
        "Any interface" is exactly the fall back Cove promises not to do.
        """
        self.torrent_interface.clear()
        self.torrent_interface.addItem(ANY_INTERFACE_LABEL, ANY_INTERFACE)
        names = list_interfaces()
        for name in names:
            self.torrent_interface.addItem(name, name)
        if current and current not in names:
            self.torrent_interface.addItem(f"{current} (not available)", current)
        idx = self.torrent_interface.findData(current)
        self.torrent_interface.setCurrentIndex(idx if idx >= 0 else 0)

    def _on_torrent_toggled(self, _checked: bool = False) -> None:
        """The torrent rows follow their switch, as the proxy rows do."""
        on = self.torrent_enabled.isChecked()
        self.torrent_fallback.setEnabled(on)
        self.torrent_proxy_override.setEnabled(on)

    def _test_all_debrid(self) -> None:
        self._run_account_test(
            DEBRID_ALL_DEBRID, self.ad_test, self.ad_result,
            self.ad_key.text().strip(), debrid.all_debrid_account,
        )

    def _test_real_debrid(self) -> None:
        self._run_account_test(
            DEBRID_REAL_DEBRID, self.rd_test, self.rd_result,
            self.rd_token.text().strip(), debrid.real_debrid_account,
        )

    def _test_torbox(self) -> None:
        self._run_account_test(
            DEBRID_TORBOX, self.torbox_test, self.torbox_result,
            self.torbox_token.text().strip(), debrid.torbox_account,
        )

    def _run_account_test(self, provider, button, result, credential, fn) -> None:
        label = debrid.provider_label(provider)
        if not credential:
            result.setText(f"{label}: enter an API key first.")
            return
        result.setText(f"Checking {label}...")
        button.setProperty("testing", True)
        button.setEnabled(False)

        # Resolve the callable through the module at call time so the test
        # runs against whatever cove.debrid currently exposes.
        call = _AccountTest(provider, lambda: fn(credential))
        _INFLIGHT_ACCOUNT_TESTS.add(call)
        call.signals.done.connect(self._on_account_test_done)
        call.signals.failed.connect(self._on_account_test_failed)
        call.signals.finished.connect(
            lambda c=call: _INFLIGHT_ACCOUNT_TESTS.discard(c)
        )
        QThreadPool.globalInstance().start(call)

    def _debrid_widgets(self, provider):
        if provider == DEBRID_ALL_DEBRID:
            return self.ad_test, self.ad_result
        if provider == DEBRID_TORBOX:
            return self.torbox_test, self.torbox_result
        return self.rd_test, self.rd_result

    def _finish_account_test(self, provider, message: str) -> None:
        button, result = self._debrid_widgets(provider)
        result.setText(message)
        button.setProperty("testing", False)
        self._on_debrid_toggled()

    def _on_account_test_done(self, provider: str, account: object) -> None:
        self._finish_account_test(provider, _account_summary(provider, account))

    def _on_account_test_failed(self, provider: str, message: str) -> None:
        self._finish_account_test(provider, message)

    def _on_speed_unit_changed(self, unit: str) -> None:
        self._speed_display_unit = unit
        configure_speed_spin(
            self.speed_limit,
            unit,
            self._speed_limit_kbps,
        )

    def _on_speed_value_changed(self, value: float) -> None:
        self._speed_limit_kbps = speed_value_to_kbps(
            value, self._speed_display_unit
        )

    def _magnet_status_text(self, state) -> str:
        """Wording derived from the system, never from the stored setting."""
        if not state.supported:
            return (
                "Magnet registration needs an installed or portable build. "
                "Running Cove from source cannot register a stable path."
            )
        if state.is_default:
            return "Status: Cove is the current default"
        if state.registered:
            return "Status: Registered, but not currently selected as default"
        return "Status: Not registered"

    def _begin_magnet_op(self) -> int:
        """Claim the magnet controls for one operation, returning its token.

        Registration and removal both rewrite the same association, so they
        must never overlap - and a control left disabled by a failed probe
        would strand the user with no way to retry. Every path back out goes
        through _on_magnet_status, which restores the controls.
        """
        self._magnet_op += 1
        self.magnet_action_btn.setEnabled(False)
        self.magnet_remove_btn.setEnabled(False)
        self.magnet_repair_check.setEnabled(False)
        return self._magnet_op

    def _refresh_magnet_status(self, token: int | None = None) -> None:
        if token is None:
            token = self._begin_magnet_op()
        _run_magnet_probe(
            self._magnet_handler.status,
            lambda state, t=token: self._on_magnet_status(t, state),
        )

    def _on_magnet_status(self, token: int, state) -> None:
        """Apply a probe result. Never called on the worker thread.

        A result from a superseded operation is dropped: the newer one owns
        the controls and will report its own outcome.
        """
        if token != self._magnet_op:
            return
        if state is None:
            # The probe failed, so the real state is unknown. Re-enable the
            # controls rather than leaving the user unable to try again.
            self.magnet_status_label.setText("Status: could not be determined")
            self.magnet_action_btn.setEnabled(True)
            self.magnet_remove_btn.setEnabled(True)
            self.magnet_repair_check.setEnabled(True)
            return
        self.magnet_status_label.setText(self._magnet_status_text(state))
        self.magnet_action_btn.setEnabled(state.supported)
        self.magnet_remove_btn.setEnabled(state.supported)
        self.magnet_repair_check.setEnabled(state.supported)
        if not state.supported:
            self.magnet_repair_check.setChecked(False)

    def _on_magnet_enable(self) -> None:
        # enable() runs xdg-mime too, so it goes off-thread for the same reason
        # the status probe does. All the magnet controls are disabled meanwhile,
        # so a removal cannot race a registration over the same association.
        token = self._begin_magnet_op()
        _run_magnet_probe(
            self._magnet_handler.enable,
            lambda result, t=token: self._on_magnet_enabled(t, result),
        )

    def _on_magnet_enabled(self, token: int, result) -> None:
        if token != self._magnet_op:
            return
        # Re-probe under the same token so the controls come back exactly once.
        self._refresh_magnet_status(token)
        if result is None:
            return
        if not result.ok:
            if result.message:
                QMessageBox.information(self, "Magnet links", result.message)
            return
        # Only open the Default Apps deep link on success: opening it after
        # a failed enable() puts the failure message box behind that window.
        url = self._magnet_handler.default_apps_url()
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _on_magnet_disable(self) -> None:
        token = self._begin_magnet_op()
        _run_magnet_probe(
            self._magnet_handler.disable,
            lambda result, t=token: self._on_magnet_disabled(t, result),
        )

    def _on_magnet_disabled(self, token: int, result) -> None:
        if token != self._magnet_op:
            return
        self._refresh_magnet_status(token)
        if result is None:
            return
        if not result.ok and result.message:
            QMessageBox.information(self, "Magnet links", result.message)
            return
        self.magnet_repair_check.setChecked(False)

    def _refresh_indexer_table(self) -> None:
        """Rebuild the table from ``self._indexer_draft`` in draft order.

        Table rows mirror the draft list 1:1 and sorting is disabled, so a row
        index is the draft position and no display reordering can mutate the
        persisted order S5 uses as its deterministic tie-break.
        """
        self.indexer_table.setRowCount(0)
        for record in self._indexer_draft:
            row = self.indexer_table.rowCount()
            self.indexer_table.insertRow(row)
            checkbox = QCheckBox()
            checkbox.setChecked(record.enabled)
            checkbox.toggled.connect(
                lambda checked, rec=record: self._on_indexer_toggled(rec, checked)
            )
            self.indexer_table.setCellWidget(row, 0, checkbox)
            name_item = QTableWidgetItem(record.name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
            self.indexer_table.setItem(row, 1, name_item)
            url_item = QTableWidgetItem(redact_url(record.url))
            url_item.setFlags(url_item.flags() & ~Qt.ItemIsEditable)
            self.indexer_table.setItem(row, 2, url_item)
        self._refresh_indexer_buttons()

    def _refresh_indexer_buttons(self) -> None:
        has_selection = self.indexer_table.currentRow() >= 0
        self.indexer_edit_btn.setEnabled(has_selection)
        self.indexer_remove_btn.setEnabled(has_selection)

    def _on_indexer_toggled(self, record: CustomTorznabIndexer, checked: bool) -> None:
        # Mutate the draft record in place: id, name, url, key and position are
        # all untouched, so a toggle never reorders or re-identifies the row.
        record.enabled = checked

    def _indexer_interface(self) -> str:
        return self.torrent_interface.currentData() or ""

    def _add_indexer(self) -> None:
        indexer = CustomTorznabIndexer(
            id=new_custom_indexer_id(), enabled=True, name="", url="", api_key=""
        )
        editor = IndexerEditorDialog(
            indexer, interface=self._indexer_interface(), is_new=True, parent=self
        )
        if editor.exec() == QDialog.Accepted:
            result = editor.result()
            if result is not None:
                self._indexer_draft.append(result)
                self._refresh_indexer_table()
                self.indexer_table.setCurrentCell(
                    self.indexer_table.rowCount() - 1, 0
                )

    def _edit_indexer(self) -> None:
        row = self.indexer_table.currentRow()
        if row < 0 or row >= len(self._indexer_draft):
            return
        editor = IndexerEditorDialog(
            self._indexer_draft[row],
            interface=self._indexer_interface(),
            is_new=False,
            parent=self,
        )
        if editor.exec() == QDialog.Accepted:
            result = editor.result()
            if result is not None:
                # Replace in place: id and position are preserved by design.
                self._indexer_draft[row] = result
                self._refresh_indexer_table()
                self.indexer_table.setCurrentCell(row, 0)

    def _remove_indexer(self) -> None:
        row = self.indexer_table.currentRow()
        if row < 0 or row >= len(self._indexer_draft):
            return
        del self._indexer_draft[row]
        self._refresh_indexer_table()

    def _on_accept(self) -> None:
        self.settings.download_dir = self.dir_edit.text().strip() or self.settings.download_dir
        self.settings.connections_per_server = self.connections.currentData()
        self.settings.max_concurrent = self.max_concurrent.value()
        self.settings.overall_speed_limit_kbps = self._speed_limit_kbps
        self.settings.speed_limiter_enabled = self.speed_enabled.isChecked()
        self.settings.speed_limit_unit = self.speed_unit.currentText()
        self.settings.time_format_24h = self.use_24h.isChecked()
        self.settings.auto_update_check = self.auto_update.isChecked()
        # Read back on the shared Settings object MainWindow already holds, so
        # the next X press honours the new value without a restart.
        self.settings.close_to_tray = self.close_to_tray.isChecked()
        self.settings.magnet_handler_enabled = self.magnet_repair_check.isChecked()
        self.settings.intelligent_segments = self.smart_segments.isChecked()
        self.settings.notify_on_complete = self.notify_complete.isChecked()
        self.settings.notify_on_error = self.notify_error.isChecked()
        self.settings.proxy_type = self.proxy_type.currentData()
        self.settings.proxy_host = self.proxy_host.text().strip()
        self.settings.proxy_port = self.proxy_port.value()
        self.settings.proxy_username = self.proxy_user.text().strip()
        self.settings.proxy_password = self.proxy_pass.text()
        self.settings.auto_sort_by_category = self.auto_sort.isChecked()
        self.settings.all_debrid_enabled = self.ad_enabled.isChecked()
        self.settings.all_debrid_api_key = self.ad_key.text().strip()
        self.settings.real_debrid_enabled = self.rd_enabled.isChecked()
        self.settings.real_debrid_api_token = self.rd_token.text().strip()
        self.settings.torbox_enabled = self.torbox_enabled_cb.isChecked()
        self.settings.torbox_api_token = self.torbox_token.text().strip()
        # The combo has no "TorBox first" entry while the feature gate is
        # off, so its currentData() can never be "torbox" in that state.
        # Saving it unconditionally would then silently reset a previously
        # stored TorBox preference (e.g. set during T1 development testing)
        # back to AllDebrid every time Settings is saved.
        if debrid.TORBOX_FEATURE_AVAILABLE or self.settings.debrid_preferred_provider != DEBRID_TORBOX:
            self.settings.debrid_preferred_provider = self.debrid_preferred.currentData()
        self.settings.torrent_support_enabled = self.torrent_enabled.isChecked()
        self.settings.torrent_fallback_mode = self.torrent_fallback.currentData()
        self.settings.torrent_allow_with_proxy = self.torrent_proxy_override.isChecked()
        self.settings.torrent_network_interface = (
            self.torrent_interface.currentData() or ""
        )
        # torrent_ip_disclosure_shown is not written here on purpose: it
        # records the user's answer to the one-time P2P notice, and Save
        # must neither grant nor revoke that consent.
        for name, edit in self._cat_edits.items():
            setattr(self.settings.category_dirs, name, edit.text().strip())
        # Copy the draft custom-indexer list onto the shared Settings object
        # MainWindow already holds, so the S5 live provider sees the change on
        # the next Search generation. Saving stays the canonical Settings path.
        self.settings.custom_indexers = list(self._indexer_draft)
        self.settings.save()
        self.accept()
