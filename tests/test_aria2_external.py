"""Tests for the concurrency cap and external-download discovery."""
import base64
import json
import os
import re
import socket
import stat
import sys
import threading
import traceback
from pathlib import Path
from collections import namedtuple
from types import SimpleNamespace
from urllib.parse import quote, urlsplit
from unittest.mock import MagicMock, patch

import pytest
import requests

from cove import aria2, config

from cove.aria2 import (
    Aria2Daemon,
    Aria2Error,
    Aria2InterfaceError,
    Aria2RPC,
    MAX_CONCURRENT_DOWNLOADS,
    bittorrent_enabled,
)
from cove.config import MAX_CONNECTIONS_PER_SERVER, Settings


class ScriptedRPC:
    """Requests-layer aria2 model; Unauthorized latency is simulated, never slept."""
    def __init__(self, monkeypatch, data):
        self.data, self.calls, self.spawns, self.children = data, [], [], []
        self.accepted, self.negative, self.positive = None, "auth", {"version": "1.37"}
        self.failure, self.on_spawn, self.after_positive, self.after_negative = None, None, None, None
        self.busy, self.release = False, True
        monkeypatch.setattr(requests.Session, "post", lambda session, url, **kw: self.post(session, url, **kw))

    def response(self, status, body):
        response = requests.Response()
        if isinstance(body, dict): body = {"jsonrpc": "2.0", "id": "scripted", **body}
        response.status_code, response._content = status, json.dumps(body).encode()
        return response

    def post(self, session, url, *, json, timeout, allow_redirects):
        assert session.trust_env is False and allow_redirects is False, "OP-17 probe transport policy"
        method, params = json["method"], json["params"]; self.calls.append((method, params, timeout))
        if self.failure: raise self.failure
        accepted = self.accepted
        if accepted is None:
            if self.spawns:
                accepted = {self.spawns[-1][1].splitlines()[0].removeprefix("rpc-secret=")}
            else:
                paths = list(self.data.glob(".aria2.launch.*.conf")) or [self.data / "aria2.rpc.conf"]
                accepted = {p.read_text().splitlines()[0].removeprefix("rpc-secret=") for p in paths if p.is_file()}
        tokens = [c["params"][0] for c in params[0]] if method == "system.multicall" else params[:1]
        authorized = accepted == "OPEN" or all(t.removeprefix("token:") in accepted for t in tokens)
        if not authorized:
            if timeout < 1.99 or self.negative == "timeout":
                raise requests.exceptions.ReadTimeout("simulated Unauthorized delay")
            if self.negative == "non-json":
                response = self.response(400, None); response._content = b"garbage"; return response
            error = {"code": 2 if self.negative == "code" else 1,
                     "message": "Unauthorized?" if self.negative == "message" else "Unauthorized"}
            if method == "system.multicall":
                return self.response(200, {"result": [error] * 3})
            if self.after_negative: self.after_negative()
            return self.response(200 if self.negative == "http200" else 400, {"error": error})
        if method == "aria2.shutdown" and self.release: self.busy = False
        if method == "aria2.getVersion" and self.after_positive: self.after_positive()
        result = self.positive if method == "aria2.getVersion" else ([[[]]] * 3 if method == "system.multicall" else [])
        return self.response(200, {"result": result})

    def spawn(self, args, **kwargs):
        path = Path(next(a.split("=", 1)[1] for a in args if a.startswith("--conf-path=")))
        self.spawns.append((path, path.read_text(), args, kwargs))
        child = SimpleNamespace(dead=False); child.poll = lambda: 1 if child.dead else None
        child.send_signal = lambda signal: setattr(child, "dead", True)
        child.terminate = lambda: child.send_signal(aria2.signal.SIGTERM)
        child.wait = child.kill = lambda **kw: setattr(child, "dead", True)
        self.children.append(child)
        if self.on_spawn: self.on_spawn()
        return child

    def shutdowns(self):
        return sum(method == "aria2.shutdown" for method, _, _ in self.calls)

@pytest.fixture
def startup_wire(monkeypatch):
    return ScriptedRPC(monkeypatch, aria2.DATA_DIR)

@pytest.mark.parametrize("case", ["enforcing", "open", "wrong", "short-timeout", "multicall", "in-band-fault"])
def test_p1_scripted_transport_GUARD(startup_wire, case):
    w = startup_wire; secret = "MODEL_CONTROL_SECRET_0123456789"; w.accepted = "OPEN" if case == "open" else {secret}
    rpc = Aria2RPC(Settings(rpc_secret=secret), timeout=1 if case == "short-timeout" else 5)
    if case in ("wrong", "short-timeout", "in-band-fault"): rpc.secret = "WRONG_SECRET_0123456789"
    call = rpc.tell_all_downloads if case in ("multicall", "in-band-fault") else rpc.get_version
    if case in ("wrong", "short-timeout", "in-band-fault"):
        with pytest.raises(Aria2Error, match="transport error" if case == "short-timeout" else "Unauthorized"): call()
    else: assert call() == ([] if case == "multicall" else {"version": "1.37"})
    assert len(w.calls) == 1

@pytest.fixture
def p1(startup_wire, monkeypatch):
    w = startup_wire; w.daemon = Aria2Daemon(Settings(rpc_port=16801, rpc_secret="P1_PERSISTED_SECRET_0123456789"))
    monkeypatch.setattr(aria2, "_resolve_aria2c", lambda: "synthetic-aria2c")
    monkeypatch.setattr(aria2, "_OwnedPopen", w.spawn)
    monkeypatch.setattr(Aria2Daemon, "_port_in_use", lambda self: w.busy)
    monkeypatch.setattr(Aria2Daemon, "_wait_for_port_release", lambda self: None)
    w.ticks = ticks = iter(i / 8 for i in range(10000))
    monkeypatch.setattr(aria2, "time", SimpleNamespace(time=lambda: next(ticks), monotonic=lambda: next(ticks), sleep=lambda n: None))
    yield w
    for child in w.children: child.dead = True
    try: w.daemon.stop()
    except OSError: pass  # Deliberate unlink/termination injection is asserted in the test.

def p1_refusal(w, reason="stale"):
    error, owned_from = None, len(w.children)
    try: w.daemon.start()
    except Aria2Error as exc: error = exc
    assert w.shutdowns() == 0, "unproved responder was shut down"
    assert error is not None, "unproved responder became ready"
    expected = str(w.daemon._stale_daemon_error()) if reason == "stale" else "aria2 RPC did not come up."
    assert str(error) == expected
    assert not any(not child.dead for child in w.children[owned_from:]), "failed start leaked its child"
    assert not list(w.data.glob(".aria2.launch.*.conf")), "failed start leaked launch conf"

def test_p1_OP1_OP2_OP4_OP11_OP13_OP18_fresh_private_starts(p1, monkeypatch, caplog):
    w = p1; secrets, paths, creations, draws = [], [], [], []; original, random_token = os.open, aria2.secrets.token_urlsafe
    def draw(n): value = random_token(n); draws.append((n, value)); return value
    monkeypatch.setattr(aria2.secrets, "token_urlsafe", draw)
    def opened(path, flags, mode=0o777, **kw):
        fd = original(path, flags, mode, **kw)
        if Path(path).name.startswith(".aria2.launch."):
            creations.append((flags, stat.S_IMODE(os.fstat(fd).st_mode)))
        return fd
    monkeypatch.setattr(os, "open", opened)
    w.daemon.settings.proxy_type, w.daemon.settings.proxy_host = "http", "proxy.invalid"
    def boundary():
        path, text, args, kwargs = w.spawns[-1]; secret = text.splitlines()[0].removeprefix("rpc-secret=")
        secrets.append(secret); paths.append(path); record = w.data / "aria2.rpc.auth"
        assert record.is_file(), "OP-11 record missing inside Popen"
        assert record.read_text() == f"rpc-secret={secret}\n" and stat.S_IMODE(record.stat().st_mode) == 0o600
        assert re.fullmatch(r"\.aria2\.launch\.[0-9a-f]{24}\.conf", path.name)
        assert secret not in repr((args, kwargs, path, caplog.text))
        assert f"--conf-path={path}" in args and "all-proxy=" in text
    w.on_spawn = boundary
    for count, separate_object in enumerate((False, False, True), 1):
        if separate_object: w.daemon = Aria2Daemon(w.daemon.settings)
        w.daemon.start(); w.daemon.start()
        assert len(w.spawns) == count, "idempotent start spawned twice"
        assert not paths[-1].exists(), "ready launch conf remains"
        assert w.daemon.active_rpc_secret() == secrets[-1]
        w.daemon.stop(); assert w.daemon.active_rpc_secret() is None
    assert len(set(secrets)) == len(set(paths)) == 3
    assert w.daemon.settings.rpc_secret not in secrets and all((32, secret) in draws for secret in secrets)
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{43}", s) for s in secrets)
    assert len(creations) == 3 and all(flags & os.O_EXCL and (not hasattr(os, "O_NOFOLLOW") or flags & os.O_NOFOLLOW) and mode == 0o600 for flags, mode in creations)

