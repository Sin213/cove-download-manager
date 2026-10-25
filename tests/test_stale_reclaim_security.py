"""A7b-4 behavioral security/deadline regressions.

The in-memory wire runs the production HTTP connection factory without opening
sockets. Linux integration tests below use real sockets and are lead-run under
the author sandbox. No permissive identity fixture applies to this module.
"""
import ast
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
import threading
import time
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from cove import app, aria2 as a
from tests.test_app_async_startup import CANARY, Responder, launch, observe_body_read, until, worker
from tests.test_stale_reclaim_async import Drive, assert_closed, pump, stale_record


@pytest.fixture(autouse=True)
def isolated_reclaim_environment(tmp_path, monkeypatch):
    for name in ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        directory = tmp_path / name.lower(); directory.mkdir()
        monkeypatch.setenv(name, str(directory))
    monkeypatch.setattr(a, "DATA_DIR", tmp_path)
    monkeypatch.setattr(a, "ARIA2_SESSION", tmp_path / "aria2.session")
    monkeypatch.setattr(a, "ARIA2_LOG", tmp_path / "aria2.log")


@pytest.fixture
def proc(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    (root / "net").mkdir(parents=True)
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(a, "_PROC_ROOT", root, raising=False)
    monkeypatch.setattr(a, "DATA_DIR", data)
    monkeypatch.setattr(a, "_GATE_ACCEPT_WAIT", .015, raising=False)
    port, ephemeral, inode, pid = 19671, 39821, 715, 1234
    def row(number=inode, uid=None):
        return (f" 0: 0100007F:{port:04X} 0100007F:{ephemeral:04X} 01 "
                f"00000000:00000000 00:00000000 00000000 {os.geteuid() if uid is None else uid} 0 {number}\n")
    def stat(start="998"):
        return "1234 (aria2 name ) tricky) " + " ".join(["S"] + ["0"] * 18 + [start])
    def holder(number=pid, exe="/usr/bin/aria2c", flags=None):
        p = root / str(number)
        (p / "fd").mkdir(parents=True)
        (p / "fd/9").symlink_to(f"socket:[{inode}]")
        (p / "exe").symlink_to(exe)
        (p / "cmdline").write_bytes(b"\0".join(x.encode() for x in (flags or [
            exe, "--enable-rpc", f"--rpc-listen-port={port}", f"--conf-path={data}/gone.conf"])))
        (p / "stat").write_text(stat())
        return p
    tcp = root / "net/tcp"
    tcp.write_text("header\n" + row())
    p = holder()
    return SimpleNamespace(root=root, data=data, tcp=tcp, pid=p, port=port,
                           ephemeral=ephemeral, row=row, holder=holder, stat=stat)


@pytest.fixture
def wire(proc, monkeypatch):
    """Exercise actual _AuthCancel.session -> Connection._new_conn -> write."""
    sockets, sent = [], []
    class Socket:
        def __init__(self, *args):
            self.closed = False
            self.interrupted = threading.Event()
            sockets.append(self)
        def setsockopt(self, *args): pass
        def settimeout(self, value): pass
        def connect(self, target): self.target = target
        def getsockname(self): return ("127.0.0.1", proc.ephemeral)
        def fileno(self): return -1 if self.closed else 91
        def close(self): self.closed = True
        def shutdown(self, *args): self.interrupted.set()
        def sendall(self, data): sent.append(data)
    monkeypatch.setattr(a.socket, "socket", Socket)
    calls = []
    class RPC:
        def __init__(self, settings, secret_provider, session_factory=None):
            self.port, self.secret = settings.rpc_port, secret_provider
            self.session = session_factory()
            self.timeout = 5
        def connect(self):
            pool = self.session.get_adapter("http://").poolmanager.pool_classes_by_scheme["http"]
            conn = pool.ConnectionCls("127.0.0.1", port=self.port, timeout=self.timeout)
            try: return conn._new_conn()
            except a.NewConnectionError as error: raise a.Aria2Error("connect failed") from error
        def transmit(self, method):
            sock = self.connect()
            try:
                sock.sendall((method + " token:" + self.secret()).encode())
                calls.append(method)
            finally: sock.close()
        def shutdown(self): self.transmit("aria2.shutdown")
        def get_version(self):
            self.transmit("aria2.getVersion")
            if self.secret() != CANARY:
                error = a.Aria2RpcError("aria2.getVersion", 1, "Unauthorized")
                error.http_status = 400
                raise error
            return {"version": "test"}
        def close(self): self.session.close()
    monkeypatch.setattr(a, "Aria2RPC", RPC)
    monkeypatch.setattr(a, "_probe_port", lambda port: "busy")
    return SimpleNamespace(sockets=sockets, sent=sent, calls=calls, RPC=RPC)


def request(proc, deadline=float("inf"), gate=None):
    return SimpleNamespace(port=proc.port, secret=CANARY, deadline=deadline, gate=gate)


@pytest.mark.parametrize("fault", ["inode_zero", "multiple_rows", "uid", "holder_uid", "no_holder", "two_holders",
                                  "unreadable_fds", "exe", "rpc_flag", "port_flag", "conf_dir",
                                  "vanished", "starttime", "fd_changed"])
def test_red10_uncertain_peer_receives_no_request(proc, wire, monkeypatch, fault):
    if fault == "inode_zero":
        proc.tcp.write_text(proc.row(0))
        (proc.pid / "fd/9").unlink(); (proc.pid / "fd/9").symlink_to("socket:[0]")
    elif fault == "multiple_rows": proc.tcp.write_text(proc.row() * 2)
    elif fault == "uid": proc.tcp.write_text(proc.row(uid=os.geteuid() + 1))
    elif fault == "holder_uid":
        original_stat = Path.stat
        def stat(path, *args, **kwargs):
            if path == proc.pid: return SimpleNamespace(st_uid=os.geteuid() + 1)
            return original_stat(path, *args, **kwargs)
        monkeypatch.setattr(Path, "stat", stat)
    elif fault == "no_holder": (proc.pid / "fd/9").unlink()
    elif fault == "two_holders": proc.holder(5678)
    elif fault == "exe":
        (proc.pid / "exe").unlink(); (proc.pid / "exe").symlink_to("/usr/bin/python3")
    elif fault in ("rpc_flag", "port_flag", "conf_dir"):
        content = (proc.pid / "cmdline").read_bytes()
        old, new = {"rpc_flag": (b"--enable-rpc", b"--rpc-disabled"),
                    "port_flag": (str(proc.port).encode(), b"9"),
                    "conf_dir": (str(proc.data).encode(), b"/other")}[fault]
        (proc.pid / "cmdline").write_bytes(content.replace(old, new))
    else:
        original = Path.read_text
        reads = []
        def read(path, *args, **kwargs):
            if path == proc.pid / "stat":
                reads.append(1)
                if fault == "vanished": raise FileNotFoundError()
                if fault == "starttime" and len(reads) > 1: return proc.stat("999")
                if fault == "fd_changed" and len(reads) > 1:
                    (proc.pid / "fd/9").unlink()
                    (proc.pid / "fd/9").symlink_to("socket:[999]")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "read_text", read)
        if fault == "unreadable_fds":
            original_iter = Path.iterdir
            def iterate(path):
                if path == proc.pid / "fd": raise PermissionError(13, "controlled unreadable fd directory")
                return original_iter(path)
            monkeypatch.setattr(Path, "iterdir", iterate)
    gate = LazyGate()
    result = a.Aria2Daemon.reclaim_release(request(proc, gate=gate), a._AuthCancel(), timeout=.001)
    assert wire.sent == [], "uncertain kernel peer received a credential/shutdown"
    assert all(sock.closed for sock in wire.sockets)
    assert result == "busy"
    assert gate.denied


