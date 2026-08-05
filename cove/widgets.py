"""Reusable cove widgets.

Mirrors the visual primitives used in cove-compressor / cove-screen-recorder:
    * Titlebar       — frameless bar with brand badge, title, version, controls
    * BrandBadge     — DPR-aware QPainter render of cove_icon.png so the
                       skull stays crisp at small sizes
    * IconButton     — QPainter min/max/restore/close glyphs
    * FramelessResizer — installs an event filter so the QMainWindow
                         supports edge-drag resize via startSystemResize()
    * StatusPill / Section / StatsStrip / Footer — content widgets
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QIcon,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


def _named_icon_candidates(name: str) -> list[Path]:
    """Every place `name` can live: source tree, bundles, installed trees."""
    import os
    import sys

    here = Path(__file__).resolve().parent
    candidates: list[Path] = [
        here / name,                                # package data
        here.parent / name,                         # source-tree root
        Path.cwd() / name,
    ]

    # PyInstaller one-file: assets are extracted under _MEIPASS at runtime.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.insert(0, Path(meipass) / "cove" / name)
        candidates.insert(0, Path(meipass) / name)

    # PyInstaller one-dir: assets sit next to the executable (or under
    # _internal/ depending on the PyInstaller version).
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates += [
            exe_dir / "cove" / name,
            exe_dir / "_internal" / "cove" / name,
            exe_dir / name,
        ]

    appdir = os.environ.get("APPDIR")
    if appdir:
        appdir_path = Path(appdir)
        candidates += [
            appdir_path / name,
            appdir_path / f"usr/lib/cove-download-manager/cove/{name}",
        ]
    return candidates


def _installed_icon_candidates() -> list[Path]:
    """Icons installed under their packaging names rather than a source name.

    The AppImage and .deb both install whichever artwork the build copied in
    under these fixed names, so they need no name preference of their own.
    """
    import os

    appdir = os.environ.get("APPDIR")
    if not appdir:
        return []
    appdir_path = Path(appdir)
    return [
        appdir_path / "cove.png",
        appdir_path / "usr/share/icons/hicolor/256x256/apps/cove.png",
        appdir_path / "usr/share/icons/hicolor/256x256/apps/cove-download-manager.png",
        appdir_path / "usr/share/icons/hicolor/512x512/apps/cove.png",
    ]


def find_icon() -> Path | None:
    """Locate the app icon. Works from source, from PyInstaller bundles
    (one-file via _MEIPASS, one-dir via sys.executable), from the
    python-appimage layout, and from the cove-compressor-style AppImage
    (icon at $APPDIR root + hicolor tree).

    The download-manager mark wins everywhere it exists; the shared suite
    icon is the fallback so a build without the new asset still has an icon.
    """
    candidates = [
        *_named_icon_candidates("cove_dm_icon.png"),
        *_named_icon_candidates("cove_icon.png"),
        *_installed_icon_candidates(),
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


# ── Brand badge — Cove icon inside a rounded square ────────────────────────
class BrandBadge(QLabel):
    """Square tile with the Cove icon centered inside.

    The source PNG is 512x512. We render to a high-DPR pixmap so the icon
    stays crisp on HiDPI and standard displays alike — that's the
    difference between a sharp icon and the blurry one a naive
    `QLabel.setPixmap(QPixmap.scaled(...))` produces.
    """

    def __init__(self, size: int = 26):
        super().__init__()
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignCenter)
        self._src: QPixmap | None = None
        path = find_icon()
        if path is not None:
            pm = QPixmap(str(path))
            if not pm.isNull():
                self._src = pm

    def paintEvent(self, _event):
        if self._src is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        dpr = max(1.0, float(self.devicePixelRatioF()))
        side = self.width()
        target = self._src.scaled(
            int(side * dpr),
            int(side * dpr),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        target.setDevicePixelRatio(dpr)
        p.drawPixmap(0, 0, side, side, target)
        p.end()


# ── Theme toggle (sun / moon glyph) ───────────────────────────────────────
class ThemeButton(QToolButton):
    """Sun/moon glyph button rendered with QPainter — no SVG dependency.

    Mirrors the upscaler's `<ThemeToggle>`: dark mode shows a sun (click =
    go light), light mode shows a moon (click = go dark)."""

    toggled_theme = Signal(str)  # emits "dark" or "light"

    def __init__(self, theme_name: str = "dark", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("themeBtn")
        self.setFixedSize(32, 28)
        self.setCursor(Qt.PointingHandCursor)
        self.setAutoRaise(True)
        self._theme = theme_name
        self._refresh_tooltip()
        self.clicked.connect(self._on_click)

    def set_theme(self, name: str) -> None:
        self._theme = name
        self._refresh_tooltip()
        self.update()

    def _refresh_tooltip(self) -> None:
        if self._theme == "dark":
            self.setToolTip("Switch to light mode")
        else:
            self.setToolTip("Switch to dark mode")

    def _on_click(self) -> None:
        self.toggled_theme.emit("light" if self._theme == "dark" else "dark")

    def sizeHint(self) -> QSize:
        return QSize(32, 28)

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        color = self.palette().color(self.foregroundRole())
        pen = QPen(color, 1.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        cx, cy = self.width() / 2, self.height() / 2
        if self._theme == "dark":
            # Sun: small circle + 8 rays
            r = 3.2
            p.drawEllipse(QPoint(int(cx), int(cy)), int(r), int(r))
            import math
            for i in range(8):
                a = i * (math.pi / 4)
                inner = 5.2
                outer = 7.4
                x1 = cx + math.cos(a) * inner
                y1 = cy + math.sin(a) * inner
                x2 = cx + math.cos(a) * outer
                y2 = cy + math.sin(a) * outer
                p.drawLine(int(x1), int(y1), int(x2), int(y2))
        else:
            # Moon: crescent
            path = QPainterPath()
            path.addEllipse(QPoint(int(cx), int(cy)), 6, 6)
            cut = QPainterPath()
            cut.addEllipse(QPoint(int(cx + 3), int(cy - 1)), 5, 5)
            crescent = path.subtracted(cut)
            p.setBrush(color)
            p.setPen(Qt.NoPen)
            p.drawPath(crescent)
        p.end()


# ── Window control buttons (painted glyphs, no fonts/SVG) ─────────────────
class _CtrlButton(QToolButton):
    def __init__(self, kind: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(32, 28)
        self.setObjectName("CtrlBtn")
        self.setCursor(Qt.ArrowCursor)
        self._kind = kind
        self.setAutoRaise(True)

    def sizeHint(self) -> QSize:
        return QSize(32, 28)

    def paintEvent(self, _event):
        super().paintEvent(_event)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        color = self.palette().color(self.foregroundRole())
        p.setPen(QPen(color, 1.6))
        p.setBrush(Qt.NoBrush)
        cx, cy = self.width() / 2, self.height() / 2
        if self._kind == "min":
            p.drawLine(int(cx - 5), int(cy), int(cx + 5), int(cy))
        elif self._kind == "max":
            p.drawRect(QRect(int(cx - 5), int(cy - 5), 10, 10))
        elif self._kind == "restore":
            p.drawRect(QRect(int(cx - 5), int(cy - 3), 8, 8))
            p.drawRect(QRect(int(cx - 3), int(cy - 5), 8, 8))
        elif self._kind == "close":
            p.drawLine(int(cx - 5), int(cy - 5), int(cx + 5), int(cy + 5))
            p.drawLine(int(cx + 5), int(cy - 5), int(cx - 5), int(cy + 5))
        p.end()


class Titlebar(QFrame):
    """Frameless-window drag bar.

    Drag is delegated to the windowing system via `startSystemMove()` —
    that's the only approach that works on Wayland (clients can't move
    themselves). Double-click toggles maximize.
    """

    def __init__(
        self,
        window: QMainWindow,
        app_name: str,
        version: str,
        theme_name: str = "dark",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("titlebar")
        self.setFixedHeight(38)
        self._window = window

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 6, 0)
        lay.setSpacing(10)

        self.badge = BrandBadge(26)
        lay.addWidget(self.badge, 0, Qt.AlignVCenter)

        lay.addStretch(1)

        primary = QLabel(app_name)
        primary.setObjectName("titlebarTitle")
        primary.setProperty("role", "primary")
        lay.addWidget(primary, 0, Qt.AlignVCenter)

        ver = QLabel(f"v{version}")
        ver.setObjectName("titlebarVer")
        lay.addWidget(ver, 0, Qt.AlignVCenter)

        lay.addStretch(1)

        # Theme toggle + window controls form one tight button cluster
        # (matches Cove Universal Converter's titlebar control grouping).
        controls = QWidget(self)
        ctrl_lay = QHBoxLayout(controls)
        ctrl_lay.setContentsMargins(0, 0, 0, 0)
        ctrl_lay.setSpacing(2)

        self.theme_btn = ThemeButton(theme_name)
        ctrl_lay.addWidget(self.theme_btn)

        self.min_btn = _CtrlButton("min")
        self.min_btn.setToolTip("Minimize")
        self.min_btn.clicked.connect(self._window.showMinimized)
        ctrl_lay.addWidget(self.min_btn)

        self.max_btn = _CtrlButton("max")
        self.max_btn.setToolTip("Maximize")
        self.max_btn.clicked.connect(self._toggle_maximize)
        ctrl_lay.addWidget(self.max_btn)

        self.close_btn = _CtrlButton("close")
        self.close_btn.setObjectName("CloseBtn")
        self.close_btn.setToolTip("Close")
        self.close_btn.clicked.connect(self._window.close)
        ctrl_lay.addWidget(self.close_btn)

        lay.addWidget(controls, 0, Qt.AlignVCenter)

    def _toggle_maximize(self) -> None:
        if self._window.isMaximized():
            self._window.showNormal()
        else:
            self._window.showMaximized()
        self.sync_max_glyph()

    def sync_max_glyph(self) -> None:
        """Match the button glyph to the window's actual maximized state, so
        it stays correct even when the state changes outside this button
        (OS shortcut, window snapping, etc.)."""
        kind = "restore" if self._window.isMaximized() else "max"
        if self.max_btn._kind != kind:
            self.max_btn._kind = kind
            self.max_btn.update()

    # ── drag / double-click ───────────────────────────────────────────
    def _on_button(self, child: QWidget | None) -> bool:
        while child is not None:
            if isinstance(child, QToolButton):
                return True
            child = child.parentWidget()
        return False

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton and not self._on_button(self.childAt(event.pos())):
            handle = self._window.windowHandle()
            if handle is not None:
                handle.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton and not self._on_button(self.childAt(event.pos())):
            self._toggle_maximize()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class FramelessResizer(QObject):
    """Lets a frameless QMainWindow be resized from its edges via
    `startSystemResize()`. Cross-platform; no WM-specific glue."""

    BORDER = 18

    def __init__(self, window: QMainWindow):
        super().__init__(window)
        self._w = window
        window.setMouseTracking(True)
        window.installEventFilter(self)

    def _edge_for(self, pos: QPoint) -> Qt.Edges:
        w = self._w
        if w.isMaximized() or w.isFullScreen():
            return Qt.Edges()
        r = w.rect()
        b = self.BORDER
        edges = Qt.Edges()
        if pos.x() <= b:
            edges |= Qt.LeftEdge
        elif pos.x() >= r.width() - b:
            edges |= Qt.RightEdge
        if pos.y() <= b:
            edges |= Qt.TopEdge
        elif pos.y() >= r.height() - b:
            edges |= Qt.BottomEdge
        return edges

    def _cursor_for(self, edges: Qt.Edges):
        h = bool(edges & (Qt.LeftEdge | Qt.RightEdge))
        v = bool(edges & (Qt.TopEdge | Qt.BottomEdge))
        if h and v:
            tl_br = (
                (edges & (Qt.TopEdge | Qt.LeftEdge)) == (Qt.TopEdge | Qt.LeftEdge)
                or (edges & (Qt.BottomEdge | Qt.RightEdge))
                == (Qt.BottomEdge | Qt.RightEdge)
            )
            return Qt.SizeFDiagCursor if tl_br else Qt.SizeBDiagCursor
        if h:
            return Qt.SizeHorCursor
        if v:
            return Qt.SizeVerCursor
        return None

    def eventFilter(self, obj, event):
        if obj is not self._w:
            return False
        et = event.type()
        if et == QEvent.MouseMove and not (event.buttons() & Qt.LeftButton):
            edges = self._edge_for(event.position().toPoint())
            shape = self._cursor_for(edges)
            if shape is not None:
                self._w.setCursor(shape)
            else:
                self._w.unsetCursor()
        elif et == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            edges = self._edge_for(event.position().toPoint())
            if edges:
                handle = self._w.windowHandle()
                if handle is not None:
                    handle.startSystemResize(edges)
                    return True
        elif et == QEvent.Leave:
            self._w.unsetCursor()
        return False


class StatusPill(QLabel):
    """Uppercase mono badge whose color responds to a `state` property."""

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text.upper(), parent)
        self.setObjectName("statusPill")
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)

    def set_state(self, state: str, text: str) -> None:
        """state in {ok, paused, error, off}."""
        self.setText(text.upper())
        self.setProperty("state", state)
        self.style().unpolish(self)
        self.style().polish(self)


class Section(QFrame):
    """Bordered surface card with optional uppercase label and content."""

    def __init__(self, label: str | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setProperty("role", "section")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)
        if label:
            lab = QLabel(label.upper())
            lab.setProperty("role", "section-label")
            outer.addWidget(lab)
        self._body = QVBoxLayout()
        self._body.setContentsMargins(0, 0, 0, 0)
        self._body.setSpacing(10)
        outer.addLayout(self._body)

    def body(self) -> QVBoxLayout:
        return self._body


class StatsStrip(QFrame):
    """Horizontal row of (KEY, value) mono cells."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("statsStrip")
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(0)
        self._values: dict[str, QLabel] = {}

    def add_cell(self, key: str, value: str = "—", *, last: bool = False) -> None:
        cell = QFrame()
        cell.setObjectName("statCellLast" if last else "statCell")
        v = QVBoxLayout(cell)
        v.setContentsMargins(14, 10, 14, 10)
        v.setSpacing(2)
        k_lab = QLabel(key.upper())
        k_lab.setObjectName("statKey")
        val_lab = QLabel(value)
        val_lab.setObjectName("statValue")
        v.addWidget(k_lab)
        v.addWidget(val_lab)
        self._lay.addWidget(cell, 1)
        self._values[key] = val_lab

    def set_value(self, key: str, value: str) -> None:
        if key in self._values:
            self._values[key].setText(value)