@pytest.mark.parametrize("case", ["enforcing", "late-positive", "open-start", "open-reclaim", "old", "bind-loss", "dead-after-probes"])
def test_p1_OP3_OP5_OP6_OP17_ownership(p1, case):
    w = p1
    if case.startswith("open"):
        w.accepted = "OPEN"; w.busy = case == "open-reclaim"
    if case in ("old", "bind-loss"):
        w.accepted = {"UNKNOWN_OLD_SECRET_0123456789"}
    if case == "dead-after-probes":
        w.after_negative = lambda: setattr(w.children[-1], "dead", True)
    if case == "late-positive": w.after_positive = lambda: [next(w.ticks) for _ in range(32)]
    if case in ("enforcing", "late-positive"):
        w.daemon.start(); assert len(w.spawns) == 1 and w.shutdowns() == 0
        assert len(w.calls) == 2 and w.calls[1][2] >= 4.0
        assert w.calls[0][1][0] != w.calls[1][1][0]
    else:
        p1_refusal(w)
        assert len(w.spawns) <= 1, "takeover respawn survived"

@pytest.mark.parametrize("case", ["valid", "http200", "code", "message", "timeout", "non-json", "missing-version", "invalid-version", "positive-auth"])
def test_p1_OP7_ambiguous_proof(p1, case):
    w = p1; w.negative = case
    if case == "valid":
        w.daemon.start(); assert len(w.calls) == 2 and w.calls[-1][2] >= 4.0
    else:
        if case == "missing-version": w.positive = {}
        if case == "invalid-version": w.positive = {"version": 1}
        if case == "positive-auth": w.accepted = {"DIFFERENT_SECRET_0123456789"}
        error = None
        try: w.daemon.start()
        except Aria2Error as exc: error = exc
        assert error is not None, f"ambiguous {case} became ready"
        assert str(error) in ("aria2 RPC did not come up.", str(w.daemon._stale_daemon_error())) and w.shutdowns() == 0 and not list(w.data.glob(".aria2.launch.*.conf"))
        assert sum(p[0] != "token:" + w.spawns[-1][1].splitlines()[0].split("=", 1)[1] for m, p, t in w.calls) == int(case in ("http200", "code", "message", "timeout", "non-json"))

@pytest.mark.parametrize("state", ["absent", "valid", "valid-unknown", "corrupt", "multi-line", "charset", "short", "long", "oversized", "directory", "fifo", "symlink", "dangling", "unreadable"])
def test_p1_OP8_OP9_OP10_OP11_OP12_record_candidates(p1, monkeypatch, state):
    w = p1; w.data.mkdir(parents=True, exist_ok=True); record = w.data / "aria2.rpc.auth"
    old = "P1_RECORDED_ORPHAN_0123456789"
    values = {"valid": f"rpc-secret={old}\n", "corrupt": "garbage", "multi-line": f"rpc-secret={old}\nrpc-secret={old}\n",
              "charset": "rpc-secret=wrong!characters!\n", "short": "rpc-secret=abc\n", "long": "rpc-secret=" + "a" * 129 + "\n", "oversized": "a" * 1025}
    if state in values: record.write_text(values[state])
    if state == "valid-unknown": record.write_text(f"rpc-secret={old}\n")
    if state == "directory": record.mkdir()
    if state == "fifo":
        if not hasattr(os, "mkfifo"): pytest.skip("FIFO creation is POSIX-only")
        os.mkfifo(record)
    if state in ("symlink", "dangling"):
        target = w.data / "target"
        if state == "symlink": target.write_text(f"rpc-secret={old}\n")
        record.symlink_to(target)
    if state == "unreadable":
        record.write_text(f"rpc-secret={old}\n"); original = Path.lstat
        def denied(path, *a, **kw):
            if path == record: raise PermissionError("synthetic record refusal")
            return original(path, *a, **kw)
        monkeypatch.setattr(Path, "lstat", denied)
    w.busy = True; candidate = old if state == "valid" else w.daemon.settings.rpc_secret; w.accepted = {candidate}
    if state in ("valid", "absent"):
        w.on_spawn = lambda: setattr(w, "accepted", None); w.daemon.start()
        assert w.shutdowns() == 1 and len(w.spawns) == 1
        assert [p[0] for m, p, t in w.calls if m == "aria2.getVersion"].count(f"token:{candidate}") == 1
        assert w.calls[1][2] >= 4.0
    else:
        p1_refusal(w)
        if state == "valid-unknown":
            assert not w.spawns and [p[0] for m, p, t in w.calls] == [f"token:{old}"]
            return
        assert not w.spawns and not w.calls, "present-invalid record leaked a legacy candidate"

@pytest.mark.parametrize("case", ["unknown", "port-stays-busy"])
def test_p1_OP9_OP10_reclaim_refusal(p1, case):
    w = p1; w.data.mkdir(parents=True, exist_ok=True)
    old = w.daemon.settings.rpc_secret; (w.data / "aria2.rpc.auth").write_text(f"rpc-secret={old}\n")
    w.busy, w.release = True, False
    w.accepted = {old if case == "port-stays-busy" else "UNKNOWN_SECRET_0123456789"}
    with pytest.raises(Aria2Error) as caught: w.daemon.start()
    assert str(caught.value) == str(w.daemon._stale_daemon_error())
    assert not w.spawns and w.shutdowns() == (case == "port-stays-busy")
    if case == "port-stays-busy": assert w.calls[1][0] == "aria2.getVersion", "shutdown lacked negative proof"

@pytest.mark.parametrize("failure", [requests.ConnectionError("refused"), requests.ReadTimeout("responder present")], ids=["no-responder", "responder-timeout"])
def test_p1_OP14_timeout_cleanup(p1, failure):
    w = p1; w.failure = failure; p1_refusal(w, "generic")
    assert len(w.spawns) == 1 and (w.data / "aria2.rpc.auth").is_file()
    assert w.daemon.active_rpc_secret() is None

def test_p1_OP13_two_objects_cannot_cross_authenticate(p1):
    w = p1; w.daemon.start(); first = w.daemon
    secret = w.spawns[-1][1].splitlines()[0].split("=", 1)[1]
    w.accepted = {secret}; w.daemon = Aria2Daemon(first.settings); p1_refusal(w)
    assert len(w.spawns) == 2 and w.spawns[0][0] != w.spawns[1][0]
    assert w.spawns[1][1].splitlines()[0] != f"rpc-secret={secret}"
    assert first.active_rpc_secret() == secret and w.daemon.active_rpc_secret() is None

def test_p1_A1_upgrade_overlap_and_OP18_interface_order(p1, monkeypatch):
    w = p1; legacy = w.data / "aria2.rpc.conf"
    w.data.mkdir(parents=True, exist_ok=True); legacy.write_text("rpc-secret=PRE_P1_OLD_SECRET_0123456789\n"); before = legacy.read_bytes()
    def delayed_pre_p1_child(args):
        assert (w.data / "aria2.rpc.auth").is_file(), "record missing before delayed child read"
        path = Path(next(a.split("=", 1)[1] for a in args if a.startswith("--conf-path=")))
        w.accepted = {path.read_text().splitlines()[0].split("=", 1)[1]}
    w.on_spawn = lambda: delayed_pre_p1_child(["old-aria2c", f"--conf-path={legacy}"])
    p1_refusal(w); assert legacy.read_bytes() == before, "P1 changed a pre-P1 child's config"

def test_p1_OP18_interface_before_any_files_or_secret(p1, monkeypatch):
    w = p1; w.daemon.settings.torrent_network_interface = "missing-vpn"
    monkeypatch.setattr(aria2, "interface_exists", lambda name: False)
    generated = []; monkeypatch.setattr(aria2.secrets, "token_urlsafe", lambda n: generated.append(n) or "CANARY")
    with pytest.raises(Aria2InterfaceError, match="missing-vpn"): w.daemon.start()
    assert not generated and not w.spawns
    assert not w.data.exists() or not list(w.data.iterdir()), "interface refusal created private artifacts"

@pytest.mark.parametrize("kind", ["transport", "structured", "unstructured", "parse", "gid-control", "multicall"])
def test_p1_A2_hostile_canary(startup_wire, monkeypatch, caplog, kind):
    from cove import native_messaging as nm
    w = startup_wire; secret = "P1_HOSTILE_CANARY_0123456789"; rpc = Aria2RPC(Settings(rpc_secret=secret)); w.accepted = {secret}
    message = f"GID {secret} is not found" if kind == "gid-control" else f"hostile {secret}"
    def hostile(session, url, **kw):
        if kind == "transport": raise requests.ConnectionError(message)
        response = w.response(200, {"error": message if kind == "unstructured" else {"code": 1, "message": message}})
        if kind == "parse": response.json = lambda: (_ for _ in ()).throw(ValueError(message))
        if kind == "multicall" and kw["json"]["method"] == "system.multicall": response = w.response(200, {"result": [{"code": 1, "message": message}] * 3})
        return response
    monkeypatch.setattr(requests.Session, "post", hostile)
    with pytest.raises(Aria2Error) as caught: (rpc.tell_all_downloads if kind == "multicall" else rpc.get_version)()
    pending, surfaces, seen = [caught.value], [], set()
    while pending:
        error = pending.pop()
        if id(error) not in seen: seen.add(id(error)); surfaces.extend([str(error), repr(error), repr(vars(error)), "".join(traceback.format_exception(error))]); pending.extend(e for e in (error.__cause__, error.__context__) if e is not None)
    reply = nm.handle_message({"action": "status"}, rpc, Settings())
    assert reply["status"] == "error"
    assert secret not in " ".join(surfaces + [repr(reply), caplog.text])
    if kind in ("structured", "gid-control", "multicall"): assert caught.value.code == 1
    if kind == "gid-control": assert caught.value.gid_not_found(), "redaction destroyed gid-not-found semantics"

