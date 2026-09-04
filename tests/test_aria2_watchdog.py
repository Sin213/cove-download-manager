"""The mid-session aria2 health check.

`Aria2Daemon.start()` only consults `poll()` on the way in, so an aria2c
that exits after boot used to stay dead for the whole session: the window
kept saying IDLE, every download failed with a generic error, and the
unreaped child sat there as a zombie. The watchdog's whole job is to say so
- it reports, it does not repair, and these tests own that boundary.
"""
import inspect
import logging

import cove.app as app_mod
from cove.app import ARIA2_LOST_MESSAGE, Aria2Watchdog
from cove.main_window import MainWindow


class _Daemon:
    """A daemon whose liveness answers come from a script, one per probe."""

    def __init__(self, alive):
        self.alive = list(alive)
        self.probes = 0

    def is_running(self) -> bool:
        self.probes += 1
        return self.alive.pop(0) if self.alive else True


def _said():
    """A recorder for what the user was told."""
    return []


def test_a_healthy_daemon_is_never_mentioned():
    daemon = _Daemon([True, True, True])
    said = _said()

    watchdog = Aria2Watchdog(daemon, said.append)
    for _ in range(3):
        watchdog.check()

    assert daemon.probes == 3
    assert said == []


def test_a_dead_daemon_is_reported_once_not_every_tick():
    """The check runs every few seconds; reporting per tick would bury the
    user in identical dialogs. The call count is the assertion."""
    daemon = _Daemon([False] * 20)
    said = _said()

    watchdog = Aria2Watchdog(daemon, said.append)
    for _ in range(20):
        watchdog.check()

    assert said == [ARIA2_LOST_MESSAGE]


def test_the_message_says_what_to_do_about_it():
    """An outage message whose whole value is the recovery step has to
    carry the recovery step."""
    assert "restart" in ARIA2_LOST_MESSAGE.lower()
    assert "aria2" in ARIA2_LOST_MESSAGE.lower()


def test_the_watchdog_never_tries_to_repair_anything():
    """Restarting aria2c from here leaves Cove holding gids from a process
    that no longer exists. `_Daemon` has no start/ensure_running at all, so
    a watchdog that tried would raise."""
    daemon = _Daemon([False] * 3)
    said = _said()

    watchdog = Aria2Watchdog(daemon, said.append)
    for _ in range(3):
        watchdog.check()

    assert daemon.probes == 3
    assert len(said) == 1


def test_a_new_outage_is_reported_again_after_a_recovery():
    """Reporting once is per outage, not once per session: aria2 coming
    back and dying again is news."""
    daemon = _Daemon([False, False, True, False, False])
    said = _said()

    watchdog = Aria2Watchdog(daemon, said.append)
    for _ in range(5):
        watchdog.check()

    assert said == [ARIA2_LOST_MESSAGE, ARIA2_LOST_MESSAGE]


def test_the_outage_is_logged_as_well_as_shown(caplog):
    daemon = _Daemon([False])

    with caplog.at_level(logging.ERROR, logger="cove"):
        Aria2Watchdog(daemon, _said().append).check()

    assert any("aria2_unavailable" in r.getMessage() for r in caplog.records)


def test_shutdown_silences_the_watchdog():
    """Cleanup stops the daemon on purpose. A "downloads will fail" dialog
    raised at a window being torn down is noise about something the user
    just asked for."""
    daemon = _Daemon([False] * 5)
    said = _said()

    watchdog = Aria2Watchdog(daemon, said.append)
    watchdog.stop()
    for _ in range(5):
        watchdog.check()

    assert said == []
    assert daemon.probes == 0


# --- the message has to reach a human ---------------------------------------


def test_the_outage_does_not_go_through_the_channel_that_discards_it():
    """`MainWindow._on_error` takes a message and renders a generic four
    second "Error" pill, dropping the text. Routing the outage there would
    ship a recovery instruction nobody ever sees - which is how the bug
    being fixed presented in the first place.
    """
    # The channel really does drop its argument: the parameter is named and
    # then never used again in the body.
    body = inspect.getsource(MainWindow._on_error).split("\n", 1)[1]
    assert "msg" not in body

    # So the watchdog is not given a queue to route it through at all.
    assert "queue" not in inspect.signature(Aria2Watchdog).parameters


def test_the_window_shows_the_outage_where_it_cannot_be_missed(monkeypatch):
    """The wiring under test: the watchdog's notify is the window method
    that puts the text in front of the user."""
    shown = {}

    class _Box:
        Warning = "warning"

        def __init__(self, parent=None):
            pass

        def setIcon(self, icon):
            pass

        def setWindowTitle(self, title):
            shown["title"] = title

        def setText(self, text):
            shown["text"] = text

        def exec(self):
            shown["shown"] = True

    import cove.main_window as mw

    monkeypatch.setattr(mw, "QMessageBox", _Box)
    MainWindow.note_aria2_unavailable(object(), ARIA2_LOST_MESSAGE)

    assert shown["shown"] is True
    assert shown["text"] == ARIA2_LOST_MESSAGE
    assert "aria2" in shown["title"]


def test_the_app_wires_the_watchdog_to_that_window_method():
    source = inspect.getsource(app_mod.run)
    assert "Aria2Watchdog(daemon, window.note_aria2_unavailable)" in source
