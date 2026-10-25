"""A7b-3: real loopback reclamation, Qt authority and owned-child regressions.

Socket cases are intentionally not skipped under EPERM: the lead executes them
outside the author sandbox. Tests using new protocol names are INTERFACE REDs;
the heartbeat test uses the pre-existing production startup entry points.
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QTimer
from PySide6.QtWidgets import QApplication

from cove import app, aria2 as a
from cove.config import Settings
from tests.test_app_async_startup import (
    CANARY, Responder, launch, observe_body_read, until, worker,
)
from tests.test_app_launch import _HARNESS, _isolated_child


def pump(predicate, timeout=4):
    def check():
        QApplication.processEvents()
        return predicate()
    until(check, timeout)


@pytest.fixture(autouse=True)
def worker_exceptions(monkeypatch):
    errors = []
    monkeypatch.setattr(threading, "excepthook", lambda args: errors.append((args.thread.name, args.exc_type.__name__)))
    yield
    assert errors == [], "unhandled worker-thread exceptions"


class MethodResponder(Responder):
    def __init__(self, *args):
        self.methods = []
        super().__init__(*args)
    def handle(self, conn):
        methods = self.methods
        class Observed:
            data, recorded = b"", False
            def __getattr__(self, name): return getattr(conn, name)
            def recv(self, size):
                chunk = conn.recv(size)
                self.data += chunk
                if not self.recorded and b"\r\n\r\n" in self.data:
                    try: message = json.loads(self.data.split(b"\r\n\r\n", 1)[1])
                    except ValueError: pass
                    else: methods.append(message["method"]); self.recorded = True
                return chunk
        super().handle(Observed())


class ReclaimListener(Responder):
    """Extend the shared owned server with shutdown and bounded reply barriers."""
    def __init__(self, *, mode="aria2", negative_delay=0, release_delay=0,
                 release_block=False, hold=None):
        self.kind, self.negative_delay, self.release_delay = mode, negative_delay, release_delay
        self.hold = hold
        self.let_reply, self.release_gate = threading.Event(), threading.Event()
        if not release_block: self.release_gate.set()
        self.shutdown_seen, self.vacated = threading.Event(), threading.Event()
        self.calls, self.errors, self.gets = [], [], []
        super().__init__(secret=CANARY)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    def vacate(self):
        self.stop.set()
        if threading.current_thread() is not self.thread: self.thread.join(1)
        self.listener.close()
        self.vacated.set()

    def handle(self, conn):
        try:
            conn.settimeout(.5)
            stream = conn.makefile("rb")
            with stream:
                first = stream.readline()
                if not first: return  # TCP availability probe, not an RPC.
                if first.startswith(b"GET"):
                    self.gets.append(first)
                    return
                headers = {}
                while (line := stream.readline()) not in (b"\r\n", b""):
                    key, value = line.split(b":", 1)
                    headers[key.lower()] = value.strip()
                data = json.loads(stream.read(int(headers[b"content-length"])))
                method, token = data["method"], data["params"][0]
                self.calls.append((method, token))
                self.tokens.append(token)
                positive = token == "token:" + self.secret
                (self.positive if positive else self.negative).set()
                if method == "aria2.shutdown":
                    if not positive or self.kind == "foreign":
                        body = b'{"error":{"code":1,"message":"Unauthorized"}}'
                        conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
                        return
                    assert positive, "shutdown used an unverified credential"
                    self.shutdown_seen.set()
                    self.reply(conn, b'"OK"')
                    if self.hold == "shutdown":
                        # Exercise cancellation during the mutating RPC read.
                        return
                    if self.release_delay is not None:
                        if self.release_gate.wait(4) and not self.stop.wait(self.release_delay):
                            self.vacate()
                    return
                if self.hold == ("positive" if positive else "negative"):
                    self.entered.set()
                    self.let_reply.wait(4)
                if not positive and self.negative_delay:
                    self.stop.wait(self.negative_delay)
                if self.kind == "vanish":
                    self.vacate()
                    return
                if self.kind == "tcp":
                    conn.sendall(b"not an HTTP server\n")
                    return
                if self.kind == "redirect":
                    conn.sendall(b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:"
                                 + str(self.redirect_port).encode() + b"/leak\r\nContent-Length: 0\r\n\r\n")
                    return
                if self.kind in ("invalid", "truncated", "status", "bool", "str", "message", "accept_wrong"):
                    if self.kind in ("invalid", "truncated", "status"):
                        payload = b"{" if self.kind == "invalid" else b'{"result":' if self.kind == "truncated" else b'{"result":{"version":"x"}}'
                        status = b"503 Unavailable" if self.kind == "status" else b"200 OK"
                        conn.sendall(b"HTTP/1.1 " + status + b"\r\nContent-Length: "
                                     + str(len(payload)).encode() + b"\r\n\r\n" + payload)
                        return
                rejected = not positive or self.kind == "foreign"
                error = {"code": True if self.kind == "bool" else "1" if self.kind == "str" else 1,
                         "message": "wrong" if self.kind == "message" else "Unauthorized"}
                payload = {"error": error} if rejected and self.kind != "accept_wrong" else {"result": {"version": "old"}}
                body = json.dumps(payload).encode()
                status = b"400 Bad Request" if "error" in payload else b"200 OK"
                conn.sendall(b"HTTP/1.1 " + status + b"\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        except (OSError, ValueError, KeyError):
            pass
        except BaseException as error:
            self.errors.append(error)
        finally:
            conn.close()

    def reply(self, conn, result):
        if self.hold == "shutdown":
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
            self.entered.set()
            self.let_reply.wait(4)
            return
        body = b'{"result":' + result + b'}'
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: "
                     + str(len(body)).encode() + b"\r\n\r\n" + body)

    def close(self):
        self.let_reply.set()
        self.release_gate.set()
        super().close()
        assert self.errors == []


@pytest.fixture
def listeners():
    made = []
    def make(**kwargs):
        server = ReclaimListener(**kwargs)
        made.append(server)
        return server
    yield make
    for server in reversed(made): server.close()


class Drive:
    """Real daemon/controller, with GUI-owned callbacks and a closed intake gate."""
    def __init__(self, daemon, monkeypatch):
        self.daemon, self.gui = daemon, threading.get_ident()
        self.spawns, self.ready, self.errors, self.notifications = [], [], [], []
        self.ready_attempts, self.callback_threads = [], []
        self.admitted = []
        self.gate = app.BrowserDownloadGate()
        self.gate.queue = Mock()
        self.gate.queue.add_url.side_effect = lambda *args, **kwargs: self.admitted.append(args)
        spawn = daemon._spawn
        def owned_spawn(args):
            self.callback_threads.append(threading.get_ident())
            self.spawns.append(tuple(args))
            return spawn(args)
        monkeypatch.setattr(daemon, "_spawn", owned_spawn)
        self.controller = app._StartupController(None, daemon, self.prepare, self.on_ready, self.on_failure)
        self.controller.pending.connect(self.pending)

    def pending(self, on):
        self.callback_threads.append(threading.get_ident())
        self.notifications.append(on)

    def prepare(self):
        self.callback_threads.append(threading.get_ident())
        try:
            return self.daemon.begin_start()
        except a.Aria2Error as error:
            self.controller.present(False)
            self.errors.append(str(error))
            return False

    def on_ready(self):
        self.ready_attempts.append((threading.get_ident(), bool(self.daemon.active_rpc_secret()), tuple(self.notifications)))
        self.ready.append(True)
        self.gate.ready = True

    def on_failure(self, error):
        self.callback_threads.append(threading.get_ident())
        self.errors.append(str(error))

    def finish(self):
        pump(lambda: self.controller.state in ("READY", "FAILED") or bool(self.ready_attempts), 6)
        self.check_ready(self.controller.state == "READY")

    def check_ready(self, expected):
        assert all(thread == self.gui for thread, _, _ in self.ready_attempts), "READY attempted off GUI thread"
        if not expected:
            assert self.ready_attempts == [], "non-READY outcome attempted readiness"
        else:
            assert len(self.ready_attempts) == 1
            assert self.ready_attempts[0][1] and self.ready_attempts[0][2][-1] is False
        assert all(thread == self.gui for thread in self.callback_threads), "GUI callback used worker thread"

    def close(self):
        self.controller.shutdown()
        join = getattr(self.controller, "join", None)
        if join is not None:
            assert join(2), "reclaim/auth worker survived cancellation"
        elif self.controller.worker is not None:  # pristine RED-01 cleanup
            self.controller.worker.join(2)
            assert not self.controller.worker.is_alive()
        assert self.daemon.stop()


@pytest.fixture
def drive(launch, monkeypatch):
    daemon, root = launch
    monkeypatch.setattr(a, "ARIA2_SESSION", root / "aria2.session")
    instance = Drive(daemon, monkeypatch)
    try: yield instance, root
    finally:
        was_ready = instance.controller.state == "READY"
        instance.close()
        observed = bool(instance.ready_attempts)
        assert not (was_ready and not observed), "READY state had no observed readiness attempt"
        instance.check_ready(observed)


def stale_record(drive, root, server):
    drive.daemon.settings.rpc_port = server.port
    (root / "aria2.rpc.auth").write_text(f"rpc-secret={CANARY}\n")
    (root / "aria2.session").write_text("must survive occupied-port reclamation")


def assert_closed(drive):
    assert drive.ready_attempts == []
    assert not drive.gate.accept({"url": "https://example.com/queued"})
    assert not drive.admitted
    assert not drive.ready
    assert drive.daemon.active_rpc_secret() is None


def test_red01_real_startup_reclaim_keeps_qt_heartbeat(drive, listeners):
    """Behavioral RED on pristine startup: synchronous begin blocks >250 ms."""
    d, root = drive
    old = listeners(negative_delay=.4, release_delay=.4)
    stale_record(d, root, old)
    beats = [time.monotonic()]
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: beats.append(time.monotonic()))
    timer.start()
    started = time.monotonic()
    try:
        d.controller.begin()
        d.finish()
        beats.append(time.monotonic())
        assert max(right - left for left, right in zip(beats, beats[1:])) <= .25, "GUI reclaim blocked heartbeat"
        assert time.monotonic() - started >= .7, "test did not exercise slow reclaim"
        assert len(beats) >= 20
        assert d.controller.generation == 1 and len(d.spawns) == 1 and d.ready == [True]
    finally:
        timer.stop()


def test_red02_free_port_has_one_spawn_and_fresh_auth(drive):
    d, root = drive
    started = time.monotonic()
    d.controller.begin()
    d.finish()
    assert time.monotonic() - started < 2
    assert len(d.spawns) == 1 and d.ready == [True] and d.controller.generation == 1
    assert (root / "positive.done").exists() and (root / "negative.done").exists()
    assert d.notifications == [True, False]
    assert d.daemon.preflight is None, "spawn reused an unconsumed preflight"
    assert d.daemon.stop()
    assert isinstance(d.daemon.begin_start(), a._ReclaimRequest), "restart reused the previous verdict"


def test_refusing_port_unlinks_real_legacy_session_exactly_once(drive, monkeypatch):
    """Positive control: the session-survival assertions can detect an unlink."""
    d, root = drive
    session = root / "aria2.session"
    assert a.ARIA2_SESSION == session and a.DATA_DIR == root
    session.write_text("legacy state on a genuinely refusing port")
    unlinked = []
    path_type = type(session)
    unlink = path_type.unlink
    def record(path, *args, **kwargs):
        if path == session: unlinked.append(path)
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(path_type, "unlink", record)
    d.controller.begin()
    d.finish()
    assert d.ready == [True] and len(d.spawns) == 1
    assert not session.exists() and unlinked == [session]


def test_red03_verified_old_child_is_replaced_not_inherited(drive, listeners, monkeypatch):
    d, root = drive
    old = listeners(release_block=True)
    stale_record(d, root, old)
    observed = []
    probe, release = a.Aria2Daemon.reclaim_probe, a.Aria2Daemon.reclaim_release
    def checked(fn):
        def call(request, cancel):
            observed.append((fn.__name__, threading.get_ident(), request.port))
            return fn(request, cancel)
        return call
    monkeypatch.setattr(a.Aria2Daemon, "reclaim_probe", staticmethod(checked(probe)))
    monkeypatch.setattr(a.Aria2Daemon, "reclaim_release", staticmethod(checked(release)))
    d.controller.begin()
    pump(old.shutdown_seen.is_set)
    assert old.positive.is_set() and old.negative.is_set()
    assert len([call for call in old.calls if call[0] == "aria2.shutdown"]) == 1
    assert_closed(d)
    assert d.spawns == [] and d.notifications == [True]
    old.release_gate.set()
    d.finish()
    assert len(observed) == 2
    assert all(thread != d.gui for _, thread, _ in observed), "network waiting used GUI thread"
    assert len(d.spawns) == 1 and d.ready == [True]
    assert d.daemon.active_rpc_secret() != CANARY
    assert CANARY not in repr(d.spawns)
    assert (root / "aria2.session").exists(), "initially occupied port unlinked legacy state"
    assert (root / "positive.done").exists() and (root / "negative.done").exists()
    assert d.notifications == [True, False], "banner flickered between startup phases"


@pytest.mark.parametrize("kind", ["foreign", "tcp", "invalid", "truncated", "status", "redirect", "bool", "str", "message", "accept_wrong"])
def test_red04_06_untrusted_listener_never_gets_shutdown(drive, listeners, kind, caplog):
    d, root = drive
    target = listeners()
    old = listeners(mode=kind)
    old.redirect_port = target.port
    stale_record(d, root, old)
    d.controller.begin()
    d.finish()
    assert d.controller.state == "FAILED"
    assert d.spawns == [] and not old.shutdown_seen.is_set()
    assert old.calls and all(method == "aria2.getVersion" for method, _ in old.calls)
    assert target.calls == [] and target.gets == [], "followed RPC redirect"
    assert any(text.startswith("Another aria2 process is already using port") for text in d.errors)
    assert CANARY not in repr(d.errors) + caplog.text
    assert (root / "aria2.session").exists()
    assert_closed(d)
    assert d.notifications == [True, False]


@pytest.mark.parametrize("mode", ["silent", "partial", "drip", "http10", "close", "eof"])
@pytest.mark.parametrize("step", ["positive", "negative"])
def test_red06_hostile_framing_cancelled_without_shutdown(drive, mode, step, monkeypatch):
    d, root = drive
    old = MethodResponder(mode, step)
    stale_record(d, root, old)
    observe_body_read(old, monkeypatch.setattr)
    try:
        d.controller.begin()
        pump(old.entered.is_set)
        if mode in ("drip", "http10", "close", "eof"): pump(old.body_read.is_set)
        assert_closed(d)
        start = time.monotonic()
        d.controller.shutdown()
        assert d.controller.join(1.8)
        assert time.monotonic() - start < 1.8
        QApplication.processEvents()
        assert d.controller.state == "STOPPED" and d.spawns == [] and d.errors == []
        assert "aria2.shutdown" not in old.methods
    finally:
        d.controller.shutdown()
        old.close()


@pytest.mark.parametrize("release", [True, False])
def test_red07_release_wait_is_responsive_and_respects_bound(drive, listeners, monkeypatch, release):
    d, root = drive
    old = listeners(release_delay=.35 if release else None)
    stale_record(d, root, old)
    monkeypatch.setattr(a, "_PORT_RELEASE_TIMEOUT", .65)
    d.controller.begin()
    pump(old.shutdown_seen.is_set)
    assert d.spawns == []
    ticks = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    try:
        d.finish()
    finally:
        timer.stop()
    assert len(ticks) >= 10, "port-release poll blocked GUI dispatch"
    assert len(d.spawns) == int(release)
    assert d.ready == ([True] if release else [])
    assert d.controller.state == ("READY" if release else "FAILED")
    assert len([c for c in old.calls if c[0] == "aria2.shutdown"]) == 1
    assert (root / "aria2.session").exists()


def test_red08_vanishing_probe_never_unlinks_legacy_session(drive, listeners):
    d, root = drive
    old = listeners(mode="vanish")
    stale_record(d, root, old)
    d.controller.begin()
    d.finish()
    assert old.vacated.is_set() and not old.shutdown_seen.is_set()
    assert (root / "aria2.session").exists(), "F3: refusal after occupied probe must mean free"
    assert len(d.spawns) == 1 and d.ready == [True]


def test_red08_port_taken_after_free_result_never_becomes_ready(drive, listeners, monkeypatch):
    d, root = drive
    (root / "aria2.session").write_text("must survive a port claimed after the probe")
    foreign = listeners(mode="foreign")
    d.daemon.settings.rpc_port = foreign.port
    # Real listener occupies the port before spawn. Only the first observation
    # is controlled to represent an earlier free-port check.
    observed = threading.Event()
    def free(request, cancel): observed.set(); return "free"
    monkeypatch.setattr(a.Aria2Daemon, "reclaim_probe", staticmethod(free))
    d.controller.begin()
    pump(observed.is_set)
    d.finish()
    assert len(d.spawns) == 1 and d.controller.state == "FAILED"
    assert not foreign.shutdown_seen.is_set()
    assert (root / "aria2.session").exists()
    assert_closed(d)


def test_red10_duplicate_begin_and_duplicate_op_do_not_release_twice(drive, listeners):
    d, root = drive
    old = listeners(hold="negative", release_block=True)
    stale_record(d, root, old)
    d.controller.begin()
    pump(old.entered.is_set)
    c = d.controller
    generation, op, first_worker = c.generation, c.op, c.worker
    for _ in range(4): c.begin()
    assert c.generation == generation == 1 and c.worker is first_worker
    old.let_reply.set()
    pump(old.shutdown_seen.is_set)
    assert c.state == "RELEASING"
    c.reclaim(generation, op, "eligible")
    assert c.state == "RELEASING" and not d.errors, "duplicate eligible changed release authority"
    release_op = c.op
    old.release_gate.set()
    d.finish()
    c.reclaim(generation, release_op, "released")
    c.complete(generation, "ok")
    assert len(d.spawns) == 1 and d.ready == [True]
    assert len([x for x in old.calls if x[0] == "aria2.shutdown"]) == 1


@pytest.mark.parametrize("phase", ["probe", "release", "auth"])
def test_red09_cancel_queued_result_cannot_spawn_or_ready(drive, listeners, phase):
    d, root = drive
    old = listeners(release_block=phase == "release")
    stale_record(d, root, old)
    if phase == "auth": (root / "mode.json").write_text('{"negative_delay": 0.5}')
    c = d.controller
    c.begin()
    if phase != "probe":
        pump(lambda: old.shutdown_seen.is_set())
    if phase == "release": old.release_gate.set()
    if phase == "auth": pump(lambda: (root / "negative.entered").exists())
    # Stop pumping while a worker finishes: completion authority is still GUI-owned.
    assert c.worker is not None
    if phase != "auth":
        c.worker.join(2)
        assert not c.worker.is_alive()
    before = len(d.spawns)
    c.shutdown()
    assert c.join(2)
    QApplication.processEvents()
    assert c.state == "STOPPED" and len(d.spawns) == before
    assert not d.ready and not d.errors
    if phase == "probe": assert not old.shutdown_seen.is_set()


def test_red11_prior_generation_and_op_have_no_authority(monkeypatch):
    """INTERFACE RED: stale completions cannot mutate a live newer attempt."""
    daemon, ready, failed = Mock(), Mock(), Mock()
    c = app._StartupController(None, daemon, Mock(), ready, failed)
    c.state, c.generation, c.op = "RECLAIMING", 2, 7
    c.request = a._ReclaimRequest(12345, CANARY)
    notifications = []
    c.pending.connect(notifications.append)
    run = Mock()
    monkeypatch.setattr(c, "run", run)
    for generation, op in ((1, 7), (2, 6)):
        for result in ("eligible", "free", "unexpected"):
            c.reclaimed.emit(generation, op, result)
    QApplication.processEvents()
    assert c.state == "RECLAIMING" and c.request.port == 12345
    assert c.op == 7 and notifications == []
    run.assert_not_called()
    failed.assert_not_called()
    ready.assert_not_called()
    daemon.stop.assert_not_called()
    c.shutdown()
    c.reclaim(2, 7, "eligible")
    run.assert_not_called()


@pytest.mark.parametrize("state", ["RECLAIMING", "RELEASING"])
def test_direct_closing_guard_ignores_matching_reclaim_identity(state):
    for result in ("free", "refused", "stale", "eligible", "released", "busy"):
        daemon = Mock(preflight=object())
        c = app._StartupController(None, daemon, Mock(), Mock(), Mock())
        c.state, c.generation, c.op, c.closing = state, 3, 7, True
        request, previous = a._ReclaimRequest(12345, CANARY), Mock()
        c.request, c.worker = request, previous
        preflight, notifications = daemon.preflight, []
        c.pending.connect(notifications.append)
        c.run, c.advance, c.failure = Mock(), Mock(), Mock()
        c.reclaim(3, 7, result)
        assert c.state == state and c.generation == 3 and c.op == 7
        assert c.request is request and c.worker is previous
        assert daemon.preflight is preflight and notifications == []
        for callback in (c.run, c.advance, c.prepare, c.failure, daemon._spawn):
            callback.assert_not_called()


@pytest.mark.parametrize("state", ["AUTHENTICATING", "READY", "FAILED", "STARTING", "STOPPED"])
def test_inactive_state_rejects_matching_reclaim_identity(state):
    for result in ("free", "refused", "stale", "eligible", "released", "busy", "cancelled", "unexpected", "unknown"):
        errors, notifications = [], []
        daemon = Mock(preflight=object())
        c = app._StartupController(None, daemon, Mock(), Mock(), errors.append)
        c.state, c.generation, c.op = state, 3, 7
        c.request, c.worker = a._ReclaimRequest(12345, CANARY), Mock()
        snapshot = c.request, c.worker, daemon.preflight
        c.pending.connect(notifications.append)
        c.run, c.advance, c.failure = Mock(), Mock(), Mock(side_effect=errors.append)
        c.reclaim(3, 7, result)
        assert c.state == state and c.generation == 3 and c.op == 7
        assert c.request is snapshot[0] and c.worker is snapshot[1]
        assert daemon.preflight is snapshot[2] and errors == notifications == []
        for callback in (c.run, c.advance, c.prepare, c.failure, daemon._spawn, c.ready):
            callback.assert_not_called()


def test_red12_settings_change_uses_snapshot_and_new_cycle(drive, listeners):
    d, root = drive
    old = listeners(hold="negative")
    other = listeners(mode="foreign")
    stale_record(d, root, old)
    c = d.controller
    c.begin()
    pump(old.entered.is_set)
    first_op = c.op
    d.daemon.settings.rpc_port = other.port
    d.daemon.settings.rpc_secret = "CHANGED_LIVE_SECRET_123456789"
    old.let_reply.set()
    d.finish()
    assert old.shutdown_seen.is_set(), "release mixed the new port with the old credential"
    assert [token for method, token in old.calls if method == "aria2.shutdown"] == ["token:" + CANARY]
    assert not other.shutdown_seen.is_set()
    assert other.calls and other.calls[0][1] == "token:" + CANARY  # record, not live Settings secret
    assert len(d.spawns) == 0 and c.generation == 1 and c.state == "FAILED"
    assert c.op > first_op
    c.reclaim(1, first_op, "eligible")
    assert c.state == "FAILED" and not other.shutdown_seen.is_set()
    assert_closed(d)


def test_red16_invalid_record_is_fail_closed_without_token(drive, listeners, caplog):
    d, root = drive
    old = listeners()
    stale_record(d, root, old)
    (root / "aria2.rpc.auth").write_text("rpc-secret=truncated")
    d.controller.begin()
    d.finish()
    assert d.spawns == [] and old.calls == []
    assert not old.shutdown_seen.is_set() and (root / "aria2.session").exists()
    assert CANARY not in caplog.text + repr(d.errors)
    assert_closed(d)


@pytest.mark.parametrize("record,expected", [("ABSENT", "live"), ("VALID", CANARY), ("PRESENT-INVALID", None)])
def test_deferred_prepare_is_network_free_and_redacts_request(tmp_path, monkeypatch, record, expected):
    daemon = a.Aria2Daemon(Settings(rpc_port=12345, rpc_secret="live", download_dir=str(tmp_path)))
    daemon.preflight = None
    monkeypatch.setattr(a, "_resolve_aria2c", lambda: "/owned/fake-aria2")
    monkeypatch.setattr(a, "read_rpc_record", lambda: (record, CANARY if record == "VALID" else None))
    network = Mock(side_effect=AssertionError("GUI did network I/O"))
    monkeypatch.setattr(a, "_probe_port", network)
    monkeypatch.setattr(daemon, "_port_in_use", network)
    monkeypatch.setattr(daemon, "_spawn", Mock(side_effect=AssertionError("spawn before reclaim")))
    request = daemon.begin_start()
    assert request.port == 12345 and request.secret == expected
    assert CANARY not in repr(request) and "live" not in repr(request)
    assert str(request.port) in repr(request)
    network.assert_not_called()


@pytest.mark.parametrize("stage", ["probe", "release", "auth"])
def test_red14_thread_start_failure_retains_predecessors(monkeypatch, stage):
    predecessor = Mock()
    predecessor.is_alive.return_value = True
    daemon, failed = Mock(), Mock()
    c = app._StartupController(None, daemon, Mock(return_value=object()), Mock(), failed)
    c.worker = predecessor
    c.request = a._ReclaimRequest(12345, CANARY)
    c.generation, c.state = 1, "RECLAIMING"
    original = RuntimeError(CANARY)
    monkeypatch.setattr(app.threading, "Thread", Mock(return_value=Mock(start=Mock(side_effect=original))))
    if stage == "probe":
        c.prepare.return_value = c.request
        c.advance()
    elif stage == "release":
        c.reclaim(1, 0, "eligible")
    else:
        c.advance()
    assert c.worker is None and c.retired == [predecessor]
    assert c.state == "FAILED" and c.cancel.cancelled
    failed.assert_called_once_with(None)
    assert not c.join(0)
    predecessor.join.assert_called_once_with(0)
    predecessor.is_alive.return_value = False
    assert c.join(0)


def test_red14_join_uses_one_deadline_for_all_threads(monkeypatch):
    c = app._StartupController(None, Mock(), Mock(), Mock(), Mock())
    clock = iter((10., 10., 10.75, 11.5))
    monkeypatch.setattr(app.time, "monotonic", lambda: next(clock))
    threads = [Mock() for _ in range(3)]
    for thread in threads: thread.is_alive.return_value = False
    c.retired, c.worker = threads[:2], threads[2]
    assert c.join(2)
    assert [t.join.call_args.args[0] for t in threads] == [2, 1.25, .5]


@pytest.mark.parametrize("phase", ["RECLAIMING", "RELEASING"])
def test_red14_destroyed_receiver_worker_is_joined(monkeypatch, phase):
    entered, release = threading.Event(), threading.Event()
    parent = QObject()
    c = app._StartupController(parent, Mock(), Mock(), Mock(), Mock())
    c.state, c.generation = phase, 1
    c.request = a._ReclaimRequest(12345, CANARY)
    def fn(request, cancel):
        entered.set()
        assert release.wait(2)
        return "free"
    c.run(1, fn)
    try:
        assert entered.wait(1)
        c.shutdown()
        parent.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        release.set()
        assert c.join(1)
        assert c.worker is not None and not c.worker.is_alive()
    finally:
        release.set()
        assert c.join(2)


@pytest.mark.parametrize("phase", ["probe", "release"])
@pytest.mark.parametrize("event_only", [False, True], ids=["socket-shutdown", "event-only"])
def test_f1_connect_succeeds_after_cancel_closes_before_read(listeners, monkeypatch, phase, event_only):
    old = listeners(hold="positive" if phase == "probe" else "shutdown")
    cancel = a._AuthCancel()
    original = socket.socket.connect
    cancelled_once = threading.Event()
    def connect(sock, address):
        if sock in cancel.sockets and not cancelled_once.is_set():
            cancelled_once.set()
            cancel.event.set() if event_only else cancel.cancel()
        return original(sock, address)
    monkeypatch.setattr(socket.socket, "connect", connect)
    request = a._ReclaimRequest(old.port, CANARY)
    fn = a.Aria2Daemon.reclaim_probe if phase == "probe" else a.Aria2Daemon.reclaim_release
    with worker(lambda: fn(request, cancel)) as (_, result, done):
        try:
            assert done.wait(1.5), "post-connect cancellation entered an RPC read"
            assert cancelled_once.is_set(), "did not exercise the transport connect window"
            assert result == ["cancelled"]
            assert old.calls == [], "cancelled connect sent RPC bytes"
        finally:
            cancel.cancel()


def test_cancel_shutdown_rpc_and_port_release_wait(drive, listeners):
    d, root = drive
    old = listeners(hold="shutdown")
    stale_record(d, root, old)
    d.controller.begin()
    pump(old.entered.is_set)
    assert d.controller.state == "RELEASING"
    d.controller.shutdown()
    assert d.controller.join(1.5)
    QApplication.processEvents()
    assert d.spawns == [] and not d.ready
    assert len([c for c in old.calls if c[0] == "aria2.shutdown"]) == 1


def test_red15_direct_sync_start_still_reclaims_occupied_port(launch, listeners):
    daemon, root = launch
    old = listeners()
    daemon.settings.rpc_port = old.port
    (root / "aria2.rpc.auth").write_text(f"rpc-secret={CANARY}\n")
    assert daemon.preflight is a._SYNC_RECLAIM
    daemon.start()
    assert daemon.active_rpc_secret() and daemon.active_rpc_secret() != CANARY
    assert old.shutdown_seen.is_set()
    assert len([c for c in old.calls if c[0] == "aria2.shutdown"]) == 1
    assert (root / "positive.done").exists() and (root / "negative.done").exists()


def test_red17_only_owned_child_may_receive_process_signals(drive, listeners, monkeypatch):
    d, root = drive
    unrelated = listeners(mode="foreign")
    old = listeners()
    stale_record(d, root, old)
    sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"], start_new_session=True)
    signals = []
    kill, killpg = os.kill, os.killpg
    def checked(fn):
        def send(pid, sig):
            signals.append((pid, sig, threading.get_ident()))
            assert d.daemon._proc is not None and pid == d.daemon._proc.pid
            assert pid != sentinel.pid and threading.get_ident() == d.gui
            return fn(pid, sig)
        return send
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "kill", checked(kill))
            patch.setattr(os, "killpg", checked(killpg))
            d.controller.begin()
            d.finish()
            assert d.ready == [True]
            assert sentinel.poll() is None and a._probe_port(unrelated.port) == "busy"
            assert unrelated.calls == []
            assert not signals, "reclamation signalled a process"
            d.close()
            assert sentinel.poll() is None
    finally:
        sentinel.terminate()
        sentinel.wait(timeout=2)


# Real run(), MainWindow and QueueManager, with socket-free worker barriers.
# Loopback protocol behavior is exercised separately above; these tests prove
# the actual app wiring, modal boundaries and buffered intake sequencing.
_RECLAIM_UI = r'''
import threading
from PySide6.QtWidgets import QApplication, QLabel
from PySide6.QtCore import QEventLoop, QSignalBlocker, QThread, Qt
from cove import aria2 as lifecycle, main_window as mw
from cove.scheduler import Scheduler
from tests.test_app_async_startup import until
case = __CASE__
entered = [threading.Event() for _ in range(3)]
release = [threading.Event() for _ in range(3)]
controllers, windows, events, probes, shutdowns, order, worker_threads, thread_errors = [], [], [], [], [], [], [], []
threading.excepthook = lambda args: thread_errors.append((args.thread.name, args.exc_type.__name__))
gui = threading.get_ident()
class Controller(app_mod._StartupController):
    def __init__(self, *args):
        super().__init__(*args)
        controllers.append(self)
app_mod._StartupController = Controller
original_begin = FakeAria2Daemon.begin_start
def begin(self):
    assert threading.get_ident() == gui
    if self.preflight is None:
        return lifecycle._ReclaimRequest(windows[0].settings.rpc_port, "FROZEN_RECORD_123456789")
    assert self.preflight.kind == "released"
    if case in ("stale", "stale-quit"): raise Aria2Error("Another aria2 process is already using port 6800.")
    if case in ("interface", "settings") and not getattr(self, "recovered", False):
        raise Aria2InterfaceError("Network interface 'x' was not found.")
    self.preflight = None
    return original_begin(self)
FakeAria2Daemon.begin_start = begin
def operation(index, result):
    def run(request, cancel):
        worker_threads.append(threading.get_ident())
        if index == 0: probes.append(request.port)
        if index == 1: shutdowns.append(request.port)
        entered[index].set()
        assert release[index].wait(4)
        return result
    return run
FakeAria2Daemon.reclaim_probe = staticmethod(operation(0, "eligible"))
FakeAria2Daemon.reclaim_release = staticmethod(operation(1, "released"))
def authenticate(ticket, cancel):
    worker_threads.append(threading.get_ident())
    entered[2].set()
    assert release[2].wait(4)
    return None
FakeAria2Daemon.authenticate = staticmethod(authenticate)
class Window(mw.MainWindow):
    def __init__(self, *args):
        super().__init__(*args)
        windows.append(self)
        self.sub = next(w for w in self.findChildren(QLabel) if w.property("role") == "hero-sub")
        self.original = self.sub.text()
        settle(self.queue)
        self._refresh_status_pill()
        self.baseline = self.snapshot()
    def snapshot(self):
        return (self.status_pill.text(), [(t.id, t.status) for t in self.queue.tasks.values()],
                [self.tree.topLevelItem(i).text(mw.COL_STATUS) for i in range(self.tree.topLevelItemCount())],
                self.queue.aria2_admission, list(calls.get("rpc", [])))
    def set_engine_starting(self, on):
        assert threading.get_ident() == gui
        events.append(on)
        order.append(("pending", on))
        super().set_engine_starting(on)
    def _open_settings(self):
        assert not controllers[0].shown and self.sub.text() == self.original
        FakeAria2Daemon.instance.recovered = True
    def add_url_interactive(self, url):
        assert controllers[0].state == "READY" and FakeAria2Daemon.instance.ready
        assert entered[2].is_set() and release[2].is_set()
        calls["add_url"].append(url)
app_mod.MainWindow, app_mod.Scheduler = Window, Scheduler
old_load = app_mod.Settings.load
def load(cls):
    settings = old_load()
    settings.max_concurrent = 1
    settings.torrent_support_enabled = True
    with db.connect() as conn:
        conn.execute("DELETE FROM downloads")
        for i in range(0 if case == "zero" else 4 if case == "many" else 1):
            conn.execute("INSERT INTO downloads (url,out_dir,connections,speed_limit_kbps,status,created_at,category,backend) VALUES (?,?,4,0,'queued',1,'Other','aria2')",
                         (f"https://example.com/{i}.zip", str(Path.cwd())))
    if case == "scheduled":
        settings.schedule.enabled, settings.schedule.days = True, []
    return settings
app_mod.Settings.load = classmethod(load)
if case in ("paused", "scheduled"): StartupQueue._pause_everything = lambda self: None
old_init = StartupQueue.__init__
def queue_init(self, *args, **kwargs):
    old_init(self, *args, **kwargs)
    if case == "paused": self.stop_queue()
StartupQueue.__init__ = queue_init
old_admission = StartupQueue.set_aria2_admission
def admission(self, state):
    calls["admission"].append(state)
    if state == "open":
        assert controllers[0].state == "READY" and FakeAria2Daemon.instance.ready
        assert windows[0].sub.text() == windows[0].original
    return old_admission(self, state)
StartupQueue.set_aria2_admission = admission
def modal():
    c, w = controllers[0], windows[0]
    assert not c.shown and w.sub.text() == w.original, "banner remained under modal"
    before = list(events), list(calls["admission"])
    c.reclaimed.emit(c.generation - 1, c.op, "eligible")
    c.reclaimed.emit(c.generation, c.op - 1, "released")
    c.completed.emit(c.generation, "ok")
    QApplication.processEvents()
    assert (events, calls["admission"]) == before, "modal reentry advanced stale completion"
    assert "open" not in calls["admission"]
old_critical = FakeQMessageBox.critical
def critical(*args):
    order.append(("modal", "critical"))
    modal()
    result = old_critical(*args)
    if case == "stale-quit":
        controllers[0].shutdown()
        QApplication.instance().quit()
    return result
FakeQMessageBox.critical = staticmethod(critical)
old_box = FakeQMessageBox.exec
def box(self):
    order.append(("modal", "interface"))
    modal()
    return old_box(self)
FakeQMessageBox.exec = box
class ProbeApplication(_RealQApplication):
    def pump(self, predicate):
        def check(): self.processEvents(); return predicate()
        until(check, 5)
    def exec(self):
        try:
            self.pump(entered[0].is_set)
            c, w = controllers[0], windows[0]
            assert c.state == "RECLAIMING" and c.generation == 1
            assert events == [True] and c.shown and w.sub.text() != w.original
            assert w.snapshot() == w.baseline
            for _ in range(3): c.begin()
            assert probes == [c.request.port]
            release[0].set()
            self.pump(entered[1].is_set)
            assert c.state == "RELEASING" and events == [True]
            assert w.snapshot() == w.baseline
            # Real magnet preflight must remain blocked during old-daemon cleanup.
            # Suppress the deliberate refused-action toast while comparing
            # presentation snapshots across the subsequent auth transition.
            with QSignalBlocker(w.queue):
                result = w.queue.resolve_magnet_preflight("magnet:?xt=urn:btih:" + "b" * 40, on_resolved=lambda _: None)
            assert result.state == "blocked"
            assert not [x for x in calls.get("rpc", []) if x[0] in ACTIVATING]
            if case == "quit":
                c.shutdown()
                release[1].set()
                assert c.join(2)
                self.processEvents()
                assert not entered[2].is_set() and "open" not in calls["admission"]
                return 0
            release[1].set()
            if case == "stale-quit":
                self.pump(lambda: c.state == "STOPPED")
                assert [entry[0] for entry in calls["boxes"]] == ["critical"], "quit opened another modal"
                assert "daemon_start" not in calls["constructed"] and "open" not in calls["admission"]
                assert not entered[2].is_set() and calls["add_url"] == []
                return 0
            if case in ("stale", "interface"):
                self.pump(lambda: c.state == "FAILED")
                target = "critical" if case == "stale" else "interface"
                assert order.index(("pending", False)) < order.index(("modal", target)), "modal preceded banner clear"
                assert events == [True, False]
                assert not entered[2].is_set() and "open" not in calls["admission"]
                return 0
            self.pump(entered[2].is_set)
            assert c.state == "AUTHENTICATING"
            assert events == ([True, False, True] if case == "settings" else [True])
            assert w.snapshot() == w.baseline, f"{w.snapshot()} != {w.baseline}"
            release[2].set()
            self.pump(lambda: c.state == "READY")
            self.pump(lambda: calls["admission"] == ["open"])
            assert events == ([True, False, True, False] if case == "settings" else [True, False])
            assert len(probes) == (2 if case == "settings" else 1)
            if case == "settings":
                assert order.index(("pending", False)) < order.index(("modal", "interface"))
            assert c.generation == 1 and not c.shown
            assert calls["drains"] == 1
            assert len(calls["add_url"]) == 2, "buffered CLI/IPC delivery was not gated by new-child READY"
        finally:
            for event in release: event.set()
            for c in controllers:
                c.shutdown()
                assert c.join(2)
            assert thread_errors == [] and all(thread != gui for thread in worker_threads)
            self.aboutToQuit.emit()
            self.processEvents()
        return 0
app_mod.QApplication = ProbeApplication
'''


@pytest.mark.parametrize("case", ["zero", "one", "many", "paused", "scheduled", "stale", "stale-quit", "interface", "settings", "quit"])
def test_red13_14_18_real_reclaim_banner_modals_and_queue(case):
    script = _HARNESS.replace("rc = app_mod.run()", _RECLAIM_UI.replace("__CASE__", repr(case)) + "\nrc = app_mod.run()")
    substitutions = {"__A7A_FLAGS__": {"real_queue": True, "interface_choices": ["Open Settings"] if case == "settings" else ["Close"]},
                     "__ARGV__": ["magnet:?xt=urn:btih:" + "a" * 40], "__IS_PRIMARY__": True,
                     "__SETTINGS_UNREADABLE__": False, "__DAEMON_FAILS__": False,
                     "__EARLY_IPC__": ["magnet:?xt=urn:btih:" + "c" * 40], "__LATE_IPC__": None,
                     "__FORWARD_OK__": True, "__BAD_URL__": "", "__CLEANUP_STATUS__": "not_needed"}
    for key, value in substitutions.items(): script = script.replace(key, repr(value))
    result = _isolated_child(script)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr


def test_retired_worker_remains_owned_until_real_thread_exits(monkeypatch):
    real_thread = threading.Thread
    hold, entered = threading.Event(), threading.Event()
    class Thread(real_thread):
        def __init__(self, *, target, name):
            def wrapped():
                target()
                if name == "aria2-reclaim":
                    entered.set()
                    assert hold.wait(3)
            super().__init__(target=wrapped, name=name)
    monkeypatch.setattr(app.threading, "Thread", Thread)
    monkeypatch.setattr(a.Aria2Daemon, "reclaim_probe", staticmethod(lambda request, cancel: "free"))
    monkeypatch.setattr(a.Aria2Daemon, "authenticate", staticmethod(lambda ticket, cancel: None))
    ticket = a._LaunchTicket(None, CANARY, 12345)
    daemon, ready, failed = Mock(), Mock(), Mock()
    c = app._StartupController(None, daemon, Mock(side_effect=[a._ReclaimRequest(12345, CANARY), ticket]), ready, failed)
    try:
        c.begin()
        assert entered.wait(1)
        pump(lambda: c.state == "READY")
        assert len(c.retired) == 1 and c.retired[0].is_alive()
        assert not c.retired[0].daemon and not c.worker.daemon
        assert not c.join(.02), "teardown detached a retired worker"
        ready.assert_called_once_with()
        failed.assert_not_called()
    finally:
        hold.set()
        c.shutdown()
        assert c.join(2)


@pytest.mark.parametrize("result", ["cancelled", "unexpected", "unknown"])
def test_unknown_reclaim_outcome_fails_closed_on_gui(monkeypatch, result):
    monkeypatch.setattr(a.Aria2Daemon, "reclaim_probe", staticmethod(lambda request, cancel: result))
    daemon, ready, failed = Mock(), Mock(), Mock()
    c = app._StartupController(None, daemon, Mock(return_value=a._ReclaimRequest(12345, CANARY)), ready, failed)
    events, gui = [], threading.get_ident()
    c.pending.connect(lambda on: events.append((on, threading.get_ident())))
    try:
        c.begin()
        pump(lambda: c.state == "FAILED")
        assert events == [(True, gui), (False, gui)]
        failed.assert_called_once_with(None)
        ready.assert_not_called()
        daemon.stop.assert_called_once_with()
        assert c.cancel.cancelled
    finally:
        c.shutdown()
        assert c.join(2)


def test_cancel_before_worker_creation_cannot_authorize_release(monkeypatch):
    entered = threading.Event()
    def probe(request, cancel):
        assert cancel.cancelled
        entered.set()
        return "eligible"
    monkeypatch.setattr(a.Aria2Daemon, "reclaim_probe", staticmethod(probe))
    release = Mock()
    monkeypatch.setattr(a.Aria2Daemon, "reclaim_release", release)
    ready, failed = Mock(), Mock()
    c = app._StartupController(None, Mock(), Mock(return_value=a._ReclaimRequest(12345, CANARY)), ready, failed)
    c.pending.connect(lambda on: c.shutdown() if on else None)
    c.begin()
    assert entered.wait(1) and c.join(1)
    QApplication.processEvents()
    assert c.state == "STOPPED" and c.generation == 2
    release.assert_not_called()
    ready.assert_not_called()
    failed.assert_not_called()


def test_cancel_during_active_port_poll_has_no_late_spawn(drive, listeners, monkeypatch):
    d, root = drive
    monkeypatch.setattr(a, "_PORT_RELEASE_TIMEOUT", 30)
    old = listeners(release_delay=None)
    stale_record(d, root, old)
    polling, unblock, lock = threading.Event(), threading.Event(), threading.Lock()
    counts = {"total": 0, "cancelled": 0}
    c = d.controller
    def observed_probe(port):
        cancelled = c.cancel.cancelled
        with lock:
            counts["total"] += 1
            counts["cancelled"] += int(cancelled)
        if old.shutdown_seen.is_set(): polling.set()
        return "free" if unblock.is_set() else "busy"
    monkeypatch.setattr(a, "_probe_port", observed_probe)
    try:
        c.begin()
        pump(old.shutdown_seen.is_set)
        pump(polling.is_set)
        assert c.state == "RELEASING" and c.worker.is_alive()
        with lock: n0 = counts["total"]
        started = time.monotonic()
        c.shutdown()
        assert c.join(2), "release worker ignored cancellation"
        assert time.monotonic() - started < 1, "release cancellation waited for its deadline"
        assert not unblock.is_set(), "test released the poller before cancellation was proved"
        assert counts["total"] >= n0 >= 2, "did not observe both initial and release probes"
        assert counts["cancelled"] <= 2, "release worker kept probing after cancellation"
    finally:
        unblock.set()  # Let a broken poller exit without a listener flood or 30 s wait.
        c.shutdown()
        assert c.join(2)
    QApplication.processEvents()
    assert c.state == "STOPPED" and not d.ready and not d.spawns
    assert d.ready_attempts == []


@pytest.mark.parametrize("exception", [RuntimeError, KeyboardInterrupt])
def test_reclaim_worker_exception_never_exposes_secret(monkeypatch, caplog, exception):
    def broken(request, cancel): raise exception(request.secret)
    monkeypatch.setattr(a.Aria2Daemon, "reclaim_probe", staticmethod(broken))
    ready, failed = Mock(), Mock()
    c = app._StartupController(None, Mock(), Mock(return_value=a._ReclaimRequest(12345, CANARY)), ready, failed)
    try:
        c.begin()
        pump(lambda: c.state == "FAILED")
        failed.assert_called_once_with(None)
        ready.assert_not_called()
        assert CANARY not in caplog.text + repr(failed.call_args)
    finally:
        c.shutdown()
        assert c.join(2)


def test_stale_result_remains_fail_closed_if_port_frees_before_delivery(drive, listeners):
    d, root = drive
    old = listeners(mode="foreign")
    stale_record(d, root, old)
    d.controller.begin()
    d.controller.worker.join(2)  # Hold the Qt completion in the event queue.
    assert not d.controller.worker.is_alive()
    old.vacate()
    d.finish()
    assert d.controller.state == "FAILED" and not d.spawns and not d.ready
    assert not old.shutdown_seen.is_set() and (root / "aria2.session").exists()


def test_second_listener_during_release_never_receives_shutdown(drive, listeners, monkeypatch):
    d, root = drive
    old = listeners(release_block=True)
    stale_record(d, root, old)
    blocked, resume = threading.Event(), threading.Event()
    probe = a._probe_port
    def port_probe(port):
        if old.vacated.is_set() and not resume.is_set():
            blocked.set()
            assert resume.wait(2)
        return probe(port)
    monkeypatch.setattr(a, "_probe_port", port_probe)
    monkeypatch.setattr(a, "_PORT_RELEASE_TIMEOUT", .4)
    d.controller.begin()
    try:
        pump(old.shutdown_seen.is_set)
        old.release_gate.set()
        pump(blocked.is_set)
        bind = socket.socket.bind
        def same_port(sock, address):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            return bind(sock, ("127.0.0.1", old.port))
        with monkeypatch.context() as patch:
            patch.setattr(socket.socket, "bind", same_port)
            replacement = listeners(mode="foreign")
        assert replacement.port == old.port
        resume.set()
        d.finish()
        assert d.controller.state == "FAILED" and not d.spawns and not d.ready
        assert replacement.calls == [] and not replacement.shutdown_seen.is_set()
        assert probe(replacement.port) == "busy"
        assert len([c for c in old.calls if c[0] == "aria2.shutdown"]) == 1
    finally:
        resume.set()


@pytest.mark.parametrize("method", ["failure", "shutdown"])
def test_terminal_controller_drops_reclaim_token(method):
    c = app._StartupController(None, Mock(), Mock(), Mock(), Mock())
    c.request = a._ReclaimRequest(12345, CANARY)
    if method == "failure": c.failure(None)
    else: c.shutdown()
    assert c.request is None


def test_foreign_replacement_between_probe_and_release_is_rejected(drive, listeners, monkeypatch):
    """No identity guarantee across this window: the replacement rejects the one RPC."""
    d, root = drive
    old = listeners()
    stale_record(d, root, old)
    entered, proceed, dialogs = threading.Event(), threading.Event(), []
    release, prepare = a.Aria2Daemon.reclaim_release, d.controller.prepare
    def delayed(request, cancel):
        entered.set()
        proceed.wait(2)
        return release(request, cancel)
    def prepare_with_dialog():
        ticket = prepare()
        if ticket is False: app.QMessageBox.critical(None, "aria2 missing", d.errors[-1])
        return ticket
    monkeypatch.setattr(a.Aria2Daemon, "reclaim_release", staticmethod(delayed))
    monkeypatch.setattr(d.controller, "prepare", prepare_with_dialog)
    monkeypatch.setattr(app.QMessageBox, "critical", lambda *args: dialogs.append(tuple(d.notifications)))
    signals = [Mock(), Mock()]
    monkeypatch.setattr(os, "kill", signals[0]); monkeypatch.setattr(os, "killpg", signals[1])
    monkeypatch.setattr(a, "_PORT_RELEASE_TIMEOUT", .25)
    d.controller.begin()
    try:
        pump(entered.is_set)
        assert old.positive.is_set() and old.negative.is_set()
        old.vacate()
        bind = socket.socket.bind
        def same_port(sock, address):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            return bind(sock, ("127.0.0.1", old.port))
        with monkeypatch.context() as patch:
            patch.setattr(socket.socket, "bind", same_port)
            foreign = listeners(mode="foreign")
        foreign.secret = "DIFFERENT_FOREIGN_SECRET_123456789"
        proceed.set()
        d.finish()
        assert [method for method, _ in foreign.calls] == ["aria2.shutdown"]
        assert not foreign.shutdown_seen.is_set() and a._probe_port(foreign.port) == "busy"
        assert d.controller.state == "FAILED" and d.spawns == []
        assert any(error.startswith("Another aria2") for error in d.errors)
        assert dialogs == [(True, False)] and d.ready_attempts == []
        for signal in signals: signal.assert_not_called()
    finally:
        proceed.set()