@pytest.mark.parametrize("method", ["get_version", "tell_all_downloads"])
def test_p1_A3_provider_state_and_assignment(startup_wire, method):
    w = startup_wire; current, resolutions = [None], []
    def provider(): resolutions.append(True); return current[0]
    rpc = Aria2RPC(Settings(rpc_secret="PERSISTED_SECRET_0123456789"), secret_provider=provider)
    for state in (None, "ACTIVE_SECRET_0123456789", None, None):
        current[0] = state; w.accepted = {state}; before = len(w.calls); resolutions.clear()
        rpc.secret = "ASSIGNED_MUST_NOT_OVERRIDE_PROVIDER"
        if state is None:
            with pytest.raises(Aria2Error, match="not ready"): getattr(rpc, method)()
            assert len(w.calls) == before
        else:
            getattr(rpc, method)()
            tokens = [c["params"][0] for c in w.calls[-1][1][0]] if method == "tell_all_downloads" else w.calls[-1][1][:1]
            assert set(tokens) == {f"token:{state}"}
        assert len(resolutions) == 1, "token provider resolved more than once per request"

def test_p1_A3_legacy_assignment_guard():
    legacy = object.__new__(Aria2RPC); legacy.secret = "x"; assert legacy.secret == "x"

@pytest.mark.parametrize("kind", ["int", "float", "str", "nested", "error-float", "error-list", "error-dict", "multicall-code", "multicall-faultCode", "multicall-faultString", "multicall-message", "status", "control"] + [prefix + case for prefix in ("", "multicall-") for case in ("contains-equal", "contains-prefix", "contains-suffix", "contains-middle", "contains-float", "benign-1", "benign-2", "benign--32600", "benign-1.5", "benign-auth")])
def test_repair1_F1_complete_error_object(startup_wire, monkeypatch, kind):
    case = kind.removeprefix("multicall-"); secret = "1234567890123456" if case.startswith(("contains-", "benign-")) else "1234567890123456789"
    w = startup_wire; rpc = Aria2RPC(Settings(rpc_secret=secret))
    value = {"int": int(secret), "str": secret, "nested": {"n": [float(secret), {"x": secret}]} }.get(kind, float(secret))
    contains = {"contains-equal": int(secret), "contains-prefix": int(secret + "9"), "contains-suffix": int("7" + secret), "contains-middle": int("7" + secret + "9"), "contains-float": float(secret + ".5")}
    if case in contains: value = contains[case]
    if case.startswith("benign-"):
        number = case.removeprefix("benign-"); value = 1 if number == "auth" else (float(number) if "." in number else int(number))
    error = {"code": value, "message": "denied"}
    if case.startswith("benign-"): error["message"] = "Unauthorized" if case == "benign-auth" else f"GID {secret} is not found"
    if kind.startswith("error-"): error = {"error-float": float(secret), "error-list": [float(secret), secret], "error-dict": {"nested": [float(secret), secret]}}[kind]
    if kind in ("control", "status"): error = {"code": 1, "message": f"GID {secret} is not found"}
    fault = {"faultCode": value, "faultString": error["message"]} if case.startswith(("contains-", "benign-")) else {kind.split("-", 1)[-1]: value}
    body = {"result": [fault] * 3} if kind.startswith("multicall-") else {"error": error}
    monkeypatch.setattr(requests.Session, "post", lambda *a, **kw: w.response(int(secret) if kind == "status" else 400, body))
    with pytest.raises(Aria2Error) as caught: (rpc.tell_all_downloads if kind.startswith("multicall-") else rpc.get_version)()
    if case.startswith(("contains-", "benign-")): assert type(caught.value) is aria2.Aria2RpcError
    def walk(value):
        if isinstance(value, dict): return [leaf for pair in value.items() for part in pair for leaf in walk(part)]
        if isinstance(value, (tuple, list)): return [leaf for part in value for leaf in walk(part)]
        return [value]
    pending, seen = [caught.value], set()
    while pending:
        exc = pending.pop()
        if id(exc) in seen: continue
        seen.add(id(exc)); pending.extend(e for e in (exc.__cause__, exc.__context__) if e is not None)
        for leaf in walk([vars(exc), repr(vars(exc)), exc.args, str(exc), repr(exc), "".join(traceback.format_exception(exc))]):
            assert secret not in str(leaf) and str(float(secret)) not in str(leaf), "sent token escaped error object"
            assert isinstance(leaf, bool) or leaf not in (int(secret), float(secret)), "numeric token escaped error object"
    if kind == "control": assert caught.value.code == 1 and caught.value.gid_not_found() and caught.value._gid_absent
    if case.startswith("benign-"):
        assert caught.value.code == value and type(caught.value.code) is type(value)
        assert caught.value.gid_not_found() == caught.value._gid_absent == (case != "benign-auth")
        if case == "benign-auth": assert caught.value.rpc_message == "Unauthorized" and (kind.startswith("multicall-") or caught.value.http_status == 400)

@pytest.mark.parametrize("stage", ["readiness", "reclaim"])
def test_repair1_F2_contradictory_envelope(p1, stage):
    w = p1; original = w.post
    def post(*a, **kw):
        response = original(*a, **kw); body = response.json()
        if "error" in body: response = w.response(400, {**body, "result": {"version": "x"}})
        return response
    w.post = post; w.busy = stage == "reclaim"
    if w.busy: w.accepted = {w.daemon.settings.rpc_secret}
    p1_refusal(w, "stale" if w.busy else "generic")
    assert w.daemon.active_rpc_secret() is None and len(w.spawns) == (stage == "readiness")

@pytest.mark.parametrize("kind", ["nested", "unexpected"] + [(depth, field) for depth in (200, 800, 1200, 3000) for field in ("message", "code", "raw", "multicall")])
def test_repair1_F3_parse_and_start_cleanup(p1, monkeypatch, request, kind):
    w = p1; original, await_rpc = w.post, w.daemon._await_rpc; injected = RuntimeError("synthetic unexpected readiness"); depth, field = kind if isinstance(kind, tuple) else (0, ""); nested = f"GID {w.daemon.settings.rpc_secret} is not found"
    for _ in range(depth): nested = [nested]
    body = {"result": [{"code": 1, "message": nested}] * 3} if field == "multicall" else {"error": nested if field == "raw" else {"code": nested if field == "code" else 1, "message": nested if field == "message" else f"GID {w.daemon.settings.rpc_secret} is not found"}}
    if depth: limit = sys.getrecursionlimit(); sys.setrecursionlimit(2200 if depth < 1000 else 1000); request.addfinalizer(lambda: sys.setrecursionlimit(limit))
    def post(*a, **kw):
        response = original(*a, **kw); response._content = ("[" * 200000 + "]" * 200000).encode(); response.json = (lambda: body) if depth else response.json; return response
    if kind != "unexpected":
        w.post = post; error = None
        try: rpc = Aria2RPC(w.daemon.settings); (rpc.tell_all_downloads if field == "multicall" else rpc.get_version)()
        except Exception as exc: error = exc
        if depth and depth < 1000: assert type(error) is (aria2.Aria2RpcError if field != "raw" else Aria2Error) and (field == "raw" or error.gid_not_found()) and (field not in ("message", "multicall") or error.code == 1); return
        assert type(error) is Aria2Error and str(error).startswith("RPC bad response:"), "deep JSON escaped sanitized parse failure"
        if depth: assert error.__cause__ is error.__context__ is None and not vars(error) and "GID" not in str(error) + repr(error) and w.daemon.settings.rpc_secret not in str(error) + repr(error) + "".join(traceback.format_exception(error))
    else: monkeypatch.setattr(w.daemon, "_await_rpc", lambda: (_ for _ in ()).throw(injected))
    with pytest.raises(Aria2Error if kind != "unexpected" else RuntimeError) as caught: w.daemon.start()
    assert kind != "unexpected" or caught.value is injected
    assert all(child.dead for child in w.children) and not list(w.data.glob(".aria2.launch.*.conf"))
    assert w.daemon.active_rpc_secret() is None and len(w.spawns) == 1 and w.shutdowns() == 0
    w.post = original; monkeypatch.setattr(w.daemon, "_await_rpc", await_rpc); w.calls.clear(); w.daemon.start()
    assert len(w.spawns) == 2 and len(w.calls) == 2 and w.daemon.active_rpc_secret() is not None