@pytest.mark.parametrize("exe", ["/usr/bin/aria2c-real", "/usr/bin/ARIA2-portable (deleted)"])
def test_red10_eligible_alias_and_absent_conf_are_admitted(proc, wire, exe):
    (proc.pid / "exe").unlink(); (proc.pid / "exe").symlink_to(exe)
    alias = proc.data.parent / "alias"
    alias.symlink_to(proc.data, target_is_directory=True)
    (proc.pid / "cmdline").write_bytes(
        f"aria2\0--enable-rpc\0--rpc-listen-port={proc.port}\0--conf-path={alias}/absent.conf\0".encode())
    result = a.Aria2Daemon.reclaim_probe(request(proc), a._AuthCancel())
    assert result == "eligible"
    assert len(wire.sent) == 2


def test_red01_02_unknown_peer_probe_has_zero_token_bytes(proc, wire):
    (proc.pid / "exe").unlink(); (proc.pid / "exe").symlink_to("/usr/bin/python3")
    assert a.Aria2Daemon.reclaim_probe(request(proc), a._AuthCancel()) == "stale"
    assert wire.sent == []


@pytest.mark.parametrize("phase", ["probe", "release"])
def test_red11_expired_request_cannot_open_transport(proc, wire, phase):
    fn = getattr(a.Aria2Daemon, "reclaim_" + phase)
    result = fn(request(proc, time.monotonic() - 1), a._AuthCancel())
    assert result == "deadline"
    assert wire.sent == [] and wire.sockets == []


def test_red13_worker_deadline_interrupts_transport_without_gui(proc, wire, monkeypatch):
    entered = threading.Event()
    def blocked(client):
        sock = client.connect()
        try:
            sock.sendall(b"aria2.shutdown")
            entered.set()
            assert sock.interrupted.wait(.4), "worker deadline did not interrupt the transport"
        finally: sock.close()
    monkeypatch.setattr(wire.RPC, "shutdown", blocked)
    cancel = a._AuthCancel()
    with worker(lambda: a.Aria2Daemon.reclaim_release(request(proc, time.monotonic() + .08), cancel, timeout=.001)) as (thread, results, done):
        assert entered.wait(.3)
        assert done.wait(.6)
        assert results == ["deadline"]
        assert not thread.is_alive()
        assert wire.sockets[-1].interrupted.is_set(), "deadline set a flag without interrupting the socket"
    assert cancel.cancelled
    assert all(sock.closed for sock in wire.sockets)
    assert cancel.sockets == set()


@pytest.mark.parametrize("offset", [-.001, 0, .001])
def test_red16_17_deadline_result_ordering_one_terminal(monkeypatch, offset):
    daemon, ready, failed = Mock(), Mock(), Mock()
    controller = app._StartupController(None, daemon, Mock(return_value=None), ready, failed)
    controller.state, controller.generation, controller.op = "RELEASING", 4, 8
    controller.deadline = 100.0
    controller.request = a._ReclaimRequest(19731, CANARY)
    controller.shown = True
    monkeypatch.setattr(app.time, "monotonic", lambda: 100.0 + offset)
    controller.reclaim(4, 8, "released")
    controller.reclaim(4, 8, "released")
    if offset < 0:
        ready.assert_called_once(); failed.assert_not_called()
    else:
        ready.assert_not_called(); failed.assert_called_once()
        assert str(failed.call_args.args[0]) == (
            "The download engine did not respond during startup. Cove could not safely reclaim the RPC port. Restart Cove and try again.")
        assert controller.state == "FAILED" and controller.cancel.cancelled
        assert not controller.shown
    controller.shutdown()


