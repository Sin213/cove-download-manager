"""A7b-2 author tests: focused/offscreen; owned sockets/processes, bounded cleanup.
Missing APIs are INTERFACE REDs. Discover completion names by contracted Qt
signature. The unnamed platform gate is called _async_startup_supported here.
"""
import json
import socket
import sys
import threading
import time
from contextlib import contextmanager, ExitStack
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from cove import app, aria2 as a
from cove.config import Settings
from tests.test_app_launch import _HARNESS, _isolated_child
from tests.test_queue_aria2_admission import (
    LEGS, MAGNET, SAVED, URL, env as admission_env, rows, settle,
)

CANARY = "A7B2_AUTH_SECRET_CANARY"

@pytest.mark.parametrize("outcome", ["ok", "cancelled", "stale_daemon", "rpc_down", "unexpected"])
def test_completion_and_shutdown_never_wait_for_worker(outcome, monkeypatch):
    entered, release, blocked = threading.Event(), threading.Event(), []
    def work(): entered.set(); assert release.wait(4)
    thread = threading.Thread(target=work); thread.start()
    controller = app._StartupController(None, Mock(), Mock(), Mock(), Mock())
    controller.state, controller.generation, controller.worker = "AUTHENTICATING", 1, thread
    controller.ticket = SimpleNamespace(port=12345, secret=CANARY)
    gui = threading.get_ident()
    def guard(original):
        def checked(*args, **kwargs):
            if threading.get_ident() == gui:
                blocked.append(original.__qualname__); raise AssertionError("GUI blocking wait")
            return original(*args, **kwargs)
        return checked
    try:
        assert entered.wait(1)
        with monkeypatch.context() as patch:
            for cls, name in ((threading.Thread, "join"), (threading.Event, "wait"), (threading.Condition, "wait")):
                patch.setattr(cls, name, guard(getattr(cls, name)))
            assert thread.is_alive() and not release.is_set()
            controller.complete(1, outcome); controller.shutdown()
            assert blocked == []
    finally:
        release.set(); thread.join(2); assert not thread.is_alive()

def test_auth_cancellation_is_isolated_between_operations(responders):
    gates, servers = [a._AuthCancel(), a._AuthCancel()], [responders(), responders()]
    daemons = [a.Aria2Daemon(Settings(rpc_port=s.port)) for s in servers]
    for daemon in daemons: daemon._secret = CANARY
    left, right = socket.socketpair()
    with left, right, worker(lambda: daemons[0]._await_rpc(cancel=gates[0])) as (_, _, done_a), worker(lambda: daemons[1]._await_rpc(cancel=gates[1])) as (thread_b, _, done_b):
        try:
            assert all(s.entered.wait(3) for s in servers)
            gates[0].cancel(); assert done_a.wait(2)
            servers[1].connections[0].settimeout(.2)
            with pytest.raises(socket.timeout): servers[1].connections[0].recv(1)
            assert not done_b.wait(.1) and thread_b.is_alive()
            left.sendall(b"x"); right.settimeout(.2); assert right.recv(1) == b"x"
            gates[1].cancel(); assert done_b.wait(2)
        finally:
            for gate in gates: gate.cancel()
            for server in servers: server.close()

@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("phase", ["prepare", "thread_start"])
def test_startup_baseexception_propagates_after_cleanup(exception, phase, monkeypatch):
    original, daemon, failed = exception(CANARY), Mock(), Mock()
    prepare = Mock(side_effect=original) if phase == "prepare" else Mock(return_value=object())
    controller = app._StartupController(None, daemon, prepare, Mock(), failed)
    if phase == "thread_start":
        monkeypatch.setattr(app.threading, "Thread", Mock(return_value=Mock(start=Mock(side_effect=original))))
    with pytest.raises(exception) as raised:
        controller.begin()
    assert raised.value is original
    daemon.stop.assert_called_once_with()
    assert controller.state == "FAILED" and controller.cancel.cancelled
    assert controller.worker is None
    failed.assert_not_called()

def until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        threading.Event().wait(.005)
    assert predicate(), "bounded condition did not become true"

@contextmanager
def worker(fn):
    result, done = [], threading.Event()
    def run():
        try:
            result.append(fn())
        except BaseException as error:
            result.append(error)
        finally:
            done.set()
    thread = threading.Thread(target=run, name="a7b2-test-worker")
    thread.start()
    try:
        yield thread, result, done
    finally:
        thread.join(7)
        assert not thread.is_alive(), "test worker survived fixture cleanup"

