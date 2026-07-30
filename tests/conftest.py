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
