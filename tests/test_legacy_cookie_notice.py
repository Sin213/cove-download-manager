"""TEST-DISCIPLINE: fake notice, no filesystem or live window construction."""
from types import SimpleNamespace

from cove import main_window

NOTICE = (
    "Cove could not finish clearing old browser cookie data from its download history, "
    "or could not confirm that it did, so some of that data may still be stored. "
    "Cove will try again the next time it starts. Cove only clears the cookie field "
    "in the database this copy of Cove uses, and does not erase the database file, backups or snapshots."
)


def test_s08b2_L_T_notice(monkeypatch):
    events = []
    window = SimpleNamespace()
    class Box:
        Warning, Ok = 2, 1024
        def __init__(self, parent):
            assert parent is window
            events.append('parent')
        def setIcon(self, value):
            assert value == self.Warning
        def setWindowTitle(self, value):
            assert 'Cove' in value
        def setText(self, value):
            assert value == NOTICE and len(value) == 336
        def setStandardButtons(self, value):
            assert value == self.Ok
        def open(self):
            events.append('open')
        def exec(self):
            raise AssertionError('nested event loop')
    monkeypatch.setattr(main_window, 'QMessageBox', Box)
    main_window.MainWindow.note_legacy_cookie_cleanup_incomplete(window)
    assert events == ['parent', 'open']
    assert any(isinstance(value, Box) for value in vars(window).values())
    assert NOTICE in vars(main_window).values()
