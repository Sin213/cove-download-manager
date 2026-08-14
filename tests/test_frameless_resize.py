"""Frameless edge-drag resize works over the widgets that cover the border.

The window is frameless, so FramelessResizer is what turns a press near the
window boundary into a native resize. Its event filter used to be installed on
the QMainWindow alone, and the chrome fills the interior edge-to-edge, so every
press inside the intended grab band landed on a child widget and was never seen
by the resizer. The configured band existed on paper only: the user had to hit
the outermost pixel row that no child covered.

These tests drive real QWidget hierarchies with real Qt event delivery. Only
the last hop - the compositor call, QWindow.startSystemResize - is replaced by
a recorder, because a unit test cannot ask the window manager to resize a
window.
"""
import pytest
import shiboken6
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from cove.widgets import FramelessResizer, Titlebar

QApplication.instance() or QApplication([])

_live = []


@pytest.fixture(autouse=True)
def _destroy():
    yield
    while _live:
        obj = _live.pop()
        if shiboken6.isValid(obj):
            obj.setParent(None)
            shiboken6.delete(obj)
    QApplication.processEvents()


class RecordingHandle:
    """Stands in for the QWindow handle at the compositor boundary only."""

    def __init__(self):
        self.calls = []

    def startSystemResize(self, edges):
        self.calls.append(edges)
        return True


def _window(width=400, height=300):
    """A frameless window whose interior is fully covered by a child widget.

    The child is a QPushButton because the real chrome is made of interactive
    widgets: they *accept* mouse presses, so Qt does not propagate the press up
    to the QMainWindow. A plain QWidget would ignore the press and let it
    bubble, which would hide the routing defect this file is about.
    """
    win = QMainWindow()
    _live.append(win)
    win.resize(width, height)
    central = QWidget()
    layout = QVBoxLayout(central)
    layout.setContentsMargins(0, 0, 0, 0)
    child = QPushButton("content")
    child.setObjectName("edgeCoveringChild")
    child.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    layout.addWidget(child)
    win.setCentralWidget(central)
    win.show()
    QApplication.processEvents()

    handle = RecordingHandle()
    win.windowHandle = lambda: handle
    resizer = FramelessResizer(win)
    return win, child, resizer, handle


def _press(expected_widget, window, window_point, button=Qt.LeftButton):
    """Press at a window-relative point, delivered where Qt would deliver it.

    The receiver is resolved with childAt(), so the test cannot cheat by
    handing the event to the window when a child actually covers the pixel.
    """
    widget = window.childAt(window_point) or window
    assert widget is expected_widget, f"{window_point} is not covered by the child"
    global_pos = window.mapToGlobal(window_point)
    local = widget.mapFromGlobal(global_pos)
    event = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(local),
        QPointF(global_pos),
        QPointF(global_pos),
        button,
        button,
        Qt.NoModifier,
    )
    QApplication.sendEvent(widget, event)
    return event


# --- Group A: edge math characterization (green before the fix) -------------


@pytest.mark.parametrize(
    "point, expected",
    [
        ((2, 150), Qt.LeftEdge),
        ((397, 150), Qt.RightEdge),
        ((200, 2), Qt.TopEdge),
        ((200, 297), Qt.BottomEdge),
        ((2, 2), Qt.LeftEdge | Qt.TopEdge),
        ((397, 2), Qt.RightEdge | Qt.TopEdge),
        ((2, 297), Qt.LeftEdge | Qt.BottomEdge),
        ((397, 297), Qt.RightEdge | Qt.BottomEdge),
        ((18, 150), Qt.LeftEdge),
        ((19, 150), Qt.Edges()),
        ((200, 150), Qt.Edges()),
    ],
)
def test_edge_for_classifies_band_points(point, expected):
    win, _child, resizer, _handle = _window()
    assert resizer._edge_for(QPoint(*point)) == expected


# --- Group B: the routing defect -------------------------------------------