def test_red12_second_request_preserves_generation_budget(monkeypatch):
    now = [50.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: now[0])
    daemon = Mock()
    controller = app._StartupController(None, daemon, lambda: a._ReclaimRequest(19731, CANARY), Mock(), Mock())
    monkeypatch.setattr(controller, "run", Mock())
    controller.begin()
    first = getattr(controller.request, "deadline", None)
    now[0] += 3
    controller.advance()
    assert first == 62.0 and controller.request.deadline == first
    controller.shutdown()


def test_red27_request_repr_never_discloses_secret():
    assert CANARY not in repr(a._ReclaimRequest(19731, CANARY))


class LazyGate:
    """Delay seam lookup so unchanged production fails on delivered bytes."""
    impl = None
    @property
    def denied(self): return bool(self.impl and self.impl.denied)
    def check(self, *args):
        if self.impl is None: self.impl = a._PeerGate()
        return self.impl.check(*args)


@pytest.mark.parametrize("boundary", ["negative", "shutdown"])
def test_red06_09_provenance_eligible_replacement_does_not_inherit_pin(proc, wire, monkeypatch, boundary):
    gate = LazyGate()
    req, cancel = request(proc, gate=gate), a._AuthCancel()
    def replace():
        (proc.pid / "fd/9").unlink()
        proc.holder(5678)
    original = wire.RPC.get_version
    def version(client):
        result = original(client)
        if boundary == "negative": replace()
        return result
    monkeypatch.setattr(wire.RPC, "get_version", version)
    result = a.Aria2Daemon.reclaim_probe(req, cancel)
    if boundary == "shutdown":
        assert result == "eligible"
        replace()
        before = len(wire.sent)
        assert a.Aria2Daemon.reclaim_release(req, cancel, timeout=.001) == "busy"
        assert len(wire.sent) == before, "replacement inherited shutdown authorization"
    else:
        assert result == "stale"
        assert len(wire.sent) == 1, "reconnected negative probe bypassed identity pin"
    assert gate.denied


def test_red20_p1_authentication_has_no_reclaim_peer_gate(proc, wire):
    (proc.pid / "exe").unlink(); (proc.pid / "exe").symlink_to("/python")
    ticket = SimpleNamespace(port=proc.port, secret=CANARY)
    assert a.Aria2Daemon.authenticate(ticket, a._AuthCancel()) is None
    assert len(wire.sent) == 2


def test_red24_25_gui_timer_expires_once_and_cancels_before_notification(monkeypatch):
    monkeypatch.setattr(app, "_RECLAIM_DEADLINE", .03, raising=False)
    daemon, ready, seen = Mock(), Mock(), []
    controller = app._StartupController(None, daemon, lambda: a._ReclaimRequest(19731, CANARY), ready,
        lambda error: seen.append((str(error), controller.cancel.cancelled, controller.shown)))
    monkeypatch.setattr(controller, "run", Mock())
    controller.begin()
    try:
        def expired():
            QApplication.processEvents()
            return controller.state == "FAILED"
        until(expired, .3)
        assert seen == [("The download engine did not respond during startup. Cove could not safely reclaim the RPC port. Restart Cove and try again.", True, False)]
        controller.reclaim(controller.generation, controller.op, "eligible")
        QApplication.processEvents()
        assert len(seen) == 1
        ready.assert_not_called()
    finally: controller.shutdown()


@pytest.mark.parametrize("action", ["early", "old_generation", "closing", "late_result", "modal"])
def test_red17_18_timer_ordering_and_modal_reentry(monkeypatch, action):
    now = [40.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: now[0])
    failures, ready = [], Mock()
    def failed(error):
        failures.append(str(error))
        if action == "modal":
            controller.expire()
            controller.reclaim(controller.generation, controller.op, "eligible")
    controller = app._StartupController(None, Mock(), lambda: a._ReclaimRequest(19731, CANARY), ready, failed)
    monkeypatch.setattr(controller, "run", Mock())
    controller.begin()
    try:
        if action == "early":
            controller.expire()
            assert controller.state == "RECLAIMING" and controller.deadline_timer.isActive()
            assert failures == []
        elif action == "old_generation":
            controller.generation += 1
            now[0] = 60.0
            controller.expire()
            assert failures == []
        elif action == "closing":
            controller.shutdown(); now[0] = 60.0
            controller.expire()
            assert failures == []
        else:
            now[0] = 60.0
            controller.expire()
            controller.reclaim(controller.generation, controller.op, "free")
            assert len(failures) == 1 and controller.state == "FAILED"
            ready.assert_not_called()
    finally: controller.shutdown()


