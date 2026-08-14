"""The visible progress bar, rendered by the real MainWindow._render.

tests/test_queue.py covers the model that feeds it. These cover the seam the
user actually looks at: the QProgressBar value the ~30 Hz smooth tick writes
between aria2's 2 Hz status samples.
"""

import time

from PySide6.QtWidgets import QProgressBar, QTreeWidget, QTreeWidgetItem

import cove.main_window as mw
from cove.queue import DownloadTask


class _Row:
    """The real _render against real widgets, without MainWindow's heavy
    constructor - the same seam tests/test_search_main_window.py uses. _render
    reads nothing off the window but the row's item and progress bar.
    """

    def __init__(self, task):
        self.window = mw.MainWindow.__new__(mw.MainWindow)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.item = QTreeWidgetItem(self.tree)
        self.bar = QProgressBar()
        self.window._items = {task.id: self.item}
        self.window._bars = {task.id: self.bar}
        self.task = task

    def percent(self) -> int:
        self.window._render(self.task)
        return self.bar.value()


def _task(**overrides) -> DownloadTask:
    fields = dict(
        id=1,
        url="https://example.com/big.zip",
        out_dir="/dl",
        gid="gid-a",
        status="active",
        total_bytes=1_000_000_000,
        completed_bytes=535_000_000,
        download_speed=20_000_000,
        last_status_at=time.time(),
    )
    fields.update(overrides)
    return DownloadTask(**fields)


def test_the_bar_does_not_step_back_when_the_next_sample_undershoots():
    """53% -> 54% -> 53% as the user reported it. Both backend samples move
    forward; only Cove's extrapolation moved backward."""
    task = _task(last_status_at=time.time() - 0.4)
    row = _Row(task)

    first = row.percent()
    task.completed_bytes = 538_000_000
    task.download_speed = 5_000_000
    task.last_status_at = time.time()

    assert row.percent() >= first


def test_the_bar_still_advances_between_two_polls():
    """Positive control: the row must keep moving smoothly, not step at 2 Hz."""
    task = _task(completed_bytes=500_000_000, download_speed=40_000_000,
                 last_status_at=time.time())
    row = _Row(task)
    at_sample = row.percent()

    task.last_status_at = time.time() - 0.45
    assert row.percent() > at_sample


def test_a_finished_download_shows_one_hundred_percent():
    task = _task(status="completed", completed_bytes=1_000_000_000,
                 download_speed=0)

    assert _Row(task).percent() == 100


def test_a_promoted_payload_gid_starts_the_bar_over():
    """A magnet's metadata gid is a different transfer from its payload gid.
    Whatever the metadata stage showed cannot floor the payload."""
    task = _task(completed_bytes=900_000_000, download_speed=0)
    row = _Row(task)
    assert row.percent() == 90

    task.gid = "gid-child"
    task.total_bytes = 8_000_000_000
    task.completed_bytes = 80_000_000
    assert row.percent() == 1


def test_a_stalled_row_does_not_sit_at_a_speculative_hundred_percent():
    """The bar may approach the end but only aria2 declares the end reached."""
    task = _task(completed_bytes=999_999_000, download_speed=500_000_000,
                 last_status_at=time.time() - 0.5)
    row = _Row(task)
    assert row.percent() == 99

    task.download_speed = 0
    task.last_status_at = time.time()
    assert row.percent() == 99