def test_repair1_F5_release_wait_uses_monotonic(startup_wire, monkeypatch):
    w = startup_wire; daemon = Aria2Daemon(Settings()); w.accepted = {daemon.settings.rpc_secret}; wall = []
    def backwards():
        wall.append(True); assert len(wall) < 20, "backward wall clock made port-release wait unbounded"
        return -len(wall)
    monkeypatch.setattr(aria2, "time", SimpleNamespace(time=backwards, monotonic=iter(range(100)).__next__, sleep=lambda n: None))
    monkeypatch.setattr(aria2, "_resolve_aria2c", lambda: "synthetic-aria2c"); monkeypatch.setattr(aria2, "_OwnedPopen", w.spawn)
    monkeypatch.setattr(Aria2Daemon, "_port_in_use", lambda self: True); monkeypatch.setattr(aria2.socket, "socket", MagicMock())
    with pytest.raises(Aria2Error, match="Another aria2 process"): daemon.start()
    assert not w.spawns and w.shutdowns() == 1 and len(wall) < 20

def test_p1_A3_active_secret_tracks_child_death_and_stop(p1):
    w = p1
    rpc = Aria2RPC(w.daemon.settings, secret_provider=w.daemon.active_rpc_secret)
    for phase in ("before", "ready", "dead", "stopped"):
        if phase == "ready": w.daemon.start()
        if phase == "dead": w.children[-1].dead = True
        if phase == "stopped": w.daemon.stop()
        before = len(w.calls)
        if phase == "ready":
            rpc.tell_all_downloads(); assert len(w.calls) == before + 1
        else:
            assert w.daemon.active_rpc_secret() is None
            with pytest.raises(Aria2Error, match="not ready"): rpc.tell_all_downloads()
            assert len(w.calls) == before

@pytest.mark.parametrize("point", ["create", "publish", "spawn", "readiness", "terminate", "unlink"])
def test_p1_A5_cleanup_injection(p1, monkeypatch, point):
    w = p1; canary = "P1_CLEANUP_CANARY_0123456789"; reached = []; original_open, original_replace, original_unlink = os.open, os.replace, Path.unlink
    foreign = w.data / ".aria2.launch.000000000000000000000000.conf"
    w.data.mkdir(parents=True, exist_ok=True); foreign.write_text("unrelated bytes")
    if point == "create": monkeypatch.setattr(aria2.secrets, "token_hex", lambda n: "0" * (2 * n))
    def opened(path, flags, *a, **kw):
        if point == "create" and Path(path).name.startswith(".aria2.launch."):
            reached.append(True); assert Path(path) == foreign; raise FileExistsError(canary)
        return original_open(path, flags, *a, **kw)
    def replaced(source, destination, *a, **kw):
        if point == "publish" and Path(destination).name == "aria2.rpc.auth": reached.append(True); raise OSError(canary)
        return original_replace(source, destination, *a, **kw)
    def unlinked(path, *a, **kw):
        assert path != foreign, "attempt unlinked a file it did not create"
        if point == "unlink" and path.name.startswith(".aria2.launch."): reached.append(True); raise OSError(canary)
        return original_unlink(path, *a, **kw)
    monkeypatch.setattr(os, "open", opened); monkeypatch.setattr(os, "replace", replaced); monkeypatch.setattr(Path, "unlink", unlinked)
    if point == "spawn": monkeypatch.setattr(aria2, "_OwnedPopen", lambda *a, **kw: (_ for _ in ()).throw(OSError(canary)))
    if point in ("readiness", "terminate"): w.failure = requests.ConnectionError(canary)
    def failed_terminate(signal):
        reached.append(True)
        raise OSError(canary)
    if point == "terminate": w.on_spawn = lambda: setattr(w.children[-1], "send_signal", failed_terminate)
    error = None
    try: w.daemon.start()
    except Exception as exc: error = exc
    if point != "unlink": assert isinstance(error, Aria2Error), f"{point} did not reach sanitized failure"
    if error: assert canary not in str(error) + repr(error) + "".join(traceback.format_exception(error))
    if point in ("create", "publish", "unlink", "terminate"): assert reached, f"{point} injection was never reached"
    assert foreign.read_text() == "unrelated bytes"
    assert point == "unlink" or list(w.data.glob(".aria2.launch.*.conf")) == [foreign]
    assert (w.data / "aria2.rpc.auth").exists() == (point not in ("create", "publish")), "crash record state"

@pytest.mark.parametrize("method,args", [
    ("add_uri", (["https://example.com/file"], "/scratch", 4)),
    ("add_magnet", ("magnet:?xt=urn:btih:abc", "/scratch")),
    ("add_torrent", (b"torrent bytes", "/scratch")),
])
@pytest.mark.parametrize("gid", [None, "123456789abcdef0"])
def test_caller_owned_gid_option_is_forwarded_only_when_supplied(monkeypatch, method, args, gid):
    rpc = Aria2RPC(Settings())
    calls = []
    monkeypatch.setattr(rpc, "_call", lambda method, params: calls.append((method, params)) or "123456789abcdef0")
    kwargs = {} if gid is None else {"gid": gid}
    assert getattr(rpc, method)(*args, **kwargs) == "123456789abcdef0"
    opts = calls[0][1][-1]
    assert opts.get("gid") == gid
    assert ("gid" in opts) == (gid is not None)


@pytest.mark.parametrize("method,args", [
    ("add_uri", (["https://example.com/file"], "/scratch", 4)),
    ("add_magnet", ("magnet:?xt=urn:btih:abc", "/scratch")),
    ("add_torrent", (b"torrent bytes", "/scratch")),
])
@pytest.mark.parametrize("gid", ["", "0" * 16, "a" * 15, "a" * 17, "A" * 16, "g" * 16, 123])
def test_invalid_caller_gid_is_rejected_before_rpc(monkeypatch, method, args, gid):
    rpc = Aria2RPC(Settings())
    calls = []
    monkeypatch.setattr(rpc, "_call", lambda *args: calls.append(args))
    with pytest.raises(Aria2Error):
        getattr(rpc, method)(*args, gid=gid)
    assert not calls


def test_generated_gid_is_lowercase_nonzero_and_skips_reserved_identity(monkeypatch):
    import cove.aria2 as module
    generator = getattr(module, "generate_gid", None)
    assert callable(generator), "caller-owned GID generator missing"
    values = iter(["0" * 16, "123456789abcdef0", "abcdef0123456789"])
    monkeypatch.setattr(module.secrets, "token_hex", lambda size: next(values) if size == 8 else pytest.fail("wrong GID width"))
    assert generator() == "123456789abcdef0"
    assert generator() == "abcdef0123456789"


@pytest.fixture(autouse=True)
def _no_stale_daemon(tmp_path, monkeypatch):
    """Daemon startup tests must not depend on what holds 6800 locally.

    Tests that exercise the reclaim path re-patch this inside the test, and
    the inner patch wins.
    """
    data_dir = tmp_path / "data"
    monkeypatch.setattr(aria2, "DATA_DIR", data_dir)
    monkeypatch.setattr(aria2, "ARIA2_SESSION", data_dir / "aria2.session")
    monkeypatch.setattr(aria2, "ARIA2_LOG", data_dir / "aria2.log")
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(Aria2Daemon, "_port_in_use", lambda self: False)
    yield


def _live_proc():
    """A Popen stand-in that reports a running child (poll() is None).

    A bare MagicMock returns a truthy MagicMock from poll(), which the
    daemon correctly reads as "our child already exited".
    """
    proc = MagicMock()
    proc.poll.return_value = None
    return proc


@pytest.fixture
def s04_daemon(monkeypatch, tmp_path, startup_wire):
    """Real scratch paths and start(), with only the child/RPC mocked."""
    settings = Settings(
        download_dir=str(tmp_path / "downloads"), rpc_secret="S04_RPC_SECRET_unit",
        proxy_type="http", proxy_host="127.0.0.1", proxy_port=18080,
        proxy_username="s04user", proxy_password="S04_ARGV_SECRET_9f3c",
    )
    def spawned(args, **kwargs):
        popen.conf_path = Path(next(a.split("=", 1)[1] for a in args if a.startswith("--conf-path=")))
        popen.conf_text = popen.conf_path.read_text()
        return _live_proc()
    popen = MagicMock(side_effect=spawned)
    monkeypatch.setattr(aria2, "_resolve_aria2c", lambda: "aria2c")
    monkeypatch.setattr(aria2, "_OwnedPopen", popen)
    daemon = Aria2Daemon(settings)
    try:
        yield daemon, popen, aria2.DATA_DIR / "aria2.rpc.auth"
    finally:
        daemon.stop()