class Footer(QFrame):
    """Bottom hotkey/info strip.

    Hotkeys render as object-named QLabels (not inline-styled HTML) so
    they pick up palette changes when the theme switches at runtime."""

    folder_clicked = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("footer")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(28, 8, 28, 8)
        lay.setSpacing(0)

        items_wrap = QWidget()
        self._items_lay = QHBoxLayout()
        self._items_lay.setContentsMargins(0, 0, 0, 0)
        self._items_lay.setSpacing(22)
        items_wrap.setLayout(self._items_lay)
        items_wrap.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        lay.addWidget(items_wrap, 0, Qt.AlignLeft)
        lay.addStretch(1)

        right = QHBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(20)

        # Folder chip — clickable, opens the downloads folder.
        self.folder_btn = QPushButton("")
        self.folder_btn.setObjectName("folderChip")
        self.folder_btn.setCursor(Qt.PointingHandCursor)
        self.folder_btn.setVisible(False)
        self.folder_btn.clicked.connect(self.folder_clicked.emit)
        self.folder_btn.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        right.addWidget(self.folder_btn, 0, Qt.AlignRight)

        self.platform = QLabel("")
        self.platform.setObjectName("footerPlatform")
        self.platform.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        right.addWidget(self.platform, 0, Qt.AlignRight)
        lay.addLayout(right)

    def add_hotkey(self, label: str, keys: str) -> None:
        wrap = QFrame()
        h = QHBoxLayout(wrap)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        lab = QLabel(label.upper())
        lab.setObjectName("footerLabel")
        key = QLabel(keys)
        key.setObjectName("footerKey")
        h.addWidget(lab)
        h.addWidget(key)
        wrap.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        self._items_lay.addWidget(wrap)

    def set_platform(self, text: str) -> None:
        self.platform.setText(text)

    def set_folder(self, full_path: str, display: str) -> None:
        self.folder_btn.setText(display)
        self.folder_btn.setToolTip(f"Open {full_path}")
        self.folder_btn.setVisible(bool(display))


