"""A7b-2-UX: real run() wiring, window and queue; isolated, socket-free auth gates."""
import pytest

from tests.test_app_launch import _HARNESS, _isolated_child


_PROBE = r'''
import threading
from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QThread, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QLabel
from cove import main_window as mw
from cove.scheduler import Scheduler
from unittest.mock import Mock

MESSAGE = "Starting download engine…\nDownloads remain queued until the engine is ready."
case = __CASE__
entered, release = threading.Event(), threading.Event()
if case == "fast": release.set()
events, controllers, windows = [], [], []
gui_thread = QThread.currentThread()

def auth(ticket, cancel):
    entered.set()
    assert release.wait(5), "test did not release authentication"
    return Aria2Error("Another aria2 owns this port") if case == "occupied" else Aria2Error("rejected") if case in ("rejected", "wrong-token", "failure-modal-close") else None
FakeAria2Daemon.authenticate = staticmethod(auth)
original_commit = FakeAria2Daemon.commit_ready
def commit(self, *args):
    original_commit(self, *args)
    events.append("committed")
FakeAria2Daemon.commit_ready = commit

class Controller(app_mod._StartupController):
    def __init__(self, *args):
        super().__init__(*args)
        controllers.append(self)
app_mod._StartupController = Controller

class Window(mw.MainWindow):
    def __init__(self, *args):
        super().__init__(*args)
        windows.append(self)
        self.sub = next(w for w in self.findChildren(QLabel) if w.property("role") == "hero-sub")
        self.original = self.sub.text()
        # Finish existing pause/schedule callbacks before measuring presentation.
        settle(self.queue)
        self._refresh_status_pill()
        self.baseline = self.snapshot()
        self.label_count = len(self.findChildren(QLabel))
        self.pending_seen = False

    def snapshot(self):
        return (self.status_pill.text(), [(t.id, t.status) for t in self.queue.tasks.values()],
                [self.tree.topLevelItem(i).text(mw.COL_STATUS) for i in range(self.tree.topLevelItemCount())],
                self.queue.aria2_admission, list(calls.get("rpc", [])))

    def set_engine_starting(self, starting):
        assert QThread.currentThread() == gui_thread, "presentation used off GUI thread"
        before, focus = self.snapshot(), self.focusWidget()
        handler = getattr(super(), "set_engine_starting", None)
        if handler is not None:
            handler(starting)
        events.append(starting)
        assert self.snapshot() == before, "presentation changed queue/admission/RPC"
        assert self.focusWidget() is focus, "presentation stole focus"
        assert len(self.findChildren(QLabel)) == self.label_count, "duplicate indicator"
        if starting:
            self.pending_seen = True
            assert controllers[-1].state == "AUTHENTICATING" and controllers[-1].ticket is not None
            assert controllers[-1].worker is None, "pending notification arrived after worker creation"
            assert not entered.is_set(), "pending notification arrived after worker start"
        elif case not in ("missing", "interface-close", "commit-fails", "rejected", "wrong-token", "occupied", "failure-modal-close") and controllers and controllers[-1] is controllers[0] and events.count(False) == (2 if case == "interface-retry" else 1):
            assert controllers[0].state == "READY", "cleared before READY state"
            assert FakeAria2Daemon.instance.ready and "committed" in events, "cleared before READY"
            assert self.queue.aria2_admission == "pending", "admission moved before presentation clear"
        if handler is not None:
            assert self.sub.text() == (MESSAGE if starting else self.original)

app_mod.MainWindow, app_mod.Scheduler = Window, Scheduler
old_load = app_mod.Settings.load
def load(cls):
    settings = old_load()
    settings.theme = "light" if case == "light" else "dark"
    settings.max_concurrent = 1
    if case == "scheduled":
        settings.schedule.enabled = True
        settings.schedule.days = []
    with db.connect() as conn:
        conn.execute("DELETE FROM downloads")
        count = 0 if case == "zero" else 4 if case == "many" else 1
        for i in range(count):
            conn.execute("INSERT INTO downloads (url,out_dir,connections,speed_limit_kbps,status,created_at,category,backend) VALUES (?,?,4,0,'queued',1,'Other','aria2')",
                         (f"https://example.com/{i}.zip", str(Path.cwd())))
    return settings
app_mod.Settings.load = classmethod(load)
if case in ("paused", "scheduled"):
    # Keep real queue transitions, without an unauthenticated pool RPC racing teardown.
    StartupQueue._pause_everything = lambda self: None
original_queue_init = StartupQueue.__init__
def queue_init(self, *args, **kwargs):
    original_queue_init(self, *args, **kwargs)
    if case == "paused": self.stop_queue()
StartupQueue.__init__ = queue_init
original_admission = StartupQueue.set_aria2_admission
def admission(self, state):
    calls["admission"].append(state)
    original_admission(self, state)
StartupQueue.set_aria2_admission = admission
if case == "ready-warning":
    StartupQueue.resume_persisted = Mock(side_effect=RuntimeError("controlled resume failure"))

def check_clear():
    window = windows[0]
    assert window.sub.text() == window.original, "original subtitle was not restored exactly"
    assert not window.sub.wordWrap(), "word wrap was not restored"
    assert window.sub.accessibleName() == "", "accessible startup message was not cleared"

def modal_boundary(shutdown=False):
    check_clear()
    controller, loop = controllers[0], QEventLoop()
    generation, notifications, admitted = controller.generation, list(events), list(calls["admission"])
    delivered, checked = [], []
    def observe(generation, result): delivered.append((generation, result))
    controller.completed.connect(observe, Qt.QueuedConnection)
    def unchanged():
        check_clear()
        assert events == notifications, "presentation notified during modal reentry"
        assert calls["admission"] == admitted, "admission changed during modal reentry"
    def close_dialog():
        unchanged()
        assert len(delivered) == (3 if shutdown else 2), "queued modal completions were not delivered"
        checked.append(True)
        loop.quit()
    def finish():
        unchanged()
        assert delivered == [(generation - 1, "ok"), (generation, "ok")]
        if shutdown:
            controller.shutdown()
            controller.completed.emit(generation, "ok")
        QTimer.singleShot(0, close_dialog)
    def inject():
        controller.completed.emit(generation - 1, "ok")
        controller.completed.emit(generation, "ok")
        QTimer.singleShot(0, finish)
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    deadline.start(1000)
    QTimer.singleShot(0, inject)
    try: loop.exec()
    finally:
        deadline.stop()
        controller.completed.disconnect(observe)
    assert checked == [True], "modal callback assertions did not finish"
    QCoreApplication.sendPostedEvents(None, QEvent.MetaCall)
    unchanged()

old_stop = FakeAria2Daemon.stop
def stop(self, **kwargs):
    events.append("stop")
    return old_stop(self, **kwargs)
FakeAria2Daemon.stop = stop

old_critical = FakeQMessageBox.critical
def critical(parent, title, text):
    check_clear()
    if case not in ("missing", "interface-close"):
        assert windows[0].pending_seen, "failure had no preceding pending notification"
        assert events.index(False) < events.index("stop"), "daemon stop preceded pending clear"
    assert not FakeAria2Daemon.instance.ready
    assert "open" not in calls["admission"]
    modal_boundary(shutdown=case == "failure-modal-close")
    old_critical(parent, title, text)
FakeQMessageBox.critical = staticmethod(critical)
old_interface_exec = FakeQMessageBox.exec
def interface_exec(self):
    check_clear()
    assert not windows[0].pending_seen, "pre-auth interface dialog showed startup pending"
    modal_boundary(shutdown=case == "interface-close")
    old_interface_exec(self)
FakeQMessageBox.exec = interface_exec
old_warning = FakeQMessageBox.warning
def warning(parent, title, text):
    assert text == app_mod.RESUME_FAILED_TEXT
    assert controllers[0].state == "READY" and FakeAria2Daemon.instance.ready
    assert calls["admission"] == ["open"]
    modal_boundary(shutdown=True)
    old_warning(parent, title, text)
FakeQMessageBox.warning = staticmethod(warning)

class ProbeApplication(_RealQApplication):
    def until(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            self.processEvents()
            threading.Event().wait(.001)
        assert predicate(), "event-loop condition did not settle"

    def exec(self):
        try:
            self.until(lambda: entered.is_set() or bool(calls["boxes"]) or (case == "fallback" and calls["admission"]))
            window = windows[0]
            if case == "fallback":
                assert controllers == [] and events == [], "fallback created controller/pending connection"
                check_clear()
                return 0
            controller = controllers[0]
            if case in ("missing", "interface-close"):
                assert not window.pending_seen and events == [False], "modal must explicitly clear pending"
                assert len(calls["boxes"]) == 1
                check_clear()
                return 0
            if case == "fast":
                self.until(lambda: controller.state == "READY")
                self.processEvents()
                check_clear()
                assert events.count(True) == 1 and events.count(False) == 1, "fast READY must notify and clear without delay"
                assert calls["admission"] == ["open"]
                return 0
            assert window.sub.text() == MESSAGE, "pending header must show immediately during authentication"
            assert window.sub.isVisible(), "pending header is hidden"
            assert window.snapshot() == window.baseline, f"pending changed queue state/admission/RPC: {window.baseline} -> {window.snapshot()}"
            assert window.queue.aria2_admission == "pending"
            if case in ("paused", "scheduled"):
                assert window.status_pill.text() == ("QUEUE PAUSED" if case == "paused" else "OUTSIDE SCHEDULE")
            assert controller.receivers("2pending(bool)") == 1, "run() must connect presentation exactly once"
            assert window.sub.wordWrap() and window.sub.textFormat() == Qt.PlainText
            assert window.sub.accessibleName() == MESSAGE
            assert window.sub.hasHeightForWidth()
            assert window.sub.heightForWidth(210) > window.sub.heightForWidth(700)
            assert window.sub.minimumSizeHint().width() <= 210, "narrow label clips message"
            window.sub.ensurePolished()
            assert window.sub.palette().color(QPalette.WindowText).name().lower() == app_mod.theme.TEXT_DIM.lower()
            if case in ("light", "dark"):
                def luminance(color):
                    channels = (color.redF(), color.greenF(), color.blueF())
                    linear = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in channels]
                    return sum(w * c for w, c in zip((.2126, .7152, .0722), linear))
                foreground = luminance(window.sub.palette().color(QPalette.WindowText))
                for endpoint in (app_mod.theme.BG_GRAD_1, app_mod.theme.BG_GRAD_2):
                    background = luminance(QColor(endpoint))
                    contrast = (max(foreground, background) + .05) / (min(foreground, background) + .05)
                    assert contrast >= 4.5, f"{case} hero subtitle contrast against {endpoint}: {contrast:.3f} < 4.5"
            assert len(window.findChildren(QLabel)) == window.label_count
            mw.MainWindow.set_engine_starting(window, True)
            mw.MainWindow.set_engine_starting(window, True)
            assert window.sub.text() == MESSAGE and window.snapshot() == window.baseline
            assert len(window.findChildren(QLabel)) == window.label_count
            if case == "slow":
                heartbeat = []
                QTimer.singleShot(0, lambda: heartbeat.append(True))
                self.until(lambda: bool(heartbeat))
                class SettingsProbe:
                    Accepted = 1
                    def __init__(self, *args): events.append("settings")
                    def exec(self): return 0
                mw.SettingsDialog = SettingsProbe
                window._open_settings()
                window.nav_search.click(); window.nav_downloads.click()
                assert "settings" in events and window.sub.text() == MESSAGE
                assert window.snapshot() == window.baseline
            if case in ("stale", "close", "destroy"):
                before, notifications = window.sub.text(), list(events)
                if case == "stale":
                    controller.complete(controller.generation - 1, "ok")
                    controller.complete(controller.generation - 1, "rpc_down")
                else:
                    controller.shutdown()
                    controller.complete(controller.generation - 1, "ok")
                    controller.failure(None)
                assert window.sub.text() == before and events == notifications, "late completion changed presentation"
                assert window.queue.aria2_admission == "pending"
                if case == "destroy":
                    window.stop_ui_timers()
                    window.deleteLater()
                    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
                    release.set(); controller.worker.join(2)
                    assert not controller.worker.is_alive()
                    self.processEvents()
                    assert events == notifications, "destroyed window received pending notification"
                    return 0
                if case == "close":
                    release.set(); controller.worker.join(2); self.processEvents()
                    assert events == notifications and calls["admission"] == []
                    return 0
            controller.begin()  # Duplicate begin must not notify or start another worker.
            assert events.count(True) == 1
            release.set()
            self.until(lambda: controller.state in ("READY", "FAILED") or controller.closing)
            self.processEvents()
            check_clear()
            assert events.count(True) == 1 and events.count(False) == (2 if case == "interface-retry" else 1)
            if case in ("failure-modal-close", "ready-warning"):
                assert controller.closing and controller.state == "STOPPED"
                assert calls["admission"] == (["open"] if case == "ready-warning" else ["failed"])
                assert len(calls["boxes"]) == 1
                return 0
            if case in ("rejected", "wrong-token", "occupied", "commit-fails"):
                assert controller.state == "FAILED" and calls["admission"] == ["failed"]
                assert len(calls["boxes"]) == 1
                return 0
            assert controller.state == "READY" and calls["admission"] == ["open"]
            notifications = list(events)
            controller.complete(controller.generation, "ok")
            self.processEvents()
            assert events == notifications, "duplicate READY emitted presentation notification"
            if case == "duplicates":
                before = window.snapshot()
                window.set_engine_starting(False); window.set_engine_starting(False)
                controller.failure(None, notify=False); controller.failure(None, notify=False)
                check_clear()
                assert window.snapshot() == before
                # There is no production retry. A new controller represents another attempt.
                again = Controller(window, FakeAria2Daemon.instance, lambda: controller.ticket, Mock(), Mock())
                again.pending.connect(window.set_engine_starting)
                entered.clear(); release.clear()
                again.begin()
                assert window.sub.text() == MESSAGE
                release.set()
                self.until(lambda: again.state == "READY")
                check_clear()
            return 0
        finally:
            release.set()
            for controller in controllers:
                if controller.worker:
                    controller.worker.join(2)
                    assert not controller.worker.is_alive(), "test auth worker survived cleanup"
            self.aboutToQuit.emit()
            self.processEvents()

app_mod.QApplication = ProbeApplication
if case == "fallback": app_mod._async_startup_supported = lambda: False
'''


