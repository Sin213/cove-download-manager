"""Process-wide Qt application for the test suite.

Several test modules construct a Qt application at import time, and they
disagree about which kind: the IPC and API-server tests only need a
``QCoreApplication``, while the window tests build real widgets and need a
full ``QApplication``. Only one application object may exist per process, and
whichever module is imported first wins - so in a full run (alphabetical
collection puts ``test_api_server`` first) the widget tests would find a
plain ``QCoreApplication`` already installed, build ``QMainWindow`` objects
under it, and segfault Qt during interpreter shutdown.

Creating the ``QApplication`` here, before any test module is imported, fixes
the ordering for good: ``QApplication`` *is* a ``QCoreApplication``, so the
modules that only ask for the latter are satisfied by it, and the widget
tests get the GUI application they actually require.
"""
import os

# Must be set before the application is constructed - no test may depend on a
# real display (or a real system tray) being present.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])


import pytest  # noqa: E402


class FakeKey:
    """In-memory stand-in for a winreg key handle, used as a context manager."""

    def __init__(self, store, path=None):
        self.store = store
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWinreg:
    """Minimal in-memory stand-in for the winreg module.

    Shared by tests/test_magnet_win.py and tests/test_portable_magnet_registration.py
    so both suites exercise one fake registry instead of two near-duplicates.
    Never touches the real winreg module or the machine's registry.
    """

    HKEY_CURRENT_USER = "HKCU"
    KEY_WRITE = 2
    KEY_READ = 1
    REG_SZ = 1

    def __init__(self, initial=None):
        self.data = dict(initial or {})
        self.roots_used = set()

    def CreateKeyEx(self, root, path, reserved=0, access=0):
        self.roots_used.add(root)
        self.data.setdefault(path, {})
        return FakeKey(self.data[path], path)

    def OpenKey(self, root, path, reserved=0, access=0):
        self.roots_used.add(root)
        if path not in self.data:
            raise OSError("missing key: {}".format(path))
        return FakeKey(self.data[path], path)

    def SetValueEx(self, key, name, reserved, value_type, value):
        key.store[name] = value

    def QueryValueEx(self, key, name):
        if name not in key.store:
            raise OSError("missing value: {}".format(name))
        return key.store[name], self.REG_SZ

    def DeleteKey(self, root, path):
        self.roots_used.add(root)
        if path not in self.data:
            raise OSError("missing key: {}".format(path))
        del self.data[path]

    def DeleteValue(self, key, name):
        if name not in key.store:
            raise OSError("missing value: {}".format(name))
        del key.store[name]


@pytest.fixture(scope="session", autouse=True)
def _drain_qt_deletions():
    """Flush Qt's deferred-deletion queue before the interpreter exits.

    Tests across this suite leak QObjects on purpose (QueueManagers kept
    alive to inspect, dialogs handed to deleteLater()). Left pending, those
    deletions are processed after the QApplication itself is torn down during
    interpreter shutdown, which crashes Qt. Draining them here - while the
    application is still alive - keeps the run's exit status meaningful.
    """
    yield
    for _ in range(3):
        _app.sendPostedEvents(None, 0)
        _app.processEvents()