# A copied interpreter with the approved exe/argv signature is intentionally
# admitted. This explicitly asserts the accepted same-UID impersonation residual.
HOSTILE = r'''
import json, os, socket, sys, threading, time
from pathlib import Path
root = Path(sys.argv[1]); port = int(sys.argv[2]); mode = sys.argv[3]
secret = "SYNTHETIC_A7B4_SECRET_1234567890123456789"
listener = socket.socket(); listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", port)); listener.listen(16); listener.settimeout(.03)
(root / "bound").touch()
stopping = threading.Event(); workers = []
def handle(conn, number):
    try:
        conn.settimeout(3)
        class Recorded:
            def __init__(self): self.buffer = b""
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, count):
                while len(self.buffer) < count:
                    chunk = conn.recv(65536)
                    if not chunk: break
                    with (root / "received").open("ab") as record: record.write(chunk)
                    self.buffer += chunk
                value, self.buffer = self.buffer[:count], self.buffer[count:]
                return value
            def readline(self):
                while b"\n" not in self.buffer:
                    chunk = conn.recv(65536)
                    if not chunk: break
                    with (root / "received").open("ab") as record: record.write(chunk)
                    self.buffer += chunk
                end = self.buffer.find(b"\n") + 1 if b"\n" in self.buffer else len(self.buffer)
                value, self.buffer = self.buffer[:end], self.buffer[end:]
                return value
        stream = Recorded()
        with stream:
            while not stopping.is_set():
                try: first = stream.readline()
                except OSError: return
                if not first: return
                headers = {}
                while (line := stream.readline()) not in (b"\r\n", b""):
                    key, value = line.split(b":", 1); headers[key.lower()] = value.strip()
                data = json.loads(stream.read(int(headers[b"content-length"])))
                method, token = data["method"], data["params"][0]
                with (root / "calls").open("a") as record: record.write(json.dumps([method, token]) + "\n")
                positive = token == "token:" + secret
                phase = "shutdown" if method == "aria2.shutdown" else "positive" if positive else "negative"
                (root / (phase + ".entered")).touch()
                target = mode.split(":", 1)[-1] if ":" in mode else "positive"
                framing = mode.split(":", 1)[0]
                if phase == target and framing in ("body", "header", "eof", "http10", "close", "silent"):
                    head = {"body": b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n",
                            "header": b"HTTP/1.1 200 OK\r\nX-Pad: ",
                            "eof": b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n",
                            "http10": b"HTTP/1.0 200 OK\r\n\r\n",
                            "close": b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 100000\r\n\r\n",
                            "silent": b""}[framing]
                    conn.sendall(head); (root / "hostile.entered").touch()
                    while not stopping.wait(.02):
                        if framing != "silent": conn.sendall(b"x")
                    return
                if framing == "slow" and phase == "positive":
                    threading.Event().wait(4.6)
                if framing == "barrier" and phase == "positive":
                    (root / "barrier.entered").touch()
                    while not stopping.wait(.005) and not (root / "response.release").exists(): pass
                    if stopping.is_set(): return
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 30\r\nConnection: close\r\n\r\n"
                                 b'{"result":{"version":"test"}} ')
                    (root / "response.released").touch()
                    return
                rejected = not positive and framing != "secretless"
                payload = {"error":{"code":1,"message":"Unauthorized"}} if rejected else {
                    "result": "OK" if phase == "shutdown" else {"version":"test"}}
                body = json.dumps(payload).encode()
                close = rejected or framing in ("disconnect", "silent_disconnect")
                conn.sendall(b"HTTP/1.1 " + (b"400 Bad Request" if rejected else b"200 OK") +
                             b"\r\nContent-Length: " + str(len(body)).encode() +
                             (b"\r\nConnection: close" if close and (rejected or framing != "silent_disconnect") else b"") + b"\r\n\r\n" + body)
                if phase == "shutdown": stopping.set(); return
                if close: return
    except (OSError, ValueError, KeyError): pass
    finally:
        conn.close(); (root / ("closed-" + str(number))).touch()
while not stopping.is_set() and not (root / "stop").exists():
    if (root / "unbind").exists():
        listener.close(); (root / "unbound").touch()
        break  # Keep the admitted accepted connection alive until response.release.
    try: conn, _ = listener.accept()
    except socket.timeout: continue
    thread = threading.Thread(target=handle, args=(conn, len(workers))); workers.append(thread); thread.start()
if not (root / "unbound").exists(): stopping.set()
listener.close()
for thread in workers: thread.join(6)
'''