@pytest.mark.parametrize(
    "point, expected",
    [
        ((2, 150), Qt.LeftEdge),
        ((397, 150), Qt.RightEdge),
        ((200, 2), Qt.TopEdge),
        ((200, 297), Qt.BottomEdge),
        ((2, 2), Qt.LeftEdge | Qt.TopEdge),
        ((397, 2), Qt.RightEdge | Qt.TopEdge),
        ((2, 297), Qt.LeftEdge | Qt.BottomEdge),
        ((397, 297), Qt.RightEdge | Qt.BottomEdge),
    ],
)
def test_press_on_child_inside_band_starts_native_resize(point, expected):
    win, child, _resizer, handle = _window()
    _press(child, win, QPoint(*point))
    assert handle.calls == [expected]
    # The child never sees a press that starts a window resize.
    assert not child.isDown()


def test_press_deep_inside_band_over_child_still_resizes():
    """A point well inside the band, not on the outermost pixel row."""
    win, child, _resizer, handle = _window()
    _press(child, win, QPoint(15, 150))
    assert handle.calls == [Qt.LeftEdge]


def test_resize_starts_once_per_press():
    win, child, _resizer, handle = _window()
    _press(child, win, QPoint(2, 150))
    assert len(handle.calls) == 1


def test_non_left_button_press_in_band_does_not_resize():
    win, child, _resizer, handle = _window()
    _press(child, win, QPoint(2, 150), button=Qt.RightButton)
    assert handle.calls == []


# --- Group C: centre content stays interactive ------------------------------


def test_press_on_child_outside_band_passes_through():
    win, child, _resizer, handle = _window()
    _press(child, win, QPoint(200, 150))
    assert handle.calls == []
    assert child.isDown()


def test_press_one_pixel_outside_band_passes_through():
    win, child, _resizer, handle = _window()
    _press(child, win, QPoint(19, 150))
    assert handle.calls == []
    assert child.isDown()


# --- Titlebar controls keep their clicks ------------------------------------


def _titlebar_window():
    win = QMainWindow()
    _live.append(win)
    win.resize(400, 300)
    central = QWidget()
    layout = QVBoxLayout(central)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    bar = Titlebar(win, "Cove", "3.5.2")
    layout.addWidget(bar)
    layout.addStretch(1)
    win.setCentralWidget(central)
    win.show()
    QApplication.processEvents()

    handle = RecordingHandle()
    win.windowHandle = lambda: handle
    resizer = FramelessResizer(win)
    return win, bar, resizer, handle


@pytest.mark.parametrize(
    "control", ["theme_btn", "min_btn", "max_btn", "close_btn"]
)
def test_titlebar_controls_stay_clickable_inside_top_band(control):
    """The window controls sit at the top of the window, so their upper half
    is inside the resize band. A press there must still hit the button."""
    win, bar, _resizer, handle = _titlebar_window()
    button = getattr(bar, control)
    top_left = button.mapTo(win, QPoint(0, 0))
    point = QPoint(top_left.x() + button.width() // 2, top_left.y() + 2)
    assert point.y() <= FramelessResizer.BORDER, "control is not inside the band"

    _press(button, win, point)
    assert handle.calls == []
    assert button.isDown()


# --- Group D: other top-level windows are not touched -----------------------


def test_edge_press_in_another_window_is_ignored():
    win, _child, _resizer, handle = _window()

    other = QMainWindow()
    _live.append(other)
    other.resize(300, 200)
    other_child = QPushButton("other content")
    other.setCentralWidget(other_child)
    other.show()
    QApplication.processEvents()

    _press(other_child, other, QPoint(2, 100))
    assert handle.calls == []
    assert other_child.isDown()


# --- Group E: maximized / fullscreen guards ---------------------------------


def test_maximized_window_does_not_resize_from_band():
    win, child, _resizer, handle = _window()
    win.showMaximized()
    QApplication.processEvents()
    assert win.isMaximized()
    _press(child, win, QPoint(2, 150))
    assert handle.calls == []


def test_fullscreen_window_does_not_resize_from_band():
    win, child, _resizer, handle = _window()
    win.showFullScreen()
    QApplication.processEvents()
    assert win.isFullScreen()
    _press(child, win, QPoint(2, 150))
    assert handle.calls == []