class Responder:
    """Owned loopback listener; Events mark complete request/response stages."""
    def __init__(self, mode="silent", step="positive", secret=CANARY):
        self.mode, self.step = mode, step
        self.secret = secret
        self.stop = threading.Event()
        self.entered = threading.Event()
        self.positive = threading.Event()
        self.negative = threading.Event()
        self.body_read = threading.Event()
        self.connections, self.handlers, self.tokens = [], [], []
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.listener.listen(8)
        self.listener.settimeout(.05)
        self.thread = threading.Thread(target=self.serve, name="a7b2-responder")
        self.thread.start()

    def serve(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.listener.accept()
            except (OSError, socket.timeout):
                continue
            self.connections.append(conn)
            thread = threading.Thread(target=self.handle, args=(conn,), name="a7b2-response")
            self.handlers.append(thread)
            thread.start()

    def handle(self, conn):
        try:
            conn.settimeout(.1)
            data = b""
            deadline = time.monotonic() + 3
            while b"\r\n\r\n" not in data and time.monotonic() < deadline:
                chunk = conn.recv(4096)
                if not chunk: return
                data += chunk
            head, body = data.split(b"\r\n\r\n", 1)
            length = int(next(line.split(b":", 1)[1] for line in head.split(b"\r\n")
                              if line.lower().startswith(b"content-length:")))
            while len(body) < length and time.monotonic() < deadline:
                body += conn.recv(4096)
            token = json.loads(body)["params"][0]
            self.tokens.append(token)
            positive = token == "token:" + self.secret
            (self.positive if positive else self.negative).set()
            if ("positive" if positive else "negative") == self.step:
                if self.mode == "partial":
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Len")
                elif self.mode in ("drip", "close", "http10", "eof"):
                    conn.sendall({"drip": b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n",
                                  "close": b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 100000\r\n\r\n",
                                  "http10": b"HTTP/1.0 200 OK\r\n\r\n",
                                  "eof": b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n"}[self.mode])
                self.entered.set()
                while not self.stop.wait(.05):
                    if self.mode in ("drip", "close", "http10", "eof"):
                        conn.sendall(b"x")
            else:
                value = {"result": {"version": "test"}} if positive else {
                    "error": {"code": 1, "message": "Unauthorized"}}
                body = json.dumps(value).encode()
                status = b"200 OK" if positive else b"400 Bad Request"
                conn.sendall(b"HTTP/1.1 " + status + b"\r\nConnection: close\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\n\r\n" + body)
        except (OSError, ValueError, StopIteration, KeyError):
            pass
        finally:
            conn.close()

    def close(self):
        self.stop.set()
        self.listener.close()
        self.thread.join(2)
        for conn in self.connections:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for thread in self.handlers:
            thread.join(2)
            assert not thread.is_alive()
        assert not self.thread.is_alive()

def observe_body_read(server, install):
    import http.client
    original = http.client.HTTPResponse.read
    def read(response, *args, **kwargs):
        if response.getheader("Content-Length") in (None, "100000"): server.body_read.set()
        return original(response, *args, **kwargs)
    install(http.client.HTTPResponse, "read", read)

@pytest.fixture
def responders():
    made = []
    def make(*args, **kwargs):
        item = Responder(*args, **kwargs)
        made.append(item)
        return item
    yield make
    for item in reversed(made):
        item.close()

# Child watchdog; file-gated negative probe falls back after 1.2s on sync base.
FAKE_ARIA2 = r'''
import json, signal, sys, time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
root = Path(__file__).parent
mode = json.loads((root / "mode.json").read_text())
signal.alarm(12)
if mode.get("ignore_term"): signal.signal(signal.SIGTERM, signal.SIG_IGN)
if mode.get("exit_early"): sys.exit(7)
if mode.get("no_rpc") or mode.get("sleep_only"): time.sleep(10); sys.exit(0)
port = int(next(x.split("=",1)[1] for x in sys.argv if x.startswith("--rpc-listen-port=")))
conf = Path(next(x.split("=",1)[1] for x in sys.argv if x.startswith("--conf-path=")))
secret = conf.read_text().split("rpc-secret=",1)[1].splitlines()[0]
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        positive = data["params"][0] == "token:" + secret
        step = "positive" if positive else "negative"
        (root / (step + ".entered")).touch()
        deadline = time.monotonic() + mode.get(step + "_delay", 0)
        while time.monotonic() < deadline and not (root / "release").exists(): time.sleep(.005)
        if mode.get("tcp_only") or (mode.get("positive_only") and not positive):
            time.sleep(5); return
        rejected = (not positive and not mode.get("accept_wrong")) or mode.get("positive_fail")
        payload = {"error":{"code":1,"message":"Unauthorized"}} if rejected else {"result":{"version":"fake"}}
        if mode.get("secretless"): payload = {"result":{"version":"fake"}}; rejected = False
        status, case = 400 if rejected else 200, mode.get("negative_case") if not positive else None
        if case:
            status = 200 if case == "status" else 302 if case == "redirect" else status
            payload = {"error": {"code": True if case == "bool" else "1" if case == "str" else 1,
                                 "message": "wrong" if case == "message" else "Unauthorized"}}
            if case == "contradictory": payload["result"] = {}
        body = b"{" if case == "invalid" else b"" if case == "empty" else json.dumps(payload).encode()
        with (root / "rpc.calls").open("a") as record: record.write(json.dumps([data["method"], port]) + "\n")
        self.send_response(status)
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        try: self.wfile.write(body)
        except OSError: pass
        (root / (step + ".done")).touch()
server = HTTPServer(("127.0.0.1", port), Handler)
(root / "listening").touch()
server.serve_forever(poll_interval=.05)
'''

@pytest.fixture
def launch(tmp_path, monkeypatch):
    root = tmp_path
    script = root / "fake-aria2c"
    script.write_text("#!" + sys.executable + "\n" + FAKE_ARIA2)
    script.chmod(0o700)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    monkeypatch.setattr(a, "DATA_DIR", root)
    monkeypatch.setattr(a, "ARIA2_SESSION", root / "session")
    monkeypatch.setattr(a, "ARIA2_LOG", root / "log")
    monkeypatch.setattr(a, "_resolve_aria2c", lambda: str(script))
    monkeypatch.setattr(a, "STOP_TERM_TIMEOUT", .15)
    monkeypatch.setattr(a, "STOP_KILL_TIMEOUT", .3)
    daemon = a.Aria2Daemon(Settings(rpc_port=port, download_dir=str(root)))
    (root / "mode.json").write_text("{}")
    yield daemon, root
    (root / "release").touch()
    assert daemon.stop() is True

@pytest.mark.parametrize("mode", ["silent", "partial", "drip", "close", "http10", "eof"], ids=["11A", "V1E", "V1A", "V1B", "V1C", "V1D"])
@pytest.mark.parametrize("step", ["positive", "negative"], ids=["11D", "11E"])
def test_transport_cancel_interrupts_owned_read(responders, mode, step, caplog, monkeypatch):
    gate = a._AuthCancel()  # Deliberate INTERFACE RED on pristine base.
    server = responders(mode, step)
    observe_body_read(server, monkeypatch.setattr)
    daemon = a.Aria2Daemon(Settings(rpc_port=server.port))
    daemon._secret = CANARY
    signals = Mock(side_effect=AssertionError("transport must never signal a process"))
    monkeypatch.setattr(a.os, "kill", signals); monkeypatch.setattr(a.os, "killpg", signals)
    import requests
    observed, send = [], requests.Session.send
    def guarded_send(session, request, **kwargs):
        observed.append((session.trust_env, kwargs["allow_redirects"], session.get_adapter(request.url).max_retries.total))
        return send(session, request, **kwargs)
    monkeypatch.setattr(requests.Session, "send", guarded_send)
    try:
        with worker(lambda: daemon._await_rpc(cancel=gate)) as (thread, result, done):
            try:
                assert server.entered.wait(3), "did not reach intended probe"
                if mode in ("drip", "close", "http10", "eof"): assert server.body_read.wait(3)
                assert server.positive.is_set()
                if step == "negative":
                    assert server.negative.is_set() and len(set(server.tokens)) == 2
                start = time.monotonic()
                gate.cancel()
                assert done.wait(2), "cancel did not interrupt blocked transport"
                assert time.monotonic() - start <= 2
                assert isinstance(result[0], a.Aria2Error)
                assert "cancel" in str(result[0]).lower() and CANARY not in str(result[0])
                assert daemon._active_secret is None
                signals.assert_not_called()
                assert observed and all(item == (False, False, 0) for item in observed)
            finally:
                gate.cancel()
                server.close()
    finally:
        assert CANARY not in caplog.text

@pytest.mark.parametrize("when", ["before", "after", "twice", "closed", "replacement"],
                         ids=["11F", "11G", "11H", "11L", "11N"])
def test_gate_registration_races(responders, monkeypatch, when):
    gate = a._AuthCancel()
    server = responders(step="negative" if when == "replacement" else "positive")
    registered, proceed = threading.Event(), threading.Event()
    original = type(gate).register
    count = []
    def register(self, sock):
        count.append(sock)
        if when == "replacement" and len(count) == 1:
            return original(self, sock)
        if when == "before":
            registered.set(); assert proceed.wait(3)
        original(self, sock)
        if when == "closed": sock.close()  # Close belongs to the worker.
        if when != "before":
            registered.set(); assert proceed.wait(3)
    monkeypatch.setattr(type(gate), "register", register)
    daemon = a.Aria2Daemon(Settings(rpc_port=server.port)); daemon._secret = CANARY
    try:
        with worker(lambda: daemon._await_rpc(cancel=gate)) as (_, result, done):
            try:
                assert registered.wait(3)
                if when == "closed":
                    assert count[0].fileno() == -1
                gate.cancel()
                if when == "twice": gate.cancel()
                proceed.set()
                assert done.wait(2)
                assert isinstance(result[0], a.Aria2Error)
                if when == "replacement":
                    assert len(count) >= 2
                    # Any next auth step/new connection must fail closed.
                    with socket.socket() as other, pytest.raises(OSError):
                        original(gate, other)
            finally:
                proceed.set(); gate.cancel(); server.close()
    finally:
        assert daemon._active_secret is None

def test_gate_idempotence_completion_and_sentinel(responders):
    """RED-11H/I/L/M: only shutdown registered live sockets; never close."""
    gate = a._AuthCancel()
    owned = Mock(); failing = Mock(); failing.shutdown.side_effect = OSError("closed")
    gate.register(owned); gate.register(failing)
    left, right = socket.socketpair()
    try:
        gate.cancel(); gate.cancel()
        owned.shutdown.assert_called_with(socket.SHUT_RDWR)
        owned.close.assert_not_called(); failing.close.assert_not_called()
        left.sendall(b"sentinel"); right.settimeout(.3)
        assert right.recv(8) == b"sentinel"
        with socket.socket() as late, pytest.raises(OSError): gate.register(late)
        assert gate.wait(.01)
    finally:
        left.close(); right.close()

def test_cancel_after_completed_auth_is_harmless(responders):
    """RED-11I: terminal transport result is never retroactively published READY."""
    gate = a._AuthCancel(); server = responders(step="neither")
    daemon = a.Aria2Daemon(Settings(rpc_port=server.port)); daemon._secret = CANARY
    with worker(lambda: daemon._await_rpc(cancel=gate)) as (_, result, done):
        try:
            assert done.wait(3) and result == [None]
            gate.cancel(); gate.cancel()
            assert daemon._active_secret is None
        finally:
            gate.cancel(); server.close()

@pytest.mark.parametrize("mutation", ["flag-only", "close-only"])
def test_cancellation_negative_controls(mutation):
    """Control: neither a flag nor close interrupts another thread's file read."""
    left, right = socket.socketpair()
    stream = left.makefile("rb")
    entered, flag = threading.Event(), threading.Event()
    def read():
        entered.set()
        return stream.read(1)
    try:
        with worker(read) as (_, result, done):
            try:
                assert entered.wait(1)
                if mutation == "flag-only": flag.set()
                else: left.close()  # makefile retains the underlying read descriptor.
                assert not done.wait(.15), "negative control unexpectedly unblocked"
            finally:
                right.shutdown(socket.SHUT_RDWR)
            assert done.wait(1) and result == [b""]
    finally:
        stream.close(); left.close(); right.close()

def test_rpc_session_factory_preserves_proxy_and_redirect_guards():
    calls = []
    session = SimpleNamespace(trust_env=True, close=lambda: None)
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: {"result": {"version": "test"}})
    session.post = post
    rpc = a.Aria2RPC(Settings(rpc_port=12345), secret_provider=lambda: CANARY,
                     session_factory=lambda: session)
    try:
        assert rpc.get_version() == {"version": "test"}
        assert session.trust_env is False
        assert calls[0][0] == "http://127.0.0.1:12345/jsonrpc"
        assert calls[0][1]["allow_redirects"] is False
        ordinary = a.Aria2RPC(Settings())
        try: assert ordinary._session() is not session and not ordinary._session().trust_env
        finally: ordinary.close()
    finally:
        rpc.close()

def test_split_ticket_and_ready_guards(launch, monkeypatch):
    daemon, root = launch
    ticket = daemon.begin_start()
    until(lambda: (root / "listening").exists())
    assert (ticket.proc, ticket.secret, ticket.port) == (daemon._proc, daemon._secret, daemon.settings.rpc_port)
    with pytest.raises((AttributeError, TypeError)): ticket.port = 1
    with pytest.raises(a.Aria2Error, match="progress"): daemon.begin_start()
    assert daemon.active_rpc_secret() is None and daemon._launch_conf.exists()
    error = a.Aria2Daemon.authenticate(ticket, a._AuthCancel())
    assert error is None and daemon.active_rpc_secret() is None
    daemon.commit_ready(ticket, error)
    assert daemon.active_rpc_secret() == ticket.secret and daemon._launch_conf is None
    assert daemon.begin_start() is None
    daemon._secret = "different"
    with pytest.raises(a.Aria2Error): daemon.commit_ready(ticket, None)
    assert daemon._proc is None

def test_ticket_auth_never_reads_live_settings(launch, monkeypatch):
    daemon, root = launch
    ticket = daemon.begin_start()
    until(lambda: (root / "listening").exists())
    class Forbidden:
        def __getattribute__(self, name): raise AssertionError("live settings read: " + name)
    daemon.settings = Forbidden()
    poll = Mock(wraps=ticket.proc.poll); monkeypatch.setattr(ticket.proc, "poll", poll)
    gate = a._AuthCancel()
    with worker(lambda: a.Aria2Daemon.authenticate(ticket, gate)) as (_, result, done):
        try: assert done.wait(3) and result == [None]
        finally: gate.cancel()
    poll.assert_not_called()
    assert daemon._active_secret is None

@pytest.mark.parametrize("retained", ["unreleased", "pending"])
def test_begin_refuses_retained_child_before_spawn(launch, monkeypatch, retained):
    daemon, _ = launch
    if retained == "unreleased": daemon._unreleased = True
    else: monkeypatch.setattr(a, "_owned_pending", SimpleNamespace(poll=lambda: 0))
    with monkeypatch.context() as patch:
        spawn = Mock(); patch.setattr(daemon, "_spawn", spawn)
        with pytest.raises(a.Aria2Error, match="unresolved"): daemon.begin_start()
        spawn.assert_not_called()
    if retained == "pending": monkeypatch.setattr(a, "_owned_pending", None)
    daemon._unreleased = False

def test_sync_start_public_contract_survives(launch):
    daemon, _ = launch
    daemon.start(); secret, proc = daemon.active_rpc_secret(), daemon._proc
    assert secret
    daemon.start(); assert daemon._proc is proc
    assert daemon.stop() and proc.poll() is not None
    daemon.start(); assert daemon.active_rpc_secret() != secret

def test_port_collision_never_adopts_or_stops_listener(launch, responders, monkeypatch):
    daemon, _ = launch
    sentinel = responders(step="neither", secret="unrelated")
    daemon.settings.rpc_port, daemon.settings.rpc_secret = sentinel.port, CANARY
    spawn = Mock(); monkeypatch.setattr(daemon, "_spawn", spawn)
    with pytest.raises(a.Aria2Error, match="already using port"): daemon.start()
    spawn.assert_not_called()
    assert sentinel.thread.is_alive() and daemon._proc is None

@pytest.mark.parametrize("reason", ["auth_error", "child_exit", "secret_changed", "proc_changed"])
def test_commit_failure_releases_owned_child(launch, reason):
    daemon, root = launch
    ticket = daemon.begin_start()
    proc = ticket.proc
    if reason == "child_exit": proc.terminate(); proc.wait(timeout=2)
    if reason == "secret_changed": daemon._secret = "new"
    if reason == "proc_changed":
        # Preserve the owned handle for fixture cleanup; substitute only identity.
        other = Mock(); other.poll.return_value = 0
        daemon._proc = other
    try:
        with pytest.raises(a.Aria2Error):
            daemon.commit_ready(ticket, a.Aria2Error("down") if reason == "auth_error" else None)
        assert daemon._active_secret is None
    finally:
        if reason == "proc_changed": daemon._proc = proc
        assert daemon.stop()
        assert proc.poll() is not None

APP_PATCH = r'''
import logging, socket, threading
from pathlib import Path
from PySide6.QtCore import QEventLoop, QMetaMethod, Qt
import cove.aria2 as aria
from cove.config import Settings as RealSettings
root = Path.cwd()
(root / "mode.json").write_text(json.dumps(flags.get("mode", {"negative_delay": 1.2})))
script = root / "fake-aria2c"
script.write_text("#!" + sys.executable + "\n" + __FAKE__); script.chmod(0o700)
with socket.socket() as reserve:
    reserve.bind(("127.0.0.1", 0)); port = reserve.getsockname()[1]
changed_listener = socket.socket() if flags.get("wire_config") else None
if changed_listener: changed_listener.bind(("127.0.0.1", 0)); changed_listener.listen(2); changed_listener.setblocking(False)
hostile = None
if flags.get("hostile"):
    from tests.test_app_async_startup import Responder, observe_body_read
    hostile = Responder(flags["hostile"], "negative"); port = hostile.port
    observe_body_read(hostile, setattr)
aria.DATA_DIR = root / "private"; aria.DATA_DIR.mkdir()
aria.ARIA2_SESSION = root / "session"; aria.ARIA2_LOG = root / "aria.log"
aria._resolve_aria2c = lambda: str(script)
aria.STOP_TERM_TIMEOUT, aria.STOP_KILL_TIMEOUT = .15, .3
settings = RealSettings(rpc_port=port, download_dir=str(root), auto_update_check=False,
                        api_enabled=True, intelligent_segments=False)
RealSettings.load = classmethod(lambda cls: settings)
RealSettings.save = lambda self: None
app_mod.Settings = RealSettings
main = threading.get_ident()
calls.update(heartbeat=[], auth_threads=[], lifecycle=[], hook=[], logs=[], pending_ticks=0)
class Logs(logging.Handler):
    def emit(self, record): calls["logs"].append(record.getMessage())
logging.getLogger("cove").addHandler(Logs())
threading.excepthook = lambda args: calls["hook"].append(str(args.exc_value))
real_thread = threading.Thread
workers, launch_secrets = [], []
release_auth, auth_done, cleanup_joins = threading.Event(), threading.Event(), []
class Thread(real_thread):
    def join(self, timeout=None):
        if flags.get("linger_auth"): cleanup_joins.append(timeout)
        return super().join(timeout)
    def start(self):
        if self.name in ("native-host-install", "magnet-self-heal"): return
        if self.name == "a7b2-response": return super().start()
        if flags.get("thread_failure"): raise RuntimeError("SECRET-CANARY-123")
        workers.append(self)
        return super().start()
app_mod.threading.Thread = Thread
class Daemon(aria.Aria2Daemon):
    def __init__(self, *args):
        super().__init__(*args); Daemon.instance = self
    def _spawn(self, args):
        calls["lifecycle"].append(["spawn", threading.get_ident() == main])
        launch_secrets.append(self._secret)
        if hostile: hostile.secret = self._secret
        return super()._spawn(args)
    def _port_in_use(self):
        return False if hostile else super()._port_in_use()
    def stop(self, **kwargs):
        calls["lifecycle"].append(["stop", threading.get_ident() == main])
        calls["constructed"].append("daemon_stop")
        return super().stop(**kwargs)
    def commit_ready(self, *args):
        if flags.get("commit_failure"): raise RuntimeError("SECRET-CANARY-123")
        return super().commit_ready(*args)
    def _await_rpc(self, *args, **kwargs):
        calls["auth_threads"].append(threading.get_ident() == main)
        return super()._await_rpc(*args, **kwargs)
app_mod.Aria2Daemon = Daemon
if hasattr(aria.Aria2Daemon, "authenticate"):
    authenticate = aria.Aria2Daemon.authenticate
    def auth(ticket, cancel):
        calls["auth_threads"].append(threading.get_ident() == main)
        assert threading.current_thread() in workers, "authentication requires a plain Python worker"
        if flags.get("worker_exception"): raise KeyboardInterrupt(ticket.secret + "SECRET-CANARY-123")
        if flags.get("linger_auth"):
            (root / "negative.entered").touch()
            assert cancel.wait(3)
            release_auth.wait(8 if flags["linger_auth"] == "blocked" else .4)
            auth_done.set()
            return Aria2Error("aria2 authentication cancelled.")
        return authenticate(ticket, cancel)
    Daemon.authenticate = staticmethod(auth)
class RPC(RealAria2RPC):
    def set_overall_speed_limit_kbps(self, value):
        calls["constructed"].append("speed_limit")
        if flags.get("wire_config"): super().set_overall_speed_limit_kbps(value)
    def shutdown(self): pass
app_mod.Aria2RPC = RPC
if flags.get("continuation"):
    from cove import db
    from cove.queue import QueueManager as RealQueue
    init, connect = db.init, db.connect
    db.init, db.connect = lambda: init(root / "queue.sqlite3"), lambda: connect(root / "queue.sqlite3")
    class Queue(RealQueue):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs); self._poll.stop(); self._ext_poll.stop()
        def set_aria2_admission(self, state):
            super().set_aria2_admission(state)
            if state == "open" and flags["continuation"] == "duplicate": controllers[0].complete(controllers[0].generation, "ok")
    app_mod.QueueManager = Queue
    def optional_failure(self, *args):
        calls["constructed"].append("optional:" + flags["continuation"])
        raise RuntimeError("SECRET-CANARY-123")
    if flags["continuation"] == "api": FakeLocalApiServer.start = optional_failure
    if flags["continuation"] == "watchdog": FakeWatchdog.__init__ = optional_failure
    if flags["continuation"] == "watchdog_start":
        class Timer(QTimer):
            def start(self, *args): return optional_failure(self)
        app_mod.QTimer = Timer
    if flags["continuation"] == "links": FakeQMessageBox.warning = staticmethod(lambda *args: optional_failure(None))
controllers = []
emitted = threading.Event()
calls["completions"] = []
def observed_completion(gen, outcome):
    calls["completions"].append([gen, outcome]); emitted.set()
def generation(controller):
    return next((key, value) for key, value in vars(controller).items()
                if key.lstrip("_") in ("generation", "gen") and type(value) is int)
def completion(controller, gen, result, queued=True):
    meta = controller.metaObject()
    kind = QMetaMethod.Signal if queued else QMetaMethod.Slot
    matches = [meta.method(i) for i in range(QObject.staticMetaObject.methodCount(), meta.methodCount())
               if meta.method(i).methodType() == kind and
               bytes(meta.method(i).methodSignature()).endswith(b"(int,PyObject)")]
    assert len(matches) == 1, "contract requires one typed completion signal/slot"
    method = getattr(controller, bytes(matches[0].name()).decode())
    method.emit(gen, result) if queued else method(gen, result)
if hasattr(app_mod, "_StartupController"):
    Original = app_mod._StartupController
    class Controller(Original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs); controllers.append(self)
            meta = self.metaObject()
            for i in range(QObject.staticMetaObject.methodCount(), meta.methodCount()):
                m = meta.method(i)
                if m.methodType() == QMetaMethod.Signal and bytes(m.methodSignature()).endswith(b"(int,PyObject)"):
                    getattr(self, bytes(m.name()).decode()).connect(observed_completion, Qt.DirectConnection)
        def begin(self):
            calls["begin_count"] = calls.get("begin_count", 0) + 1
            return super().begin()
        def shutdown(self):
            calls["constructed"].append("startup_shutdown")
            return super().shutdown()
    app_mod._StartupController = Controller
if flags.get("platform"):
    assert hasattr(app_mod, "_async_startup_supported")
    app_mod._async_startup_supported = lambda: False
    # Do not spoof sys.platform: Qt/path libraries still run on real Linux.
original_set = FakeQueueManager.set_aria2_admission
def set_admission(self, state):
    original_set(self, state)
    if state == "open" and flags.get("duplicate") and not calls.get("duplicate_sent"):
        calls["duplicate_sent"] = True
        controller = controllers[0]
        completion(controller, generation(controller)[1], "ok", queued=False)
FakeQueueManager.set_aria2_admission = set_admission
original_add = FakeMainWindow.add_url_interactive
def add(self, url):
    if flags.get("continuation") == "links": raise RuntimeError("controlled URL refusal")
    original_add(self, url)
    if flags.get("quit_inbox"):
        calls.setdefault("activation_at_quit", calls["activated"])
        loop = QEventLoop(); QTimer.singleShot(0, _RealQApplication.instance().quit); loop.exec()
FakeMainWindow.add_url_interactive = add
original_box = FakeQMessageBox.exec
def box(self):
    def nested():
        if flags.get("reenter"): controllers[0].begin()
        if flags.get("quit_modal") == "box": _RealQApplication.instance().quit()
        loop.quit()
    loop = QEventLoop(); QTimer.singleShot(0, nested); loop.exec()
    original_box(self)
FakeQMessageBox.exec = box
original_critical = FakeQMessageBox.critical
def critical(*args):
    original_critical(*args)
    if flags.get("quit_critical"):
        loop = QEventLoop(); QTimer.singleShot(0, _RealQApplication.instance().quit); loop.exec()
FakeQMessageBox.critical = staticmethod(critical)
original_settings = FakeMainWindow._open_settings
def open_settings(self):
    original_settings(self)
    if flags.get("quit_modal") == "settings":
        loop = QEventLoop(); QTimer.singleShot(0, _RealQApplication.instance().quit); loop.exec()
FakeMainWindow._open_settings = open_settings
if flags.get("interface"):
    begin = Daemon.begin_start
    def begin_start(self):
        if not calls.get("interface_seen"):
            calls["interface_seen"] = True
            raise Aria2InterfaceError("Network interface 'test' was not found.")
        return begin(self)
    Daemon.begin_start = begin_start
class RealExec(_RealQApplication):
    def exec(self):
        start = time.monotonic()
        previous = [start]
        acted = [False]
        timer = QTimer(self); timer.setInterval(10)
        def tick():
            now = time.monotonic(); calls["heartbeat"].append(now - previous[0]); previous[0] = now
            pending = (hostile.body_read.is_set() if flags["hostile"] in ("drip", "close", "http10", "eof") else hostile.entered.is_set()) if hostile else ((root / "negative.entered").exists() and not (root / "negative.done").exists())
            if pending:
                calls["pending_ticks"] += 1
                if not acted[0]:
                    acted[0] = True
                    if flags.get("reenter"): controllers[0].begin()
                    if flags.get("config"):
                        settings.rpc_port = changed_listener.getsockname()[1] if changed_listener else settings.rpc_port + 1
                        settings.rpc_secret = "changed"; settings.proxy_host = "changed"
                    action = flags.get("late")
                    if action:
                        c = controllers[0]; key, gen = generation(c)
                        if action == "generation": setattr(c, key, gen + 1)
                        elif action == "failure": completion(c, gen, "rpc_down", queued=False)
                        elif action == "destroy":
                            c.shutdown()
                            import shiboken6
                            shiboken6.delete(c)
                        else: c.shutdown()
                        if action == "failure": calls["failed_snapshot"] = [c.state, Daemon.instance.is_running(), FakeMainWindow.instance.queue.aria2_admission]
                        if action != "destroy": completion(c, gen, "ok")
                        calls["late_sent"] = True
                    if flags.get("quit_auth"):
                        calls["admission_at_quit"] = list(calls["admission"])
                        self.quit(); return
                    (root / "release").touch()
                    if flags.get("queued_cancel"):
                        assert emitted.wait(2), "worker never queued completion"
                        controllers[0].shutdown()
            if now - start > flags.get("duration", 2.1):
                if flags.get("continuation"):
                    gate = FakeSingleInstanceServer.instance.browser_download_handler.__self__
                    calls["continuation_snapshot"] = [controllers[0].state, gate.queue.aria2_admission, gate.ready, gate.shutting_down, Daemon.instance.is_running()]
                    calls["stops_before_quit"] = len([item for item in calls["lifecycle"] if item[0] == "stop"])
                self.quit()
        timer.timeout.connect(tick); timer.start()
        QTimer.singleShot(9000, self.quit)
        self.aboutToQuit.connect(lambda: calls.update(real_about_to_quit=True))
        status = super().exec(); timer.stop()
        calls["elapsed"] = time.monotonic() - start
        return status
app_mod.QApplication = RealExec
if flags.get("linger_auth"):
    original_diagnostics = app_mod.shutdown_diagnostics
    def diagnostics():
        try:
            calls.update(join_at_cleanup=list(cleanup_joins), auth_done_at_cleanup=auth_done.is_set())
            original_diagnostics()
        finally:
            release_auth.set()
            for thread in workers:
                thread.join(2); assert not thread.is_alive()
    app_mod.shutdown_diagnostics = diagnostics
'''

def app_case(**flags):
    script = _HARNESS
    # Keep real run(), Daemon and exec(); replace the old manual event pump.
    script = script.replace("app_mod.threading.Thread = MagicMock()", "")
    patch = APP_PATCH.replace("__FAKE__", repr(FAKE_ARIA2))
    script = script.replace('sys.argv = ["cove"] + __ARGV__', patch + '\nsys.argv = ["cove"] + __ARGV__')
    substitutions = {"__A7A_FLAGS__": flags, "__ARGV__": flags.get("urls", []),
                     "__IS_PRIMARY__": True, "__SETTINGS_UNREADABLE__": False,
                     "__DAEMON_FAILS__": False, "__EARLY_IPC__": None,
                     "__LATE_IPC__": flags.get("late_ipc"), "__FORWARD_OK__": True,
                     "__BAD_URL__": "", "__CLEANUP_STATUS__": "not_needed"}
    for key, value in substitutions.items(): script = script.replace(key, repr(value))
    # Release owned child even on run() errors; watchdog bounds broken runners.
    script = script.replace("rc = app_mod.run()", """try:
    rc = app_mod.run()
    calls["live_workers_at_return"] = sum(t.is_alive() for t in workers)
    calls["controller_count"] = len(controllers)
finally:
    release_auth.set()
    calls["wire_methods"] = json.loads("[" + ",".join((root / "rpc.calls").read_text().splitlines()) + "]") if (root / "rpc.calls").exists() else []
    calls["launch_port"], calls["sentinel_hit"] = port, False
    if changed_listener:
        try: connection, _ = changed_listener.accept(); connection.close(); calls["sentinel_hit"] = True
        except BlockingIOError: pass
        finally: changed_listener.close()
    if hasattr(Daemon, 'instance'): Daemon.instance.stop()
    if hostile: hostile.close()
    for t in workers:
        t.join(2)
        assert not t.is_alive(), "worker survived cleanup"
assert all(secret not in json.dumps(calls) for secret in launch_secrets)
""")
    result = _isolated_child(script)
    assert result.returncode == 0, result.stdout + result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert "SECRET-CANARY-123" not in result.stdout + result.stderr
    assert out["hook"] == []
    assert out["live_workers_at_return"] == 0
    assert all(row[1] in ("ok", "cancelled", "stale_daemon", "rpc_down", "unexpected") for row in out["completions"])
    assert all(on_main for _, on_main in out["lifecycle"])
    return out

@pytest.mark.parametrize("linger", ["delayed", "blocked"])
def test_real_cleanup_joins_or_records_failed_worker(linger):
    out = app_case(linger_auth=linger, quit_auth=True)
    assert out["real_about_to_quit"] and out["join_at_cleanup"] == [2]
    assert out["auth_done_at_cleanup"] is (linger == "delayed")
    assert out["returncode"] == (70 if linger == "blocked" else 0)
    assert ("aria2_auth_teardown_failed type=Thread" in out["logs"]) is (linger == "blocked")

@pytest.mark.parametrize("fault", ["api", "watchdog", "watchdog_start", "links", "duplicate"])
def test_post_admission_continuation_preserves_ready_engine(fault):
    urls = ["magnet:?xt=urn:btih:" + "a" * 40] if fault == "links" else []
    out = app_case(continuation=fault, urls=urls, duplicate=True)
    assert out["continuation_snapshot"] == ["READY", "open", True, False, True]
    assert out["stops_before_quit"] == 0 and out["boxes"] == []
    logs = [line for line in out["logs"] if line.startswith("startup_continuation_failed")]
    assert len(logs) == int(fault != "duplicate")
    if logs: assert "RuntimeError" in logs[0] and "SECRET" not in logs[0]
    assert out["constructed"].count("optional:" + fault) == int(fault != "duplicate")
    assert out["constructed"].count("speed_limit") == 1 and out["drains"] == 1

@pytest.mark.parametrize("site", ["ipc_batch", "failure_dialog"])
def test_quit_modal_stops_later_batch_and_dialogs(site):
    urls = ["magnet:?xt=urn:btih:" + char * 40 for char in "abc"]
    if site == "ipc_batch":
        out = app_case(late_ipc=urls, quit_inbox=True)
        assert out["add_url"] == urls[:1] and out["activated"] == out["activation_at_quit"]
        assert out["boxes"] == []
    else:
        out = app_case(worker_exception=True, quit_critical=True, urls=urls)
        assert [kind for kind, _ in out["boxes"]] == ["critical"] and out["add_url"] == []
        assert out["activated"] == 0
    assert out["real_about_to_quit"]

def test_post_ready_rpc_uses_captured_port_not_changed_settings():
    out = app_case(config=True, wire_config=True)
    assert ["aria2.changeGlobalOption", out["launch_port"]] in out["wire_methods"]
    assert not out["sentinel_hit"] and out["admission"] == ["open"]

@pytest.mark.parametrize("case", ["exact", "status", "bool", "str", "message", "contradictory", "invalid", "redirect", "empty"])
def test_async_auth_negative_envelope_matrix(launch, case):
    daemon, root = launch
    (root / "mode.json").write_text(json.dumps({"negative_case": case}))
    ticket, gate = daemon.begin_start(), a._AuthCancel()
    until(lambda: (root / "listening").exists())
    with worker(lambda: a.Aria2Daemon.authenticate(ticket, gate)) as (_, result, done):
        try:
            assert done.wait(3)
            assert (result[0] is None) is (case == "exact")
            if result[0] is not None: assert isinstance(result[0], a.Aria2Error) and ticket.secret not in str(result[0])
            assert daemon.active_rpc_secret() is None
        finally: gate.cancel()
    assert len((root / "rpc.calls").read_text().splitlines()) == 2

def test_preconnect_cancel_residual_is_bounded_and_never_ready(monkeypatch, capsys):
    """U1 accepted bounded residual (operator decision 3); not a 2 s guarantee"""
    parked, release = threading.Event(), threading.Event()
    controller = app._StartupController(None, Mock(), Mock(), Mock(), Mock())
    controller.state, controller.generation = "AUTHENTICATING", 1
    with ExitStack() as stack:
        listener = stack.enter_context(socket.socket()); listener.bind(("127.0.0.1", 0)); listener.listen(0)
        for _ in range(6):
            filler = stack.enter_context(socket.socket()); filler.setblocking(False)
            try: filler.connect(listener.getsockname())
            except BlockingIOError: pass
        connect = socket.socket.connect
        def before_connect(sock, address):
            if type(sock).__name__ == "Sock": parked.set(); assert release.wait(3)
            return connect(sock, address)
        monkeypatch.setattr(socket.socket, "connect", before_connect)
        rpc = a.Aria2RPC(Settings(rpc_port=listener.getsockname()[1]), timeout=.25,
                         secret_provider=lambda: CANARY, session_factory=controller.cancel.session)
        def probe():
            try: return rpc.get_version()
            finally: rpc.close()
        with worker(probe) as (_, result, done):
            try:
                assert parked.wait(3)
                start = time.monotonic(); controller.shutdown(); release.set()
                assert done.wait(rpc.timeout + 1)
                elapsed = time.monotonic() - start
                assert elapsed <= rpc.timeout + 1
                assert isinstance(result[0], a.Aria2Error)
                controller.complete(1, "ok")
                controller.ready.assert_not_called(); controller.failed.assert_not_called()
                controller.daemon.commit_ready.assert_not_called()
                with capsys.disabled(): print(f"U1 existing connect timeout={rpc.timeout}, measured={elapsed:.3f}s")
            finally: controller.shutdown(); release.set()

def test_real_run_auth_keeps_heartbeat_alive():
    out = app_case()
    assert out["pending_ticks"] >= 1
    assert max(out["heartbeat"]) < .25, out["heartbeat"]
    assert out["auth_threads"] and not any(out["auth_threads"])
    assert out["admission"] == ["open"]
    assert len(out["completions"]) == 1 and out["completions"][0][1] == "ok"

@pytest.mark.parametrize("control", ["positive", "synchronous-negative"])
def test_heartbeat_controls(control):
    if control == "positive":
        out = app_case(mode={})
        assert max(out["heartbeat"]) < .25 and out["admission"] == ["open"]
    else:
        assert app._async_startup_supported
        out = app_case(platform="control")
        assert max(out["heartbeat"]) >= 1 and out["pending_ticks"] == 0

def test_real_exec_quit_during_auth():
    out = app_case(quit_auth=True, mode={"negative_delay": 5, "ignore_term": True})
    assert out["real_about_to_quit"]
    assert out["pending_ticks"] and out["admission_at_quit"] == []
    assert "open" not in out["admission"] and "watchdog" not in out["constructed"]
    assert out["elapsed"] < 2.5
    assert out["constructed"].index("startup_shutdown") < out["constructed"].index("daemon_stop")

@pytest.mark.parametrize("mode", ["accept_wrong", "secretless", "tcp_only", "positive_only", "positive_fail", "exit_early", "no_rpc"])
def test_real_run_never_ready_on_incomplete_auth(mode):
    out = app_case(mode={mode: True}, duration=6.2)
    assert out["admission"] == ["failed"]
    assert "resume_persisted" not in out["constructed"] and "api_start" not in out["constructed"]
    assert len([x for x in out["boxes"] if x[0] == "critical"]) == 1
    text = next(x[1] for x in out["boxes"] if x[0] == "critical")
    assert ("already using port" if mode in ("accept_wrong", "secretless", "positive_fail") else "RPC did not come up") in text

@pytest.mark.parametrize("late", ["shutdown", "generation", "destroy", "failure"])
def test_controller_discards_late_completion(late):
    assert app._StartupController  # Interface RED, before any socket fixture.
    out = app_case(late=late)
    assert out["late_sent"] and out["admission"] == (["failed"] if late == "failure" else [])
    assert "resume_persisted" not in out["constructed"]
    if late == "failure": assert out["failed_snapshot"] == ["FAILED", False, "failed"]

def test_controller_duplicate_completion_consumed_before_continuation():
    assert app._StartupController
    out = app_case(duplicate=True)
    assert out["admission"] == ["open"]
    for item in ("speed_limit", "resume_persisted", "api_start", "watchdog"):
        assert out["constructed"].count(item) == 1
    order = out["constructed"]
    assert [order.index(x) for x in ("admission:open", "speed_limit", "resume_persisted", "api_start", "watchdog")] == sorted(
        order.index(x) for x in ("admission:open", "speed_limit", "resume_persisted", "api_start", "watchdog"))

@pytest.mark.parametrize("quit_modal", [None, "box", "settings"])
def test_interface_modal_reentry_and_quit(quit_modal):
    assert app._StartupController
    out = app_case(interface=True, reenter=True, quit_modal=quit_modal,
                   interface_choices=["Open Settings"])
    assert out["begin_count"] >= 2
    assert len([x for x in out["lifecycle"] if x[0] == "spawn"]) == (0 if quit_modal else 1)
    assert out["admission"] == ([] if quit_modal else ["open"])

def test_cancel_while_completion_is_queued():
    assert app._StartupController
    out = app_case(queued_cancel=True)
    assert out["pending_ticks"] and out["admission"] == []

@pytest.mark.parametrize("hostile", ["silent", "partial", "drip", "close", "http10", "eof"])
def test_real_exec_quit_with_hostile_responder(hostile):
    assert app._StartupController
    out = app_case(hostile=hostile, quit_auth=True, mode={"sleep_only": True})
    assert out["real_about_to_quit"] and out["admission_at_quit"] == []
    assert out["admission"] == [] and out["elapsed"] < 2.5
    assert out["returncode"] == 0 and "aria2_auth_teardown_failed type=Thread" not in out["logs"]

@pytest.mark.parametrize("failure", ["thread_failure", "worker_exception", "commit_failure"])
def test_worker_boundaries_fail_once_without_secret(failure):
    assert app._StartupController
    out = app_case(**{failure: True}, mode={})
    assert out["admission"] == ["failed"]
    assert len([x for x in out["boxes"] if x[0] == "critical"]) == 1
    assert not any("SECRET" in line for line in out["logs"])
    assert "api_start" not in out["constructed"]

def test_config_changes_while_auth_pending_apply_only_next_launch():
    out = app_case(config=True)
    assert out["pending_ticks"] and out["admission"] == ["open"]

def test_quit_in_inbox_stops_remaining_delivery_and_services():
    urls = ["magnet:?xt=urn:btih:" + char * 40 for char in "abc"]
    out = app_case(quit_inbox=True, urls=urls)
    assert out["add_url"] == urls[:1]
    assert "api_start" not in out["constructed"] and "watchdog" not in out["constructed"]

def test_startup_and_late_ipc_are_delivered_once():
    urls = ["magnet:?xt=urn:btih:" + char * 40 for char in "ab"]
    out = app_case(urls=urls[:1], late_ipc=urls[1])
    assert out["add_url"] == urls and out["drains"] == 1

@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_platform_fallback_creates_no_controller_or_auth_worker(platform, monkeypatch):
    gate = app._async_startup_supported
    monkeypatch.setattr(sys, "platform", platform)
    assert gate() is False
    monkeypatch.undo()
    out = app_case(platform=platform)
    assert all(out["auth_threads"]) and "begin_count" not in out
    assert out["controller_count"] == 0
    assert out["admission"] == ["open"]

@pytest.mark.parametrize("leg", [*LEGS, "retry", "api", "pollers", "interactive", "duplicates"])
def test_pending_real_callers_cannot_send_tokens_or_reap(admission_env, leg, monkeypatch):
    """RED-6/9; existing containment legs are passing controls, not new REDs."""
    queue, rpc = admission_env("pending", SAVED)
    daemon = a.Aria2Daemon(queue.settings)
    proc = Mock(); proc.poll.return_value = None
    daemon._proc = proc
    rpc._secret_provider = daemon.active_rpc_secret
    if leg in LEGS: LEGS[leg](queue)
    elif leg == "retry":
        queue.tasks[1].status = "error"; queue.resume(1)
    elif leg == "api":
        from cove.api_server import QueueApiBridge
        QueueApiBridge(queue).invoke("add", {"url": "https://example.com/api.zip"})
    elif leg == "pollers":
        queue._poll.timeout.emit(); queue._ext_poll.timeout.emit()
    elif leg == "interactive":
        assert queue.resolve_magnet_preflight(MAGNET, on_resolved=lambda _: None).state == "blocked"
    else:
        from cove import main_window as mw
        from PySide6.QtWidgets import QMainWindow
        class Window(mw.MainWindow):
            def __init__(self): QMainWindow.__init__(self); self.queue = queue
            def _confirm_duplicate(self, *args, **kwargs): return True
        host = Window()
        try: assert len(host.add_urls_checked([URL], intake="unknown")) == 1
        finally: host.deleteLater()
    settle(queue)
    assert rpc.activations() == [] and admission_env.http == []
    proc.poll.assert_not_called()  # Behavioral RED on base's external poller.
    queue.set_aria2_admission("failed")
    assert all(t.startup_held for t in queue.tasks.values() if t.status == "error")
    assert all(row[1] in ("queued", "paused") for row in rows(admission_env.path))