@pytest.fixture
def native_listeners(tmp_path, monkeypatch):
    monkeypatch.setattr(a, "DATA_DIR", tmp_path)
    made = []
    class Native:
        secret = "SYNTHETIC_A7B4_SECRET_1234567890123456789"
        def __init__(self, mode="normal", port=None):
            self.root = tmp_path / str(len(made)); self.root.mkdir()
            self.exe = self.root / "aria2c"
            shutil.copyfile(Path(sys.executable).resolve(), self.exe); self.exe.chmod(0o700)
            self.script = self.root / "listener.py"; self.script.write_text(HOSTILE)
            if port is None:
                with socket.socket() as s: s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
            self.port = port
            self.proc = subprocess.Popen([str(self.exe), str(self.script), str(self.root), str(port), mode,
                "--enable-rpc", f"--rpc-listen-port={port}", f"--conf-path={tmp_path}/absent.conf"],
                env={**os.environ, "PYTHONHOME": "/usr"}, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            self.start = self.identity()
            made.append(self)
            until(lambda: (self.root / "bound").exists() or self.proc.poll() is not None)
            assert self.proc.poll() is None, self.proc.stderr.read().decode()
        def identity(self):
            return Path(f"/proc/{self.proc.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        @property
        def calls(self):
            p = self.root / "calls"
            return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []
        def close(self):
            (self.root / "stop").touch()
            if self.proc.poll() is None:
                assert self.identity() == self.start
                self.proc.terminate()
            try: self.proc.wait(2)
            except subprocess.TimeoutExpired:
                assert self.identity() == self.start
                self.proc.kill(); self.proc.wait(2)
            self.proc.stderr.close()
    yield Native
    for item in reversed(made): item.close()


def native_request(server, seconds=2):
    return a._ReclaimRequest(server.port, server.secret, time.monotonic() + seconds, a._PeerGate())


@pytest.mark.parametrize("mode", ["body", "header", "eof", "http10", "close", "silent", "body:negative", "body:shutdown"])
def test_red11_13_gate_admitted_hostile_response_is_finite(native_listeners, mode, monkeypatch):
    server = native_listeners(mode)
    server.body_read = threading.Event()
    if mode not in ("header", "silent"):
        observe_body_read(server, monkeypatch.setattr)
    else:
        import http.client
        original = http.client.HTTPResponse._read_status
        def read_status(response):
            server.body_read.set()
            return original(response)
        monkeypatch.setattr(http.client.HTTPResponse, "_read_status", read_status)
    cancel = a._AuthCancel(); req = native_request(server, .35)
    def reclaim():
        probe = a.Aria2Daemon.reclaim_probe(req, cancel)
        return a.Aria2Daemon.reclaim_release(req, cancel) if probe == "eligible" else probe
    start = time.monotonic()
    with worker(reclaim) as (thread, results, done):
        try:
            until(lambda: (server.root / "hostile.entered").exists(), .3)
            assert server.body_read.wait(.2), "target transport read was not entered"
            assert req.gate.pin == (server.proc.pid, server.start), "hostile fixture was not admitted"
            assert done.wait(1)
            assert results == ["deadline"] and not thread.is_alive()
            assert cancel.sockets == set() and cancel.cancelled
        finally: cancel.cancel()
    assert time.monotonic() - start <= 1.35
    assert cancel.sockets == set() and cancel.cancelled
    assert not req.gate.denied


def test_red11_real_twelve_second_budget(native_listeners, monkeypatch):
    server = native_listeners("body")
    server.body_read = threading.Event()
    observe_body_read(server, monkeypatch.setattr)
    cancel = a._AuthCancel(); req = native_request(server, 12.0)
    start = time.monotonic()
    with worker(lambda: a.Aria2Daemon.reclaim_probe(req, cancel)) as (thread, results, done):
        try:
            until(lambda: (server.root / "hostile.entered").exists())
            assert server.body_read.wait(.5)
            assert req.gate.pin is not None
            assert done.wait(13)
            assert results == ["deadline"] and not thread.is_alive()
            assert cancel.cancelled and not cancel.sockets
        finally: cancel.cancel()
    assert 11.8 <= time.monotonic() - start <= 13.0
    assert not cancel.sockets


@pytest.mark.parametrize("mode", ["normal", "disconnect", "silent_disconnect"])
def test_red03_same_uid_forgery_is_asserted_residual(native_listeners, mode):
    server = native_listeners(mode)
    req, cancel = native_request(server), a._AuthCancel()
    assert a.Aria2Daemon.reclaim_probe(req, cancel) == "eligible"
    assert req.gate.pin == (server.proc.pid, server.start)
    assert a.Aria2Daemon.reclaim_release(req, cancel) == "released"
    assert [method for method, token in server.calls].count("aria2.shutdown") == 1


@pytest.mark.parametrize("mode", ["normal", "disconnect", "silent_disconnect", "barrier"])
def test_red01_06_09_two_eligible_processes_cannot_share_authorization(native_listeners, mode, monkeypatch):
    first = native_listeners(mode)
    req, cancel = native_request(first, 4), a._AuthCancel()
    if mode == "barrier":
        with worker(lambda: a.Aria2Daemon.reclaim_probe(req, cancel)) as (_, results, done):
            try:
                until(lambda: (first.root / "barrier.entered").exists())
                assert req.gate.pin == (first.proc.pid, first.start)
                assert not (first.root / "response.released").exists()
                (first.root / "unbind").touch()
                until(lambda: (first.root / "unbound").exists())
                second = native_listeners(port=first.port)
                assert (second.root / "bound").exists() and first.proc.poll() is None
                assert not (first.root / "response.released").exists()
                (first.root / "response.release").touch()
                until(lambda: (first.root / "response.released").exists())
                assert done.wait(2) and results == ["stale"]
            finally:
                (first.root / "response.release").touch(); cancel.cancel()
    else:
        assert a.Aria2Daemon.reclaim_probe(req, cancel) == "eligible"
        first.close(); second = native_listeners(port=first.port)
        assert a.Aria2Daemon.reclaim_release(req, cancel) == "busy"
    until(lambda: (second.root / "closed-0").exists())
    assert second.calls == [] and not (second.root / "received").exists(), "eligible replacement received request bytes"
    assert req.gate.denied


def test_red02_real_unknown_listener_gets_zero_request_bytes(monkeypatch):
    server = Responder("silent")
    received, closed, original = [], [], server.handle
    def handle(conn):
        class Recorded:
            def __getattr__(self, name): return getattr(conn, name)
            def recv(self, count):
                chunk = conn.recv(count); received.append(chunk); return chunk
        try: original(Recorded())
        finally: closed.append(conn)
    server.handle = handle
    try:
        req = a._ReclaimRequest(server.port, CANARY, time.monotonic() + 2, a._PeerGate())
        assert a.Aria2Daemon.reclaim_probe(req, a._AuthCancel()) == "stale"
        until(lambda: len(server.connections) >= 3 and len(closed) == len(server.connections))
        assert server.tokens == [] and not server.entered.is_set()
        assert b"".join(received) == b""
        assert req.gate.denied
    finally: server.close()


@pytest.mark.parametrize("mode", ["foreign", "secretless"])
def test_red04_05_eligible_unauthorized_listener_is_untouched(native_listeners, mode):
    server = native_listeners("secretless" if mode == "secretless" else "normal")
    req = native_request(server)
    if mode == "foreign": req = req._replace(secret=CANARY)
    assert a.Aria2Daemon.reclaim_probe(req, a._AuthCancel()) == "stale"
    assert server.proc.poll() is None
    assert all(method != "aria2.shutdown" for method, token in server.calls)


def test_red15_slow_authorized_response_succeeds_within_budget(native_listeners):
    server = native_listeners("slow")
    req, cancel = native_request(server, 12), a._AuthCancel()
    start = time.monotonic()
    assert a.Aria2Daemon.reclaim_probe(req, cancel) == "eligible"
    assert 4.5 <= time.monotonic() - start < 12
    assert a.Aria2Daemon.reclaim_release(req, cancel) == "released"


@pytest.mark.parametrize("quit_early", [False, True])
def test_red14_21_23_24_gui_hostile_lifetime_queue_and_close(native_listeners, monkeypatch, quit_early):
    server = native_listeners("body")
    monkeypatch.setattr(app, "_RECLAIM_DEADLINE", .4)
    daemon, ready, failures = Mock(), Mock(), []
    admission = app.BrowserDownloadGate()
    admission.queue = Mock()
    ready.side_effect = lambda: setattr(admission, "ready", True)
    controller = app._StartupController(None, daemon,
        lambda: a._ReclaimRequest(server.port, server.secret, gate=a._PeerGate()), ready, failures.append)
    beats = []
    heartbeat = QTimer(); heartbeat.setInterval(10)
    heartbeat.timeout.connect(lambda: beats.append(time.monotonic())); heartbeat.start()
    controller.begin()
    try:
        pump(lambda: (server.root / "hostile.entered").exists(), .3)
        assert controller.request.gate.pin is not None and controller.shown
        assert not admission.accept({"url": "https://example.com/pending"})
        if quit_early:
            start = time.monotonic(); controller.shutdown()
            assert controller.join(2) and time.monotonic() - start <= 2
            QApplication.processEvents()
            assert failures == [] and controller.state == "STOPPED"
        else:
            pump(lambda: controller.state == "FAILED", 1.4)
            assert controller.join(2)
            assert len(failures) == 1
            assert str(failures[0]) == app.RECLAIM_DEADLINE_TEXT
            assert len(beats) >= 5 and max(b - a for a, b in zip(beats, beats[1:])) < .25
            assert controller.state == "FAILED"
        ready.assert_not_called()
        assert not admission.accept({"url": "https://example.com/terminal"})
        admission.queue.add_url.assert_not_called()
        assert not controller.shown and not controller.cancel.sockets
    finally:
        heartbeat.stop(); controller.shutdown(); assert controller.join(2)


def test_red03_20_21_22_28_real_aria2_reclaim_spawn_auth_and_download(launch, monkeypatch):
    """Real automatic reclaim, GUI-owned spawn, P1 and deterministic local I/O."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    daemon, root = launch
    executable = shutil.which("aria2c")
    assert executable, "lead integration environment requires aria2c"
    monkeypatch.setattr(a, "_resolve_aria2c", lambda: executable)
    monkeypatch.setattr(a, "ARIA2_SESSION", root / "aria2.session")
    conf = root / "stale.conf"
    conf.write_text(f"rpc-secret={CANARY}\n")
    old = subprocess.Popen([executable, f"--conf-path={conf}", "--enable-rpc", "--rpc-listen-all=false",
        f"--rpc-listen-port={daemon.settings.rpc_port}", f"--dir={root}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    starttime = Path(f"/proc/{old.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    drive = Drive(daemon, monkeypatch)
    class Stale:
        port = daemon.settings.rpc_port
    stale_record(drive, root, Stale())
    shutdowns = []
    original_shutdown = a.Aria2RPC.shutdown
    def shutdown(client):
        shutdowns.append(threading.get_ident())
        return original_shutdown(client)
    monkeypatch.setattr(a.Aria2RPC, "shutdown", shutdown)
    payload = b"Cove isolated A7b4 integration\n" * 8192
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            self.send_response(200); self.send_header("Content-Length", str(len(payload))); self.end_headers()
            self.wfile.write(payload)
    http = HTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=http.serve_forever, kwargs={"poll_interval": .02})
    serving.start()
    client = None
    try:
        until(lambda: a._probe_port(daemon.settings.rpc_port) == "busy")
        drive.controller.begin()
        assert_closed(drive)
        pump(lambda: drive.controller.state in ("READY", "FAILED"), 12)
        assert drive.controller.state == "READY" and drive.ready == [True]
        assert len(drive.spawns) == 1 and len(shutdowns) == 1
        assert shutdowns[0] != drive.gui
        assert daemon._proc.pid != old.pid and old.wait(2) == 0
        assert daemon.active_rpc_secret() != CANARY
        drive.check_ready(True)
        client = a.Aria2RPC(daemon.settings, secret_provider=daemon.active_rpc_secret)
        gid = client.add_uri([f"http://127.0.0.1:{http.server_port}/payload"], str(root), 1, filename="payload.bin")
        until(lambda: client.tell_status(gid)["status"] == "complete", 5)
        assert hashlib.sha256((root / "payload.bin").read_bytes()).digest() == hashlib.sha256(payload).digest()
    finally:
        if client: client.close()
        drive.close()
        http.shutdown(); serving.join(2); http.server_close()
        assert not serving.is_alive()
        if old.poll() is None:
            assert Path(f"/proc/{old.pid}/stat").read_text().rsplit(")", 1)[1].split()[19] == starttime
            old.terminate(); old.wait(2)


def test_red17_indicator_callback_reentry_cannot_duplicate_deadline(monkeypatch):
    now = [20.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: now[0])
    failed = Mock()
    controller = app._StartupController(None, Mock(), lambda: a._ReclaimRequest(19731, CANARY), Mock(), failed)
    monkeypatch.setattr(controller, "run", Mock())
    reentered = []
    def pending(on):
        if not on and not reentered:
            reentered.append(True)
            controller.expire()
    controller.pending.connect(pending)
    controller.begin(); now[0] = 40.0
    try:
        controller.expire()
        failed.assert_called_once()
        assert controller.state == "FAILED" and controller.cancel.cancelled
    finally: controller.shutdown()


def test_red24_close_clears_controller_pending_indicator(monkeypatch):
    controller = app._StartupController(None, Mock(), lambda: a._ReclaimRequest(19731, CANARY), Mock(), Mock())
    monkeypatch.setattr(controller, "run", Mock())
    controller.begin()
    controller.shutdown()
    assert not controller.shown


def test_red12_negative_probe_uses_remaining_total_budget(proc, wire, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(a.time, "monotonic", lambda: now[0])
    # No real wall clock is involved in this exact deadline-boundary test.
    monkeypatch.setattr(a.threading, "Timer", Mock())
    timeouts = []
    original = wire.RPC.get_version
    def version(client):
        timeouts.append(client.timeout)
        result = original(client)
        now[0] = 100.15
        return result
    monkeypatch.setattr(wire.RPC, "get_version", version)
    assert a.Aria2Daemon.reclaim_probe(request(proc, 100.2), a._AuthCancel()) == "eligible"
    assert timeouts[0] == pytest.approx(.2)
    assert timeouts[1] == pytest.approx(.05)


@pytest.mark.parametrize("phase", ["probe", "release"])
def test_red14_cancelled_worker_cannot_send_new_request(proc, wire, phase):
    cancel = a._AuthCancel(); cancel.cancel()
    result = getattr(a.Aria2Daemon, "reclaim_" + phase)(request(proc), cancel)
    assert result == "cancelled" and wire.sent == [] and wire.sockets == []


def test_red13_worker_owns_and_joins_non_daemon_timer(proc, wire, monkeypatch):
    timers, original = [], threading.Timer
    def timer(*args, **kwargs):
        value = original(*args, **kwargs); timers.append(value); return value
    monkeypatch.setattr(a.threading, "Timer", timer)
    assert a.Aria2Daemon.reclaim_probe(request(proc, time.monotonic() + 1), a._AuthCancel()) == "eligible"
    assert len(timers) == 1 and not timers[0].daemon and not timers[0].is_alive()


def test_red26_gate_is_fail_closed_off_linux(proc, wire, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert a.Aria2Daemon.reclaim_probe(request(proc), a._AuthCancel()) == "stale"
    assert wire.sent == []


def test_red24_reclaim_failure_preparation_disarms_timer(monkeypatch):
    controller = app._StartupController(None, Mock(), Mock(side_effect=[a._ReclaimRequest(19731, CANARY), False]), Mock(), Mock())
    monkeypatch.setattr(controller, "run", Mock())
    controller.begin()
    try:
        controller.reclaim(controller.generation, controller.op, "stale")
        assert controller.state == "FAILED"
        assert not controller.deadline_timer.isActive()
    finally: controller.shutdown()


def test_red10_unexpected_peer_evidence_failure_closes_before_write(proc, wire, monkeypatch):
    original = Path.read_text
    def read(path, *args, **kwargs):
        if path == proc.tcp: raise RuntimeError(CANARY)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    gate = LazyGate()
    assert a.Aria2Daemon.reclaim_release(request(proc, gate=gate), a._AuthCancel(), timeout=.001) == "busy"
    assert wire.sent == [] and all(sock.closed for sock in wire.sockets)
    assert gate.denied


def test_v3_release_keeps_original_generation_deadline(proc, wire, monkeypatch):
    real_clock, now = time.monotonic, [100.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(app, "_RECLAIM_DEADLINE", 1.0)
    probe_entered, probe_continue, release_entered = (threading.Event() for _ in range(3))
    carried, polls, notifications = [], [], []
    def probe(req, cancel):
        probe_entered.set()
        assert probe_continue.wait(1)
        return "eligible"
    release = a.Aria2Daemon.reclaim_release
    def observed_release(req, cancel):
        carried.append(req.deadline)
        return release(req, cancel)
    def shutdown(client):
        sock = client.connect()
        try:
            sock.sendall(b"aria2.shutdown")
            release_entered.set()
            assert sock.interrupted.wait(1), "release transport was not interrupted"
        finally: sock.close()
    monkeypatch.setattr(a.Aria2Daemon, "reclaim_probe", staticmethod(probe))
    monkeypatch.setattr(a.Aria2Daemon, "reclaim_release", staticmethod(observed_release))
    monkeypatch.setattr(wire.RPC, "shutdown", shutdown)
    monkeypatch.setattr(a, "_probe_port", lambda port: polls.append(now[0]) or "busy")
    ready = Mock()
    controller = app._StartupController(None, Mock(),
        lambda: a._ReclaimRequest(proc.port, CANARY, gate=a._PeerGate()), ready, notifications.append)
    generation_start = now[0]
    controller.begin()
    original_deadline = controller.deadline
    try:
        assert probe_entered.wait(1)
        assert original_deadline == generation_start + 1.0
        now[0] = generation_start + .8
        probe_continue.set()
        bounded = real_clock() + 1
        while not release_entered.is_set() and real_clock() < bounded:
            QApplication.processEvents()
            release_entered.wait(.002)
        assert release_entered.is_set(), "release phase did not start"
        now[0] = original_deadline
        controller.expire()
        assert controller.state == "FAILED", "release reset the original generation deadline"
        assert carried == [original_deadline]
        assert controller.cancel.cancelled and controller.join(1)
        assert wire.sockets[-1].interrupted.is_set() and not controller.cancel.sockets
        QApplication.processEvents()
        controller.expire()
        assert len(notifications) == 1 and str(notifications[0]) == app.RECLAIM_DEADLINE_TEXT
        assert polls == [] and len(wire.sent) == 1
        ready.assert_not_called()
    finally:
        probe_continue.set(); controller.shutdown(); assert controller.join(2)


@pytest.mark.parametrize("phase", ["probe", "release"])
@pytest.mark.parametrize("cause", ["deadline", "cancel", "both"])
def test_v4_terminal_phase_has_no_trailing_connect(proc, wire, monkeypatch, phase, cause):
    now, polls, cancel = [100.0], [], a._AuthCancel()
    monkeypatch.setattr(a.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(a.threading, "Timer", Mock())  # Explicit checks must work without timer delivery.
    monkeypatch.setattr(cancel, "wait", lambda timeout: cancel.cancelled)
    def poll(port):
        polls.append(now[0])
        if cause in ("deadline", "both"): now[0] = 101.0
        if cause in ("cancel", "both"): cancel.cancel()
        return "busy"
    monkeypatch.setattr(a, "_probe_port", poll)
    req = request(proc, 101.0)
    if phase == "probe": req.secret = None
    result = getattr(a.Aria2Daemon, "reclaim_" + phase)(req, cancel)
    assert result == ("cancelled" if cause == "cancel" else "deadline")
    assert polls == [100.0], "terminal reclaim started another port connection"
    assert len(wire.sent) == (1 if phase == "release" else 0)


def test_v5_positive_response_waits_for_rebind_barrier(tmp_path):
    """Run the shipped child handler without sockets to verify its response barrier."""
    parsed = ast.parse(HOSTILE)
    handler = next(n for n in parsed.body if isinstance(n, ast.FunctionDef) and n.name == "handle")
    secret = "SYNTHETIC_A7B4_SECRET_1234567890123456789"
    body = json.dumps({"method": "aria2.getVersion", "params": ["token:" + secret]}).encode()
    class Connection:
        def __init__(self):
            self.incoming = b"POST /jsonrpc HTTP/1.1\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
            self.sent, self.closed = [], False
        def settimeout(self, timeout): pass
        def recv(self, count):
            data, self.incoming = self.incoming[:count], self.incoming[count:]
            return data
        def sendall(self, data): self.sent.append(data)
        def close(self): self.closed = True
    conn = Connection()
    namespace = dict(root=tmp_path, mode="barrier", secret=secret, json=json, threading=threading,
                     stopping=threading.Event())
    exec(compile(ast.Module(body=[handler], type_ignores=[]), "HOSTILE.handle", "exec"), namespace)
    with worker(lambda: namespace["handle"](conn, 0)) as (_, _, done):
        try:
            until(lambda: (tmp_path / "barrier.entered").exists())
            assert conn.sent == [], "positive bytes were released before the replacement bound"
            assert not (tmp_path / "response.released").exists()
            (tmp_path / "L2.bound").touch()
            (tmp_path / "response.release").touch()
            assert done.wait(1)
            assert (tmp_path / "response.released").exists()
            assert b"Connection: close" in b"".join(conn.sent) and conn.closed
        finally:
            (tmp_path / "continue").touch()  # Also bounds teardown against the old handler.
            (tmp_path / "response.release").touch()


def test_v6_visible_census_admits_with_hidden_coholders(proc, wire, monkeypatch):
    """D-01/D-02/D-06 accepted residual: visible admission cannot exclude hidden co-holders."""
    unreadable, other_uid = proc.holder(5678), proc.holder(9012)
    original_stat, original_iterdir = Path.stat, Path.iterdir
    visited = []
    def stat(path, *args, **kwargs):
        if path == other_uid:
            visited.append("other_uid")
            return SimpleNamespace(st_uid=os.geteuid() + 1)
        return original_stat(path, *args, **kwargs)
    def iterdir(path):
        if path == unreadable / "fd":
            visited.append("unreadable")
            raise PermissionError(13, "controlled hidden socket co-holder")
        return original_iterdir(path)
    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(Path, "iterdir", iterdir)
    gate = a._PeerGate()
    assert a.Aria2Daemon.reclaim_probe(request(proc, gate=gate), a._AuthCancel()) == "eligible"
    assert set(visited) == {"other_uid", "unreadable"}
    assert gate.pin == (int(proc.pid.name), "998") and not gate.denied
    assert len(wire.sent) == 2


def test_v2_deadline_guidance_is_exact_and_sanitized():
    text = app.RECLAIM_DEADLINE_TEXT
    assert text == ("The download engine did not respond during startup. Cove could not safely reclaim the RPC port. "
                    "Restart Cove and try again.")
    assert CANARY not in text and "token" not in text.lower() and not any(c.isdigit() for c in text)
    assert "aria2" not in text.lower() and "retry" not in text.lower()