@pytest.mark.parametrize("group,case", [
    ("RED-01", "pending"), ("RED-02", "slow"), ("RED-03", "ready"),
    ("RED-04", "fast"), ("RED-05", "missing"), ("RED-05", "rejected"),
    ("RED-05", "wrong-token"), ("RED-05", "occupied"), ("RED-05", "commit-fails"),
    ("RED-05", "interface-close"), ("RED-05", "interface-retry"),
    ("RED-06", "close"), ("RED-06", "destroy"), ("RED-07", "stale"),
    ("RED-08", "duplicates"), ("RED-09", "light"), ("RED-09", "dark"),
    ("RED-10", "zero"), ("RED-10", "many"), ("RED-10", "paused"),
    ("RED-10", "scheduled"), ("RED-11", "fallback"),
    ("F1", "ready-warning"), ("F1", "failure-modal-close"),
])
def test_engine_startup_notification(group, case):
    flags = {"real_queue": True, "no_secret": case == "commit-fails",
             "interface_errors": case.startswith("interface"),
             "interface_choices": ["Open Settings"] if case == "interface-retry" else ["Close"]}
    script = _HARNESS.replace("rc = app_mod.run()", _PROBE.replace("__CASE__", repr(case)) + "\nrc = app_mod.run()")
    substitutions = {"__A7A_FLAGS__": flags, "__ARGV__": [], "__IS_PRIMARY__": True,
                     "__SETTINGS_UNREADABLE__": False, "__DAEMON_FAILS__": case == "missing",
                     "__EARLY_IPC__": None, "__LATE_IPC__": None, "__FORWARD_OK__": True,
                     "__BAD_URL__": "", "__CLEANUP_STATUS__": "not_needed"}
    for token, value in substitutions.items():
        script = script.replace(token, repr(value))
    if case == "interface-retry":
        script = script.replace("rc = app_mod.run()", "mw.SettingsDialog = type('SettingsProbe', (), {'Accepted': 1, '__init__': lambda self, *a: None, 'exec': lambda self: 0})\nrc = app_mod.run()")
    result = _isolated_child(script)
    assert result.returncode == 0, f"{group}/{case}\n{result.stdout}\n{result.stderr}"
    assert "Traceback" not in result.stderr, result.stderr  # Qt reports slot exceptions here.


