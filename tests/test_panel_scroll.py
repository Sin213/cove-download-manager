"""The controls panel scrolls instead of squeezing its sections.

Adding the Browser extension section pushed the right-hand column past the
window height. A QVBoxLayout short of space shrinks its children below their
sensible heights, which rendered as hint labels overlapping the spin boxes
above them. The column is now scrollable, so sections keep their height and
the overflow is reachable instead of overlapping.
"""
import pytest
import shiboken6
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt

import cove.main_window as mw

QApplication.instance() or QApplication([])

_live = []


@pytest.fixture(autouse=True)
def _destroy():
    """Destroy top-level widgets only, newest first.

    Reparenting (setCentralWidget) hands ownership to Qt, so a child that a
    test also tracked is already gone by the time its own turn comes - hence
    the isValid guard.
    """
    yield
    while _live:
        obj = _live.pop()
        if shiboken6.isValid(obj):
            obj.setParent(None)
            shiboken6.delete(obj)
    QApplication.processEvents()


def _tall_content() -> QWidget:
    """Stands in for the real panel column: taller than any short window."""
    content = QWidget()
    lay = QVBoxLayout(content)
    for i in range(8):
        label = QLabel(f"section {i}")
        label.setMinimumHeight(80)
        lay.addWidget(label)
    return content


def _panel_area() -> QScrollArea:
    area = mw.build_panel_scroll_area(_tall_content())
    _live.append(area)
    return area


def test_the_panel_lives_in_a_scroll_area():
    assert isinstance(_panel_area(), QScrollArea)


def test_the_panel_widget_tracks_the_column_width():
    """Without this the panel keeps its own width and clips horizontally."""
    assert _panel_area().widgetResizable() is True


def test_the_panel_never_scrolls_sideways():
    area = _panel_area()
    assert area.horizontalScrollBarPolicy() == Qt.ScrollBarAlwaysOff


def test_the_panel_scrolls_vertically_when_it_has_to():
    area = _panel_area()
    assert area.verticalScrollBarPolicy() == Qt.ScrollBarAsNeeded


def test_the_panel_has_no_frame_of_its_own():
    """The sections already draw their own surfaces; a frame double-borders."""
    assert _panel_area().frameShape() == QScrollArea.NoFrame


def test_sections_keep_their_height_in_a_short_column():
    """The regression: a squeezed column must not shrink a section to nothing."""
    area = _panel_area()
    host = QMainWindow()
    _live.append(host)
    host.setCentralWidget(area)
    host.resize(420, 200)  # far shorter than the panel needs
    QApplication.processEvents()

    inner = area.widget()
    assert inner.sizeHint().height() > area.viewport().height()
    assert inner.height() >= inner.sizeHint().height()