def _hex_to_bits(hexstr: str, num_pieces: int) -> list[bool]:
    if not hexstr:
        return [False] * num_pieces
    try:
        bits = bin(int(hexstr, 16))[2:].zfill(len(hexstr) * 4)
    except ValueError:
        return [False] * num_pieces
    return [b == "1" for b in bits[:num_pieces]]


class SegmentBar(QFrame):
    """Colored block visualization of per-piece download progress."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._bitfield = ""
        self._num_pieces = 0
        self._segments = 0
        self.setFixedHeight(14)

    def set_data(self, bitfield: str, num_pieces: int, segments: int = 0) -> None:
        self._bitfield = bitfield
        self._num_pieces = num_pieces
        self._segments = segments
        self.update()

    def paintEvent(self, _event):
        from . import theme as _theme
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        w = self.width()
        h = self.height()
        bg = QColor(_theme.SURFACE_2)
        p.fillRect(0, 0, w, h, bg)
        if self._num_pieces <= 0:
            if self._segments > 0:
                label = f"{self._segments} conn"
                p.setPen(QColor(_theme.TEXT_FAINT))
                p.drawText(self.rect(), Qt.AlignCenter, label)
            p.end()
            return
        bits = _hex_to_bits(self._bitfield, self._num_pieces)
        done_color = QColor(_theme.ACCENT)
        cell_w = w / self._num_pieces
        for i, bit in enumerate(bits):
            if bit:
                x = int(i * cell_w)
                cw = max(1, int((i + 1) * cell_w) - x)
                p.fillRect(x, 0, cw, h, done_color)
        p.end()