@pytest.mark.parametrize("password", ["S04_ARGV_SECRET_9f3c", "S04_ARGV_SECRET_9f3c:@% #"])
def test_s04_proxy_credentials_absent_from_argv(s04_daemon, password):
    """P1/M6: raw and encoded credentials must never be process arguments."""
    daemon, popen, conf = s04_daemon
    daemon.settings.proxy_password = password
    daemon.start()
    args = popen.call_args.args[0]
    joined = "\0".join(args)
    for value in (password, quote(password, safe=""), "s04user", daemon._build_proxy_url()):
        assert value not in joined
    assert not any(arg.startswith("--all-proxy") for arg in args)
    assert f"--conf-path={popen.conf_path}" in args
    assert popen.conf_text.splitlines() == [
        popen.conf_text.splitlines()[0], f"all-proxy={daemon._build_proxy_url()}",
    ]


def test_s04_rpc_secret_remains_absent_from_argv(s04_daemon):
    """P2/M12: the already-fixed RPC argv boundary stays fixed."""
    daemon, popen, conf = s04_daemon
    daemon.start()
    assert daemon.settings.rpc_secret not in "\0".join(popen.call_args.args[0])
    assert f"--conf-path={popen.conf_path}" in popen.call_args.args[0]
    assert conf.is_file() and conf.read_text() == popen.conf_text.splitlines()[0] + "\n"
    assert daemon.settings.rpc_secret not in conf.read_text()


@pytest.mark.parametrize("active", ["http", "none", "empty-host"])
@pytest.mark.parametrize("value", ["http://127.0.0.1:1", ""], ids=["conflict", "empty"])
def test_s04_proxy_environment_preserves_argv_precedence(s04_daemon, monkeypatch, active, value):
    """C2: strip only the env option that newly overrides the conf transport."""
    for name in ("all_proxy", "ALL_PROXY", "http_proxy", "HTTP_PROXY", "https_proxy",
                 "HTTPS_PROXY", "ftp_proxy", "FTP_PROXY"):
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("no_proxy", "example.invalid")
    monkeypatch.setenv("S04_ENV_SENTINEL", "preserved")
    before = dict(os.environ)
    daemon, popen, _ = s04_daemon
    if active == "none":
        daemon.settings.proxy_type = "none"
    elif active == "empty-host":
        daemon.settings.proxy_host = ""
    daemon.start()
    child_env = popen.call_args.kwargs.get("env")
    if active == "http":
        expected = dict(before)
        expected.pop("all_proxy", None)
        if os.name == "nt":
            expected.pop("ALL_PROXY", None)  # Windows environment names are case insensitive.
        assert isinstance(child_env, dict), "an active conf proxy needs an explicit child environment"
        assert child_env == expected
        assert child_env is not os.environ
        assert daemon.settings.proxy_password not in "\0".join(child_env.values())
        assert daemon.settings.rpc_secret not in "\0".join(child_env.values())
    else:
        assert child_env is None or child_env == before
    assert dict(os.environ) == before, "never mutate the parent process environment"


_S04_PROXY_SEMANTICS = [
    pytest.param("http", "proxy.invalid", 8080, "s04user", "S04_ARGV_SECRET_9f3c",
                 "http://s04user:S04_ARGV_SECRET_9f3c@proxy.invalid:8080", id="http"),
    pytest.param("https", "proxy.invalid", 8080, "s04user", "S04_ARGV_SECRET_9f3c",
                 "https://s04user:S04_ARGV_SECRET_9f3c@proxy.invalid:8080", id="https"),
    pytest.param("socks5", "proxy.invalid", 8080, "s04user", "S04_ARGV_SECRET_9f3c",
                 "socks5://s04user:S04_ARGV_SECRET_9f3c@proxy.invalid:8080", id="socks5"),
    pytest.param("http", "proxy.invalid", 0, "s04user", "", "http://s04user@proxy.invalid", id="user-only"),
    pytest.param("http", "proxy.invalid", 0, "", "S04_ARGV_SECRET_9f3c",
                 "http://proxy.invalid", id="password-without-user"),
    pytest.param("http", "proxy.invalid", 0, "", "", "http://proxy.invalid", id="no-auth-no-port"),
    pytest.param("http", "proxy.invalid", 8080, "", "", "http://proxy.invalid:8080", id="no-auth-port"),
    pytest.param("none", "proxy.invalid", 8080, "s04user", "S04_ARGV_SECRET_9f3c", None, id="none"),
    pytest.param("http", "", 8080, "s04user", "S04_ARGV_SECRET_9f3c", None, id="empty-host"),
]


@pytest.mark.parametrize("kind,host,port,user,password,expected", _S04_PROXY_SEMANTICS)
def test_s04_proxy_url_semantics_preserved(kind, host, port, user, password, expected):
    """Existing URL semantics, independent of the new conf transport."""
    daemon = Aria2Daemon(Settings(
        proxy_type=kind, proxy_host=host, proxy_port=port,
        proxy_username=user, proxy_password=password,
    ))
    active = kind != "none" and bool(host)
    assert (daemon._build_proxy_url() if active else None) == expected


@pytest.mark.parametrize("kind,host,port,user,password,expected", _S04_PROXY_SEMANTICS)
def test_s04_proxy_semantics_in_conf(s04_daemon, kind, host, port, user, password, expected):
    daemon, popen, conf = s04_daemon
    settings = daemon.settings
    settings.proxy_type, settings.proxy_host, settings.proxy_port = kind, host, port
    settings.proxy_username, settings.proxy_password = user, password
    daemon.start()
    expected_lines = [popen.conf_text.splitlines()[0]]
    if expected is not None:
        expected_lines.append(f"all-proxy={expected}")
    assert popen.conf_text.splitlines() == expected_lines
    assert not any(arg.startswith("--all-proxy") for arg in popen.call_args.args[0])


def test_s04_disabling_proxy_rewrites_conf_without_old_secret(s04_daemon):
    """P5/M8: check both the credential-bearing start and the disabled start."""
    daemon, popen, conf = s04_daemon
    daemon.settings.proxy_password = "S04_OLD_PROXY_SECRET"
    daemon.start()
    first = popen.conf_text
    daemon.stop()
    daemon.settings.proxy_type = "none"
    daemon.start()
    current = popen.conf_text
    assert "S04_OLD_PROXY_SECRET" not in current
    assert current.splitlines() == [popen.conf_text.splitlines()[0]]
    assert "S04_OLD_PROXY_SECRET" in first, "the enabled start must use the protected conf"
    assert all("S04_OLD_PROXY_SECRET" not in "\0".join(call.args[0]) for call in popen.call_args_list)


def test_s04_password_rotation_replaces_conf_and_argv_stays_secret_free(s04_daemon):
    """P6/M8: the current conf contains B only; neither spawn exposes A/B."""
    daemon, popen, conf = s04_daemon
    daemon.settings.proxy_password = "S04_OLD_PROXY_SECRET"
    daemon.start()
    first = popen.conf_text
    daemon.stop()
    daemon.settings.proxy_password = "S04_ARGV_SECRET_9f3c_rotated"
    daemon.start()
    current = popen.conf_text
    assert "S04_OLD_PROXY_SECRET" not in current
    assert current.splitlines() == [
        popen.conf_text.splitlines()[0], f"all-proxy={daemon._build_proxy_url()}",
    ]
    assert "S04_OLD_PROXY_SECRET" in first
    assert all(
        secret not in "\0".join(call.args[0])
        for call in popen.call_args_list
        for secret in ("S04_OLD_PROXY_SECRET", "S04_ARGV_SECRET_9f3c_rotated")
    )


@pytest.mark.parametrize("field", ["proxy_host", "rpc_secret", "proxy_type", "proxy_port"])
@pytest.mark.parametrize("control", ["\r", "\n", "\x00", "\x01", "\x1f", "\x7f"],
                         ids=["cr", "lf", "nul", "soh", "us", "del"])
def test_s04_raw_conf_controls_fail_closed(s04_daemon, field, control):
    """P8/M10: validate raw conf values before writing or spawning."""
    daemon, popen, conf = s04_daemon
    value = f"S04_RPC_SECRET_injection{control}max-concurrent-downloads=999"
    setattr(daemon.settings, field, value)
    if field == "rpc_secret":
        daemon.start()
        assert value not in popen.conf_text and "max-concurrent-downloads=999" not in popen.conf_text
        return
    error = None
    try:
        daemon.start()
    except Aria2Error as exc:
        error = exc
    text = popen.conf_text if conf.exists() else ""
    assert "max-concurrent-downloads=999" not in text
    assert not popen.called, "invalid raw fields must never reach the child"
    assert error is not None, "expected Aria2Error for a control character"
    assert value not in str(error)
    assert "S04_RPC_SECRET_injection" not in str(error)


def test_daemon_lifts_max_concurrent_downloads(startup_wire):
    """aria2 defaults to 5; the daemon must pass an explicit higher cap."""
    daemon = Aria2Daemon(Settings())
    with patch("cove.aria2._resolve_aria2c", return_value="aria2c"), \
         patch("cove.aria2._OwnedPopen", return_value=_live_proc()) as popen:
        daemon.start()

    args = popen.call_args[0][0]
    assert f"--max-concurrent-downloads={MAX_CONCURRENT_DOWNLOADS}" in args
    assert MAX_CONCURRENT_DOWNLOADS > 5


def test_daemon_caps_per_server_connections_for_stock_aria2(startup_wire):
    settings = Settings(connections_per_server=32)
    daemon = Aria2Daemon(settings)
    with patch("cove.aria2._resolve_aria2c", return_value="aria2c"), \
         patch("cove.aria2._OwnedPopen", return_value=_live_proc()) as popen:
        daemon.start()

    args = popen.call_args[0][0]
    assert f"--max-connection-per-server={MAX_CONNECTIONS_PER_SERVER}" in args
    assert f"--split={MAX_CONNECTIONS_PER_SERVER}" in args


def test_daemon_stops_seeding_when_the_download_completes(startup_wire):
    """Local BitTorrent must not keep uploading after Cove says "done"."""
    daemon = Aria2Daemon(Settings())
    with patch("cove.aria2._resolve_aria2c", return_value="aria2c"), \
         patch("cove.aria2._OwnedPopen", return_value=_live_proc()) as popen:
        daemon.start()

    args = popen.call_args[0][0]
    assert "--seed-time=0" in args


def test_daemon_binds_to_the_selected_network_interface(startup_wire):
    settings = Settings(torrent_network_interface="wg0-mullvad")
    daemon = Aria2Daemon(settings)
    with patch("cove.aria2._resolve_aria2c", return_value="aria2c"), \
         patch("cove.aria2.interface_exists", return_value=True), \
         patch("cove.aria2._OwnedPopen", return_value=_live_proc()) as popen:
        daemon.start()

    assert "--interface=wg0-mullvad" in popen.call_args[0][0]


def test_daemon_passes_no_interface_flag_for_any_interface(startup_wire):
    daemon = Aria2Daemon(Settings())
    with patch("cove.aria2._resolve_aria2c", return_value="aria2c"), \
         patch("cove.aria2._OwnedPopen", return_value=_live_proc()) as popen:
        daemon.start()

    assert not [a for a in popen.call_args[0][0] if a.startswith("--interface=")]


def test_daemon_refuses_to_launch_when_the_interface_is_gone(startup_wire):
    """A VPN adapter that vanished must block traffic, not reroute it."""
    settings = Settings(torrent_network_interface="wg0-mullvad")
    daemon = Aria2Daemon(settings)
    with patch("cove.aria2._resolve_aria2c", return_value="aria2c"), \
         patch("cove.aria2.interface_exists", return_value=False), \
         patch("cove.aria2._OwnedPopen", return_value=_live_proc()) as popen:
        with pytest.raises(Aria2InterfaceError) as excinfo:
            daemon.start()

    assert "wg0-mullvad" in str(excinfo.value)
    assert popen.call_count == 0
    # Still an Aria2Error for every existing handler, but distinguishable:
    # startup must keep the window usable so Settings can be reached, rather
    # than treating this like a missing aria2 binary.
    assert isinstance(excinfo.value, Aria2Error)


def test_daemon_reclaims_the_port_from_a_leftover_aria2(startup_wire):
    """A previous Cove's aria2c can outlive it and keep port 6800.

    It answers RPC with the same secret, so a naive health check passes
    while our own child is dying of a failed bind. Cove must not drive a
    process it cannot stop or restart: shut the leftover down first, then
    launch a daemon it owns.
    """
    daemon = Aria2Daemon(Settings())
    # In use before the shutdown call, free afterwards.
    in_use = iter([True, False, False])
    startup_wire.accepted = {daemon.settings.rpc_secret}
    def spawned(*args, **kwargs):
        startup_wire.accepted = None
        return _live_proc()
    with patch("cove.aria2._resolve_aria2c", return_value="aria2c"), \
         patch("cove.aria2._OwnedPopen", side_effect=spawned) as popen, \
         patch.object(Aria2Daemon, "_port_in_use", side_effect=lambda: next(in_use)), \
         patch.object(Aria2Daemon, "_wait_for_port_release") as wait, \
         patch.object(Aria2RPC, "shutdown") as shutdown:
        daemon.start()

    assert shutdown.call_count == 1
    assert wait.call_count == 1
    # Exactly one spawn: the leftover is cleared before we launch, so no
    # child is ever burned on a doomed bind.
    assert popen.call_count == 1


def test_daemon_reports_a_port_it_cannot_reclaim(startup_wire):
    """If the leftover will not go away, say so instead of pretending."""
    daemon = Aria2Daemon(Settings())
    startup_wire.accepted = {daemon.settings.rpc_secret}
    with patch("cove.aria2._resolve_aria2c", return_value="aria2c"), \
         patch("cove.aria2._OwnedPopen", return_value=_live_proc()) as popen, \
         patch.object(Aria2Daemon, "_port_in_use", return_value=True), \
         patch.object(Aria2Daemon, "_wait_for_port_release") as wait, \
         patch.object(Aria2RPC, "shutdown"):
        with pytest.raises(Aria2Error) as excinfo:
            daemon.start()

    message = str(excinfo.value)
    assert "6800" in message and "aria2c" in message
    assert wait.call_count == 1
    assert popen.call_count == 0


def test_daemon_refuses_a_foreign_aria2_on_our_port(startup_wire):
    """A stranger's aria2 rejects our secret and must not be shut down."""
    daemon = Aria2Daemon(Settings())
    startup_wire.accepted = {"FOREIGN_SECRET_0123456789"}
    with patch("cove.aria2._resolve_aria2c", return_value="aria2c"), \
         patch("cove.aria2._OwnedPopen", return_value=_live_proc()), \
         patch.object(Aria2Daemon, "_port_in_use", return_value=True), \
         patch.object(Aria2RPC, "shutdown") as shutdown:
        with pytest.raises(Aria2Error) as excinfo:
            daemon.start()

    assert "6800" in str(excinfo.value)
    assert shutdown.call_count == 0


def test_daemon_keeps_a_healthy_child_without_shutting_anything_down(startup_wire):
    """The normal path must not gain a stray shutdown call."""
    daemon = Aria2Daemon(Settings())
    with patch("cove.aria2._resolve_aria2c", return_value="aria2c"), \
         patch("cove.aria2._OwnedPopen", return_value=_live_proc()) as popen, \
         patch.object(Aria2Daemon, "_port_in_use", return_value=False), \
         patch.object(Aria2RPC, "shutdown") as shutdown:
        daemon.start()

    assert popen.call_count == 1
    assert shutdown.call_count == 0


def test_daemon_adds_no_speculative_bittorrent_flags(startup_wire):
    """Only --seed-time=0 is new; DHT/PEX/ports stay on aria2 defaults."""
    daemon = Aria2Daemon(Settings())
    with patch("cove.aria2._resolve_aria2c", return_value="aria2c"), \
         patch("cove.aria2._OwnedPopen", return_value=_live_proc()) as popen:
        daemon.start()

    args = " ".join(popen.call_args[0][0])
    for flag in ("--enable-dht", "--enable-peer-exchange", "--bt-",
                 "--listen-port", "--dht-listen-port", "--seed-ratio"):
        assert flag not in args


def _rpc() -> Aria2RPC:
    s = Settings()
    s.rpc_secret = "test"
    return Aria2RPC(s)


def test_tell_external_snapshot_combines_active_and_stopped():
    rpc = _rpc()
    with patch.object(rpc, "_call", side_effect=[[{"gid": "a"}], [{"gid": "b"}]]) as m:
        out = rpc.tell_external_snapshot()
    assert out == [{"gid": "a"}, {"gid": "b"}]
    methods = [c.args[0] for c in m.call_args_list]
    assert methods == ["aria2.tellActive", "aria2.tellStopped"]


def test_tell_stopped_passes_offset_num_keys():
    rpc = _rpc()
    with patch.object(rpc, "_call", return_value=[]) as m:
        rpc.tell_stopped()
    method, params = m.call_args[0]
    assert method == "aria2.tellStopped"
    assert params[0] == 0 and params[1] == 1000
    assert "gid" in params[2] and "status" in params[2]


# ---------------------------------------------------------------------------
# BitTorrent RPC surface
# ---------------------------------------------------------------------------

TORRENT_BYTES = b"d4:infod6:lengthi7e4:name5:a.bin12:piece lengthi16384eee"


def test_add_torrent_base64_encodes_the_payload():
    rpc = _rpc()
    with patch.object(rpc, "_call", return_value="gid-t") as m:
        gid = rpc.add_torrent(TORRENT_BYTES, "/dl")
    method, params = m.call_args[0]
    assert method == "aria2.addTorrent"
    assert gid == "gid-t"
    assert params[0] == base64.b64encode(TORRENT_BYTES).decode("ascii")
    assert params[1] == []          # no selective files in Slice B
    assert params[2]["dir"] == "/dl"


def test_add_torrent_never_seeds_after_completion():
    rpc = _rpc()
    with patch.object(rpc, "_call", return_value="gid-t") as m:
        rpc.add_torrent(TORRENT_BYTES, "/dl")
    assert m.call_args[0][1][2]["seed-time"] == "0"


def test_add_torrent_applies_the_speed_limit():
    rpc = _rpc()
    with patch.object(rpc, "_call", return_value="gid-t") as m:
        rpc.add_torrent(TORRENT_BYTES, "/dl", speed_limit_kbps=256)
    assert m.call_args[0][1][2]["max-download-limit"] == "256K"


def test_add_torrent_can_carry_a_forward_compatible_file_selection():
    rpc = _rpc()
    with patch.object(rpc, "_call", return_value="gid-t") as m:
        rpc.add_torrent(TORRENT_BYTES, "/dl", select_file="1,2")
    params = m.call_args[0][1]
    assert params[2]["select-file"] == "1,2"


def test_add_torrent_rejects_non_bytes_without_echoing_the_payload():
    rpc = _rpc()
    with pytest.raises(Aria2Error) as exc:
        rpc.add_torrent("not bytes", "/dl")
    assert "not bytes" not in str(exc.value)


def test_add_torrent_failure_does_not_leak_the_torrent_data():
    rpc = _rpc()
    with patch.object(rpc, "_call", side_effect=Aria2Error("RPC aria2.addTorrent failed")):
        with pytest.raises(Aria2Error) as exc:
            rpc.add_torrent(TORRENT_BYTES, "/dl")
    text = str(exc.value)
    assert base64.b64encode(TORRENT_BYTES).decode("ascii") not in text
    assert "piece length" not in text


def test_add_magnet_uses_add_uri_and_does_not_seed():
    rpc = _rpc()
    with patch.object(rpc, "_call", return_value="gid-m") as m:
        gid = rpc.add_magnet("magnet:?xt=urn:btih:" + "a" * 40, "/dl")
    method, params = m.call_args[0]
    assert method == "aria2.addUri"
    assert gid == "gid-m"
    assert params[0] == ["magnet:?xt=urn:btih:" + "a" * 40]
    assert params[1]["dir"] == "/dl"
    assert params[1]["seed-time"] == "0"


def test_tell_status_requests_the_torrent_lifecycle_keys():
    rpc = _rpc()
    with patch.object(rpc, "_call", return_value={}) as m:
        rpc.tell_status("gid-1")
    keys = m.call_args[0][1][1]
    for required in ("followedBy", "following", "infoHash", "bittorrent", "files"):
        assert required in keys
    # Existing keys must survive.
    for existing in ("gid", "status", "totalLength", "completedLength",
                     "downloadSpeed", "errorCode", "errorMessage",
                     "connections", "dir", "bitfield", "numPieces"):
        assert existing in keys


def test_external_snapshot_keys_expose_torrent_relationships():
    """The adoption guard needs these to spot a torrent child gid."""
    rpc = _rpc()
    with patch.object(rpc, "_call", return_value=[]) as m:
        rpc.tell_active()
    keys = m.call_args[0][1][0]
    for required in ("following", "followedBy", "infoHash", "bittorrent"):
        assert required in keys


def test_get_files_returns_the_aria2_file_list():
    rpc = _rpc()
    with patch.object(rpc, "_call", return_value=[{"path": "/dl/a.bin"}]) as m:
        files = rpc.get_files("gid-1")
    assert m.call_args[0] == ("aria2.getFiles", ["gid-1"])
    assert files == [{"path": "/dl/a.bin"}]


# ---------------------------------------------------------------------------
# BitTorrent capability
# ---------------------------------------------------------------------------


def test_bittorrent_enabled_reads_the_feature_list():
    assert bittorrent_enabled({"enabledFeatures": ["Async DNS", "BitTorrent"]}) is True


def test_bittorrent_enabled_false_when_the_build_lacks_it():
    assert bittorrent_enabled({"enabledFeatures": ["Async DNS", "HTTPS"]}) is False


def test_bittorrent_enabled_false_for_a_malformed_response():
    for bad in (None, {}, {"enabledFeatures": "BitTorrent"},
                {"enabledFeatures": [None, 3]}, "BitTorrent"):
        assert bittorrent_enabled(bad) is False


def test_rpc_session_is_thread_local():
    """requests.Session isn't thread-safe; each thread must get its own."""
    rpc = _rpc()
    sessions = {}

    def grab(name):
        sessions[name] = rpc._session()

    t1 = threading.Thread(target=lambda: grab("a"))
    t2 = threading.Thread(target=lambda: grab("b"))
    t1.start(); t1.join()
    t2.start(); t2.join()
    assert sessions["a"] is not sessions["b"]
    # Same thread reuses one session.
    assert rpc._session() is rpc._session()


# ---- mid-session death ---------------------------------------------------
#
# start() only consults poll() on the way in, so an aria2c that dies after
# boot used to stay dead for the rest of the session: every download failed
# with a generic error and the zombie was never even reaped. is_running()
# is the probe that notices. It deliberately does not repair.


def _dead_proc(code: int = 1):
    """A Popen stand-in for a child that has already exited."""
    proc = MagicMock()
    proc.poll.return_value = code
    return proc


def test_is_running_reads_the_child_and_nothing_else():
    """The GUI timer calls this every few seconds: it must be a poll() and
    no more - no port probe, no RPC round trip."""
    daemon = Aria2Daemon(Settings())
    assert daemon.is_running() is False  # never booted

    daemon._proc = _live_proc()
    with patch.object(Aria2Daemon, "_port_in_use", side_effect=AssertionError), \
         patch.object(Aria2RPC, "get_version", side_effect=AssertionError):
        assert daemon.is_running() is True
        daemon._proc = _dead_proc()
        assert daemon.is_running() is False


def test_is_running_reaps_the_dead_child():
    """poll() is what reaps a Popen child; without something calling it,
    a dead aria2c lingers as a zombie for the rest of the session - which
    is exactly what was found in the field."""
    daemon = Aria2Daemon(Settings())
    proc = _dead_proc()
    daemon._proc = proc

    assert daemon.is_running() is False
    assert proc.poll.called


def test_is_running_never_restarts_anything():
    """The probe is the whole contract. Restarting aria2c from here would
    leave Cove holding gids issued by a process that no longer exists."""
    daemon = Aria2Daemon(Settings())
    daemon._proc = _dead_proc()
    with patch("cove.aria2._OwnedPopen") as popen:
        for _ in range(5):
            assert daemon.is_running() is False
    assert popen.call_count == 0


@pytest.fixture
def i2r1_wire(monkeypatch):
    """Send-boundary configuration evidence; no wire traffic is permitted."""
    for key in ("HTTP_PROXY http_proxy HTTPS_PROXY https_proxy ALL_PROXY all_proxy "
                "FTP_PROXY ftp_proxy NO_PROXY no_proxy REQUESTS_CA_BUNDLE CURL_CA_BUNDLE").split():
        monkeypatch.delenv(key, raising=False)
    wire = SimpleNamespace(rpc=Aria2RPC(Settings(rpc_port=16800, rpc_secret="SYNTH_I2R1_RPC_SECRET_0123456789ab")),
                           sends=[], errors=[], sockets=[],
                           proxy="http://proxy-a.invalid:3128", redirect=None, failure=None,
                           target="http://redirect-target.invalid/leak",
                           content=b'{"jsonrpc":"2.0","id":"x","result":{"version":"synthetic"}}')
    Send = namedtuple("Send", "method url body proxies timeout")
    def no_socket(*args, **kwargs):
        wire.sockets.append(True)
        raise AssertionError("I2-R1 unit test touched a socket")
    def send(adapter, request, **kwargs):
        body = request.body or b""
        body = body.encode() if isinstance(body, str) else body
        wire.sends.append(Send(request.method, request.url, body, dict(kwargs["proxies"]), kwargs["timeout"]))
        if wire.failure:
            wire.errors.append(wire.failure("synthetic transport failure", request=request))
            raise wire.errors[-1]
        response = requests.Response()
        response.request, response.url = request, request.url
        response.status_code, response._content = 200, wire.content
        if wire.redirect and request.url == wire.rpc.url:
            response.status_code = wire.redirect
            response.headers.update({"Location": wire.target} if wire.target is not None else {})
        return response
    monkeypatch.setattr(socket.socket, "connect", no_socket)
    monkeypatch.setattr(socket, "create_connection", no_socket)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    yield wire
    assert wire.sockets == [], "socket isolation trap fired"


@pytest.mark.parametrize("status", [None, 307, 308])
def test_i2r1_control_unprotected_session_selects_env_proxy(i2r1_wire, monkeypatch, status):
    i2r1_wire.redirect = status
    monkeypatch.setenv("HTTP_PROXY", i2r1_wire.proxy)
    with requests.Session() as session:
        session.post(i2r1_wire.rpc.url, json={"synthetic": True}, timeout=5.0)
    assert len(i2r1_wire.sends) == (2 if status else 1), "proxy/redirect recorder is not live"
    assert i2r1_wire.sends[0].proxies["http"] == i2r1_wire.proxy
    if status:
        assert i2r1_wire.sends[1].url == i2r1_wire.target and i2r1_wire.sends[1].body == i2r1_wire.sends[0].body


@pytest.mark.parametrize("key", ["HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "HTTPS_PROXY"])
def test_i2r1_a_env_proxy_not_selected(i2r1_wire, monkeypatch, key):
    monkeypatch.setenv(key, i2r1_wire.proxy)
    i2r1_wire.rpc.get_version()
    assert len(i2r1_wire.sends) == 1, "no send recorded"
    assert i2r1_wire.sends[0].url == i2r1_wire.rpc.url and requests.utils.select_proxy(i2r1_wire.rpc.url, i2r1_wire.sends[0].proxies) is None


@pytest.mark.parametrize("port", [6800, 16800])
def test_i2r1_b_plain_rpc_unchanged(i2r1_wire, port):
    rpc = Aria2RPC(Settings(rpc_port=port, rpc_secret=i2r1_wire.rpc.secret))
    result = rpc.get_version()
    assert len(i2r1_wire.sends) == 1, "no send recorded"
    method, url, body, proxies, timeout = i2r1_wire.sends[0]
    assert (method, url, proxies, timeout) == ("POST", f"http://127.0.0.1:{port}/jsonrpc", {}, 5.0)
    payload = json.loads(body)
    assert result == {"version": "synthetic"} and set(payload) == {"jsonrpc", "id", "method", "params"} and payload["id"]
    assert (payload["jsonrpc"], payload["method"], payload["params"]) == ("2.0", "aria2.getVersion", [f"token:{rpc.secret}"])


def test_i2r1_c_all_proxy_spellings_together(i2r1_wire, monkeypatch):
    for i, key in enumerate("HTTP_PROXY http_proxy HTTPS_PROXY https_proxy ALL_PROXY all_proxy".split()):
        monkeypatch.setenv(key, f"http://proxy-{i}.invalid:3128")
    i2r1_wire.rpc.get_version()
    assert len(i2r1_wire.sends) == 1, "no send recorded"
    assert i2r1_wire.sends[0].proxies == {}


@pytest.mark.parametrize("key", ["NO_PROXY", "no_proxy"])
@pytest.mark.parametrize("value", ["", "localhost", "127.0.0.1", "127.0.0.0/8", "*", ".invalid", "example.invalid"])
def test_i2r1_d_no_proxy_variants_irrelevant(i2r1_wire, monkeypatch, key, value):
    monkeypatch.setenv("HTTP_PROXY", i2r1_wire.proxy)
    monkeypatch.setenv(key, value)
    i2r1_wire.rpc.get_version()
    assert len(i2r1_wire.sends) == 1, "no send recorded"
    assert i2r1_wire.sends[0].url == "http://127.0.0.1:16800/jsonrpc" and i2r1_wire.sends[0].proxies == {}


def test_i2r1_e_every_thread_session_protected(i2r1_wire, monkeypatch):
    w, observations = i2r1_wire, []
    monkeypatch.setenv("HTTP_PROXY", w.proxy)
    def call(name):
        w.rpc.get_version()
        session = w.rpc._session()
        observations.append((name, session, session is w.rpc._session(), w.sends[-1].proxies))
    for i in range(3):
        thread = threading.Thread(target=call, args=(f"worker-{i}",))
        thread.start()
        thread.join()
    call("main")
    w.rpc.close()
    call("rebuilt")
    assert len(w.sends) == len(observations) == 5, "thread sends missing"
    assert len({id(row[1]) for row in observations}) == 5, "sessions must be distinct"
    for name, session, reused, proxies in observations:
        assert reused and proxies == {} and session.trust_env is False, name


@pytest.mark.parametrize("case", ["token", "multicall", "dict-error", "other-error", "bad-json"])
def test_i2r1_f_auth_and_parsing_intact(i2r1_wire, case):
    i2r1_wire.content = {"dict-error": b'{"error":{"code":7,"message":"synthetic denial"}}',
                        "other-error": b'{"error":"synthetic denial"}', "bad-json": b'not-json'}.get(case, i2r1_wire.content)
    method, error = "system.multicall" if case == "multicall" else "aria2.x", None
    try:
        result = i2r1_wire.rpc._call(method, [["caller"]], with_token=case != "multicall")
    except Aria2Error as exc:
        error = exc
    assert len(i2r1_wire.sends) == 1, "no send recorded"
    assert json.loads(i2r1_wire.sends[0].body)["params"] == ([] if case == "multicall" else [f"token:{i2r1_wire.rpc.secret}"]) + [["caller"]]
    if case in ("token", "multicall"):
        assert error is None and result == {"version": "synthetic"}
    else:
        assert type(error) is (aria2.Aria2RpcError if case == "dict-error" else Aria2Error)
        assert str(error).startswith("RPC bad response:" if case == "bad-json" else "RPC aria2.x failed: synthetic denial")
        if case == "dict-error":
            assert (error.code, error.rpc_message) == (7, "synthetic denial")


@pytest.mark.parametrize("failure", [requests.ConnectionError, requests.Timeout, requests.exceptions.ProxyError])
def test_i2r1_g_transport_failure_keeps_semantics_without_secret(i2r1_wire, caplog, failure):
    i2r1_wire.failure = failure
    with caplog.at_level("DEBUG"):
        with pytest.raises(Aria2Error, match="^RPC transport error:") as caught:
            i2r1_wire.rpc.get_version()
        i2r1_wire.rpc.shutdown()
    assert len(i2r1_wire.sends) == 2, "transport/shutdown sends missing"
    error = caught.value
    assert type(error) is Aria2Error and error.__cause__ is None and error.__context__ is None
    surfaces = str(error) + repr(error) + repr(vars(error)) + "".join(traceback.format_exception(error)) + caplog.text
    assert i2r1_wire.rpc.secret not in surfaces and "token:" not in surfaces


def test_i2r1_h_other_sessions_keep_default_trust_env(i2r1_wire):
    from cove import netiface
    i2r1_wire.rpc.get_version()
    assert len(i2r1_wire.sends) == 1, "no send recorded"
    with requests.Session() as bare, netiface.bound_requests_session("") as bound:
        assert len({id(bare), id(bound), id(i2r1_wire.rpc._session())}) == 3
        assert bare.trust_env is True and bound.trust_env is True


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_i2r1_i_redirects_not_followed(i2r1_wire, status):
    w, error = i2r1_wire, None
    w.redirect = status
    with patch.object(requests.Session, "request", autospec=True, side_effect=requests.Session.request) as request:
        try:
            w.rpc.get_version()
        except Aria2Error as exc:
            error = exc
    assert len(w.sends) == 1, "RPC followed redirect beyond loopback"
    assert all(urlsplit(sent.url).hostname == "127.0.0.1" for sent in w.sends)
    assert not any(w.rpc.secret.encode() in sent.body for sent in w.sends if urlsplit(sent.url).hostname != "127.0.0.1")
    assert type(error) is Aria2Error and str(error).startswith(f"RPC unexpected redirect: HTTP {status}")
    assert all(value not in str(error) for value in (w.rpc.secret, "token:", w.target))
    assert request.call_args.kwargs.get("allow_redirects") is False  # configuration companion


@pytest.mark.parametrize("status,location", [(s, "valid") for s in (300, 304, 305, 306, 399)] +
                         [(s, k) for s in (307, 308) for k in ("absent", "bracket", "unicode", "credentials")])
def test_i2r1_i_redirect_edge_cases(i2r1_wire, status, location):
    w, error = i2r1_wire, None
    w.redirect, w.target = status, {"absent": None, "bracket": f"http://[{w.rpc.secret}]/",
                                  "unicode": "http://redirect-target.invalid/\u2603",
                                  "credentials": f"http://user:hunter2@redirect-target.invalid/?t={w.rpc.secret}"}.get(location, w.target)
    try:
        w.rpc.get_version()
    except Exception as exc:
        error = exc
    assert len(w.sends) == 1, "redirect parsing must not trigger a second authenticated send"
    assert all(urlsplit(sent.url).hostname == "127.0.0.1" for sent in w.sends)
    assert type(error) is Aria2Error, "post parsing failure escaped the RPC error contract"
    malformed = location in ("bracket", "unicode")
    assert str(error) == ("RPC message could not be processed" if malformed else f"RPC unexpected redirect: HTTP {status}")
    assert not malformed or (error.__cause__ is None and error.__context__ is None)
    surfaces = str(error) + repr(error) + "".join(traceback.format_exception(error))
    assert all(value not in surfaces for value in (w.rpc.secret, "token:", "hunter2", w.target) if value)


@pytest.mark.parametrize("error", [RuntimeError("synthetic"), KeyError("synthetic"), OSError("synthetic")])
def test_i2r1_i_unrelated_exceptions_are_not_suppressed(i2r1_wire, error):
    for injected, expected in ((error, type(error)), (ValueError("synthetic"), Aria2Error)):
        i2r1_wire.failure = lambda *args, **kwargs: injected
        with pytest.raises(expected) as caught:
            i2r1_wire.rpc.get_version()
        assert type(caught.value) is expected and len(i2r1_wire.sends) == (1 if injected is error else 2)
        assert (caught.value is error and not isinstance(caught.value, Aria2Error) if injected is error
                else str(caught.value) == "RPC message could not be processed")