@pytest.mark.parametrize("secret", ["authenticated", None], ids=["active-secret", "missing-secret"])
def test_no_ticket_startup_notification(secret):
    result = _isolated_child(r'''
from unittest.mock import Mock
from cove.app import _StartupController
daemon, ready, failed = Mock(), Mock(), Mock()
daemon.active_rpc_secret.return_value = __SECRET__
controller = _StartupController(None, daemon, Mock(return_value=None), ready, failed)
notifications = []
controller.pending.connect(notifications.append)
controller.begin()
assert notifications == [False], "no-ticket startup must clear without ever showing pending"
assert controller.worker is None and controller.ticket is None
daemon.active_rpc_secret.assert_called_once_with()
daemon.commit_ready.assert_not_called()
if __SECRET__:
    assert controller.state == "READY"
    ready.assert_called_once_with()
    failed.assert_not_called()
    daemon.stop.assert_not_called()
else:
    assert controller.state == "FAILED"
    ready.assert_not_called()
    failed.assert_called_once()
    daemon.stop.assert_called_once_with()
before = ready.call_count, failed.call_count, daemon.stop.call_count
controller.begin()
controller.complete(controller.generation, "ok")
assert notifications == [False], "duplicate no-ticket completion must be harmless"
assert (ready.call_count, failed.call_count, daemon.stop.call_count) == before
'''.replace("__SECRET__", repr(secret)))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr
